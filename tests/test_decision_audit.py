import threading

from controller.search.actions import SearchAction
from controller.search.combat_search import CombatSearcher, SearchResult
from controller.search.evaluator import evaluate_leaf, explain_leaf_score


def _combat(player_hp, enemy_hp, block=0, energy=1):
    return {
        'player': {
            'hp': player_hp,
            'max_hp': 80,
            'block': block,
            'energy': energy,
            'powers': [],
        },
        'enemies': [{
            'index': 0,
            'monster_id': 'TEST_ENEMY',
            'hp': enemy_hp,
            'max_hp': 20,
            'block': 0,
            'powers': [],
            'intent': {'intent_types': ['Attack'], 'display_damage': 5, 'hits': 1},
        }],
        'hand': [],
        'draw_pile': [],
        'discard_pile': [],
        'exhaust_pile': [],
    }


def test_balanced_score_explanation_reconciles_to_evaluator():
    root = _combat(80, 20)
    leaf = {'combat': _combat(78, 14, block=3, energy=1)}

    explanation = explain_leaf_score(leaf, root, 'balanced')

    assert explanation['total'] == evaluate_leaf(leaf, root, 'balanced')
    assert sum(row['contribution'] for row in explanation['contributions']) == explanation['total']
    assert any(row['key'] == 'enemy_hp_loss' for row in explanation['contributions'])
    assert any(row['key'] == 'player_hp_loss' for row in explanation['contributions'])
    weights = {row['key']: row.get('weight') for row in explanation['contributions']}
    assert weights['player_hp_loss'] == -5.0
    assert weights['player_block'] == 0.75
    assert weights['unblocked_damage'] == -3.5


def test_killing_thieves_exposes_preserved_gold_and_card_value():
    root = _combat(80, 20)
    root['enemies'] = [
        {
            'monster_id': 'FAT_GREMLIN', 'hp': 15, 'max_hp': 15, 'block': 0,
            'powers': [{'id': 'HEIST_POWER', 'amount': 40}],
            'intent': {'intent_types': ['Stun']},
        },
        {
            'monster_id': 'THIEVING_HOPPER', 'hp': 79, 'max_hp': 79, 'block': 0,
            'powers': [{'id': 'ESCAPE_ARTIST_POWER', 'amount': 5}],
            'intent': {'intent_types': ['Attack', 'CardDebuff'], 'display_damage': 17, 'hits': 1},
        },
    ]
    leaf = {'combat': {**_combat(80, 1), 'enemies': []}}

    explanation = explain_leaf_score(leaf, root, 'balanced')

    assert explanation['features']['prevented_gold_theft'] == 40.0
    assert explanation['features']['prevented_card_theft'] == 1.0
    assert any(row['key'] == 'prevented_gold_theft' for row in explanation['contributions'])
    assert any(row['key'] == 'prevented_card_theft' for row in explanation['contributions'])
    assert sum(row['contribution'] for row in explanation['contributions']) == explanation['total']


def test_root_coverage_separates_dedup_filtering_and_evaluation():
    searcher = object.__new__(CombatSearcher)
    coverage = searcher._root_coverage(
        available=10,
        candidates=6,
        evaluated=4,
        pruned={
            'symmetry_pruned_actions': 2,
            'state_pruned_actions': 1,
            'unsupported_pruned_actions': 1,
        },
    )

    assert coverage['available_actions'] == 10
    assert coverage['candidate_actions'] == 6
    assert coverage['deduplicated_actions'] == 3
    assert coverage['filtered_actions'] == 1
    assert coverage['evaluated_actions'] == 4
    assert coverage['unevaluated_actions'] == 2
    assert coverage['candidate_coverage_ratio'] == 4 / 6


def test_turn_space_counters_are_exposed_in_timing_summary():
    searcher = object.__new__(CombatSearcher)
    searcher._audit_lock = threading.Lock()
    searcher.timing = {
        'available_action_edges': 18,
        'candidate_action_edges': 11,
        'expanded_action_edges': 7,
        'known_unexpanded_action_edges': 4,
        'completed_turn_lines': 3,
        'depth_cutoff_leaves': 2,
        'leaf_score_requests': 6,
        'unique_leaf_scores': 5,
    }

    summary = searcher.timing_summary()

    assert summary['available_action_edges'] == 18
    assert summary['candidate_action_edges'] == 11
    assert summary['expanded_action_edges'] == 7
    assert summary['known_unexpanded_action_edges'] == 4
    assert summary['completed_turn_lines'] == 3
    assert summary['depth_cutoff_leaves'] == 2
    assert summary['unique_leaf_scores'] == 5



