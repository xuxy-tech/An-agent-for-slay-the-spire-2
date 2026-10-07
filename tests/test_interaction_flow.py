import copy
from pathlib import Path

import pytest

from controller.interaction_flow import InteractionFlow, FlowBlocked, ProgressGuard, decision, transition_completed


ROOT = Path(__file__).resolve().parents[1]
PROFILE = {
    'schema_version': 2,
    'id': 'test',
    'ports': [{'id': 'energy', 'name': 'Energy', 'target': 1, 'maximum': 2}],
    'cards': [{'id': 'BLOODLETTING', 'max_copies': 2, 'ports': ['energy']}],
}


def shop(opened=False):
    return {'screen': 'SHOP', 'run_id': 'run', 'run': {'floor': 3}, 'shop': {'is_open': opened},
            'available_actions': ['close_shop_inventory'] if opened else ['open_shop_inventory', 'proceed']}


def test_map_click_waits_for_complete_combat_population():
    before = {
        'screen': 'MAP', 'in_combat': False,
        'available_actions': ['choose_map_node'],
    }
    loading = {
        'screen': 'COMBAT', 'in_combat': True, 'turn': 1,
        'available_actions': ['end_turn'],
        'combat': {'enemies': []},
    }
    assert not transition_completed('choose_map_node', before, loading)

    ready = copy.deepcopy(loading)
    ready['available_actions'].append('play_card')
    ready['combat']['enemies'] = [{
        'enemy_id': 'TWIG_SLIME_S',
        'current_hp': 8,
        'max_hp': 8,
        'intents': [{'intent_type': 'Attack', 'damage': 4, 'hits': 1}],
    }]
    assert transition_completed('choose_map_node', before, ready)

    # Gambling Chip opens an actionable hand selection before the ordinary
    # COMBAT screen appears. It is already a settled player decision.
    opening = copy.deepcopy(ready)
    opening.update(screen='CARD_SELECTION',
                   available_actions=['select_deck_card', 'confirm_selection'],
                   selection={'kind': 'combat_hand_select', 'min_select': 0,
                              'max_select': 5, 'cards': [{'index': 0}]})
    assert transition_completed('choose_map_node', before, opening)
    opening['selection']['kind'] = 'deck_transform_select'
    assert not transition_completed('choose_map_node', before, opening)


def test_map_click_waits_past_previous_finished_event_panel():
    before = {
        'screen': 'MAP', 'run': {'floor': 22},
        'available_actions': ['choose_map_node'],
    }
    stale = {
        'screen': 'EVENT', 'run': {'floor': 23},
        'event': {'event_id': 'RANWID_THE_ELDER', 'is_finished': True,
                  'options': [{'index': 0, 'is_proceed': True}]},
        'available_actions': ['choose_event_option'],
    }
    assert not transition_completed('choose_map_node', before, stale)

    ready = copy.deepcopy(stale)
    ready['event'] = {'event_id': 'NEW_EVENT', 'is_finished': False,
                      'options': [{'index': 0, 'is_locked': False}]}
    assert transition_completed('choose_map_node', before, ready)

    returned_to_map = copy.deepcopy(stale)
    returned_to_map.update(screen='MAP', event=None,
                           available_actions=['choose_map_node'])
    assert transition_completed('choose_map_node', before, returned_to_map)


def test_skip_shop_closes_panel_then_leaves_without_reopening():
    flow = InteractionFlow(ROOT)
    state, shadow = shop(), {'decision': 'shop'}
    _, command = flow.choose(state, shadow)
    assert command.client.action == 'open_shop_inventory' and command.shadow_action is None
    flow.complete(command, state, shop(True))
    _, command = flow.choose(shop(True), shadow)
    assert command.client.action == 'close_shop_inventory' and command.shadow_action is None
    flow.complete(command, shop(True), shop())
    _, command = flow.choose(shop(), shadow)
    assert command.client.action == 'proceed'
    assert command.shadow_action == 'leave_room'
    assert transition_completed('close_shop_inventory', shop(True), shop())
    assert not transition_completed('proceed', shop(), shop())


