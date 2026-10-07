from types import MethodType, SimpleNamespace

import pytest

from controller import combat_step
from controller.combat_step import (
    CombatStepConfig,
    CombatStepResult,
    PlanState,
    _resolve_search_result,
    _turn_space_coverage_ratio,
    decide_combat_action,
)
from controller.search.actions import SearchAction
from controller.search.combat_search import CombatSearcher, CombatSpec, RecordedAction, SearchResult
from controller.search.state_cache import (
    canonicalize_search_state_for_plan_reuse,
    hash_search_state_for_plan_reuse,
)


def _combat_state(*, hp=40, energy=2):
    return {
        'success': True,
        'combat': {
            'is_player_turn': True,
            'player': {'hp': hp, 'max_hp': 40, 'block': 0, 'energy': energy},
            'hand': [],
            'enemies': [{'index': 0, 'enemy_id': 'TEST', 'hp': 10, 'block': 0}],
            'available_actions': [{'action_type': 'end_turn'}],
        },
    }


def test_combat_step_config_has_no_frontend_semantic_switches():
    config = CombatStepConfig(None, CombatSpec('Ironclad', 'TEST', 'seed'))

    forbidden = {
        'client', 'visible', 'headless', 'plan_reuse',
        'expand_potions_always', 'sanitize_root', 'raw_retry',
        'fallback_on_empty', 'resolve_live',
    }
    assert forbidden.isdisjoint(vars(config))

    with pytest.raises(TypeError):
        CombatStepConfig(
            None,
            CombatSpec('Ironclad', 'TEST', 'seed'),
            plan_reuse=False,
        )


def test_action_cap_returns_explicit_end_turn_and_checkpoints():
    searcher = object.__new__(CombatSearcher)
    audit_events = []
    searcher._audit_increment = MethodType(
        lambda self, key, *args, **kwargs: audit_events.append(key), searcher
    )
    searcher._replace_enemy_intents_from_parent = MethodType(
        lambda self, parent, child: child, searcher
    )
    searcher._record_branch_timing = MethodType(
        lambda self, action, started, result: None, searcher
    )
    searcher._evaluate_leaf_cached = MethodType(lambda self, state: 7.0, searcher)
    settled = {
        'success': True,
        'combat': {
            'is_player_turn': True,
            'player': {'hp': 35, 'max_hp': 40, 'block': 0, 'energy': 3},
            'hand': [],
            'enemies': [{'index': 0, 'enemy_id': 'TEST', 'hp': 10, 'block': 0}],
            'available_actions': [{'action_type': 'end_turn'}],
        },
    }
    searcher._settle_leaf_state = MethodType(
        lambda self, state, history: (settled, True), searcher
    )
    child = _combat_state()
    action = SearchAction('play_card', card_index=0)

    result = searcher._evaluate_child_action(
        action,
        parent_state=_combat_state(energy=3),
        history=[],
        action_budget=1,
        chance_depth=1,
        pre_chance_budget=1,
        next_history=[RecordedAction('play_card', {'card_index': 0})],
        child_state=child,
    )

    assert [item.action_type for item in result.sequence] == ['play_card', 'end_turn']
    assert result.state_hashes_after_actions == [
        hash_search_state_for_plan_reuse(child),
        hash_search_state_for_plan_reuse(settled),
    ]
    assert audit_events == [
        'expanded_action_edges',
        'depth_cutoff_leaves',
        'completed_turn_lines',
    ]


def test_search_result_keeps_expected_states_for_remaining_plan():
    first = SearchAction('play_card', card_index=0)
    second = SearchAction('play_card', card_index=1)
    end_turn = SearchAction('end_turn')
    result = SearchResult(
        score=5.0,
        sequence=[first, second, end_turn],
        leaf_state=_combat_state(),
        stats={},
        state_hashes_after_actions=['after-first', 'after-second', 'after-end'],
    )
    plan = PlanState()
    res = CombatStepResult(action='', payload={}, chosen=None, chosen_summary={})
    ra = SimpleNamespace(
        resolve_planned_action=lambda state, action: ('play_card', {'card_index': 0}),
        choose_combat_fallback=lambda state: ('end_turn', {}),
    )

    chosen, resolved, _ = _resolve_search_result(
        ra, _combat_state(), result, plan, res, {'resolve_ms': 0.0}
    )

    assert chosen is first
    assert resolved == ('play_card', {'card_index': 0})
    assert plan.sequence == [second, end_turn]
    assert plan.expected_state_hashes == ['after-first', 'after-second']


