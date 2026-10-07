"""Exercise mutable reward contents without relaxing set or item guards."""
import copy
import json
from pathlib import Path

import pytest

import controller.interaction_flow as flow_module
from controller.interaction_flow import FlowBlocked, InteractionFlow, decision


ROOT = Path(__file__).resolve().parents[1]


def _offers():
    return [
        {'index': 0, 'native_index': 0, 'reward_type': 'Card', 'model_id': None,
         'amount': None, 'cards': [{'id': 'BASH', 'upgraded': False}],
         'successfully_selected': False},
        {'index': 1, 'native_index': 1, 'reward_type': 'Card', 'model_id': None,
         'amount': None, 'cards': [{'id': 'DEFEND_IRONCLAD', 'upgraded': False}],
         'successfully_selected': False},
    ]


def _client(offers, *, screen='REWARD', rewards=(0, 1), card_options=()):
    return {
        'screen': screen, 'run_id': 'run',
        'run': {'floor': 2, 'deck': [], 'potions': [{'occupied': False}]},
        'available_actions': (['choose_reward_card', 'skip_reward_cards']
                              if screen == 'CARD_SELECTION'
                              else ['claim_reward', 'collect_rewards_and_proceed']),
        'reward': {
            'reward_set_id': 5, 'offered_rewards': copy.deepcopy(offers),
            'rewards': [{'index': i, 'native_index': i, 'reward_type': 'Card', 'claimable': True}
                        for i in rewards],
            'pending_card_choice': screen == 'CARD_SELECTION',
            'can_skip': True, 'card_options': list(card_options),
        },
    }


def _shadow(offers, *, decision='combat_reward', rewards=(0, 1), cards=()):
    return {
        'decision': decision, 'reward_set_id': 0,
        'player': {'deck': []},
        'offered_rewards': copy.deepcopy(offers),
        'rewards': [{'index': i, 'reward_type': 'Card'} for i in rewards],
        'cards': list(cards), 'can_skip': True,
    }


def _step(flow, before_client, before_shadow, after_client, after_shadow, action):
    _, command = flow.choose(before_client, before_shadow)
    assert command.client.action == action
    flow.verify_native_reward_transition(
        command, before_client, before_shadow, after_client, after_shadow)
    flow.complete(command, before_client, after_client)
    return command


def test_skip_first_card_group_then_pick_second_and_finish(monkeypatch):
    monkeypatch.setattr(flow_module, 'choose_card_reward', lambda *_args, **_kwargs: None)
    flow = InteractionFlow(ROOT)
    offers = _offers()
    client = _client(offers)
    shadow = _shadow(offers)
    first_card = _client(offers, screen='CARD_SELECTION', rewards=(1,),
                         card_options=({'index': 0, 'card_id': 'BASH', 'upgraded': False},))
    first_shadow = _shadow(offers, decision='card_reward', rewards=(1,),
                           cards=({'index': 0, 'id': 'CARD.BASH', 'upgraded': False},))
    _step(flow, client, shadow, first_card, first_shadow, 'claim_reward')

    after_skip = copy.deepcopy(offers)
    second_overview = _client(after_skip, rewards=(0, 1))
    second_overview_shadow = _shadow(after_skip, rewards=(1,))
    _step(flow, first_card, first_shadow, second_overview,
          second_overview_shadow, 'skip_reward_cards')

    monkeypatch.setattr(flow_module, 'choose_card_reward',
                        lambda *_args, **_kwargs: {'card_index': 0})
    second_card = _client(after_skip, screen='CARD_SELECTION', rewards=(0,),
                          card_options=({'index': 0, 'card_id': 'DEFEND_IRONCLAD', 'upgraded': False},))
    second_shadow = _shadow(after_skip, decision='card_reward', rewards=(),
                            cards=({'index': 0, 'id': 'CARD.DEFEND_IRONCLAD', 'upgraded': False},))
    _step(flow, second_overview, second_overview_shadow,
          second_card, second_shadow, 'claim_reward')
    after_pick = copy.deepcopy(after_skip)
    after_pick[1]['cards'] = []
    after_pick[1]['successfully_selected'] = True
    end_client = _client(after_pick, rewards=(0,))
    end_shadow = _shadow(after_pick, rewards=())
    end_client['run']['deck'].append({'card_id': 'DEFEND_IRONCLAD', 'upgraded': False})
    end_shadow['player']['deck'].append({'id': 'DEFEND_IRONCLAD', 'upgraded': False})
    _step(flow, second_card, second_shadow, end_client,
          end_shadow, 'choose_reward_card')
    _, finish = flow.choose(end_client, end_shadow)
    assert finish.client.action == 'collect_rewards_and_proceed'
    assert finish.shadow_action == 'finish_combat_rewards'
    assert flow.context.reward_set_mapping['ordered_offer'][1][4] == [
        {'id': 'DEFEND_IRONCLAD', 'upgraded': False}]
    assert flow.context.reward_set_mapping['current_offer'][1][4] == []