def test_shop_buys_whitelist_card_with_identity_mapping():
    state = shop(True)
    state['available_actions'] = ['close_shop_inventory', 'buy_card']
    state['run'].update(gold=100, deck=[])
    state['shop']['cards'] = [
        {'index': 4, 'card_id': 'HAVOC', 'price': 20, 'is_stocked': True},
        {'index': 7, 'card_id': 'BLOODLETTING', 'price': 70, 'is_stocked': True},
    ]
    shadow = {
        'decision': 'shop',
        'player': {'gold': 100, 'deck': []},
        'cards': [
            {'index': 0, 'card_id': 'HAVOC', 'cost': 20, 'is_stocked': True},
            {'index': 1, 'card_id': 'BLOODLETTING', 'cost': 70, 'is_stocked': True},
        ],
        'card_removal_cost': 120,
    }
    _, command = InteractionFlow(ROOT, deck_profile=PROFILE).choose(state, shadow)
    assert command.client.action == 'buy_card'
    assert command.client.params == {'option_index': 7}
    assert command.shadow_action == 'buy_card'
    assert command.shadow_args == {'card_index': 1}
    assert command.telemetry['requires_reanchor'] is False


def test_shop_uses_visible_inventory_and_requests_reanchor_when_rng_diverges():
    state = shop(True)
    state['available_actions'] = ['close_shop_inventory', 'buy_card', 'remove_card_at_shop']
    state['run'].update(gold=100, deck=[])
    state['shop'].update(
        cards=[
            {'index': 2, 'card_id': 'HAVOC', 'price': 20, 'is_stocked': True},
            {'index': 5, 'card_id': 'BLOODLETTING', 'price': 70, 'is_stocked': True},
        ],
        card_removal={'price': 75, 'available': True, 'used': False, 'enough_gold': True},
    )
    shadow = {
        'decision': 'shop',
        'player': {'gold': 100, 'deck': []},
        'cards': [{'index': 0, 'card_id': 'SHRUG_IT_OFF', 'cost': 50, 'is_stocked': True}],
        'card_removal_cost': 75,
    }
    flow = InteractionFlow(ROOT, deck_profile=PROFILE)
    with pytest.raises(FlowBlocked, match='Shop entry parity failed'):
        flow.choose(state, shadow)


def test_shop_leaves_client_only_after_inventory_divergence():
    flow = InteractionFlow(ROOT, deck_profile=PROFILE)
    flow.context.shop_stage = 'leaving'
    flow.context.shop_reanchor_required = True
    state = shop(False)
    state['available_actions'] = ['proceed']

    _, command = flow.choose(state, {'decision': 'shop'})

    assert command.client.action == 'proceed'
    assert command.shadow is None
    assert command.telemetry['requires_reanchor'] is True
    assert command.telemetry['client_authoritative'] is True


def test_shop_removal_uses_native_visible_action_name():
    state = shop(True)
    state['available_actions'] = ['close_shop_inventory', 'remove_card_at_shop']
    state['run'].update(gold=100, deck=[{'card_id': 'STRIKE_IRONCLAD', 'upgraded': False}])
    state['shop'].update(cards=[], card_removal={
        'price': 75, 'available': True, 'used': False, 'enough_gold': True,
    })
    shadow = {
        'decision': 'shop',
        'player': {'gold': 100, 'deck': [{'id': 'STRIKE_IRONCLAD', 'upgraded': False}]},
        'cards': [],
        'card_removal_cost': 75,
    }
    _, command = InteractionFlow(ROOT, deck_profile=PROFILE).choose(state, shadow)
    assert command.client.action == 'remove_card_at_shop'
    assert command.shadow_action == 'remove_card'
    assert command.operation == 'remove'


def test_bundle_uses_profile_priority_and_matches_card_ids():
    state = {
        'screen': 'BUNDLE_SELECTION', 'available_actions': ['choose_bundle'],
        'bundles': [
            {'index': 5, 'cards': [{'card_id': 'HAVOC'}]},
            {'index': 9, 'cards': [{'card_id': 'BLOODLETTING'}, {'card_id': 'HAVOC'}]},
        ],
    }
    shadow = {
        'decision': 'bundle_select', 'player': {'deck': []},
        'bundles': [
            {'index': 0, 'cards': [{'card_id': 'HAVOC'}]},
            {'index': 1, 'cards': [{'card_id': 'BLOODLETTING'}, {'card_id': 'HAVOC'}]},
        ],
    }
    _, command = InteractionFlow(ROOT, deck_profile=PROFILE).choose(state, shadow)
    assert command.client.params == {'option_index': 9}
    assert command.shadow_args == {'bundle_index': 1}


