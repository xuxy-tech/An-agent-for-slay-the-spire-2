"""Score-independent offline enumeration of player-turn settlement outcomes.

Only engine operations are borrowed from CombatSearcher. No search, ranking,
symmetry reduction, depth settlement, or lethal early exit is used here.
"""
from __future__ import annotations

from collections import deque
import hashlib
import json
import time
from itertools import combinations

from controller.combat_observation import project_model_state
from controller.combat_scoring import CombatScoring, history_trace
from controller.search.actions import available_actions_from_search_state
from controller.search.combat_search import RecordedAction

SCHEMA = 'sts2.turn_learning.v1'
TERMINALS = {'victory', 'card_reward', 'map_select', 'treasure', 'shop',
             'rest_site', 'defeat', 'game_over'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def trace_payload(history):
    return [{'action_type': row.action, 'args': row.args} for row in history]


def turn_groups(rows):
    """Keep raw decisions including overlaps; reject incomplete demonstrations later."""
    groups = {}
    for row in rows:
        if row.get('record_type') != 'decision':
            continue
        before = row.get('observation_before') or {}
        if before.get('in_combat') is not True:
            continue
        meta = row.get('authoritative_snapshot') or {}
        if (row.get('action') or {}).get('type') not in {'play_card', 'end_turn', 'use_potion'}:
            continue
        key = (row.get('combat_id'), meta.get('round_number', row.get('turn')))
        groups.setdefault(key, []).append(row)
    return list(groups.values())


def enumerate_turn(engine, scorer, *, max_nodes=2500, max_seconds=60, max_actions=64):
    root = engine._extract_search_state(engine.combat_to_state([]))
    if root.get('success') is not True or root.get('terminal_decision'):
        raise ValueError('Imported root is not a player decision')
    queue = deque([(root, [], None)])
    leaves, edges, errors = [], [], []
    started = time.monotonic()
    expanded = 0
    cutoffs = 0
    # No approximate state merge: ordered piles, history and RNG can matter.
    # Every reached path remains individually attributable in the output.
    while queue:
        state, history, parent = queue.popleft()
        if max_actions > 0 and len(history) >= max_actions:
            cutoffs += 1
            continue
        if state.get('terminal_decision') == 'card_select':
            choice = state['terminal_result']
            indices = [card['index'] for card in choice['cards']]
            actions = [RecordedAction('select_cards', {'indices': ','.join(map(str, selected))})
                       for count in range(choice['min_select'], min(choice['max_select'], len(indices)) + 1)
                       for selected in combinations(indices, count)]
        else:
            actions = [engine._recorded_action_from_search_action(action)
                       for action in available_actions_from_search_state(state)]
        for index, action in enumerate(actions):
            if ((max_nodes > 0 and expanded >= max_nodes)
                    or (max_seconds > 0 and time.monotonic() - started >= max_seconds)):
                queue.appendleft((state, history, parent))
                return root, leaves, {'exhaustive': False, 'expanded_edges': expanded,
                    'pending_nodes': len(queue), 'pending_actions_here': len(actions) - index,
                    'depth_cutoffs': cutoffs, 'errors': errors, 'edges': edges,
                    'seconds': time.monotonic() - started, 'stop_reason': 'budget'}
            step = action
            path = history + [step]
            edge_id = expanded
            expanded += 1
            edge = {'id': edge_id, 'parent': parent, 'action': trace_payload([step])[0]}
            edges.append(edge)
            try:
                child = engine._extract_search_state(engine.combat_to_state(path))
                if step.action == 'end_turn':
                    child = engine._validate_settled_transition(state, child)
                terminal = child.get('terminal_decision')
                if child.get('success') is not True or (terminal and terminal not in TERMINALS | {'card_select'}):
                    raise ValueError(str(child.get('error') or terminal or child))
                if step.action == 'end_turn' or terminal in TERMINALS:
                    if terminal:
                        child = engine._settle_leaf_state(child, path)[0]
                    if not scorer.valid_leaf(child):
                        raise ValueError('Invalid settlement leaf')
                    features = scorer.features(root, child, history_trace(path))
                    leaf = {'id': edge_id, 'path': trace_payload(path),
                            'state': project_model_state(child), 'features': features}
                    leaves.append(leaf)
                    edge['leaf'] = edge_id
                else:
                    queue.append((child, path, edge_id))
            except Exception as exc:
                edge['error'] = str(exc)
                errors.append({'edge': edge_id, 'error': str(exc)})
    return root, leaves, {'exhaustive': not errors and not cutoffs,
                         'expanded_edges': expanded, 'pending_nodes': 0,
                         'depth_cutoffs': cutoffs, 'errors': errors, 'edges': edges,
                         'seconds': time.monotonic() - started, 'stop_reason': 'queue_empty'}


def demonstration_leaf(root, rows, end_engine, scorer):
    """Settle the authoritative human end-turn snapshot, not a best-scoring continuation.

    The human path is preserved as recorded evidence, not claimed to be fully
    replay-verified. No creature-ID or card-name guessing is involved.
    """
    if any(row.get('settlement') != 'settled' for row in rows):
        raise ValueError('Human turn contains unsettled or overlapping actions')
    if rows[-1]['action']['type'] != 'end_turn':
        raise ValueError('Human turn has no recorded end_turn boundary')
    before = end_engine._extract_search_state(end_engine.combat_to_state([]))
    after = end_engine._extract_search_state(end_engine.combat_to_state([RecordedAction('end_turn', {})]))
    after = end_engine._validate_settled_transition(before, after)
    if not scorer.valid_leaf(after):
        raise ValueError('Human end_turn settlement failed')
    trace = [{'action': {'action_type': row['action']['type']}, 'before': {}} for row in rows]
    return {'state': project_model_state(after), 'features': scorer.features(root, after, trace),
            'human_path': [{'decision_id': row['decision_id'], 'action': row['action'],
                            'snapshot': row.get('authoritative_snapshot')} for row in rows],
            'verification': 'authoritative_endpoint_settled; full_path_not_replayed'}


def preference_record(turn):
    human = turn.get('demonstration')
    pairs = []
    seen = set()
    if human and human['features'].get('training_eligible') is True:
        for leaf in turn['leaves']:
            a, b = human['features'], leaf['features']
            # Hard-coded terminal rewards are not trainable linear preferences.
            if a.get('terminal') or b.get('terminal') or b.get('training_eligible') is not True:
                continue
            key = digest(b)
            if key in seen or a['values'] == b['values']:
                continue
            seen.add(key)
            pairs.append({'chosen': a, 'other': b, 'target': 1,
                          'other_leaf_id': leaf['id'],
                          'label_semantics': 'human_completed_turn_demonstration_preference_not_optimality'})
    return {'schema': SCHEMA, 'human': {'session_id': turn['session_id']},
            'root_id': turn['root_id'], 'fit_ready': bool(pairs),
            'coverage_exhaustive': turn['coverage']['exhaustive'],
            'label_semantics': 'human_completed_turn_demonstration_preference_not_optimality',
            'pairwise_examples': pairs,
            'reason': turn.get('demonstration_error') if not human else (None if pairs else 'no_distinct_trainable_pairs')}
