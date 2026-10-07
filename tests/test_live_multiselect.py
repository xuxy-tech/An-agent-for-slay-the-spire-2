from pathlib import Path

import pytest

from controller.live_noncombat import choose_client_selection
from controller.live_client_bridge import LiveClientBridge
from tests.test_live_observer import FakeMod
from cli.sts2_mod_adapter import Sts2ModAdapter, ModApiError
from controller.live_client_bridge import _transaction_budget_ms
from controller.interaction_flow import transition_completed


def selection_state():
    return {'run_id': 'test', 'screen': 'CARD_SELECTION', 'available_actions': ['select_deck_card'],
            'selection': {'kind': 'deck_transform_select', 'min_select': 0, 'max_select': 0,
                          'selected_count': 0, 'prompt': '选择[blue]2[/blue]张牌来[gold]变化[/gold]。',
                          'cards': [{'index': i, 'card_id': 'STRIKE_IRONCLAD', 'rarity': 'Basic',
                                     'card_type': 'Attack'} for i in range(3)]}}


def test_selection_bridge_waits_for_real_combat_boundary_after_forced_discard():
    mod = FakeMod()
    before = selection_state()
    before.update(in_combat=True, turn=1)
    transient = {'screen': 'COMBAT', 'in_combat': True, 'turn': 1,
                 'available_actions': ['discard_potion'], 'selection': None}
    mod.value = {'screen': 'COMBAT', 'in_combat': True, 'turn': 2,
                 'available_actions': ['play_card', 'end_turn'], 'selection': None}
    settled = LiveClientBridge(mod)._wait_for_decision_boundary(
        'select_deck_card', before, transient)
    assert settled['turn'] == 2
    assert 'end_turn' in settled['available_actions']


def test_explicit_prompt_preserves_two_card_transaction():
    decision = choose_client_selection(selection_state(), Path.cwd())
    assert decision.action == 'select_deck_cards'
    assert decision.params == {'indices': [0, 1]}
    assert decision.telemetry['count_source'] == 'explicit_prompt'


def test_typed_event_prompt_preserves_two_card_transaction():
    state = selection_state()
    state['selection']['kind'] = 'deck_card_select'
    state['selection']['prompt'] = '选择[blue]2[/blue]张普通牌加入到你的牌组。'
    decision = choose_client_selection(state, Path.cwd(), pending_operation='pick')
    assert decision.action == 'select_deck_cards'
    assert decision.params == {'indices': [0, 1]}


def test_pending_first_click_is_not_retried_or_counted_as_finished():
    mod = FakeMod()
    mod.value = selection_state()
    calls = []
    def action(name, *, option_index, allow_pending_selection):
        calls.append(option_index)
        if option_index == 0:
            assert allow_pending_selection
            return {'status': 'pending', 'stable': False, 'state': mod.state()}
        assert not allow_pending_selection
        mod.value.update(screen='EVENT', selection=None,
                         available_actions=['choose_event_option'],
                         event={'title': 'Done', 'description': 'Selection resolved',
                                'options': [{'index': 0, 'title': 'Continue',
                                             'description': 'Continue'}]})
        return {'status': 'completed', 'stable': True, 'state': mod.state()}
    mod.action = action
    after = LiveClientBridge(mod).execute_client_action('select_deck_cards', {'indices': [0, 1]}, mod.state())
    assert after['screen'] == 'EVENT'
    assert calls == [0, 1]


def test_multi_selection_budget_scales_with_serialized_clicks():
    assert _transaction_budget_ms({}, 20.0, action='select_deck_cards',
                                 params={'indices': [0, 1, 2, 3, 4]}) == 60000.0
    assert _transaction_budget_ms({}, 20.0, action='choose_event_option') == 120000.0
    assert _transaction_budget_ms({}, 20.0, action='choose_map_node') == 60000.0
    assert _transaction_budget_ms({'decision_telemetry': {'timing_budget_ms': 250}}, 20.0,
                                  action='select_deck_cards',
                                  params={'indices': [0, 1, 2, 3, 4]}) == 250.0


def test_mod_pending_is_only_allowed_for_explicit_intermediate_selection(monkeypatch):
    adapter = Sts2ModAdapter()
    monkeypatch.setattr(adapter, '_request', lambda method, path, body: {
        'action': body['action'], 'status': 'pending', 'stable': False, 'state': selection_state()})
    with pytest.raises(ModApiError):
        adapter.action('select_deck_card', option_index=0)
    assert adapter.action('select_deck_card', option_index=0, allow_pending_selection=True)['status'] == 'pending'
    with pytest.raises(ModApiError):
        adapter.action('play_card', card_index=0, allow_pending_selection=True)


def test_play_card_pending_is_a_valid_combat_selection_boundary(monkeypatch):
    from controller.live_client_bridge import _is_decision_boundary
    adapter = Sts2ModAdapter()
    state = selection_state()
    state['in_combat'] = True
    state['selection']['kind'] = 'combat_hand_upgrade_select'
    monkeypatch.setattr(adapter, '_request', lambda method, path, body: {
        'action': 'play_card', 'status': 'pending', 'stable': False, 'state': state})
    assert adapter.action('play_card', card_index=0)['state'] == state
    assert _is_decision_boundary('play_card', {'in_combat': True}, state)
    state['selection']['cards'] = []
    with pytest.raises(ModApiError):
        adapter.action('play_card', card_index=0)


def test_map_transition_to_card_selection_is_a_decision_boundary():
    before = {
        'screen': 'MAP', 'in_combat': False, 'turn': None,
        'available_actions': ['choose_map_node'], 'run': {'floor': 1},
    }
    after = {
        'screen': 'CARD_SELECTION', 'in_combat': False, 'turn': 1,
        'available_actions': ['select_deck_card', 'confirm_selection'],
        'selection': {
            'kind': 'deck_transform_select', 'min_select': 1,
            'max_select': 1, 'selected_count': 0,
            'cards': [{'index': 0, 'card_id': 'BASH'}],
        },
        'run': {'floor': 2},
    }
    assert transition_completed('choose_map_node', before, after)