def overview():
    return {'screen': 'REWARD', 'run_id': 'run', 'run': {'floor': 2, 'potions': [{'occupied': False}]},
            'available_actions': ['claim_reward', 'proceed'], 'reward': {'pending_card_choice': False,
             'rewards': [{'index': 0, 'reward_type': 'Gold', 'claimable': True},
                         {'index': 1, 'reward_type': 'Potion', 'claimable': True},
                         {'index': 2, 'reward_type': 'Card', 'claimable': True}]}}


def test_reward_items_are_visible_separate_transactions():
    flow = InteractionFlow(ROOT)
    state = overview()
    shadow = {'decision': 'combat_reward', 'rewards': [
        {'index': index, 'reward_type': kind}
        for index, kind in enumerate(('Gold', 'Potion', 'Card'))]}
    for expected in ('Gold', 'Potion', 'Card'):
        _, command = flow.choose(state, shadow)
        assert command.client.action == 'claim_reward'
        assert command.client.telemetry['reward_type'] == expected
        assert command.shadow_action == 'claim_combat_reward'
        after = copy.deepcopy(state)
        after['reward']['rewards'].pop(0)
        assert transition_completed('claim_reward', state, after)
        flow.complete(command, state, after)
        state = after
        shadow['rewards'].pop(0)


def test_native_reward_ids_map_per_reanchor_and_reward_occurrence():
    flow = InteractionFlow(ROOT)
    state = overview()
    state['reward']['reward_set_id'] = 1
    offered = [
            {'native_index': 0, 'reward_type': 'Gold', 'model_id': None, 'amount': 13,
             'cards': None, 'successfully_selected': False},
            {'native_index': 1, 'reward_type': 'Card', 'model_id': None, 'amount': None,
             'successfully_selected': False,
             'cards': [{'id': card_id, 'upgraded': False}
                       for card_id in ('BASH', 'SHRUG_IT_OFF', 'CLAW')]},
    ]
    state['reward']['offered_rewards'] = copy.deepcopy(offered)
    state['reward']['rewards'] = [{'index': 0, 'native_index': 0, 'reward_type': 'Gold',
                                   'claimable': True},
                                  {'index': 1, 'native_index': 1, 'reward_type': 'Card',
                                   'claimable': True}]
    shadow = {'decision': 'combat_reward', 'reward_set_id': 0,
              'offered_rewards': copy.deepcopy(offered),
              'rewards': [{'index': 0, 'reward_type': 'Gold'},
                          {'index': 1, 'reward_type': 'Card'}]}
    _, first = flow.choose(state, shadow)
    assert first.client.params == {'option_index': 0}
    assert first.shadow_args == {'reward_index': 0, 'reward_set_id': 0}
    assert first.telemetry['reward_set_mapping']['client_set_id'] == 1
    assert first.telemetry['reward_set_mapping']['shadow_set_id'] == 0
    after = copy.deepcopy(state)
    after['reward']['rewards'].pop(0)
    flow.complete(first, state, after)
    shadow['rewards'].pop(0)
    _, second = flow.choose(after, shadow)
    assert second.shadow_args == {'reward_index': 1, 'reward_set_id': 0}
    assert second.telemetry['reward_item_key'] != first.telemetry['reward_item_key']
    with pytest.raises(FlowBlocked, match='ordered native reward offers differ'):
        changed = copy.deepcopy(shadow)
        changed['offered_rewards'][1]['cards'][0]['id'] = 'STRIKE_IRONCLAD'
        flow.choose(after, changed)
    flow.invalidate_reward_mapping()
    state['reward']['reward_set_id'] = 2
    shadow['reward_set_id'] = 0
    shadow['rewards'].insert(0, {'index': 0, 'reward_type': 'Gold'})
    _, third = flow.choose(state, shadow)
    assert third.shadow_args['reward_set_id'] == 0
    assert third.telemetry['reward_set_mapping']['occurrence'] == 2
    assert third.telemetry['reward_item_key'] != first.telemetry['reward_item_key']
    flow.invalidate_reward_mapping()
    state['reward']['reward_set_id'] = 3
    _, fourth = flow.choose(state, shadow)
    assert fourth.telemetry['reward_set_mapping']['reanchor_generation'] == 2
    assert fourth.telemetry['reward_set_mapping']['occurrence'] == 3
    assert fourth.telemetry['reward_item_key'] not in {
        first.telemetry['reward_item_key'], third.telemetry['reward_item_key']}


