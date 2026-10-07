import time
from types import MethodType

import pytest

from controller.search import combat_search
from controller.search.actions import SearchAction
from controller.search.combat_search import CombatSearcher, CombatSpec, SearchResult


@pytest.mark.parametrize('mode', [
    'balanced_action', 'balanced_r0a', 'balanced_r0b', 'balanced_r0c', 'balanced_r0d',
])
def test_named_card_root_adjustment_modes_are_disabled(mode):
    with pytest.raises(ValueError, match='named-card root adjustments'):
        CombatSearcher(None, CombatSpec('Ironclad', 'TEST', 'seed', 0, 'en'), score_mode=mode)


def test_parallel_root_search_covers_every_candidate_with_deterministic_merge(monkeypatch):
    actions = [SearchAction('play_card', card_index=index) for index in range(4)]
    candidates = [
        {'action': action, 'next_history': [], 'child_state': {'success': True}, 'effect_score': 0.0, 'index': index}
        for index, action in enumerate(actions)
    ]
    monkeypatch.setattr(combat_search, 'available_actions_from_search_state', lambda state: actions)
    searcher = CombatSearcher(
        None,
        CombatSpec('Ironclad', 'TEST', 'seed', 0, 'en'),
        parallel_top_level=True,
        max_workers=2,
        max_search_ms=200,
        score_mode='balanced',
    )
    searcher._root_summary = {'player': {'hp': 50}, 'enemies': []}
    searcher._prepare_action_candidates = MethodType(lambda self, state, history, rows: candidates, searcher)
    searcher._prune_counts = MethodType(lambda self: {}, searcher)
    searcher._prune_delta = MethodType(lambda self, before, after: {}, searcher)
    searcher._root_adjustments = MethodType(lambda self, state, info, child, top: {'root_adjustment': 0.0}, searcher)
    searcher._select_root_candidate = MethodType(
        lambda self, pairs: (max(pairs, key=lambda pair: pair[1].score), {'selection_rule': 'score'}), searcher
    )
    searcher._with_root_adjusted_score = MethodType(lambda self, child, info: child, searcher)
    searcher._root_coverage = MethodType(
        lambda self, available, candidate, evaluated, pruned: {
            'available_actions': available, 'candidate_actions': candidate,
            'evaluated_actions': evaluated, 'candidate_coverage_ratio': evaluated / candidate,
        }, searcher
    )

    def evaluate(self, action_info, *args):
        index = action_info['index']
        time.sleep((4 - index) * 0.012)
        result = SearchResult(
            score=float(index), sequence=[action_info['action']],
            leaf_state={'terminal_decision': 'card_reward', 'terminal_result': {'player': {'hp': 50, 'max_hp': 50}}},
            stats={'nodes': index + 1},
        )
        return result, {'audit': {'budget_ms': 10.0, 'budget_exhausted': False,
                                  'completed_turn_lines': 1, 'nodes': index + 1}, 'timing': {}}

    searcher._evaluate_isolated_root_branch = MethodType(evaluate, searcher)
    result = searcher._search_top_level_parallel({'success': True, 'combat': {}}, [], 3, 0, 0)

    assert result.sequence[0].card_index == 3
    assert result.stats['root_coverage']['candidate_coverage_ratio'] == 1.0
    audit = searcher.timing['parallel_audit']
    assert audit['root_jobs_completed'] == 4
    assert audit['active_workers'] == 2
    assert audit['per_root_budget_ms'] == 200.0
    assert audit['root_budget_policy'] == 'one_pass_per_root'
    assert audit['root_prepare_wall_ms'] >= 0.0
    assert audit['coordinator_release_ms'] >= 0.0
    assert audit['stage_b_jobs'] == 0
    phases = searcher.timing_summary()['parallel_phase_breakdown']
    assert phases['root_candidate_prepare_ms'] == audit['root_prepare_wall_ms']
    assert phases['executor_wall_ms'] == audit['stage_a_wall_ms']
    assert set(phases['cli_transport']) == {
        'action', 'get_state', 'capture', 'fingerprint',
        'restore', 'import', 'expand'
    }
    assert all(
        set(metrics) == {'response_bytes', 'queue_wait_total_ms', 'json_parse_total_ms'}
        for metrics in phases['cli_transport'].values()
    )