def test_unique_coverage_and_leaf_sets_ignore_repeated_parallel_attempts():
    searcher = object.__new__(CombatSearcher)
    searcher._audit_lock = threading.Lock()
    searcher._coverage_sets = {
        'decision_states': {'s1', 's2'},
        'available_edges': {'a1', 'a2', 'a3'},
        'candidate_edges': {'a1', 'a2'},
        'expanded_edges': {'a1', 'a2'},
        'leaf_states': {'leaf1'},
    }
    searcher.timing = {
        'decision_states_prepared': 4,
        'available_action_edges': 6,
        'candidate_action_edges': 5,
        'expanded_action_edges': 4,
        'known_unexpanded_action_edges': 1,
        'leaf_score_requests': 4,
        'unique_leaf_scores': 3,
    }

    summary = searcher.timing_summary()

    assert summary['coverage_schema_version'] == 3
    assert summary['unique_candidate_action_edges'] == 2
    assert summary['unique_expanded_action_edges'] == 2
    assert summary['unique_known_unexpanded_action_edges'] == 0
    assert summary['repeated_candidate_action_edges'] == 3
    assert summary['unique_scored_leaf_states'] == 1
    assert summary['repeated_leaf_score_requests'] == 3
    assert summary['repeated_leaf_evaluations'] == 2

def test_verified_victory_marks_remaining_siblings_as_lethal_early_stop():
    searcher = object.__new__(CombatSearcher)
    searcher._audit_lock = threading.Lock()
    searcher.timing = {}
    result = SearchResult(
        score=1_000_120.0,
        sequence=[],
        leaf_state={'terminal_decision': 'card_reward'},
        stats={},
    )

    stopped = searcher._mark_lethal_early_stop(result, remaining_siblings=4)

    assert stopped is True
    assert result.stats['lethal_early_stop'] is True
    assert result.stats['lethal_skipped_siblings'] == 4
    assert searcher.timing['lethal_skipped_action_edges'] == 4
    assert searcher.timing.get('known_unexpanded_action_edges', 0) == 0
    assert searcher.timing['lethal_early_stops'] == 1


def test_lethal_last_sibling_is_not_reported_as_early_stop():
    searcher = object.__new__(CombatSearcher)
    searcher._audit_lock = threading.Lock()
    searcher.timing = {}
    result = SearchResult(
        score=1_000_120.0,
        sequence=[],
        leaf_state={'terminal_decision': 'card_reward'},
        stats={},
    )

    assert searcher._mark_lethal_early_stop(result, remaining_siblings=0) is False
    assert result.stats == {'lethal_class': 'clean'}


def test_potion_lethal_is_selected_but_does_not_stop_search_early():
    searcher = object.__new__(CombatSearcher)
    searcher._audit_lock = threading.Lock()
    searcher._root_summary = {'player': {'hp': 40}, 'enemies': []}
    searcher.timing = {}
    result = SearchResult(
        score=1_000_100.0,
        sequence=[SearchAction('use_potion', metadata={'potion_index': 0})],
        leaf_state={
            'terminal_decision': 'card_reward',
            'terminal_result': {'player': {'hp': 40}},
        },
        stats={},
    )

    assert searcher._lethal_classification(result) == 'costly'
    assert searcher._mark_lethal_early_stop(result, remaining_siblings=3) is False
    assert result.stats['lethal_class'] == 'costly'
    assert searcher.timing.get('lethal_early_stops', 0) == 0


def _root_pair(card_index, score, player_hp, enemy_hp):
    action = SearchAction('play_card', card_index=card_index, target_index=0)
    info = {
        'action': action,
        'adjusted_score': float(score),
        'effect_score': 0.0,
    }
    child = SearchResult(
        score=float(score),
        sequence=[action],
        leaf_state={'combat': _combat(player_hp, enemy_hp)},
        stats={},
    )
    return info, child


def test_strictly_dominated_root_is_removed_before_score_comparison():
    searcher = object.__new__(CombatSearcher)
    searcher._root_summary = {'player': {'hp': 50}, 'enemies': []}
    searcher.timing = {}
    better = _root_pair(0, score=10, player_hp=50, enemy_hp=5)
    worse_but_high_score = _root_pair(1, score=100, player_hp=50, enemy_hp=10)

    selected, audit = searcher._select_root_candidate([better, worse_but_high_score])

    assert selected is better
    assert audit['selection_rule'] == 'dominance'
    assert audit['dominated_candidates'] == 1
    assert worse_but_high_score[0]['dominance_reasons'] == ['enemy_effective_hp']


def test_hp_damage_tradeoff_stays_on_pareto_frontier_and_uses_score():
    searcher = object.__new__(CombatSearcher)
    searcher._root_summary = {'player': {'hp': 50}, 'enemies': []}
    searcher.timing = {}
    safer = _root_pair(0, score=20, player_hp=50, enemy_hp=10)
    more_damage = _root_pair(1, score=30, player_hp=45, enemy_hp=5)

    selected, audit = searcher._select_root_candidate([safer, more_damage])

    assert selected is more_damage
    assert audit['selection_rule'] == 'score'
    assert audit['pareto_frontier_size'] == 2


def test_root_time_budget_is_divided_evenly_between_candidates():
    searcher = object.__new__(CombatSearcher)
    searcher.max_search_ms = 2400.0

    assert searcher._fair_root_budget_ms(8) == 300.0
    assert searcher._fair_root_budget_ms(0) == 0.0