def test_bing_bong_reward_requires_exactly_two_matching_deck_cards():
    from controller.interaction_flow import _native_reward_flags, _native_reward_offer

    offers = _offers()[:1]
    chosen = {'card_id': 'BASH', 'upgraded': False}
    flow = InteractionFlow(ROOT)
    before = _client(offers, screen='CARD_SELECTION', rewards=(),
                     card_options=({'index': 0, **chosen},))
    shadow_before = _shadow(offers, decision='card_reward', rewards=(),
                            cards=({'index': 0, 'id': 'CARD.BASH', 'upgraded': False},))
    before['run']['relics'] = [{'relic_id': 'BING_BONG'}]
    shadow_before['player']['relics'] = [{'id': 'RELIC.BING_BONG'}]
    flow.context.reward_set_mapping = {
        'client_set_id': 5, 'shadow_set_id': 0,
        'current_offer': _native_reward_offer(offers),
        'selected_flags': _native_reward_flags(offers),
    }
    flow.context.active_reward_item = 'reward:0'
    after_offers = copy.deepcopy(offers)
    after_offers[0]['cards'] = []
    after_offers[0]['successfully_selected'] = True
    after = _client(after_offers, rewards=())
    shadow_after = _shadow(after_offers, rewards=())
    after['run']['deck'] = [chosen, copy.deepcopy(chosen)]
    shadow_after['player']['deck'] = [
        {'id': 'CARD.BASH', 'upgraded': False},
        {'id': 'CARD.BASH', 'upgraded': False},
    ]
    command = decision('choose_reward_card', {'option_index': 0},
                       shadow=('select_card_reward', {'card_index': 0}))
    flow.verify_native_reward_transition(
        command, before, shadow_before, after, shadow_after)
    missing_copy = copy.deepcopy(shadow_after)
    missing_copy['player']['deck'].pop()
    with pytest.raises(FlowBlocked, match='not added to the deck'):
        flow.verify_native_reward_transition(
            command, before, shadow_before, after, missing_copy)


def test_unselected_offer_change_and_wrong_set_id_remain_blocked():
    flow = InteractionFlow(ROOT)
    offers = _offers()
    before_client = _client(offers)
    before_shadow = _shadow(offers)
    _, command = flow.choose(before_client, before_shadow)
    after_client = _client(offers, screen='CARD_SELECTION', rewards=(1,),
                           card_options=({'index': 0, 'card_id': 'BASH', 'upgraded': False},))
    after_shadow = _shadow(offers, decision='card_reward', rewards=(1,),
                           cards=({'index': 0, 'id': 'CARD.BASH', 'upgraded': False},))
    after_shadow['reward_set_id'] = 1
    changed = copy.deepcopy(after_client)
    changed['reward']['offered_rewards'][1]['cards'][0]['id'] = 'CLAW'
    with pytest.raises(FlowBlocked, match='contents differ after mapped action'):
        flow.verify_native_reward_transition(
            command, before_client, before_shadow, changed, after_shadow)
    flow.verify_native_reward_transition(
        command, before_client, before_shadow, after_client, after_shadow)
    wrong_set = copy.deepcopy(after_shadow)
    wrong_set['reward_set_id'] = 2
    with pytest.raises(FlowBlocked, match='set changed'):
        flow.verify_native_reward_transition(
            command, before_client, before_shadow, after_client, wrong_set)
    flow.complete(command, before_client, after_client)
    tampered_shadow = copy.deepcopy(after_shadow)
    tampered_shadow['decision'] = 'combat_reward'
    tampered_shadow['offered_rewards'][1]['cards'][0]['id'] = 'CLAW'
    with pytest.raises(FlowBlocked, match='ordered native reward offers differ'):
        flow.choose(_client(offers, rewards=(1,)), tampered_shadow)