def test_parallel_root_search_queues_every_root_once_even_after_wall_budget(monkeypatch):
    actions = [SearchAction('play_card', card_index=index) for index in range(4)]
    candidates = [
        {'action': action, 'next_history': [], 'child_state': {'success': True},
         'effect_score': 0.0, 'index': index}
        for index, action in enumerate(actions)
    ]
    monkeypatch.setattr(combat_search, 'available_actions_from_search_state', lambda state: actions)
    searcher = CombatSearcher(
        None, CombatSpec('Ironclad', 'TEST', 'seed', 0, 'en'),
        parallel_top_level=True, max_workers=2, max_search_ms=10,
        score_mode='balanced',
    )
    searcher._root_summary = {'player': {'hp': 50}, 'enemies': []}
    searcher._prepare_action_candidates = MethodType(lambda self, state, history, rows: candidates, searcher)
    searcher._prune_counts = MethodType(lambda self: {}, searcher)
    searcher._prune_delta = MethodType(lambda self, before, after: {}, searcher)
    searcher._root_adjustments = MethodType(lambda self, state, info, child, top: {'root_adjustment': 0.0}, searcher)
    searcher._select_root_candidate = MethodType(
        lambda self, pairs: (max(pairs, key=lambda pair: pair[1].score), {'selection_rule': 'score'}), searcher
    )
    searcher._with_root_adjusted_score = MethodType(lambda self, child, info: child, searcher)
    searcher._root_coverage = MethodType(lambda self, *args: {}, searcher)

    def evaluate(self, action_info, *args):
        time.sleep(0.03)
        result = SearchResult(
            score=float(action_info['index']), sequence=[action_info['action']],
            leaf_state={'combat': {}}, stats={'nodes': 1},
        )
        return result, {'audit': {'budget_exhausted': True, 'completed_turn_lines': 0}, 'timing': {}}

    searcher._evaluate_isolated_root_branch = MethodType(evaluate, searcher)
    result = searcher._search_top_level_parallel({'success': True, 'combat': {}}, [], 3, 0, 0)

    assert result.sequence[0].card_index == 3
    audit = searcher.timing['parallel_audit']
    assert audit['root_jobs_started'] == 4
    assert audit['root_jobs_completed'] == 4
    assert audit['root_jobs_cancelled'] == 0
    assert audit['stage_b_jobs'] == 0


def test_coverage_first_horizon_never_trades_depth_for_coverage():
    searcher = CombatSearcher(
        None, CombatSpec('Ironclad', 'TEST', 'seed', 0, 'en'),
        max_search_ms=20_000, score_mode='balanced',
    )
    wide = searcher._coverage_first_horizon(12, 12, 8)
    shallow = searcher._coverage_first_horizon(3, 3, 8)
    assert wide[:2] == (12, 12)
    assert wide[2]['effective_depth'] == 12
    assert wide[2]['policy'] == 'configured'
    assert shallow[:2] == (10, 10)
    assert shallow[2]['minimum_depth'] == 10
    assert shallow[2]['policy'] == 'depth_floor_10'
    searcher.close()



def test_parallel_frontier_merges_all_siblings_without_nested_fanout():
    actions = [SearchAction('play_card', card_index=index) for index in range(5)]
    candidates = [
        {'action': action, 'next_history': [], 'child_state': {'success': True},
         'effect_score': float(index)}
        for index, action in enumerate(actions)
    ]
    searcher = CombatSearcher(
        None,
        CombatSpec('Ironclad', 'TEST', 'seed', 0, 'en'),
        max_workers=3,
        max_search_ms=0,
        score_mode='balanced',
        parallel_frontier=True,
    )

    def evaluate(self, action_info, *args):
        index = action_info['action'].card_index
        time.sleep((5 - index) * 0.004)
        return SearchResult(
            score=float(index), sequence=[action_info['action']],
            leaf_state={'combat': {}}, stats={'nodes': index + 1},
        ), {'budget_exhausted': False, 'timing': {}, 'coverage': {}}

    searcher._evaluate_isolated_frontier_child = MethodType(evaluate, searcher)
    result = searcher._search_frontier_parallel(
        {'success': True, 'combat': {}}, [], 10, 0, 0, actions, candidates
    )

    assert result is not None
    assert result.sequence[0].card_index == 4
    assert result.stats['parallel_frontier'] is True
    assert result.stats['frontier_jobs_completed'] == 5
    assert searcher.timing['frontier_jobs_cancelled'] == 0
    searcher.close()


def test_parallel_frontier_counts_siblings_left_after_shared_deadline():
    actions = [SearchAction('play_card', card_index=index) for index in range(5)]
    candidates = [
        {'action': action, 'next_history': [], 'child_state': {'success': True},
         'effect_score': 0.0}
        for action in actions
    ]
    searcher = CombatSearcher(
        None,
        CombatSpec('Ironclad', 'TEST', 'seed', 0, 'en'),
        max_workers=2,
        max_search_ms=0,
        score_mode='balanced',
        parallel_frontier=True,
    )
    searcher._search_deadline = time.perf_counter() + 0.01

    def evaluate(self, action_info, *args):
        time.sleep(0.03)
        return SearchResult(
            score=float(action_info['action'].card_index), sequence=[action_info['action']],
            leaf_state={'combat': {}}, stats={'nodes': 1},
        ), {'budget_exhausted': True, 'timing': {}, 'coverage': {}}

    searcher._evaluate_isolated_frontier_child = MethodType(evaluate, searcher)
    result = searcher._search_frontier_parallel(
        {'success': True, 'combat': {}}, [], 10, 0, 0, actions, candidates
    )

    assert result is not None
    assert searcher.timing['frontier_jobs_started'] == 2
    assert searcher.timing['frontier_jobs_cancelled'] == 3
    assert searcher.timing['known_unexpanded_action_edges'] == 3
    searcher.close()
