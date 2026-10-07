"""Probe whether a small linear action scorer has signal in human combat data.

This is deliberately a behavior-ranking probe, not a replacement for leaf-level
counterfactual labels. It compares the chosen combat action with other legal
hand-card/end-turn candidates and evaluates by leaving one complete run out.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from controller.human_capture import audit_capture_session


ACTION_TYPES = {"play_card", "end_turn"}


def scalar(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_card_id(value: Any) -> str:
    text = str(value or "").upper()
    return text[5:] if text.startswith("CARD.") else text


def intent_damage(intent: Any) -> float:
    if not isinstance(intent, dict):
        return 0.0
    if "Attack" not in (intent.get("intent_types") or []):
        return 0.0
    for key in ("total_damage", "damage", "display_damage"):
        if intent.get(key) is not None:
            return max(0.0, scalar(intent[key]))
    return 0.0


def state_context(observation: dict[str, Any]) -> dict[str, float]:
    combat = observation.get("combat") or {}
    player = combat.get("player") or {}
    enemies = [row for row in combat.get("enemies") or [] if isinstance(row, dict)]
    living = [row for row in enemies if row.get("is_alive", True) and scalar(row.get("current_hp", row.get("hp"))) > 0]
    enemy_hps = [max(0.0, scalar(row.get("current_hp", row.get("hp")))) for row in living]
    incoming = sum(intent_damage(row.get("intent")) for row in living)
    max_hp = max(1.0, scalar(player.get("max_hp"), 1.0))
    return {
        "energy": scalar(player.get("energy")) / 5.0,
        "hp_ratio": scalar(player.get("current_hp", player.get("hp"))) / max_hp,
        "block": scalar(player.get("block")) / 50.0,
        "incoming": incoming / 50.0,
        "enemy_hp": sum(enemy_hps) / 200.0,
        "enemy_count": len(living) / 3.0,
        "lowest_enemy_hp": (min(enemy_hps) if enemy_hps else 0.0) / 100.0,
        "turn": scalar(observation.get("turn")) / 10.0,
        "hand_size": len(combat.get("hand") or []) / 10.0,
    }


def card_effects(card: dict[str, Any]) -> dict[str, float]:
    damage = 0.0
    block = 0.0
    draw = 0.0
    for value in card.get("dynamic_values") or []:
        if not isinstance(value, dict):
            continue
        name = str(value.get("name") or "").lower()
        amount = max(0.0, scalar(value.get("current_value", value.get("base_value"))))
        if "damage" in name:
            damage += amount
        if "block" in name:
            block += amount
        if "draw" in name or "card" in name and "draw" in str(value).lower():
            draw += amount
    return {"damage": damage / 30.0, "block": block / 30.0, "draw": draw / 5.0}


def candidate_features(
    observation: dict[str, Any], card: dict[str, Any] | None, *, include_card_identity: bool,
) -> tuple[str, dict[str, float]]:
    context = state_context(observation)
    if card is None:
        key = "end_turn"
        base = {"end_turn": 1.0, "card_cost": 0.0, "damage": 0.0, "block_gain": 0.0,
                "draw": 0.0, "targeted": 0.0, "upgraded": 0.0}
    else:
        key = normalize_card_id(card.get("card_id"))
        effects = card_effects(card)
        base = {
            "end_turn": 0.0,
            "card_cost": scalar(card.get("energy_cost")) / 5.0,
            "damage": effects["damage"],
            "block_gain": effects["block"],
            "draw": effects["draw"],
            "targeted": 1.0 if card.get("requires_target") else 0.0,
            "upgraded": 1.0 if card.get("upgraded") else 0.0,
        }
    features = dict(base)
    features.update({
        "end_turn_incoming": base["end_turn"] * context["incoming"],
        "end_turn_low_enemy": base["end_turn"] * context["lowest_enemy_hp"],
        "end_turn_energy": base["end_turn"] * context["energy"],
        "end_turn_hand": base["end_turn"] * context["hand_size"],
        "damage_incoming": base["damage"] * context["incoming"],
        "damage_low_enemy": base["damage"] * context["lowest_enemy_hp"],
        "block_incoming": base["block_gain"] * context["incoming"],
        "block_hp_ratio": base["block_gain"] * (1.0 - context["hp_ratio"]),
        "cost_energy": base["card_cost"] * context["energy"],
    })
    if include_card_identity and card is not None:
        features[f"card:{key}"] = 1.0
    return key, features


def load_examples(root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    session_report: list[dict[str, Any]] = []
    for directory in sorted(root.glob("human_*")):
        manifest_path = directory / "manifest.json"
        events_path = directory / "events.jsonl"
        if not manifest_path.is_file() or not events_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("status") == "failed":
            continue
        audit = audit_capture_session(events_path)
        if not audit["ok"]:
            raise ValueError(f"Capture audit failed for {directory}: {audit['integrity_errors']}")
        used = 0
        skipped = Counter()
        for line in events_path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("record_type") != "decision" or row.get("settlement") != "settled":
                continue
            action = row.get("action") or {}
            if action.get("type") not in ACTION_TYPES:
                skipped["non_card_or_turn_action"] += 1
                continue
            observation = row.get("observation_before") or {}
            if observation.get("in_combat") is not True:
                skipped["not_combat"] += 1
                continue
            hand = (observation.get("combat") or {}).get("hand") or []
            candidates: dict[str, dict[str, float]] = {}
            for card in hand:
                if not isinstance(card, dict) or card.get("playable") is False:
                    continue
                key, features = candidate_features(observation, card, include_card_identity=False)
                candidates.setdefault(key, features)
            end_key, end_features = candidate_features(observation, None, include_card_identity=False)
            candidates[end_key] = end_features
            chosen = "end_turn" if action.get("type") == "end_turn" else normalize_card_id(action.get("card_id"))
            if chosen not in candidates:
                skipped["chosen_action_missing_from_reconstructed_candidates"] += 1
                continue
            examples.append({"run_id": row.get("run_id"), "combat_id": row.get("combat_id"),
                             "turn": row.get("turn"), "chosen": chosen, "candidates": candidates})
            used += 1
        session_report.append({"session_id": directory.name, "run_id": manifest.get("run_id"),
                               "used": used, "skipped": dict(skipped), "audit": audit})
    if not examples:
        raise ValueError("No usable settled combat card/end-turn examples")
    return examples, {"sessions": session_report, "examples": len(examples)}


def feature_names(examples: list[dict[str, Any]]) -> list[str]:
    names = sorted({name for row in examples for features in row["candidates"].values() for name in features})
    return names


def add_identity_features(examples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in examples:
        candidates = {}
        for key in row["candidates"]:
            features = dict(row["candidates"][key])
            if key != "end_turn":
                features[f"card:{key}"] = 1.0
            candidates[key] = features
        result.append({**row, "candidates": candidates})
    return result


def dot(weights: list[float], vector: list[float]) -> float:
    return sum(a * b for a, b in zip(weights, vector))


def fit_pairwise(rows: list[dict[str, Any]], names: list[str], *, regularization: float = 0.2) -> list[float]:
    pairs: list[list[float]] = []
    for row in rows:
        chosen = row["candidates"][row["chosen"]]
        for key, other in row["candidates"].items():
            if key != row["chosen"]:
                pairs.append([chosen.get(name, 0.0) - other.get(name, 0.0) for name in names])
    weights = np.zeros(len(names), dtype=float)
    if not pairs:
        return weights.tolist()
    matrix = np.asarray(pairs, dtype=float)
    for step in range(1000):
        logits = np.clip(matrix @ weights, -35.0, 35.0)
        probabilities = 1.0 / (1.0 + np.exp(-logits))
        gradient = (matrix.T @ (probabilities - 1.0)) / len(pairs) + regularization * weights
        learning_rate = 0.08 / (1.0 + step / 800.0)
        weights -= learning_rate * gradient
    return weights.tolist()


def evaluate(rows: list[dict[str, Any]], names: list[str], weights: list[float], frequency: Counter[str]) -> dict[str, Any]:
    top1 = 0.0
    pairwise = 0.0
    pair_count = 0
    baseline_top1 = 0.0
    ranks: list[int] = []
    for row in rows:
        scored = [(dot(weights, [features.get(name, 0.0) for name in names]), key)
                  for key, features in row["candidates"].items()]
        scored.sort(reverse=True)
        chosen_score = next(score for score, key in scored if key == row["chosen"])
        top1 += scored[0][1] == row["chosen"]
        ranks.append(next(index + 1 for index, (_, key) in enumerate(scored) if key == row["chosen"]))
        for score, key in scored:
            if key == row["chosen"]:
                continue
            pairwise += 1.0 if chosen_score > score else 0.5 if chosen_score == score else 0.0
            pair_count += 1
        baseline = max(row["candidates"], key=lambda key: (frequency[key], key))
        baseline_top1 += baseline == row["chosen"]
    count = len(rows)
    return {"examples": count, "candidate_pairs": pair_count,
            "top1_accuracy": top1 / count if count else None,
            "pairwise_accuracy": pairwise / pair_count if pair_count else None,
            "mean_reciprocal_rank": statistics.mean(1.0 / rank for rank in ranks) if ranks else None,
            "frequency_baseline_top1": baseline_top1 / count if count else None,
            "weights": dict(zip(names, weights))}


def run_probe(examples: list[dict[str, Any]], *, identity: bool) -> dict[str, Any]:
    prepared = add_identity_features(examples) if identity else examples
    groups = sorted({str(row["run_id"]) for row in prepared})
    folds = []
    for held_out in groups:
        train = [row for row in prepared if str(row["run_id"]) != held_out]
        test = [row for row in prepared if str(row["run_id"]) == held_out]
        names = feature_names(train)
        weights = fit_pairwise(train, names)
        frequency = Counter(row["chosen"] for row in train)
        metrics = evaluate(test, names, weights, frequency)
        folds.append({"held_out_run": held_out, "train_examples": len(train),
                      "test_examples": len(test), "feature_count": len(names), **metrics})
    all_test = sum((fold["test_examples"] for fold in folds), 0)
    return {"variant": "generic_plus_card_identity" if identity else "generic_numeric",
            "run_count": len(groups), "folds": folds,
            "weighted": {
                "examples": all_test,
                "top1_accuracy": sum(f["top1_accuracy"] * f["test_examples"] for f in folds) / all_test,
                "pairwise_accuracy": sum(f["pairwise_accuracy"] * f["candidate_pairs"] for f in folds) / sum(f["candidate_pairs"] for f in folds),
                "frequency_baseline_top1": sum(f["frequency_baseline_top1"] * f["test_examples"] for f in folds) / all_test,
            }}


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe a small linear scorer on human combat choices")
    parser.add_argument("--input", type=Path, default=Path("data/human_play/raw"))
    parser.add_argument("--output", type=Path, default=Path("data/human_play/experiments/linear_scorer_probe"))
    args = parser.parse_args()
    examples, source = load_examples(args.input)
    report = {"schema": "sts2.human_capture.linear_scorer_probe.v1", "source": source,
              "notes": [
                  "This evaluates action ranking, not direct leaf-value supervision.",
                  "Only settled combat play_card/end_turn decisions are used.",
                  "Validation leaves one complete run out; no adjacent decisions cross the fold boundary.",
              ],
              "variants": [run_probe(examples, identity=False), run_probe(examples, identity=True)]}
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str((args.output / 'report.json').resolve()),
                      "examples": source["examples"],
                      "variants": [{"variant": item["variant"], "weighted": item["weighted"]} for item in report["variants"]]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
