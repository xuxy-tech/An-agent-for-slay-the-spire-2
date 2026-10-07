"""Strictly join human combat choices with headless leaf counterfactuals."""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from controller.human_capture import CAPTURE_PROTOCOL
from controller.sandbox_features import FEATURE_VERSION, OBSERVATION_MODE
from controller.combat_abilities import ability_settings

SCHEMA = "sts2.human_capture.leaf_preference.v1"
ACTION_TYPES = {"play_card", "end_turn"}


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_id(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    upper = text.upper().replace("-", "_").replace(" ", "_")
    return upper.removeprefix("CARD.").removeprefix("MONSTER.")


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _powers(values: Any) -> list[dict[str, Any]]:
    result = []
    for power in values or []:
        if not isinstance(power, dict):
            continue
        result.append({
            "id": normalize_id(_first(power, "id", "power_id")),
            "amount": _number(_first(power, "amount", "stacks")),
            "extra": _text(power.get("extra")),
            "counter": _number(power.get("counter")),
        })
    return sorted(result, key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")))


def _card(card: Any) -> dict[str, Any]:
    if not isinstance(card, dict):
        return {"id": normalize_id(card), "upgrade": 0, "cost": None, "costs_x": None}
    costs_x = _first(card, "costs_x", "display_costs_x")
    return {
        "id": normalize_id(_first(card, "card_id", "id")),
        "upgrade": _number(_first(card, "upgrade", "upgrade_count"), 0),
        "cost": _number(_first(card, "current_cost", "energy_cost", "display_cost", "cost")),
        "costs_x": bool(costs_x) if costs_x is not None else None,
    }


def _intent(intent: Any) -> dict[str, Any]:
    if not isinstance(intent, dict):
        return {"types": [], "damage": None, "hits": None}
    return {
        "types": sorted(str(value) for value in (intent.get("intent_types") or [])),
        "damage": _number(_first(intent, "total_damage", "damage", "display_damage")),
        "hits": _number(intent.get("hits")),
    }


def _enemy(enemy: Any) -> dict[str, Any]:
    if not isinstance(enemy, dict):
        return {"id": normalize_id(enemy), "hp": None, "max_hp": None, "block": None,
                "powers": [], "intent": _intent(None), "target_ids": []}
    target_ids = []
    for key in ("enemy_id", "creature_id", "id", "net_id", "NetId"):
        value = enemy.get(key)
        if value is not None:
            target_ids.append(str(value))
    return {
        "id": normalize_id(_first(enemy, "monster_id", "enemy_id", "creature_id", "id")),
        "hp": _number(_first(enemy, "hp", "current_hp")),
        "max_hp": _number(enemy.get("max_hp")),
        "block": _number(enemy.get("block")),
        "powers": _powers(enemy.get("powers")),
        "intent": _intent(enemy.get("intent")),
        # Runtime creature IDs are retained in the raw root for action mapping,
        # but are excluded from the public-state fingerprint.
    }


def _combat(value: dict[str, Any]) -> dict[str, Any]:
    combat = value.get("combat") if isinstance(value.get("combat"), dict) else {}
    player = combat.get("player") if isinstance(combat.get("player"), dict) else {}
    enemies = [_enemy(enemy) for enemy in (combat.get("enemies") or [])]
    return {
        "round": _number(_first(combat, "round_number", "round"), _number(value.get("turn"))),
        "turn": _number(_first(combat, "turn_number", "turn"), _number(value.get("turn"))),
        "player": {
            "hp": _number(_first(player, "hp", "current_hp")),
            "max_hp": _number(player.get("max_hp")),
            "block": _number(player.get("block")),
            "energy": _number(_first(player, "energy", "current_energy")),
            "powers": _powers(player.get("powers")),
        },
        "enemies": enemies,
        "hand": sorted(
            [_card(card) for card in (combat.get("hand") or [])],
            key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")),
        ),
    }


def public_state(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"combat": None}
    combat = _combat(value)
    if not combat.get("player") or (not combat.get("enemies") and not combat.get("hand")):
        return {"combat": None}
    return {"combat": combat}


def state_fingerprint(value: dict[str, Any]) -> str | None:
    projected = public_state(value)
    if projected.get("combat") is None:
        return None
    payload = json.dumps(projected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _action_type(action: dict[str, Any]) -> str | None:
    value = _text(action.get("type") or action.get("action_type"))
    if not value:
        return None
    value = value.lower()
    return value if value in ACTION_TYPES else None


def _root_hand(root: dict[str, Any]) -> list[dict[str, Any]]:
    combat = root.get("combat") if isinstance(root.get("combat"), dict) else {}
    return combat.get("hand") or []


def candidate_action(candidate: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    action = candidate.get("action") if isinstance(candidate.get("action"), dict) else {}
    metadata = action.get("metadata") if isinstance(action.get("metadata"), dict) else {}
    card_index = action.get("card_index")
    card = None
    if isinstance(card_index, int) and 0 <= card_index < len(_root_hand(root)):
        card = _root_hand(root)[card_index]
    card_id = _first(metadata, "card_id", "id") or (card.get("card_id") if isinstance(card, dict) else None)
    return {
        "type": _action_type(action),
        "card_id": normalize_id(card_id),
        "card_index": card_index,
        "target_index": action.get("target_index"),
        "metadata": metadata,
    }


def human_action(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": _action_type(action),
        "card_id": normalize_id(action.get("card_id")),
        "target_id": action.get("target_creature_id"),
    }


def _target_matches(human: dict[str, Any], candidate: dict[str, Any], root: dict[str, Any]) -> bool:
    if candidate["type"] != "play_card":
        return True
    human_target = human.get("target_id")
    target_index = candidate.get("target_index")
    if human_target is None and target_index is None:
        return True
    combat = root.get("combat") if isinstance(root.get("combat"), dict) else {}
    enemies = [enemy for enemy in combat.get("enemies") or [] if isinstance(enemy, dict)]
    if isinstance(target_index, int) and 0 <= target_index < len(enemies) and human_target is not None:
        enemy = enemies[target_index]
        ids = {str(enemy.get(key)) for key in ("enemy_id", "creature_id", "id", "net_id", "NetId") if enemy.get(key) is not None}
        if str(human_target) in ids:
            return True
    living = [enemy for enemy in enemies if _number(_first(enemy, "hp", "current_hp"), 1.0) > 0]
    return len(living) == 1 and human_target is not None and target_index in (None, 0)


def action_matches(human: dict[str, Any], candidate: dict[str, Any], root: dict[str, Any]) -> bool:
    if human.get("type") != candidate.get("type"):
        return False
    if human.get("type") == "play_card" and human.get("card_id") != candidate.get("card_id"):
        return False
    return _target_matches(human, candidate, root)


def _action_record_key(action: dict[str, Any]) -> tuple[Any, ...]:
    if not isinstance(action, dict):
        return (None, None, None, None)
    metadata = action.get("metadata") if isinstance(action.get("metadata"), dict) else {}
    args = action.get("args") if isinstance(action.get("args"), dict) else {}
    return (
        action.get("action_type") or action.get("type"),
        action.get("card_index", args.get("card_index")),
        action.get("target_index", args.get("target_index")),
        metadata.get("potion_index", args.get("potion_index")),
    )


def _sequence_matches(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> bool:
    if not expected or len(expected) > len(actual):
        return False
    return all(_action_record_key(a) == _action_record_key(b) for a, b in zip(expected, actual))


def _load_jsonl(paths: Iterable[Path]) -> list[dict[str, Any]]:
    rows = []
    for path in paths:
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    row["_source_path"] = str(path)
                    row["_source_line"] = line_number
                    rows.append(row)
    return rows


def _human_rows(root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = []
    sessions = 0
    skipped_failed = 0
    skipped_protocol = 0
    for events_path in sorted(root.glob("human_*/events.jsonl")):
        manifest_path = events_path.with_name("manifest.json")
        manifest = {}
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                manifest = {}
        sessions += 1
        if manifest.get("status") == "failed":
            skipped_failed += 1
            continue
        capture_health = manifest.get("capture_health") or {}
        if capture_health.get("protocol_version") != CAPTURE_PROTOCOL:
            skipped_protocol += 1
            continue
        for row in _load_jsonl([events_path]):
            if row.get("record_type") != "decision" or row.get("settlement") != "settled":
                continue
            observation = row.get("observation_before") or {}
            action = row.get("action") or {}
            if observation.get("in_combat") is not True or _action_type(action) not in ACTION_TYPES:
                continue
            fingerprint = state_fingerprint(observation)
            snapshot = row.get("authoritative_snapshot") or {}
            if fingerprint and snapshot.get("status") == "complete" and snapshot.get("snapshot_id"):
                rows.append({
                    **row,
                    "session_id": manifest.get("session_id"),
                    "state_fingerprint": fingerprint,
                    "authoritative_snapshot_id": snapshot.get("snapshot_id"),
                    "authoritative_snapshot_sha256": snapshot.get("sha256"),
                    "capture_protocol": CAPTURE_PROTOCOL,
                })
    return rows, {
        "sessions": sessions,
        "skipped_failed_sessions": skipped_failed,
        "skipped_non_v4_sessions": skipped_protocol,
        "capture_protocol": CAPTURE_PROTOCOL,
    }


def _leaf_rows(paths: Iterable[Path]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = _load_jsonl(paths)
    return ([row for row in rows if row.get("record_type") == "decision"],
            [row for row in rows if row.get("record_type") == "leaf"])


def _coverage_complete(row: dict[str, Any]) -> bool:
    context = row.get("search_context") or {}
    root = context.get("root_coverage") or {}
    return bool(root.get("topology_exhaustive") or root.get("bounded_tree_complete"))


def _feature_row(root_id: str, candidate: dict[str, Any], leaves: dict[tuple[str, tuple[Any, ...]], list[dict[str, Any]]]) -> dict[str, Any] | None:
    action = candidate.get("action") or {}
    options = leaves.get((root_id, _action_record_key(action))) or []
    line = candidate.get("line") or []
    exact = [row for row in options if _sequence_matches(line, row.get("action_sequence") or [])]
    chosen = exact or options
    if not chosen:
        return None
    chosen = sorted(chosen, key=lambda row: float(row.get("score") if row.get("score") is not None else float("-inf")), reverse=True)[0]
    return {"features": chosen.get("features") or {}, "score": chosen.get("score"),
            "leaf_source": {"path": chosen.get("_source_path"), "line": chosen.get("_source_line")},
            "sequence_exact": bool(exact)}


def join_records(human_rows: list[dict[str, Any]], leaf_decisions: list[dict[str, Any]], leaf_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_fingerprint: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_snapshot_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in leaf_decisions:
        fingerprint = state_fingerprint(row.get("root_visible_state") or {})
        if fingerprint:
            by_fingerprint[fingerprint].append(row)
        snapshot_id = _text(row.get("root_snapshot_id"))
        if snapshot_id:
            by_snapshot_id[snapshot_id].append(row)
    leaf_index: dict[tuple[str, tuple[Any, ...]], list[dict[str, Any]]] = defaultdict(list)
    for row in leaf_rows:
        leaf_index[(str(row.get("root_id")), _action_record_key(row.get("root_action") or {}))].append(row)

    output = []
    reasons = Counter()
    matched = fit_ready = pair_count = fit_ready_pair_count = 0
    for human in human_rows:
        snapshot_id = _text(human.get("authoritative_snapshot_id"))
        candidates = by_snapshot_id.get(snapshot_id) if snapshot_id else None
        join_key = "snapshot_id" if candidates else "state_fingerprint"
        if not candidates:
            candidates = by_fingerprint.get(human["state_fingerprint"]) or []
        record = {
            "schema": SCHEMA, "record_type": "human_decision_join", "join_status": "unmatched",
            "fit_ready": False,
            "human": {"session_id": human.get("session_id"), "decision_id": human.get("decision_id"),
                       "run_id": human.get("run_id"), "combat_id": human.get("combat_id"), "turn": human.get("turn"),
                       "state_fingerprint": human["state_fingerprint"],
                       "authoritative_snapshot_id": human.get("authoritative_snapshot_id"),
                       "authoritative_snapshot_sha256": human.get("authoritative_snapshot_sha256"),
                       "action": human.get("action")},
            "reason": None, "join_key": join_key, "candidate_set": [], "pairwise_examples": [],
        }
        if not candidates:
            record["reason"] = "no_matching_headless_root"
            reasons[record["reason"]] += 1
            output.append(record)
            continue
        if len(candidates) != 1:
            record["reason"] = "multiple_matching_headless_roots"
            reasons[record["reason"]] += 1
            output.append(record)
            continue
        decision = candidates[0]
        root = decision.get("root_visible_state") or {}
        human_choice = human_action(human.get("action") or {})
        mapped = []
        for candidate in decision.get("counterfactual_candidates") or []:
            action = candidate_action(candidate, root)
            if action.get("type") not in ACTION_TYPES:
                continue
            selected = action_matches(human_choice, action, root)
            mapped.append({"candidate_id": candidate.get("candidate_id"), "action": candidate.get("action"),
                           "normalized_action": action,
                           "human_preference": "selected" if selected else "not_selected",
                           "label_semantics": "selected_over_observed_engine_candidate; not_absolute_negative",
                           "comparable": bool(candidate.get("comparable")),
                           "feature": _feature_row(str(decision.get("root_id")), candidate, leaf_index)})
        selected_rows = [row for row in mapped if row["human_preference"] == "selected"]
        record["candidate_set"] = mapped
        record["root_id"] = decision.get("root_id")
        record["root_source"] = {"path": decision.get("_source_path"), "line": decision.get("_source_line")}
        if not selected_rows:
            record["reason"] = "human_action_not_in_headless_candidates"
            reasons[record["reason"]] += 1
            output.append(record)
            continue
        if len(selected_rows) != 1:
            record["reason"] = "human_action_maps_to_multiple_candidates"
            reasons[record["reason"]] += 1
            output.append(record)
            continue
        matched += 1
        complete = _coverage_complete(decision)
        comparable = all(row["comparable"] for row in mapped)
        features_present = all(row["feature"] is not None for row in mapped)
        feature_contract = all(
            row['feature'] is not None
            and row['feature']['features'].get('version') == FEATURE_VERSION
            and row['feature']['features'].get('observation_mode') == OBSERVATION_MODE
            and row['feature']['features'].get('training_eligible') is True
            for row in mapped)
        for row in mapped:
            if row["human_preference"] == "selected" or row["feature"] is None or selected_rows[0]["feature"] is None:
                continue
            record["pairwise_examples"].append({
                "chosen": selected_rows[0]["feature"]["features"], "other": row["feature"]["features"], "target": 1,
                "label_semantics": "human_chosen_preferred_over_observed_engine_candidate",
                "chosen_candidate_id": selected_rows[0]["candidate_id"], "other_candidate_id": row["candidate_id"],
            })
        pair_count += len(record["pairwise_examples"])
        if feature_contract:
            try:
                configs = [ability_settings(row['feature']['features'].get('ability_config')) for row in mapped]
                feature_contract = all(config == configs[0] for config in configs)
            except ValueError:
                feature_contract = False
        record["fit_ready"] = bool(complete and comparable and features_present and feature_contract and record["pairwise_examples"])
        if record["fit_ready"]:
            fit_ready += 1
            fit_ready_pair_count += len(record["pairwise_examples"])
        else:
            record["reason"] = ("incompatible_or_incomplete_feature_observations"
                                if features_present and not feature_contract
                                else "incomplete_or_missing_counterfactual_features")
            reasons[record["reason"]] += 1
        record["join_status"] = "matched"
        output.append(record)
    report = {
        "schema": SCHEMA, "human_rows": len(human_rows), "headless_decisions": len(leaf_decisions),
        "headless_leaf_rows": len(leaf_rows), "matching_root_fingerprints": len(by_fingerprint),
        "matching_root_snapshot_ids": len(by_snapshot_id),
        "matched_rows": matched, "fit_ready_rows": fit_ready, "pairwise_examples": pair_count,
        "fit_ready_pairwise_examples": fit_ready_pair_count,
        "reasons": dict(sorted(reasons.items())),
        "label_semantics": "chosen human action is compared only against explicitly observed engine candidates; no global negative labels",
    }
    return output, report


def join_paths(human_root: Path, leaf_input: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    human_rows, human_report = _human_rows(human_root)
    paths = sorted(leaf_input.rglob("*.jsonl")) if leaf_input.is_dir() else ([leaf_input] if leaf_input.is_file() else [])
    decisions, leaves = _leaf_rows(paths)
    records, report = join_records(human_rows, decisions, leaves)
    report["human_capture"] = human_report
    report["leaf_input"] = str(leaf_input)
    return records, report


def pairs_from_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert only fit-ready joins to the existing preference-model contract."""
    pairs = []
    for record in records:
        if not record.get("fit_ready"):
            continue
        for example in record.get("pairwise_examples") or []:
            chosen = example.get("chosen")
            other = example.get("other")
            if not isinstance(chosen, dict) or not isinstance(other, dict):
                continue
            pairs.append({"a": chosen, "b": other, "target": float(example.get("target", 1))})
    return pairs