def test_visible_reward_claim_is_authoritative_when_shadow_step_is_missing():
    state = overview()
    state['reward']['rewards'] = [
        {'index': 4, 'reward_type': 'Gold', 'claimable': True},
    ]

    _, command = InteractionFlow(ROOT).choose(state, {'decision': 'map_select'})

    assert command.client.action == 'claim_reward'
    assert command.client.params == {'option_index': 4}
    assert command.shadow is None
    assert command.telemetry['client_authoritative'] is True
    assert command.telemetry['requires_reanchor'] is True


def test_full_potion_slots_keep_existing_policy_and_record_skip():
    flow = InteractionFlow(ROOT)
    state = overview()
    state['run']['potions'] = [{'occupied': True}]
    state['reward']['rewards'] = [{'index': 1, 'reward_type': 'Potion', 'claimable': True}]
    _, command = flow.choose(state, {'decision': 'combat_reward', 'rewards': [
        {'index': 1, 'reward_type': 'Potion'}]})
    assert command.client.action == 'proceed'
    assert command.shadow_action == 'finish_combat_rewards'
    assert command.client.telemetry['skipped_rewards'] == [{'type': 'Potion', 'reason': 'potion_slots_full'}]


def test_new_act_map_selects_the_only_ancient_start_node():
    state = {'screen': 'MAP', 'run_id': 'run', 'run': {'floor': 17},
             'available_actions': ['choose_map_node'], 'map': {'available_nodes': [
                 {'index': 0, 'row': 0, 'col': 3, 'node_type': 'Ancient'}]}}
    shadow = {'decision': 'map_select', 'player': {'hp': 9, 'max_hp': 80, 'gold': 203},
              'context': {'floor': 17}, 'choices': [{'row': 0, 'col': 3, 'type': 'Ancient'}]}
    _, command = InteractionFlow(ROOT).choose(state, shadow)
    assert command.client.action == 'choose_map_node'
    assert command.client.params == {'option_index': 0}
    assert command.shadow_action == 'select_map_node'
    assert command.shadow_args == {'row': 0, 'col': 3}


def test_visible_map_choice_requests_reanchor_when_headless_map_is_unavailable():
    state = {
        'screen': 'MAP', 'run_id': 'run',
        'run': {'floor': 17, 'current_hp': 40, 'max_hp': 80, 'gold': 0},
        'available_actions': ['choose_map_node'],
        'map': {'available_nodes': [
            {'index': 6, 'row': 0, 'col': 2, 'node_type': 'Ancient'},
        ]},
    }
    _, command = InteractionFlow(ROOT).choose(state, {'decision': 'treasure_complete'})
    assert command.client.params == {'option_index': 6}
    assert command.shadow is None
    assert command.telemetry['client_authoritative'] is True
    assert command.telemetry['requires_reanchor'] is True


def test_stale_headless_map_choice_falls_back_to_visible_map():
    state = {
        'screen': 'MAP', 'run_id': 'run',
        'run': {'floor': 8, 'current_hp': 40, 'max_hp': 80, 'gold': 0},
        'available_actions': ['choose_map_node'],
        'map': {'available_nodes': [
            {'index': 3, 'row': 0, 'col': 1, 'node_type': 'Monster'},
        ]},
    }
    shadow = {
        'decision': 'map_select',
        'player': {'hp': 40, 'max_hp': 80, 'gold': 0},
        'context': {'floor': 8},
        'choices': [{'row': 0, 'col': 5, 'type': 'Monster'}],
    }
    _, command = InteractionFlow(ROOT).choose(state, shadow)
    assert command.client.params == {'option_index': 3}
    assert command.shadow is None
    assert command.telemetry['requires_reanchor'] is True


