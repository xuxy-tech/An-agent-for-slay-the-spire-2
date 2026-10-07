"""One versioned feature/model boundary for search, sandbox and offline scoring."""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from controller.card_catalog import load_card_catalog
from controller.combat_observation import CombatObservationPair
from controller.preference_model import LinearPreferenceModel
from controller.sandbox_features import FEATURE_VERSION, OBSERVATION_MODE, extract_features

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_MODEL_PATH = ROOT / 'data/scoring/combat_preference.json'
DEFAULT_SCORE_MODE = 'preference'


def stage_for_floor(floor: int | None) -> str:
    if floor is None:
        return 'mid'
    return 'early' if floor <= 5 else 'mid' if floor <= 10 else 'late'


def active_model() -> dict:
    payload = json.loads(ACTIVE_MODEL_PATH.read_text(encoding='utf-8'))
    validate_model(payload)
    return payload


def validate_model(payload: dict) -> None:
    if (payload.get('kind') != 'linear_preference' or payload.get('feature_version') != FEATURE_VERSION
            or payload.get('observation_mode') != OBSERVATION_MODE):
        raise ValueError('Active combat scorer kind, feature version or observation mode mismatch')
    LinearPreferenceModel(payload['weights'], payload.get('ability_config'))


@lru_cache(maxsize=1)
def card_catalog() -> dict:
    return {row['id']: row for row in load_card_catalog(ROOT)}


class CombatScoring:
    def __init__(self, stage: str = 'mid', model: dict | None = None):
        if stage not in {'early', 'mid', 'late'}:
            raise ValueError('Unknown scoring stage')
        self.payload = model if model is not None else active_model()
        validate_model(self.payload)
        self.stage = stage
        self.model = LinearPreferenceModel(self.payload['weights'], self.payload.get('ability_config'))
        self.cards = card_catalog()
        self.identity = {
            'kind': self.payload['kind'], 'feature_version': FEATURE_VERSION,
            'observation_mode': OBSERVATION_MODE, 'stage': stage,
            'trained': bool(self.payload.get('trained', False)),
            'ability_config': dict(self.model.ability_config),
            'weights_sha256': hashlib.sha256(json.dumps(self.payload, sort_keys=True,
                separators=(',', ':')).encode('utf-8')).hexdigest(),
        }

    def features(self, root: dict, leaf: dict, trace: list[dict]) -> dict:
        return extract_features(root, leaf, trace, self.stage, self.cards,
                                ability_config=self.model.ability_config)

    def observation(self, root: dict, leaf: dict, trace: list[dict]) -> CombatObservationPair:
        """Structured input under the same information policy as current features.

        Features use the lightweight projection directly; this API adds immutable
        records for inspection and offline storage.
        """
        return CombatObservationPair(root, leaf, trace, self.stage,
                                     information_policy=OBSERVATION_MODE)

    @staticmethod
    def valid_leaf(leaf: dict) -> bool:
        return (leaf.get('success') is not False
                and leaf.get('terminal_decision') not in {'unknown','error','failed','card_select','bundle_select','search_state_result'}
                and not (leaf.get('terminal_decision') in {'victory','card_reward','map_select','treasure','shop','rest_site'}
                         and float(leaf.get('terminal_surviving_enemy_hp') or 0) > 0))

    def score(self, root: dict, leaf: dict, trace: list[dict]) -> float:
        if not self.valid_leaf(leaf):
            return float('-inf')
        return self.model.score_batch([self.features(root, leaf, trace)])[0]

    def explain(self, root: dict, leaf: dict, trace: list[dict]) -> dict:
        if not self.valid_leaf(leaf):
            return {'mode': DEFAULT_SCORE_MODE, 'score': None, 'total': None, 'comparable': False,
                    'scorer': self.identity, 'contributions': []}
        return self.explain_features(self.features(root, leaf, trace))

    def explain_features(self, features: dict) -> dict:
        result = self.model.explain(features)
        ability_score = (0.0 if features.get('terminal') else sum(
            self.model.weights[key] * value for key, value in features.get('ability_values', {}).items()))
        return {**result, 'mode': DEFAULT_SCORE_MODE, 'total': result['score'],
                'features': features['values'], 'feature_record': features,
                'ability_score': ability_score, 'base_score': result['score'] - ability_score,
                'terminal': features.get('terminal'), 'scorer': self.identity,
                'trained': self.identity['trained'],
                'contributions': [{**row, 'contribution': row['score']} for row in result['contributions']]}


def history_trace(history) -> list[dict]:
    """Features currently use history only for resource consumption and audit."""
    return [{'action': {'action_type': row.action}, 'before': {}} for row in history]
