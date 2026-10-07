"""Check that an independently verified combat snapshot replays deterministically.

The replay uses the same combat decision primitive and headless action command as
the visible runner's shadow. It never sends an action to the visible client.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.combat_scoring import CombatScoring, active_model, stage_for_floor, validate_model
from controller.combat_snapshot import snapshot_compatibility
from controller.combat_step import CombatStepConfig, PlanState, decide_combat_action
from controller.run_agent import _normalize_cli_state
from controller.search.combat_search import CombatSpec, CombatWorkerPool
from controller.search.state_cache import hash_search_state
from controller.snapshot_evidence import read_evidence, read_original_report


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_snapshot(directory: Path, cfg: CliConfig) -> tuple[dict[str, Any], str]:
    directory = directory.resolve()
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    if (metadata.get("schema") != "sts2.combat_snapshot.v2"
            or metadata.get("status") != "RESTORE_VERIFIED"
            or metadata.get("reusable") is not True):
        raise ValueError("Snapshot has not passed independent restore validation")
    if metadata.get("compatibility") != snapshot_compatibility(cfg):
        raise ValueError("Snapshot validation is stale for the current runtime")
    for key, filename in (("restore_sha256", "restore_snapshot.json"),
                          ("search_sha256", "search_snapshot.json")):
        data = (directory / filename).read_bytes()
        if hashlib.sha256(data).hexdigest() != metadata.get(key):
            raise ValueError(f"{filename} differs from capture metadata")
    # The actual fight must retain potions removed only from search inputs.
    return metadata, (directory / "restore_snapshot.json").read_text(encoding="utf-8")


def _recorded_config(metadata: dict[str, Any]) -> tuple[dict[str, Any], int | None]:
    evidence = read_evidence(metadata)
    if evidence is not None:
        rows = evidence['actions']
        live_workers = ((rows[0].get('decision_telemetry') or {}).get('worker_runtime') or {}) if rows else {}
        count = live_workers.get('active_workers')
        return dict(evidence.get('config') or {}), int(count) if type(count) is int and count > 0 else None
    report = read_original_report(metadata)
    if report is None:
        return {}, None
    start = datetime.fromtimestamp(float(metadata.get("created_at_utc") or 0), tz=timezone.utc)
    first = next((row for row in report.get("actions") or []
                  if row.get("decision", "").startswith("combat.")
                  and datetime.fromisoformat(row["timestamp_utc"]).astimezone(timezone.utc) > start), None)
    live_workers = ((first or {}).get("decision_telemetry") or {}).get("worker_runtime") or {}
    count = live_workers.get("active_workers")
    return dict(report.get("config") or {}), int(count) if type(count) is int and count > 0 else None


def _status(state: dict[str, Any]) -> str:
    return str(state.get("decision") or state.get("type") or "unknown")


def compact_state(state: dict[str, Any]) -> dict[str, Any]:
    """Keep only observable combat resources for comparison and the UI."""
    player = state.get("player") or ((state.get("combat") or {}).get("player")) or {}
    enemies = state.get("enemies") or ((state.get("combat") or {}).get("enemies")) or []
    return {
        "decision": _status(state), "round": state.get("round"),
        "hp": player.get("hp", player.get("current_hp")),
        "max_hp": player.get("max_hp"), "block": player.get("block"),
        "potions": [str(item.get("id") or item.get("name") or item)
                    for item in (player.get("potions") or [])],
        "enemies": [{"id": row.get("monster_id") or row.get("id") or row.get("name"),
                     "hp": row.get("hp"), "block": row.get("block")}
                    for row in enemies if isinstance(row, dict)],
    }


def replay_once(metadata: dict[str, Any], restore_json: str, cfg: CliConfig,
                *, model: dict[str, Any], depth: int, chance_depth: int,
                max_search_ms: float, workers: int, max_actions: int,
                cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    context = metadata.get("context") or {}
    evidence = read_evidence(metadata)
    floor_value = context.get("floor", metadata.get("floor"))
    floor = int(floor_value) if floor_value is not None else None
    room_type = str(context.get("room_type") or "")
    snapshot_root = json.loads(restore_json)
    character = str(snapshot_root.get("CharacterName") or "Ironclad").title()
    cli = Sts2CliAdapter(cfg)
    pool = CombatWorkerPool(cfg)
    plan = PlanState()
    steps: list[dict[str, Any]] = []
    try:
        cli.start()
        snapshot_id = "replay_gate_root"
        imported = cli.import_combat_snapshot(restore_json, snapshot_id)
        if imported.get("success") is not True:
            raise RuntimeError(f"Snapshot import failed: {imported}")
        state = cli.restore_combat_snapshot(snapshot_id)
        if _status(state) != "combat_play":
            raise RuntimeError(f"Restore did not reach combat_play: {_status(state)}")
        root = compact_state(state)
        pool.prewarm(workers)
        step_cfg = CombatStepConfig(
            cli_cfg=cfg,
            spec=CombatSpec(character, str(context.get("encounter_id") or "ImportedSnapshot"),
                            str(context.get("run_id") or metadata.get("run_id") or snapshot_id),
                            int(snapshot_root.get("AscensionLevel") or 0), "en"),
            depth=depth, chance_depth=chance_depth, score_mode="preference",
            max_workers=workers, reuse_cli_processes=True, user_parallel=workers > 1,
            floor=floor, room_type=room_type, worker_pool=pool,
            max_search_ms=max_search_ms, scorer_model=model,
            evaluator_coefficients=dict(evidence['evaluator_coefficients']) if evidence else {},
        )
        for _ in range(max_actions):
            if cancelled and cancelled():
                raise RuntimeError("Comparison cancelled")
            if _status(state) != "combat_play":
                break
            search_state = _normalize_cli_state(cli.get_search_state())
            if search_state.get("success") is not True or not isinstance(search_state.get("combat"), dict):
                raise RuntimeError("Headless engine did not provide a complete combat search state")
            before_rng = cli.get_rng_snapshot()
            decision = decide_combat_action(cli, search_state, step_cfg, plan)
            if decision.search_failed or decision.fell_back:
                raise RuntimeError(
                    f"Decision {len(steps) + 1} failed or used fallback "
                    f"(search_failed={decision.search_failed}, fell_back={decision.fell_back}, "
                    f"raw_retry_recovered={decision.raw_retry_recovered})"
                )
            if not decision.action:
                raise RuntimeError("Shared combat decision returned no action")
            result = _normalize_cli_state(cli.action(decision.action, decision.payload,
                                                     with_snapshot=False, timeout_s=20))
            if result.get("type") == "error" or result.get("success") is False:
                raise RuntimeError(f"Headless action failed: {result.get('message') or result}")
            after_rng = cli.get_rng_snapshot()
            next_state = _normalize_cli_state(cli.get_search_state()) if _status(result) == "combat_play" else result
            steps.append({
                "action": decision.action, "payload": decision.payload,
                "chosen": decision.chosen_summary, "reused_plan": decision.reused_plan,
                "score": decision.search_score, "decision_reason": decision.decision_reason,
                "score_explanation": decision.score_explanation,
                "before": compact_state(search_state), "after": compact_state(next_state),
                "before_state_hash": hash_search_state(search_state),
                "after_state_hash": hash_search_state(next_state) if next_state.get("combat") else None,
                "rng_before_sha256": _digest(before_rng), "rng_after_sha256": _digest(after_rng),
                "after_decision": _status(result),
                "time_budget_exhausted": bool(decision.searcher_timing_summary.get("time_budget_exhausted")),
            })
            state = result
            if _status(state) == "card_select":
                raise RuntimeError("Combat card selection requires visible-client policy mapping; excluded from gate")
        if _status(state) == "combat_play":
            raise RuntimeError(f"Combat did not finish within {max_actions} actions")
        if _status(state) not in {"combat_reward", "card_reward", "map_select", "victory", "game_over", "defeat"}:
            raise RuntimeError(f"Unsupported combat terminal boundary: {_status(state)}")
        return {"status": "COMPLETED", "terminal": _status(state), "actions": steps,
                "root": root, "outcome": compact_state(state),
                "scorer": CombatScoring(stage_for_floor(floor), model).identity}
    except Exception as exc:
        return {"status": "FAILED", "error": str(exc), "actions": steps,
                "terminal": _status(state) if "state" in locals() else None}
    finally:
        pool.close()
        cli.stop()


def compare_replays(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    if left.get("status") != "COMPLETED" or right.get("status") != "COMPLETED":
        return {"status": "FAILED", "reason": "incomplete_replay",
                "left_error": left.get("error"), "right_error": right.get("error"),
                "left_completed_actions": len(left.get("actions") or []),
                "right_completed_actions": len(right.get("actions") or [])}
    keys = ("action", "payload", "chosen", "reused_plan", "score", "decision_reason",
            "score_explanation",
            "before", "after",
            "before_state_hash", "after_state_hash", "rng_before_sha256", "rng_after_sha256",
            "after_decision", "time_budget_exhausted")
    for index in range(max(len(left["actions"]), len(right["actions"]))):
        a = left["actions"][index] if index < len(left["actions"]) else None
        b = right["actions"][index] if index < len(right["actions"]) else None
        if a is None or b is None:
            return {"status": "FAILED", "reason": "action_count", "first_difference": index + 1,
                    "left": a, "right": b}
        differences = [key for key in keys if a.get(key) != b.get(key)]
        if differences:
            return {"status": "FAILED", "reason": "step_mismatch", "first_difference": index + 1,
                    "fields": differences, "left": a, "right": b}
    if left["terminal"] != right["terminal"]:
        return {"status": "FAILED", "reason": "terminal_mismatch",
                "left": left["terminal"], "right": right["terminal"]}
    if left.get("outcome") != right.get("outcome"):
        return {"status": "FAILED", "reason": "terminal_resources_mismatch",
                "left": left.get("outcome"), "right": right.get("outcome")}
    return {"status": "PASS", "actions": len(left["actions"]), "terminal": left["terminal"]}


def compare_history(metadata: dict[str, Any], replay: dict[str, Any],
                    model_identity: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    source_hashes = metadata.get("source_sha256") or {}
    for name in ("controller/search/combat_search.py", "controller/run_agent.py"):
        recorded_hash = source_hashes.get(name)
        path = Path.cwd() / name
        if not recorded_hash or not path.is_file():
            return {"status": "INELIGIBLE", "reason": "decision_source_version_unrecorded", "source": name}
        if hashlib.sha256(path.read_bytes()).hexdigest() != recorded_hash:
            return {"status": "INELIGIBLE", "reason": "decision_source_changed", "source": name}
    try:
        evidence = read_evidence(metadata)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"status": "INELIGIBLE", "reason": "live_evidence_corrupt"}
    if evidence is not None:
        report = evidence
    else:
        report = read_original_report(metadata)
        if report is None:
            return {"status": "INELIGIBLE", "reason": "no_live_report"}
    config = report.get("config") or {}
    if (int(config.get("depth") or 0) != settings["depth"]
            or int(config.get("chance_depth") or 0) != settings["chance_depth"]
            or float(config.get("search_budget_ms") or 0) != settings["max_search_ms"]):
        return {"status": "INELIGIBLE", "reason": "different_search_settings"}
    start = datetime.fromtimestamp(float(metadata["created_at_utc"]), tz=timezone.utc)
    rows = [row for row in report.get("actions") or []
            if row.get("decision", "").startswith("combat.")
            and datetime.fromisoformat(row["timestamp_utc"]).astimezone(timezone.utc) > start]
    if not rows:
        return {"status": "INELIGIBLE", "reason": "no_later_live_combat_actions"}
    combat_number = (rows[0].get("decision_telemetry") or {}).get("combat_number")
    rows = [row for row in rows if (row.get("decision_telemetry") or {}).get("combat_number") == combat_number]
    saw_model_identity = False
    for index, step in enumerate(replay.get("actions") or []):
        if index >= len(rows):
            return {"status": "FAILED", "reason": "live_combat_ended_early", "first_difference": index + 1}
        row = rows[index]
        telemetry = row.get("decision_telemetry") or {}
        if telemetry.get("reused_plan"):
            return {"status": "INELIGIBLE", "reason": "live_cached_plan_not_in_snapshot",
                    "matched_prefix": index}
        live_workers = (telemetry.get("worker_runtime") or {}).get("active_workers")
        if type(live_workers) is int and live_workers != settings["workers"]:
            return {"status": "INELIGIBLE", "reason": "different_live_worker_count",
                    "first_difference": index + 1, "live_workers": live_workers,
                    "replay_workers": settings["workers"]}
        scorer = (telemetry.get("score_explanation") or {}).get("scorer") or {}
        if scorer:
            saw_model_identity = True
            if scorer.get("weights_sha256") != model_identity["weights_sha256"]:
                return {"status": "INELIGIBLE", "reason": "different_live_scorer"}
        if step["action"] != row.get("headless_action") or step["payload"] != (row.get("headless_args") or {}):
            return {"status": "FAILED", "reason": "shadow_action_mismatch", "first_difference": index + 1,
                    "replay": {"action": step["action"], "payload": step["payload"]},
                    "live_shadow": {"action": row.get("headless_action"), "payload": row.get("headless_args")}}
        chosen = telemetry.get("chosen") or {}
        if chosen and (step["chosen"].get("action_type") != chosen.get("action_type")
                       or (step["chosen"].get("metadata") or {}).get("card_id")
                       != (chosen.get("metadata") or {}).get("card_id")):
            return {"status": "FAILED", "reason": "semantic_action_mismatch", "first_difference": index + 1}
        for key, trace_key in (("shadow_rng_before", "rng_before_sha256"),
                               ("shadow_rng_after", "rng_after_sha256")):
            if not isinstance(row.get(key), dict):
                return {"status": "INELIGIBLE", "reason": "live_shadow_rng_missing",
                        "first_difference": index + 1}
            if _digest(row[key]) != step[trace_key]:
                return {"status": "FAILED", "reason": "shadow_rng_mismatch",
                        "first_difference": index + 1, "field": key}
    if not saw_model_identity:
        return {"status": "INELIGIBLE", "reason": "live_scorer_identity_missing"}
    if replay.get("status") != "COMPLETED":
        return {"status": "PARTIAL", "reason": "replay_incomplete",
                "matched_prefix": len(replay.get("actions") or [])}
    if len(rows) != len(replay["actions"]):
        return {"status": "FAILED", "reason": "different_combat_action_count",
                "replay_actions": len(replay["actions"]), "live_actions": len(rows)}
    return {"status": "PASS", "actions": len(replay["actions"]), "live_combat_number": combat_number}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--model", type=Path, help="Optional combat-preference-2 model JSON")
    parser.add_argument("--depth", type=int)
    parser.add_argument("--chance-depth", type=int)
    parser.add_argument("--max-search-ms", type=float)
    parser.add_argument("--workers", type=int,
                        help="Fixed workers; defaults to the first recorded live combat decision, then 4")
    parser.add_argument("--max-actions", type=int, default=120)
    parser.add_argument("--no-history", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.max_actions < 1:
        parser.error("max-actions must be positive")
    cfg = CliConfig(repo_root=Path.cwd().resolve())
    metadata, restore_json = load_snapshot(args.snapshot, cfg)
    recorded, recorded_workers = _recorded_config(metadata)
    settings = {
        "depth": args.depth if args.depth is not None else int(recorded.get("depth") or 8),
        "chance_depth": args.chance_depth if args.chance_depth is not None else int(recorded.get("chance_depth") or 1),
        "max_search_ms": args.max_search_ms if args.max_search_ms is not None else float(recorded.get("search_budget_ms") or 20000),
        "workers": args.workers if args.workers is not None else recorded_workers or 4,
        "max_actions": args.max_actions,
    }
    if (settings["depth"] < 1 or settings["chance_depth"] < 0
            or settings["max_search_ms"] < 0 or settings["workers"] < 1):
        parser.error("Invalid search settings")
    model = json.loads(args.model.read_text(encoding="utf-8")) if args.model else active_model()
    validate_model(model)
    first = replay_once(metadata, restore_json, cfg, model=model, **settings)
    second = replay_once(metadata, restore_json, cfg, model=model, **settings)
    aa = compare_replays(first, second)
    floor_value = (metadata.get("context") or {}).get("floor", metadata.get("floor"))
    floor = int(floor_value) if floor_value is not None else None
    model_identity = CombatScoring(stage_for_floor(floor), model).identity
    history = ({"status": "SKIPPED"} if args.no_history
               else compare_history(metadata, first, model_identity, settings))
    report = {"schema": "sts2.combat_replay_gate.v1", "snapshot_id": metadata["snapshot_id"],
              "snapshot": str(args.snapshot.resolve()), "settings": settings,
              "model": model_identity, "aa": aa, "history": history,
              "eligible_for_ab": aa["status"] == "PASS" and history["status"] == "PASS",
              "first": first, "second": second}
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")
    if aa["status"] != "PASS" or history["status"] == "FAILED":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