def test_duplicate_card_selection_removes_the_selected_position_only():
    offers = _offers()[:1]
    offers[0]['cards'] = [
        {'id': 'BASH', 'upgraded': False}, {'id': 'BASH', 'upgraded': True}]
    flow = InteractionFlow(ROOT)
    before = _client(offers, rewards=(0,))
    shadow_before = _shadow(offers, rewards=(0,))
    card_options = ({'index': 0, 'card_id': 'BASH', 'upgraded': False},
                    {'index': 1, 'card_id': 'BASH', 'upgraded': True})
    shadow_cards = ({'index': 0, 'id': 'CARD.BASH', 'upgraded': False},
                    {'index': 1, 'id': 'CARD.BASH', 'upgraded': True})
    card_state = _client(offers, screen='CARD_SELECTION', rewards=(),
                         card_options=card_options)
    shadow_card = _shadow(offers, decision='card_reward', rewards=(),
                          cards=shadow_cards)
    _step(flow, before, shadow_before, card_state, shadow_card, 'claim_reward')
    choose_second = decision('choose_reward_card', {'option_index': 1},
                             shadow=('select_card_reward', {'card_index': 1}))
    after = copy.deepcopy(offers)
    after[0]['cards'].pop(1)
    after[0]['successfully_selected'] = True
    wrong = copy.deepcopy(offers)
    wrong[0]['cards'].pop(0)
    wrong[0]['successfully_selected'] = True
    wrong_client = _client(wrong, rewards=())
    wrong_shadow = _shadow(wrong, rewards=())
    wrong_client['run']['deck'].append({'card_id': 'BASH', 'upgraded': True})
    wrong_shadow['player']['deck'].append({'id': 'BASH', 'upgraded': True})
    with pytest.raises(FlowBlocked, match='Selected reward card instance was not removed'):
        flow.verify_native_reward_transition(
            choose_second, card_state, shadow_card, wrong_client, wrong_shadow)
    selected_client = _client(after, rewards=())
    selected_shadow = _shadow(after, rewards=())
    selected_client['run']['deck'].append({'card_id': 'BASH', 'upgraded': True})
    selected_shadow['player']['deck'].append({'id': 'BASH', 'upgraded': True})
    missing_gain = copy.deepcopy(selected_shadow)
    missing_gain['player']['deck'].clear()
    with pytest.raises(FlowBlocked, match='not added to the deck'):
        flow.verify_native_reward_transition(
            choose_second, card_state, shadow_card, selected_client, missing_gain)
    wrong_upgrade = copy.deepcopy(selected_client)
    wrong_upgrade['run']['deck'][-1]['upgraded'] = False
    with pytest.raises(FlowBlocked, match='not added to the deck'):
        flow.verify_native_reward_transition(
            choose_second, card_state, shadow_card, wrong_upgrade, selected_shadow)
    flow.verify_native_reward_transition(
        choose_second, card_state, shadow_card, selected_client, selected_shadow)


