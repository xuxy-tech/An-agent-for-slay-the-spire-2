"""Immutable, versioned observations; no simulation, scoring or feature weights.

The supplied-state view is an archive of existing search input, not permission
to expose all engine information to a learned model. No native snapshot decoder
belongs here. Entity order and unknown fields/IDs are deliberately preserved.
"""
from __future__ import annotations

import math
import json
from dataclasses import dataclass
from types import MappingProxyType
from collections.abc import Mapping
from typing import Any

SCHEMA_VERSION = 'sts2.combat_observation.v1'
PAIR_VERSION = 'sts2.combat_observation_pair.v1'
PILE_NAMES = ('hand', 'draw_pile', 'discard_pile', 'exhaust_pile', 'play_pile')
MODEL_INFORMATION_POLICY = 'simulated_hand_unordered_piles_v1'
POLICIES = ('supplied_state', 'hand_and_stage', MODEL_INFORMATION_POLICY)
CARD_FIELDS = ('card_id', 'upgrade', 'current_cost', 'display_cost',
               'display_costs_x', 'keywords', 'affliction', 'affliction_count')
HIDDEN_KEYS = frozenset(PILE_NAMES[1:]) | frozenset(
    name + '_count' for name in PILE_NAMES[1:])


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError('Observation object keys must be strings')
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise TypeError('Observations require finite JSON values')


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _project(value: Any, policy: str) -> Any:
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if policy == 'hand_and_stage' and key in HIDDEN_KEYS:
                continue
            if policy == MODEL_INFORMATION_POLICY and key in PILE_NAMES[1:]:
                result[key] = _card_multiset(item)
            else:
                result[key] = _project(item, policy)
        return result
    if isinstance(value, (list, tuple)):
        return [_project(item, policy) for item in value]
    return value