def test_incomplete_search_does_not_cache_its_continuation():
    first = SearchAction('play_card', card_index=0)
    end_turn = SearchAction('end_turn')
    result = SearchResult(
        score=5.0,
        sequence=[first, end_turn],
        leaf_state=_combat_state(),
        stats={},
        state_hashes_after_actions=['after-first', 'after-end'],
    )
    plan = PlanState()
    res = CombatStepResult(action='', payload={}, chosen=None, chosen_summary={})
    res.decision_audit = {
        'turn_space': {
            'topology_exhaustive': False,
            'unique_known_unexpanded_action_edges': 3,
        }
    }
    ra = SimpleNamespace(
        resolve_planned_action=lambda state, action: ('play_card', {'card_index': 0}),
        choose_combat_fallback=lambda state: ('end_turn', {}),
    )

    chosen, resolved, resolved_result = _resolve_search_result(
        ra, _combat_state(), result, plan, res, {'resolve_ms': 0.0}
    )

    assert chosen is first
    assert resolved == ('play_card', {'card_index': 0})
    assert resolved_result.decision_audit['plan_reuse_eligible'] is False
    assert plan.sequence == []
    assert plan.expected_state_hashes == []


def test_cached_plan_reuses_only_matching_intermediate_state(monkeypatch):
    state = _combat_state()
    expected_hash = hash_search_state_for_plan_reuse(state)
    plan = PlanState(
        sequence=[SearchAction('end_turn')],
        expected_state_hashes=[expected_hash],
    )
    ra = SimpleNamespace(
        resolve_planned_action=lambda current, action: ('end_turn', {}),
        summarize_combat_action=lambda current, action, payload: {'action_type': action},
    )
    monkeypatch.setattr(combat_step, '_helpers', lambda: ra)
    cfg = SimpleNamespace(room_type=None)

    result = decide_combat_action(None, state, cfg, plan)

    assert result.reused_plan is True
    assert result.action == 'end_turn'
    assert plan.sequence == []
    assert plan.expected_state_hashes == []


def test_cached_plan_from_incomplete_search_triggers_fresh_search(monkeypatch):
    state = _combat_state()
    state['combat']['available_actions'].append({
        'action_type': 'play_card', 'card_index': 0,
        'metadata': {'card_id': 'STRIKE_IRONCLAD'},
    })
    plan = PlanState(
        sequence=[SearchAction('end_turn')],
        expected_state_hashes=[hash_search_state_for_plan_reuse(state)],
        origin_audit={
            'turn_space': {
                'topology_exhaustive': False,
                'unique_known_unexpanded_action_edges': 5,
            }
        },
    )
    ra = SimpleNamespace(
        resolve_planned_action=lambda current, action: ('end_turn', {}),
        summarize_combat_action=lambda current, action, payload: {'action_type': action},
    )
    monkeypatch.setattr(combat_step, '_helpers', lambda: ra)
    fresh_calls = []

    def fresh(cli, search_state, cfg, current_plan, res, timing, expand_potions):
        fresh_calls.append(True)
        return SearchAction('end_turn'), ('end_turn', {}), res

    monkeypatch.setattr(combat_step, '_fresh_search', fresh)
    cfg = SimpleNamespace(room_type=None)

    result = decide_combat_action(None, state, cfg, plan)

    assert fresh_calls == [True]
    assert result.reused_plan is False
    assert result.plan_diverged == {
        'reason': 'origin_search_incomplete',
        'discarded_plan_len': 1,
        'unique_unexpanded_action_edges': 5,
    }


def test_cached_plan_divergence_triggers_fresh_search(monkeypatch):
    state = _combat_state()
    state['combat']['available_actions'].append({
        'action_type': 'play_card', 'card_index': 0,
        'metadata': {'card_id': 'STRIKE_IRONCLAD'},
    })
    plan = PlanState(
        sequence=[SearchAction('end_turn')],
        expected_state_hashes=['stale'],
    )
    ra = SimpleNamespace(
        resolve_planned_action=lambda current, action: ('end_turn', {}),
        summarize_combat_action=lambda current, action, payload: {'action_type': action},
    )
    monkeypatch.setattr(combat_step, '_helpers', lambda: ra)
    fresh_calls = []

    def fresh(cli, search_state, cfg, current_plan, res, timing, expand_potions):
        fresh_calls.append(True)
        return SearchAction('end_turn'), ('end_turn', {}), res

    monkeypatch.setattr(combat_step, '_fresh_search', fresh)
    cfg = SimpleNamespace(room_type=None)

    result = decide_combat_action(None, state, cfg, plan)

    assert fresh_calls == [True]
    assert result.plan_diverged['reason'] == 'state_mismatch'
    assert result.plan_diverged['discarded_plan_len'] == 1
    assert plan.sequence == []
    assert plan.expected_state_hashes == []


