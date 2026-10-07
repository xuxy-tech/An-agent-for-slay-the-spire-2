"""Fit the current linear leaf scorer from strict human/engine joins."""
from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict
from pathlib import Path

from controller.preference_model import LinearPreferenceModel, fit_pairs
from controller.sandbox_features import FEATURE_VERSION
from controller.combat_abilities import ability_settings


REGULARIZATION_GRID = (0.01, 0.03, 0.1, 0.3, 1.0, 3.0, 10.0)


def _pairs_by_session(records: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        if not record.get("fit_ready"):
            continue
        session_id = str((record.get("human") or {}).get("session_id") or "unknown")
        for example in record.get("pairwise_examples") or []:
            chosen = example.get("chosen")
            other = example.get("other")
            if isinstance(chosen, dict) and isinstance(other, dict):
                grouped[session_id].append({
                    "a": chosen,
                    "b": other,
                    "target": float(example.get("target", 1)),
                    "root_id": record.get("root_id"),
                })
    return dict(grouped)


def _evaluate(weights: dict[str, float], pairs: list[dict], ability_config: dict | None = None) -> dict[str, float | int]:
    model = LinearPreferenceModel(weights, ability_config)
    correct = 0.0
    log_loss = 0.0
    for pair in pairs:
        left, right = model.score_batch([pair["a"], pair["b"]])
        margin = max(-40.0, min(40.0, left - right))
        probability = 1.0 / (1.0 + math.exp(-margin))
        correct += 1.0 if margin > 0 else 0.5 if margin == 0 else 0.0
        log_loss -= math.log(max(probability, 1e-12))
    count = len(pairs)
    return {
        "pairs": count,
        "pair_accuracy": correct / count if count else 0.0,
        "log_loss": log_loss / count if count else 0.0,
    }


def _leave_one_session_out(grouped: dict[str, list[dict]], regularization: float) -> dict:
    folds = []
    prior = LinearPreferenceModel().weights
    for held_out, test_pairs in sorted(grouped.items()):
        train_pairs = [pair for session, rows in grouped.items() if session != held_out for pair in rows]
        if not train_pairs or not test_pairs:
            continue
        trained = fit_pairs(train_pairs, regularization=regularization)
        folds.append({
            "held_out_session": held_out,
            "train_pairs": len(train_pairs),
            "baseline": _evaluate(prior, test_pairs, trained['ability_config']),
            "trained": _evaluate(trained["weights"], test_pairs, trained["ability_config"]),
        })
    held_out_pairs = sum(int(fold["trained"]["pairs"]) for fold in folds)

    def weighted(metric: str, branch: str) -> float:
        if not held_out_pairs:
            return 0.0
        return sum(
            float(fold[branch][metric]) * int(fold[branch]["pairs"])
            for fold in folds
        ) / held_out_pairs

    baseline_accuracy = weighted("pair_accuracy", "baseline")
    trained_accuracy = weighted("pair_accuracy", "trained")
    return {
        "method": "leave_one_session_out",
        "regularization": regularization,
        "folds": folds,
        "held_out_pairs": held_out_pairs,
        "baseline_pair_accuracy": baseline_accuracy,
        "trained_pair_accuracy": trained_accuracy,
        "accuracy_uplift": trained_accuracy - baseline_accuracy,
        "baseline_log_loss": weighted("log_loss", "baseline"),
        "trained_log_loss": weighted("log_loss", "trained"),
    }


def _retune_feature(row: dict, config: dict) -> dict:
    """Reprice recorded per-turn forecasts exactly; no new engine rollout is needed."""
    old = ability_settings(row.get('ability_config'))
    if old['max_horizon'] != config['max_horizon'] or old['damage_per_turn'] != config['damage_per_turn']:
        raise ValueError('Only realization and discount can be learned from saved turn forecasts')
    opportunities = row.get('opportunities') or {}
    if not isinstance(opportunities.get('root_potential'), dict) or not isinstance(opportunities.get('leaf_potential'), dict):
        raise ValueError('Saved features lack per-turn ability forecasts; rebuild the leaf dataset')
    horizon = float(opportunities['horizon'])
    def potential(side: str, key: str) -> float:
        total = 0.0
        for turn_row in opportunities[f'{side}_potential'].get('turns') or []:
            turn = int(turn_row['turn'])
            before = (turn_row.get('baseline') or {}).get(key, 0.0)
            after = (turn_row.get('powered') or {}).get(key, 0.0)
            total += config['realization'] * config['discount'] ** turn * min(1.0, horizon - turn) * (after - before)
        return total
    base = row.get('base_values')
    if not isinstance(base, dict):
        raise ValueError('Saved features lack base_values; rebuild the leaf dataset')
    ability = {key: 0.0 if row.get('terminal') else potential('leaf', key) - potential('root', key)
               for key in base}
    result = dict(row)
    result['ability_config'] = config
    result['ability_values'] = ability
    result['values'] = {key: float(value) + ability[key] for key, value in base.items()}
    return result


def _retune_pairs(pairs: list[dict], config: dict) -> list[dict]:
    return [{**pair, 'a': _retune_feature(pair['a'], config),
             'b': _retune_feature(pair['b'], config)} for pair in pairs]


def _write_progress(path: Path | None, phase: str, completed: int, total: int) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps({'phase': phase, 'completed': completed, 'total': total},
                                    ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fit the linear combat leaf scorer from fit-ready human joins")
    parser.add_argument("--input", type=Path, required=True, help="matches.jsonl produced by the strict join")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--regularization", type=float, default=None,
                        help="fixed regularization; omit to select by leave-one-session-out validation")
    parser.add_argument('--progress', type=Path)
    args = parser.parse_args()
    _write_progress(args.progress, '读取比较样本', 0, 1)
    records = []
    with args.input.open("r", encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if isinstance(row, dict):
                records.append(row)
    grouped = _pairs_by_session(records)
    pairs = [pair for rows in grouped.values() for pair in rows]
    if not pairs:
        raise SystemExit("No fit-ready human leaf pairs. Run the strict join after collecting paired leaf data.")
    versions = {
        str(side.get("version") or side.get("feature_version") or "")
        for pair in pairs
        for side in (pair.get("a") or {}, pair.get("b") or {})
    }
    if versions != {FEATURE_VERSION}:
        raise SystemExit(
            f"Current scorer requires {FEATURE_VERSION}; received feature versions "
            f"{sorted(versions)}. Rebuild the strict join from current leaf snapshots."
        )
    _write_progress(args.progress, '读取比较样本', 1, 1)
    grid = ((args.regularization,) if args.regularization is not None else
            ((0.1,) if len(grouped) < 2 else REGULARIZATION_GRID))
    validations = []
    for index, value in enumerate(grid, 1):
        validations.append(_leave_one_session_out(grouped, value))
        _write_progress(args.progress, '选择权重正则强度', index, len(grid))
    selected = max(
        validations,
        key=lambda row: (row["trained_pair_accuracy"], -row["trained_log_loss"], row["regularization"]),
    )
    if not selected['held_out_pairs']:
        selected['status'] = 'not_evaluated_insufficient_sessions'
        for key in ('baseline_pair_accuracy', 'trained_pair_accuracy', 'accuracy_uplift',
                    'baseline_log_loss', 'trained_log_loss'):
            selected[key] = None
    else:
        selected['status'] = 'evaluated'
    original_config = ability_settings(pairs[0]['a'].get('ability_config'))
    # Split by entire turns, never by pairs from the same root. With multiple
    # sessions, reserve one full session; with one, reserve one in five turns.
    if len(grouped) > 1:
        holdout_session = sorted(grouped)[-1]
        tuning_train = [pair for key, rows in grouped.items() if key != holdout_session for pair in rows]
        tuning_test = grouped[holdout_session]
        tuning_method = 'held_out_session'
    else:
        roots = sorted({str(pair['root_id']) for pair in pairs})
        reserved = set(roots[::5]) if len(roots) >= 5 else set()
        tuning_train = [pair for pair in pairs if str(pair['root_id']) not in reserved]
        tuning_test = [pair for pair in pairs if str(pair['root_id']) in reserved]
        tuning_method = 'held_out_turns_one_session' if reserved else 'insufficient_turns'
    candidates = [(r, d) for r in (0.0, 0.15, 0.35, 0.55, 0.75, 1.0)
                  for d in (0.35, 0.6, 0.85, 1.0)]
    candidates.append((original_config['realization'], original_config['discount']))
    candidates = list(dict.fromkeys(candidates))
    sweep = []
    if tuning_train and tuning_test:
        for index, (realization, discount) in enumerate(candidates, 1):
            config = {**original_config, 'realization': realization, 'discount': discount}
            train = _retune_pairs(tuning_train, config)
            test = _retune_pairs(tuning_test, config)
            fitted = fit_pairs(train, regularization=float(selected['regularization']), steps=300)
            measured = _evaluate(fitted['weights'], test, config)
            # A small prior keeps an undercovered ability feature from drifting
            # to a grid boundary on a single session's demonstrations.
            prior_cost = 0.02 * ((realization - original_config['realization']) ** 2
                                 + (discount - original_config['discount']) ** 2)
            sweep.append({'realization': realization, 'discount': discount,
                          'held_out_pairs': len(test), 'log_loss': measured['log_loss'],
                          'pair_accuracy': measured['pair_accuracy'],
                          'selection_loss': measured['log_loss'] + prior_cost})
            _write_progress(args.progress, '选择能力兑现参数', index, len(candidates))
        winner = max(sweep, key=lambda row: (row['pair_accuracy'], -row['selection_loss'],
                     -abs(row['realization'] - original_config['realization'])
                     -abs(row['discount'] - original_config['discount'])))
        learned_config = {**original_config, 'realization': winner['realization'],
                          'discount': winner['discount']}
    else:
        learned_config = original_config
        _write_progress(args.progress, '选择能力兑现参数', 1, 1)
    tuned_pairs = _retune_pairs(pairs, learned_config)
    _write_progress(args.progress, '拟合最终评分模型', 0, 1)
    model = fit_pairs(tuned_pairs, regularization=float(selected["regularization"]))
    _write_progress(args.progress, '拟合最终评分模型', 1, 1)
    model["training_source"] = "human_completed_turn_demonstrations"
    model["pair_count"] = len(pairs)
    model["session_count"] = len(grouped)
    model['training_metrics'] = {
        'baseline': _evaluate(LinearPreferenceModel().weights, pairs, original_config),
        'trained': _evaluate(model['weights'], tuned_pairs, model['ability_config'])}
    model['ability_parameter_learning'] = {'method': tuning_method, 'grid': sweep,
                                           'original': original_config,
                                           'selected': learned_config,
                                           'held_out_pairs': len(tuning_test)}
    model['label_semantics'] = 'human_completed_turn_demonstration_preference_not_optimality'
    model["validation"] = {
        **selected,
        "regularization_sweep": [
            {
                key: value for key, value in row.items()
                if key != "folds"
            }
            for row in validations
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(model, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    _write_progress(args.progress, '已完成', 1, 1)
    print(json.dumps({
        "pairs": len(pairs),
        "sessions": len(grouped),
        "regularization": selected["regularization"],
        "baseline_pair_accuracy": selected["baseline_pair_accuracy"],
        "trained_pair_accuracy": selected["trained_pair_accuracy"],
        "accuracy_uplift": selected["accuracy_uplift"],
        "output": str(args.output),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