def test_skipped_card_is_not_reopened():
    flow = InteractionFlow(ROOT)
    flow.context.scene = 'REWARD'
    flow.context.location = ('run', None, 2)
    flow.context.reward_card_resolved = True
    state = overview()
    state['reward']['rewards'] = [{'index': 0, 'reward_type': 'Card', 'claimable': True}]
    _, command = flow.choose(state, {'decision': 'combat_reward', 'rewards': []})
    assert command.client.action == 'proceed'


def test_second_card_reward_is_claimed_when_headless_still_has_one():
    flow = InteractionFlow(ROOT)
    flow.context.scene = 'REWARD'
    flow.context.location = ('run', None, 2)
    flow.context.reward_card_resolved = True
    state = overview()
    state['reward']['rewards'] = [{'index': 2, 'reward_type': 'Card', 'claimable': True}]
    _, command = flow.choose(state, {'decision': 'combat_reward', 'rewards': [
        {'index': 2, 'reward_type': 'Card'}]})
    assert command.client.action == 'claim_reward'
    assert command.shadow_action == 'claim_combat_reward'


def test_no_change_ack_is_not_completion_and_loops_are_bounded():
    state = {'screen': 'EVENT', 'available_actions': ['choose_event_option'], 'event': {'event_id': 'same'}}
    assert not transition_completed('choose_event_option', state, state)
    guard = ProgressGuard(max_visits=2)
    command = decision('choose_event_option', {'option_index': 0})
    guard.before(state, command)
    guard.before(state, command)
    with pytest.raises(FlowBlocked, match='loop'):
        guard.before(state, command)


def test_structural_selection_calls_existing_policy(monkeypatch):
    import controller.interaction_flow as module
    from controller.live_noncombat import ClientDecision
    captured = []
    def existing(state, root, operation):
        captured.append((state['selection']['min_select'], state['selection']['max_select'], operation))
        return ClientDecision('select_deck_cards', {'indices': [0, 1]}, operation, {})
    monkeypatch.setattr(module, 'choose_client_selection', existing)
    state = {'screen': 'CARD_SELECTION', 'available_actions': ['select_deck_card'],
             'selection': {'kind': 'deck_transform_select', 'min_select': 0, 'max_select': 0,
                           'cards': [{'index': 0}, {'index': 1}]}}
    _, command = InteractionFlow(ROOT).choose(state, {'decision': 'card_select', 'min_select': 2, 'max_select': 2,
                                                     'cards': [{'index': 0}, {'index': 1}]})
    assert captured == [(2, 2, 'transform')]
    assert command.shadow_args == {'indices': '0,1'}


def test_visible_selection_is_authoritative_when_shadow_already_advanced(monkeypatch):
    import controller.interaction_flow as module
    from controller.live_noncombat import ClientDecision

    monkeypatch.setattr(module, 'choose_client_selection', lambda *args: ClientDecision(
        'select_deck_card', {'option_index': 7}, 'transform',
        {'policy': 'card_selection_transform'},
    ))
    state = {
        'screen': 'CARD_SELECTION',
        'available_actions': ['select_deck_card'],
        'selection': {
            'kind': 'deck_transform_select', 'min_select': 1, 'max_select': 1,
            'cards': [{'index': 7, 'card_id': 'STRIKE_IRONCLAD', 'upgraded': False}],
        },
    }

    _, command = InteractionFlow(ROOT).choose(state, {'decision': 'map_select'})

    assert command.client.action == 'select_deck_card'
    assert command.shadow is None
    assert command.telemetry['requires_reanchor'] is True


def test_visible_reward_card_policy_works_without_headless_reward_step(monkeypatch):
    import controller.interaction_flow as module

    monkeypatch.setattr(module, 'choose_card_reward', lambda state, root: {'card_index': 3})
    client = {
        'screen': 'CARD_SELECTION',
        'available_actions': ['choose_reward_card', 'skip_reward_cards'],
        'run': {'deck': []},
        'reward': {
            'pending_card_choice': True,
            'can_skip': True,
            'card_options': [{'index': 3, 'card_id': 'BASH'}],
        },
    }

    _, command = InteractionFlow(ROOT).choose(client, {'decision': 'map_select'})

    assert command.client.params == {'option_index': 3}
    assert command.shadow is None
    assert command.telemetry['requires_reanchor'] is True