def _card_multiset(value: Any) -> Any:
    """Only approved card attributes cross the model boundary, never positions."""
    if value is None:
        return None
    if isinstance(value, Mapping) and value.get('kind') == 'card_multiset':
        rows = [(row['card'], row['count']) for row in value['cards']]
    elif isinstance(value, (list, tuple)):
        rows = [(card, 1) for card in value]
    else:
        raise ValueError('Expected card pile or card multiset')
    counts = {}
    for card, count in rows:
        if not isinstance(card, Mapping) or type(count) is not int or count <= 0:
            raise ValueError('Invalid card multiset entry')
        clean = {key: _thaw(card[key]) for key in CARD_FIELDS if key in card}
        token = json.dumps(clean, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        counts[token] = counts.get(token, 0) + count
    return {'kind': 'card_multiset', 'cards': [
        {'card': json.loads(token), 'count': counts[token]} for token in sorted(counts)]}


def project_model_state(state: Mapping) -> dict:
    """Pure JSON projection also usable on historical partial search states."""
    return _project(state, MODEL_INFORMATION_POLICY)


@dataclass(frozen=True)
class ObservedField:
    """Missing/null/unavailable are distinct from observed zero or empty lists.

Paths are source locations, never persistent entity identities. ``unavailable``
means a parent is null or of the wrong shape, rather than an absent object key.
"""
    path: tuple[str | int, ...]
    status: str
    value: Any = None

    def to_dict(self) -> dict:
        result = {'path': list(self.path), 'status': self.status}
        if self.status in {'value', 'null'}:
            result['value'] = _thaw(self.value)
        return result


@dataclass(frozen=True, init=False)
class CombatObservation:
    source: Mapping
    information_policy: str

    def __init__(self, state: Mapping, *, information_policy: str = 'supplied_state'):
        if information_policy not in POLICIES:
            raise ValueError('Unknown observation information policy')
        if not isinstance(state, Mapping) or 'combat' not in state:
            raise ValueError('Expected an existing search-state object with combat')
        # Validate before projection so unsupported objects cannot be silently lost.
        frozen = _freeze(state)
        object.__setattr__(self, 'source', _freeze(_project(frozen, information_policy)))
        object.__setattr__(self, 'information_policy', information_policy)

    def at(self, *path: str | int) -> ObservedField:
        current = self.source
        for part in path:
            if self.information_policy == 'hand_and_stage' and part in HIDDEN_KEYS:
                return ObservedField(tuple(path), 'excluded')
            if isinstance(current, Mapping) and isinstance(part, str):
                if part not in current:
                    return ObservedField(tuple(path), 'missing')
                current = current[part]
            elif isinstance(current, tuple) and type(part) is int:
                if part < 0 or part >= len(current):
                    return ObservedField(tuple(path), 'missing')
                current = current[part]
            else:
                return ObservedField(tuple(path), 'unavailable')
        return ObservedField(tuple(path), 'null' if current is None else 'value', current)

    @property
    def is_terminal(self) -> bool:
        return self.at('terminal_decision').status == 'value'

    def _combat_field(self, name: str) -> ObservedField:
        # Terminal combat fields may be synthetic placeholders. Read only the
        # native terminal payload for convenience views, without borrowing root
        # values or merging stale powers into the outcome.
        parent = 'terminal_result' if self.is_terminal else 'combat'
        return self.at(parent, name)

    @property
    def player(self) -> ObservedField:
        return self._combat_field('player')

    @property
    def enemies(self) -> ObservedField:
        return self._combat_field('enemies')

    @property
    def piles(self) -> Mapping[str, ObservedField]:
        return MappingProxyType({name: self._combat_field(name) for name in PILE_NAMES})

    @property
    def actions(self) -> ObservedField:
        return self._combat_field('available_actions')

    @property
    def context(self) -> Mapping[str, ObservedField]:
        return MappingProxyType({
            'character': self.at('character'),
            'encounter': self.at('combat', 'encounter_id'),
            'turn': self._combat_field('turn_number'),
            'round': self._combat_field('round_number'),
            'is_player_turn': self._combat_field('is_player_turn'),
            'success': self.at('success'),
            'terminal_decision': self.at('terminal_decision'),
            'settlement': self.at('leaf_settlement'),
        })

    def to_record(self) -> dict:
        return {'schema': SCHEMA_VERSION, 'information_policy': self.information_policy,
                'state': _thaw(self.source)}

    @classmethod
    def from_record(cls, record: Mapping) -> CombatObservation:
        if record.get('schema') != SCHEMA_VERSION:
            raise ValueError('Observation schema mismatch')
        return cls(record['state'], information_policy=record['information_policy'])


@dataclass(frozen=True, init=False)
class CombatObservationPair:
    """Root/outcome plus unmodified trace and stage, without value assumptions.

Settlement metadata is evidence from the caller, not validated by this adapter.
The existing search remains responsible for settlement and leaf eligibility.
"""
    root: CombatObservation
    outcome: CombatObservation
    trace: tuple
    stage: str

    def __init__(self, root: Mapping, outcome: Mapping, trace: list, stage: str,
                 *, information_policy: str = 'hand_and_stage'):
        if not isinstance(trace, (list, tuple)) or not isinstance(stage, str):
            raise TypeError('Expected an action trace sequence and stage string')
        object.__setattr__(self, 'root', CombatObservation(root, information_policy=information_policy))
        object.__setattr__(self, 'outcome', CombatObservation(outcome, information_policy=information_policy))
        object.__setattr__(self, 'trace', _freeze(_project(_freeze(trace), information_policy)))
        object.__setattr__(self, 'stage', stage)

    def to_record(self) -> dict:
        return {'schema': PAIR_VERSION, 'root': self.root.to_record(),
                'outcome': self.outcome.to_record(), 'trace': _thaw(self.trace),
                'stage': self.stage}

    @classmethod
    def from_record(cls, record: Mapping) -> CombatObservationPair:
        if record.get('schema') != PAIR_VERSION:
            raise ValueError('Observation pair schema mismatch')
        root = CombatObservation.from_record(record['root'])
        outcome = CombatObservation.from_record(record['outcome'])
        if root.information_policy != outcome.information_policy:
            raise ValueError('Root and outcome information policies differ')
        return cls(root.source, outcome.source, record['trace'], record['stage'],
                   information_policy=root.information_policy)
