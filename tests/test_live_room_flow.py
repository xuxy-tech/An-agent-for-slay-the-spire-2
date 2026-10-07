from types import SimpleNamespace

import pytest

from scripts.live_run_demo import _noncombat_shadow_action, _confirmed_defeat


def test_event_options_match_by_key_not_local_index():
    decision = SimpleNamespace(action='choose_event_option', params={'option_index': 0},
                               telemetry={'option_id': 'MORPHIC_GROVE.pages.INITIAL.options.GROUP'})
    shadow = {'decision': 'event_choice', 'options': [{'index': 1, 'text_key': decision.telemetry['option_id']}]}
    assert _noncombat_shadow_action(decision, {}, shadow) == ('choose_option', {'option_index': 1})
    shadow['options'] = []
    with pytest.raises(RuntimeError, match='Cannot match'):
        _noncombat_shadow_action(decision, {}, shadow)


def test_two_card_selection_mirrors_one_atomic_headless_choice():
    decision = SimpleNamespace(action='select_deck_cards', params={'indices': [4, 9]})
    client = {'selection': {'cards': [{'index': 4, 'card_id': 'STRIKE', 'upgraded': False},
                                      {'index': 9, 'card_id': 'DEFEND', 'upgraded': True}]}}
    shadow = {'decision': 'card_select', 'cards': [{'index': 0, 'id': 'CARD.STRIKE', 'upgraded': False},
                                                 {'index': 1, 'id': 'CARD.DEFEND', 'upgraded': True}]}
    assert _noncombat_shadow_action(decision, client, shadow) == ('select_cards', {'indices': '0,1'})
    shadow['cards'][1]['upgraded'] = False
    with pytest.raises(RuntimeError, match='differ'):
        _noncombat_shadow_action(decision, client, shadow)


def test_event_proceed_does_not_advance_shadow_twice():
    decision = SimpleNamespace(action='choose_event_option', telemetry={'policy': 'explicit_proceed'})
    assert _noncombat_shadow_action(decision, {}, {'decision': 'map_select'}) is None


def test_death_is_a_game_outcome_not_reward_timeout():
    assert _confirmed_defeat({'run': {'current_hp': 0}}, {'decision': 'game_over', 'victory': False})
    assert not _confirmed_defeat({'run': {'current_hp': 20}}, {'decision': 'card_reward'})
    with pytest.raises(RuntimeError, match='disagree'):
        _confirmed_defeat({'run': {'current_hp': 20}}, {'decision': 'game_over', 'victory': False})