def test_skip_rejects_native_claim_or_deck_gain(monkeypatch):
    monkeypatch.setattr(flow_module, 'choose_card_reward', lambda *_args, **_kwargs: None)
    offers = _offers()[:1]
    flow = InteractionFlow(ROOT)
    before = _client(offers, rewards=(0,))
    shadow_before = _shadow(offers, rewards=(0,))
    card_state = _client(offers, screen='CARD_SELECTION', rewards=(),
                         card_options=({'index': 0, 'card_id': 'BASH', 'upgraded': False},))
    shadow_card = _shadow(offers, decision='card_reward', rewards=(),
                          cards=({'index': 0, 'id': 'CARD.BASH', 'upgraded': False},))
    _step(flow, before, shadow_before, card_state, shadow_card, 'claim_reward')
    _, skip = flow.choose(card_state, shadow_card)
    assert skip.client.action == 'skip_reward_cards'
    claimed = copy.deepcopy(offers)
    claimed[0]['successfully_selected'] = True
    with pytest.raises(FlowBlocked, match='native selection status'):
        flow.verify_native_reward_transition(
            skip, card_state, shadow_card,
            _client(claimed, rewards=(0,)), _shadow(claimed, rewards=()))
    gained = _client(offers, rewards=(0,))
    gained['run']['deck'].append({'card_id': 'BASH'})
    with pytest.raises(FlowBlocked, match='unexpectedly changed a deck'):
        flow.verify_native_reward_transition(
            skip, card_state, shadow_card, gained, _shadow(offers, rewards=()))


MOLTEN_REPORT = ROOT / 'logs/live_dashboard/20260929_163737_065473/run_report.json'


@pytest.mark.skipif(not MOLTEN_REPORT.is_file(), reason='Molten Egg live report unavailable')
def test_real_molten_egg_changes_an_unclicked_card_offer_on_both_sides():
    from controller.interaction_flow import _native_reward_flags, _native_reward_offer

    report = json.loads(MOLTEN_REPORT.read_text(encoding='utf-8'))
    action = next(row for row in report['actions'] if row.get('sequence') == 162)
    assert action['client_action'] == 'claim_reward'
    before, after = action['client_before'], action['client_after']
    assert before['reward']['offered_rewards'][4]['cards'] != after['reward']['offered_rewards'][4]['cards']
    assert before['reward']['offered_rewards'][3]['model_id'] == 'MOLTEN_EGG'
    flow = InteractionFlow(ROOT)
    offer = _native_reward_offer(before['reward']['offered_rewards'])
    flow.context.reward_set_mapping = {
        'client_set_id': 6, 'shadow_set_id': 6, 'current_offer': offer,
        'selected_flags': _native_reward_flags(before['reward']['offered_rewards']),
    }
    command = decision('claim_reward', {'option_index': 0},
                       shadow=('claim_combat_reward', {'reward_index': 3,
                                                       'reward_set_id': 6}),
                       reward_native_index=3)
    shadow_before = {'reward_set_id': 6,
                     'offered_rewards': copy.deepcopy(before['reward']['offered_rewards'])}
    shadow_after = {'reward_set_id': 6,
                    'offered_rewards': copy.deepcopy(after['reward']['offered_rewards'])}
    flow.verify_native_reward_transition(command, before, shadow_before, after, shadow_after)
    assert flow.context.reward_set_mapping['current_offer'] == _native_reward_offer(
        after['reward']['offered_rewards'])
    mismatched = copy.deepcopy(shadow_after)
    mismatched['offered_rewards'][4]['cards'][1]['upgraded'] = False
    with pytest.raises(FlowBlocked, match='contents differ after mapped action'):
        flow.verify_native_reward_transition(command, before, shadow_before, after, mismatched)


def test_claiming_final_reward_allows_both_endpoints_to_leave_reward_screen():
    flow = InteractionFlow(ROOT)
    offers = [{
        'index': 0, 'native_index': 0, 'reward_type': 'Potion',
        'model_id': 'GLOWWATER_POTION', 'amount': None, 'cards': None,
        'successfully_selected': False,
    }]
    before_client = _client(offers, rewards=(0,))
    before_client['reward']['rewards'][0].update({
        'reward_type': 'Potion', 'native_index': 0, 'model_id': 'GLOWWATER_POTION'
    })
    before_shadow = _shadow(offers, rewards=(0,))
    before_shadow['rewards'][0]['reward_type'] = 'Potion'
    before_shadow['reward_set_id'] = 5
    _, command = flow.choose(before_client, before_shadow)
    after_client = {'screen': 'EVENT', 'reward': None, 'run_id': 'run'}
    after_shadow = {'decision': 'event_choice', 'reward_set_id': None}
    flow.verify_native_reward_transition(
        command, before_client, before_shadow, after_client, after_shadow)
    assert flow.context.reward_set_mapping['current_offer'] == []