def test_cached_plan_ignores_unmodeled_live_potion(monkeypatch):
    searched = _combat_state()
    live = _combat_state()
    live['combat']['player']['potions'] = [{'id': 'STABLE_SERUM', 'slot_index': 2}]
    live['combat']['available_actions'].append({
        'action_type': 'use_potion',
        'metadata': {'potion_index': 2, 'potion_id': 'STABLE_SERUM'},
    })
    plan = PlanState(
        sequence=[SearchAction('end_turn')],
        expected_state_hashes=[hash_search_state_for_plan_reuse(searched)],
        expected_state_keys=[canonicalize_search_state_for_plan_reuse(searched)],
    )
    ra = SimpleNamespace(
        resolve_planned_action=lambda current, action: ('end_turn', {}),
        summarize_combat_action=lambda current, action, payload: {'action_type': action},
    )
    monkeypatch.setattr(combat_step, '_helpers', lambda: ra)
    monkeypatch.setattr(
        combat_step, '_fresh_search',
        lambda *args, **kwargs: pytest.fail('unmodeled potion must not invalidate the plan'),
    )
    cfg = SimpleNamespace(room_type='Boss')

    result = decide_combat_action(None, live, cfg, plan)

    assert result.reused_plan is True
    assert result.plan_diverged is None


def test_cached_plan_reports_semantic_state_difference(monkeypatch):
    expected = _combat_state(energy=2)
    live = _combat_state(energy=1)
    plan = PlanState(
        sequence=[SearchAction('end_turn')],
        expected_state_hashes=[hash_search_state_for_plan_reuse(expected)],
        expected_state_keys=[canonicalize_search_state_for_plan_reuse(expected)],
    )
    ra = SimpleNamespace(
        resolve_planned_action=lambda current, action: ('end_turn', {}),
        summarize_combat_action=lambda current, action, payload: {'action_type': action},
    )
    monkeypatch.setattr(combat_step, '_helpers', lambda: ra)
    monkeypatch.setattr(
        combat_step, '_fresh_search',
        lambda cli, state, cfg, current_plan, res, timing, expand: (
            SearchAction('end_turn'), ('end_turn', {}), res
        ),
    )
    cfg = SimpleNamespace(room_type=None)

    result = decide_combat_action(None, live, cfg, plan)

    assert result.plan_diverged['state_differences'] == [{
        'path': 'combat.player.energy', 'expected': 2, 'actual': 1,
    }]


def test_forced_end_turn_skips_search(monkeypatch):
    state = _combat_state(energy=0)
    state['combat']['available_actions'].append({
        'action_type': 'discard_potion', 'metadata': {'potion_index': 0}
    })
    monkeypatch.setattr(combat_step, '_helpers', lambda: SimpleNamespace())
    monkeypatch.setattr(
        combat_step, '_fresh_search',
        lambda *args, **kwargs: pytest.fail('forced end_turn must not search'),
    )
    cfg = SimpleNamespace(room_type=None)

    result = decide_combat_action(None, state, cfg, PlanState())

    assert result.action == 'end_turn'
    assert result.ran_search is False
    assert result.decision_reason == 'forced_action'


def test_unexpanded_potion_does_not_block_forced_end_turn(monkeypatch):
    state = _combat_state(hp=40, energy=0)
    state['combat']['player']['potions'] = [{'id': 'STABLE_SERUM', 'slot_index': 2}]
    state['combat']['available_actions'].append({
        'action_type': 'use_potion',
        'metadata': {'potion_index': 2, 'potion_id': 'STABLE_SERUM'},
    })
    monkeypatch.setattr(combat_step, '_helpers', lambda: SimpleNamespace())
    monkeypatch.setattr(
        combat_step, '_fresh_search',
        lambda *args, **kwargs: pytest.fail('filtered potion must not force a search'),
    )
    cfg = SimpleNamespace(room_type='Boss')

    result = decide_combat_action(None, state, cfg, PlanState())

    assert result.action == 'end_turn'
    assert result.ran_search is False


