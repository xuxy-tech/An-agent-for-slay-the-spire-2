"""Small pairwise preference model; feature and scorer contracts are independent."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Protocol, Sequence

from controller.sandbox_features import FEATURES, FEATURE_VERSION, OBSERVATION_MODE
from controller.combat_abilities import ability_settings


class Scorer(Protocol):
    def score_batch(self, features: Sequence[dict]) -> list[float]: ...


class LinearPreferenceModel:
    kind = 'linear_preference'

    def __init__(self, weights: dict[str, float] | None = None, ability_config: dict | None = None):
        self.ability_config = ability_settings(ability_config)
        self.weights = weights if weights is not None else {item.key: item.initial_weight for item in FEATURES}
        if set(self.weights) != {item.key for item in FEATURES}:
            raise ValueError('Model feature names do not match the current schema')
        if any(not math.isfinite(value) or value < 0 for value in self.weights.values()):
            raise ValueError('Weights must be finite and nonnegative')

    def score_batch(self, features: Sequence[dict]) -> list[float]:
        result = []
        for row in features:
            if ability_settings(row.get('ability_config')) != self.ability_config:
                raise ValueError('Ability settings mismatch; reextract features from stored states')
            if row['version'] != FEATURE_VERSION:
                raise ValueError('Feature version mismatch')
            if row.get('observation_mode', OBSERVATION_MODE) != OBSERVATION_MODE:
                raise ValueError('Observation mode mismatch')
            if any(not math.isfinite(float(row['values'][key])) for key in self.weights):
                raise ValueError('Features must be finite')
            terminal = row.get('terminal')
            if terminal in {'game_over', 'defeat'}:
                result.append(-1_000_000.0)
            elif terminal in {'victory', 'card_reward', 'map_select', 'treasure', 'shop', 'rest_site'}:
                result.append(1_000_000.0)
            else:
                result.append(sum(self.weights[key] * float(row['values'][key]) for key in self.weights))
        return result

    def explain(self, features: dict) -> dict:
        return {'kind': self.kind, 'feature_version': FEATURE_VERSION,
                'score': self.score_batch([features])[0], 'trained': False,
                'contributions': [{'key': item.key, 'label': item.label,
                                   'value': features['values'][item.key], 'weight': self.weights[item.key],
                                   'score': features['values'][item.key] * self.weights[item.key]} for item in FEATURES]}


def fit_pairs(pairs: list[dict], regularization: float = 0.1, steps: int = 1500) -> dict:
    """Projected gradient descent with a quadratic prior around initial weights."""
    if not pairs or regularization <= 0:
        raise ValueError('Need labeled pairs and positive regularization')
    names = [item.key for item in FEATURES]
    prior = [item.initial_weight for item in FEATURES]
    weights = list(prior)
    examples = []
    config = ability_settings(pairs[0]['a'].get('ability_config'))
    for pair in pairs:
        if any(ability_settings(row.get('ability_config')) != config for row in (pair['a'], pair['b'])):
            raise ValueError('Mixed ability settings; reextract all features under one configuration')
        if pair['a']['version'] != FEATURE_VERSION or pair['b']['version'] != FEATURE_VERSION:
            raise ValueError('Feature version mismatch')
        if any(row.get('training_eligible') is not True for row in (pair['a'], pair['b'])):
            raise ValueError('Training requires complete current-policy pile observations')
        target = pair['target']
        if target not in {0.0, 0.5, 1.0}:
            raise ValueError('Invalid preference target')
        delta = [float(pair['a']['values'][key]) - float(pair['b']['values'][key]) for key in names]
        if any(not math.isfinite(value) for value in delta):
            raise ValueError('Features must be finite')
        if any(row.get('observation_mode', OBSERVATION_MODE) != OBSERVATION_MODE for row in (pair['a'], pair['b'])):
            raise ValueError('Observation mode mismatch')
        examples.append((delta, target))
    lipschitz = regularization + sum(sum(v*v for v in x) for x, _ in examples) / len(examples) / 4
    for _ in range(steps):
        gradient = [regularization * (a-b) for a, b in zip(weights, prior)]
        for delta, target in examples:
            logit = max(-40, min(40, sum(w*x for w, x in zip(weights, delta))))
            error = 1 / (1 + math.exp(-logit)) - target
            for index, value in enumerate(delta):
                gradient[index] += error * value / len(examples)
        weights = [max(0.0, value - grad / lipschitz) for value, grad in zip(weights, gradient)]
    return {'kind': 'linear_preference', 'feature_version': FEATURE_VERSION,
            'trained': True, 'ability_config': config,
            'observation_mode': OBSERVATION_MODE, 'feature_order': names,
            'active_features': [name for index, name in enumerate(names)
                                if any(abs(delta[index]) > 1e-12 for delta, _ in examples)],
            'weights': dict(zip(names, weights)), 'pair_count': len(examples),
            'regularization': regularization, 'validation': 'not_evaluated'}


def main():
    parser = argparse.ArgumentParser(description='Fit a candidate model from confirmed sandbox preferences')
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--regularization', type=float, default=0.1)
    parser.add_argument('--reextract', action='store_true', help='Recompute features from stored raw scenarios/trajectories in memory')
    args = parser.parse_args()
    data = json.loads(args.dataset.read_text(encoding='utf-8'))
    plans = {plan['id']: plan for plan in data['plans']}
    if args.reextract:
        from controller.combat_scoring import CombatScoring
        scenes = {scene['id']: scene for scene in data['scenarios']}
        for plan in plans.values():
            scene = scenes[plan['scenario_id']]
            plan['features'] = CombatScoring(scene['stage']).features(scene['root_state'], plan['leaf_state'], plan['trace'])
    pairs = []
    groups = set()
    latest = {}
    for label in sorted(data['labels'], key=lambda row: row.get('created_at', 0)):
        latest[(label['scenario_id'], tuple(sorted((label['a'], label['b']))), label.get('author'))] = label
    for label in latest.values():
        if label['preference'] == 'unsure' or label.get('review_status') != 'confirmed':
            continue
        a, b = plans[label['a']], plans[label['b']]
        if (a['scenario_id'] != b['scenario_id'] or not a['verified'] or not b['verified']
                or a.get('preference_scope') != 'realized_outcome' or b.get('preference_scope') != 'realized_outcome'):
            raise ValueError('Only same-scenario verified plans may be compared')
        if a['features'].get('terminal') or b['features'].get('terminal'):
            continue
        groups.add(a['scenario_id'])
        pairs.append({'a': a['features'], 'b': b['features'],
                      'target': {'a': 1.0, 'b': 0.0, 'tie': 0.5}[label['preference']]})
    if not pairs and data.get('demonstrations'):
        raise ValueError('This export contains action demonstrations, not usable pairwise preferences. '
                         'Unchosen actions are not automatically negative labels.')
    model = fit_pairs(pairs, args.regularization)
    model['scenario_count'] = len(groups)
    model['preference_scope'] = 'realized_outcome'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(model, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'pairs': len(pairs), 'scenarios': len(groups), 'validation': 'not_evaluated'}))


if __name__ == '__main__':
    main()