def test_direct_special_card_reward_does_not_require_candidate_cards():
    from controller.interaction_flow import _native_reward_offer

    offer = [{'native_index': 1, 'reward_type': 'SpecialCard',
              'model_id': None, 'amount': None, 'cards': None}]
    assert _native_reward_offer(offer) == [[1, 'SpecialCard', None, None, None]]


def test_final_claim_can_close_client_while_shadow_finishes_rewards():
    flow = InteractionFlow(ROOT)
    offers = [{'index': 0, 'native_index': 0, 'reward_type': 'Potion',
               'model_id': 'GLOWWATER_POTION', 'amount': None, 'cards': None,
               'successfully_selected': False}]
    before_client = _client(offers, rewards=(0,))
    before_client['reward']['rewards'][0].update({
        'reward_type': 'Potion', 'native_index': 0, 'model_id': 'GLOWWATER_POTION'
    })
    before_shadow = _shadow(offers, rewards=(0,))
    before_shadow['rewards'][0]['reward_type'] = 'Potion'
    before_shadow['reward_set_id'] = 5
    _, command = flow.choose(before_client, before_shadow)
    after_client = {'screen': 'EVENT', 'reward': None, 'run_id': 'run'}
    after_shadow = {
        'decision': 'combat_reward', 'reward_set_id': 5,
        'offered_rewards': copy.deepcopy(offers),
        'rewards': [{'index': 0, 'reward_type': 'Potion'}],
    }
    flow.verify_native_reward_transition(
        command, before_client, before_shadow, after_client, after_shadow)
    assert flow.context.reward_set_mapping['current_offer'] == []


def test_card_reward_boundary_may_omit_shadow_set_id_but_keeps_offer_checks(monkeypatch):
    flow = InteractionFlow(ROOT)
    offers = _offers()[:1]
    before_client = _client(offers, rewards=(0,))
    before_shadow = _shadow(offers, rewards=(0,))
    _, claim = flow.choose(before_client, before_shadow)
    card_client = _client(
        offers, screen='CARD_SELECTION', rewards=(),
        card_options=({'index': 0, 'card_id': 'BASH', 'upgraded': False},))
    card_shadow = _shadow(
        offers, decision='card_reward', rewards=(),
        cards=({'index': 0, 'id': 'CARD.BASH', 'upgraded': False},))
    card_shadow.pop('reward_set_id')
    flow.verify_native_reward_transition(
        claim, before_client, before_shadow, card_client, card_shadow)
    flow.complete(claim, before_client, card_client)

    monkeypatch.setattr(flow_module, 'choose_card_reward', lambda *_args, **_kwargs: {'card_index': 0})
    _, choose = flow.choose(card_client, card_shadow)
    after_client = _client(offers, rewards=())
    after_client['screen'] = 'EVENT'
    after_client['reward'] = None
    after_client['run']['deck'].append({'card_id': 'BASH', 'upgraded': False})
    after_shadow = {'decision': 'event_choice', 'reward_set_id': None,
                    'player': {'deck': [{'id': 'BASH', 'upgraded': False}]}}
    flow.verify_native_reward_transition(
        choose, card_client, card_shadow, after_client, after_shadow)


def test_card_reward_boundary_may_use_nested_shadow_set_id(monkeypatch):
    flow = InteractionFlow(ROOT)
    offers = _offers()[:1]
    before_client = _client(offers, rewards=(0,))
    before_shadow = _shadow(offers, rewards=(0,))
    _, claim = flow.choose(before_client, before_shadow)
    card_client = _client(
        offers, screen='CARD_SELECTION', rewards=(),
        card_options=({'index': 0, 'card_id': 'BASH', 'upgraded': False},))
    card_shadow = _shadow(
        offers, decision='card_reward', rewards=(),
        cards=({'index': 0, 'id': 'CARD.BASH', 'upgraded': False},))
    card_shadow['reward_set_id'] = 1
    flow.verify_native_reward_transition(
        claim, before_client, before_shadow, card_client, card_shadow)
    assert flow.context.reward_set_mapping['card_set_id'] == 1