def test_reward_selection_uses_visible_offer_and_mirrors_identity(monkeypatch):
    import controller.interaction_flow as module
    monkeypatch.setattr(module, 'choose_card_reward', lambda state, root: {'card_index': 0})
    client = {'screen': 'CARD_SELECTION', 'available_actions': ['choose_reward_card'],
              'reward': {'pending_card_choice': True, 'card_options': [{'index': 0, 'card_id': 'DEFEND'},
                                                                    {'index': 1, 'card_id': 'BASH'}]}}
    shadow = {'decision': 'card_reward', 'cards': [{'index': 0, 'id': 'CARD.BASH'},
                                                 {'index': 1, 'id': 'CARD.DEFEND'}]}
    _, command = InteractionFlow(ROOT).choose(client, shadow)
    assert command.client.params == {'option_index': 0}
    assert command.shadow_args == {'card_index': 1}
    client['reward']['card_options'].pop()
    _, command = InteractionFlow(ROOT).choose(client, shadow)
    assert command.client.params == {'option_index': 0}
    assert command.shadow_args == {'card_index': 1}


def test_event_proceed_uses_current_headless_proceed_option(monkeypatch):
    import controller.interaction_flow as module
    from controller.live_noncombat import ClientDecision

    choices = iter([
        ClientDecision(
            'choose_event_option', {'option_index': 0}, 'pick',
            {'policy': 'existing_event_heuristic', 'option_id': 'NEOW.LOST_COFFER'},
        ),
        ClientDecision(
            'choose_event_option', {'option_index': 0}, None,
            {'policy': 'explicit_proceed', 'option_id': 'PROCEED'},
        ),
    ])
    monkeypatch.setattr(module, 'choose_client_event', lambda state: next(choices))
    flow = InteractionFlow(ROOT)
    initial = {'screen': 'EVENT', 'run_id': 'run', 'run': {'floor': 1},
               'available_actions': ['choose_event_option']}
    shadow = {'decision': 'event_choice', 'options': [
        {'index': 2, 'text_key': 'NEOW.LOST_COFFER'},
        {'index': 4, 'text_key': 'NEOW.OTHER'},
    ]}

    _, command = flow.choose(initial, shadow)
    assert command.shadow_args == {'option_index': 2}
    flow.complete(command, initial, {'screen': 'REWARD', 'run_id': 'run',
                                     'run': {'floor': 1}, 'available_actions': []})
    assert flow.context.pending_event_option_id == 'NEOW.LOST_COFFER'
    assert flow.context.pending_event_shadow_index == 2

    proceed = {'screen': 'EVENT', 'run_id': 'run', 'run': {'floor': 1},
               'available_actions': ['choose_event_option']}
    settled_shadow = {'decision': 'event_choice', 'options': [
        {'index': 0, 'text_key': 'PROCEED'},
    ]}
    _, command = flow.choose(proceed, settled_shadow)
    assert command.client.params == {'option_index': 0}
    assert command.shadow_args == {'option_index': 0}
    flow.complete(command, proceed, {'screen': 'MAP', 'run_id': 'run',
                                     'run': {'floor': 1}, 'available_actions': []})
    assert flow.context.pending_event_option_id is None
    assert flow.context.pending_event_shadow_index is None


def test_event_proceed_is_client_only_when_headless_already_reached_map(monkeypatch):
    import controller.interaction_flow as module
    from controller.live_noncombat import ClientDecision

    monkeypatch.setattr(module, 'choose_client_event', lambda state: ClientDecision(
        'choose_event_option', {'option_index': 0}, None,
        {'policy': 'explicit_proceed', 'option_id': 'PROCEED'},
    ))
    state = {'screen': 'EVENT', 'run_id': 'run', 'run': {'floor': 1},
             'available_actions': ['choose_event_option']}
    _, command = InteractionFlow(ROOT).choose(state, {'decision': 'map_select'})
    assert command.shadow is None