def test_emergency_potion_keeps_search_enabled(monkeypatch):
    state = _combat_state(hp=10, energy=0)
    state['combat']['player']['potions'] = [{'id': 'BLOCK_POTION', 'slot_index': 0}]
    state['combat']['available_actions'].append({
        'action_type': 'use_potion',
        'metadata': {'potion_index': 0, 'potion_id': 'BLOCK_POTION'},
    })
    monkeypatch.setattr(combat_step, '_helpers', lambda: SimpleNamespace())
    fresh_calls = []

    def fresh(cli, search_state, cfg, current_plan, res, timing, expand_potions):
        fresh_calls.append(True)
        return SearchAction('end_turn'), ('end_turn', {}), res

    monkeypatch.setattr(combat_step, '_fresh_search', fresh)
    cfg = SimpleNamespace(room_type=None)

    decide_combat_action(None, state, cfg, PlanState())

    assert fresh_calls == [True]


def test_plan_hash_ignores_hand_slot_order_but_keeps_draw_order():
    left = _combat_state()
    left['combat']['hand'] = [
        {'index': 0, 'card_id': 'STRIKE_IRONCLAD', 'current_cost': 1},
        {'index': 1, 'card_id': 'DEFEND_IRONCLAD', 'current_cost': 1},
    ]
    right = _combat_state()
    right['combat']['hand'] = [
        {'index': 0, 'card_id': 'DEFEND_IRONCLAD', 'current_cost': 1},
        {'index': 1, 'card_id': 'STRIKE_IRONCLAD', 'current_cost': 1},
    ]
    assert hash_search_state_for_plan_reuse(left) == hash_search_state_for_plan_reuse(right)

    right['combat']['draw_pile'] = [{'card_id': 'A'}, {'card_id': 'B'}]
    left['combat']['draw_pile'] = [{'card_id': 'B'}, {'card_id': 'A'}]
    assert hash_search_state_for_plan_reuse(left) != hash_search_state_for_plan_reuse(right)


def test_plan_hash_matches_reordered_duplicate_cards_with_distinct_state():
    left = _combat_state()
    left['combat']['hand'] = [
        {'index': 0, 'card_id': 'STRIKE_IRONCLAD', 'current_cost': 1,
         'dynamic_values': [{'name': 'Damage', 'current_value': 6}]},
        {'index': 1, 'card_id': 'STRIKE_IRONCLAD', 'current_cost': 1,
         'dynamic_values': [{'name': 'Damage', 'current_value': 9}]},
    ]
    right = _combat_state()
    right['combat']['hand'] = [
        {'index': 0, 'card_id': 'STRIKE_IRONCLAD', 'current_cost': 1,
         'dynamic_values': [{'name': 'Damage', 'current_value': 9}]},
        {'index': 1, 'card_id': 'STRIKE_IRONCLAD', 'current_cost': 1,
         'dynamic_values': [{'name': 'Damage', 'current_value': 6}]},
    ]

    assert hash_search_state_for_plan_reuse(left) == hash_search_state_for_plan_reuse(right)


def test_plan_hash_ignores_potions_removed_from_search_snapshot():
    live = _combat_state()
    live['combat']['player']['potions'] = [
        {'id': 'STABLE_SERUM', 'slot_index': 2},
        {'id': 'COLORLESS_POTION', 'slot_index': 3},
    ]
    searched = _combat_state()

    assert canonicalize_search_state_for_plan_reuse(live) == canonicalize_search_state_for_plan_reuse(searched)
    assert hash_search_state_for_plan_reuse(live) == hash_search_state_for_plan_reuse(searched)


def test_plan_hash_keeps_supported_potion_state():
    left = _combat_state()
    left['combat']['player']['potions'] = [{'id': 'BLOCK_POTION', 'slot_index': 0}]
    right = _combat_state()

    assert hash_search_state_for_plan_reuse(left) != hash_search_state_for_plan_reuse(right)


def test_turn_space_coverage_cannot_exceed_one_when_edges_are_recounted():
    assert _turn_space_coverage_ratio(18, 2) == 0.9
    assert _turn_space_coverage_ratio(0, 0) == 0.0