def test_generic_proceed_reuses_existing_map_boundary_without_reanchor():
    state = {
        'screen': 'REST', 'run_id': 'run', 'run': {'floor': 5},
        'available_actions': ['proceed'],
    }
    _, command = InteractionFlow(ROOT).choose(state, {'decision': 'map_select'})
    assert command.client.action == 'proceed'
    assert command.shadow is None
    assert command.telemetry['shadow_already_at_boundary'] is True
    assert command.telemetry['requires_reanchor'] is False


def test_event_proceed_reconciles_random_relics_when_shadow_already_reached_map(monkeypatch):
    import controller.interaction_flow as module
    from controller.live_noncombat import ClientDecision

    monkeypatch.setattr(module, 'choose_client_event', lambda state: ClientDecision(
        'choose_event_option', {'option_index': 0}, None,
        {'policy': 'explicit_proceed', 'option_id': 'PROCEED'},
    ))
    state = {
        'screen': 'EVENT', 'run_id': 'run', 'available_actions': ['choose_event_option'],
        'run': {'floor': 18, 'relics': [
            {'relic_id': 'BURNING_BLOOD'}, {'relic_id': 'BOOK_OF_FIVE_RINGS'},
        ]},
    }
    shadow = {'decision': 'map_select', 'player': {'relics': [
        {'id': 'RELIC.BURNING_BLOOD'}, {'id': 'RELIC.ANCHOR'},
    ]}}

    _, command = InteractionFlow(ROOT).choose(state, shadow)

    assert command.shadow.action == 'reconcile_relics'
    assert command.shadow.params == {'relic_ids': 'BURNING_BLOOD,BOOK_OF_FIVE_RINGS'}


def test_shared_game_outcome_exception_has_one_identity():
    from controller.live_session import RunDefeat
    from scripts.live_run_demo import RunDefeat as RunnerDefeat
    assert RunDefeat is RunnerDefeat


def test_end_turn_waits_through_client_combat_teardown_before_rewards():
    before = {
        'screen': 'COMBAT', 'in_combat': True,
        'available_actions': ['play_card', 'end_turn'],
        'turn': 6,
    }
    tearing_down = {
        'screen': 'COMBAT', 'in_combat': False,
        'available_actions': ['discard_potion'],
        'turn': 7,
    }
    reward = {
        'screen': 'REWARD', 'in_combat': False,
        'available_actions': ['claim_reward', 'proceed'],
        'turn': 7,
        'reward': {'pending_card_choice': False},
    }
    assert not transition_completed('end_turn', before, tearing_down)
    assert transition_completed('end_turn', before, reward)


def test_pending_selection_waits_for_options_to_render():
    before = {'screen': 'COMBAT', 'in_combat': True, 'available_actions': ['play_card', 'end_turn']}
    after = {'screen': 'CARD_SELECTION', 'in_combat': True, 'available_actions': ['select_deck_card'],
             'selection': {'kind': 'combat_hand_upgrade_select', 'cards': []}}
    assert not transition_completed('play_card', before, after)


def test_combat_selection_waits_through_transient_combat_without_player_actions():
    before = {
        'screen': 'CARD_SELECTION', 'in_combat': True, 'turn': 1,
        'available_actions': ['select_deck_card'],
        'selection': {'kind': 'combat_hand_discard_select',
                      'cards': [{'index': 0, 'card_id': 'DISINTEGRATION'}]},
    }
    transient = {
        'screen': 'COMBAT', 'in_combat': True, 'turn': 1,
        'available_actions': ['discard_potion'], 'selection': None,
    }
    ready = {
        'screen': 'COMBAT', 'in_combat': True, 'turn': 2,
        'available_actions': ['play_card', 'end_turn'], 'selection': None,
    }
    assert not transition_completed('select_deck_card', before, transient)
    assert transition_completed('select_deck_card', before, ready)
    assert not transition_completed('confirm_selection', before, transient)
    assert transition_completed('confirm_selection', before, ready)


def test_empty_selection_uses_explicit_client_cancel_and_reanchors():
    state = {
        'screen': 'CARD_SELECTION',
        'available_actions': ['select_deck_card', 'cancel_selection'],
        'selection': {'kind': 'relic_specific_card_select', 'cards': []},
    }
    _, command = InteractionFlow(ROOT).choose(state, {'decision': 'card_select'})
    assert command.client.action == 'cancel_selection'
    assert command.shadow is None
    assert command.telemetry['requires_reanchor'] is True


def test_treasure_actions_are_mirrored_at_visible_boundaries():
    flow = InteractionFlow(ROOT)
    unopened = {
        'screen': 'CHEST', 'run_id': 'run', 'run': {'floor': 9},
        'available_actions': ['open_chest'],
        'chest': {'is_opened': False, 'has_relic_been_claimed': False, 'relic_options': []},
    }
    _, command = flow.choose(unopened, {'decision': 'treasure'})
    assert command.client.action == 'open_chest'
    assert command.shadow_action == 'open_chest'

    opened = {
        **unopened,
        'available_actions': ['choose_treasure_relic'],
        'chest': {
            'is_opened': True,
            'has_relic_been_claimed': False,
            'relic_options': [{'index': 4, 'relic_id': 'VAJRA'}],
        },
    }
    _, command = flow.choose(opened, {
        'decision': 'treasure_relic',
        'relics': [{'index': 0, 'id': 'VAJRA'}],
    })
    assert command.client.params == {'option_index': 4}
    assert command.shadow_action == 'choose_treasure_relic'
    assert command.shadow_args == {'relic_index': 0}

    claimed = {
        **unopened,
        'available_actions': ['proceed'],
        'chest': {'is_opened': True, 'has_relic_been_claimed': True, 'relic_options': []},
    }
    _, command = flow.choose(claimed, {'decision': 'treasure_complete'})
    assert command.client.action == 'proceed'
    assert command.shadow_action == 'leave_room'


def test_treasure_relic_identity_mismatch_uses_client_authority():
    state = {
        'screen': 'CHEST', 'run_id': 'run', 'run': {'floor': 9},
        'available_actions': ['choose_treasure_relic'],
        'chest': {
            'is_opened': True,
            'has_relic_been_claimed': False,
            'relic_options': [{'index': 0, 'relic_id': 'VAJRA'}],
        },
    }
    _, command = InteractionFlow(ROOT).choose(state, {
        'decision': 'treasure_relic',
        'relics': [{'index': 0, 'id': 'ANCHOR'}],
    })
    assert command.client.params == {'option_index': 0}
    assert command.shadow is None
    assert command.telemetry['client_authoritative'] is True
    assert command.telemetry['requires_reanchor'] is True


def test_shop_removal_does_not_abandon_shadow_for_unrelated_stock_difference():
    state = shop(True)
    state['available_actions'] = ['close_shop_inventory', 'remove_card_at_shop']
    state['run'].update(gold=100, deck=[{'card_id': 'STRIKE_IRONCLAD', 'upgraded': False}])
    state['shop'].update(cards=[], card_removal={
        'price': 75, 'available': True, 'used': False, 'enough_gold': True,
    })
    shadow = {'decision': 'shop', 'player': {'gold': 100},
              'cards': [{'index': 0, 'card_id': 'BASH', 'cost': 50}],
              'card_removal_cost': 75}
    flow = InteractionFlow(ROOT, deck_profile=PROFILE)
    _, tx = flow.choose(state, shadow)
    assert tx.shadow_action == 'remove_card'
    assert tx.telemetry['requires_reanchor'] is False
    flow.complete(tx, state, state)
    assert flow.context.reanchor_required is False


def test_shop_sold_slot_uses_stock_semantics_without_weakening_available_items():
    from controller.interaction_flow import shop_inventories_match
    visible = {'shop': {'cards': [
        {'card_id': '', 'price': 0, 'is_stocked': False},
        {'card_id': 'BASH', 'price': 50, 'is_stocked': True},
    ]}}
    shadow = {'decision': 'shop', 'cards': [
        {'card_id': 'STRIKE_IRONCLAD', 'cost': 75, 'is_stocked': False},
        {'card_id': 'BASH', 'cost': 50, 'is_stocked': True},
    ]}
    assert shop_inventories_match(visible, shadow)
    shadow['cards'][1]['cost'] = 51
    assert not shop_inventories_match(visible, shadow)
    shadow['cards'][1]['cost'] = 50
    shadow['cards'][0]['is_stocked'] = True
    assert not shop_inventories_match(visible, shadow)
