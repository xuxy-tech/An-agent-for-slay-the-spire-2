import json
from pathlib import Path

from controller.run_agent import choose_card_reward, choose_card_select_pick
from controller.interaction_flow import InteractionFlow
from controller.sandbox import SandboxManager
import pytest

ROOT = Path(__file__).resolve().parents[1]
PROFILE = {'schema_version': 2, 'id': 'test', 'ports': [{'id': 'x', 'target': 1, 'maximum': 1}],
           'cards': [{'id': 'RUPTURE', 'ports': ['x'], 'max_copies': 1}]}


def test_optional_reward_still_skips_and_mandatory_reward_selects():
    state = {'cards': [{'index': 4, 'id': 'CARD.STRIKE_IRONCLAD'}], 'player': {'deck': []}}
    assert choose_card_reward(state, ROOT, PROFILE) is None
    assert choose_card_reward({**state, 'can_skip': False}, ROOT, PROFILE) == {'card_index': 4}


def test_mandatory_reward_prefers_whitelisted_card_even_when_capped():
    state = {'can_skip': False, 'cards': [{'index': 4, 'id': 'STRIKE_IRONCLAD'}, {'index': 7, 'id': 'RUPTURE'}],
             'player': {'deck': [{'id': 'RUPTURE'}]}}
    assert choose_card_reward(state, ROOT, PROFILE) == {'card_index': 7}


def test_mandatory_multiselect_fills_required_count():
    state = {'min_select': 2, 'max_select': 2, 'cards': [
        {'index': 3, 'id': 'RUPTURE'}, {'index': 6, 'id': 'DEFEND_IRONCLAD'}], 'player': {'deck': []}}
    choice = choose_card_select_pick(state, ROOT, PROFILE)
    assert choice['indices'] == '0,1'
    assert choice['card_ids'] == ['RUPTURE', 'DEFEND_IRONCLAD']


@pytest.mark.parametrize('skippable', [True, False])
def test_visible_reward_uses_exported_skip_capability(skippable):
    state = {'screen': 'CARD_SELECTION', 'available_actions': ['choose_reward_card'] + (['skip_reward_cards'] if skippable else []),
             'reward': {'pending_card_choice': True, 'card_options': [{'index': 9, 'card_id': 'STRIKE_IRONCLAD'}]}}
    shadow = {'decision': 'card_reward', 'can_skip': True,
              'cards': [{'index': 4, 'id': 'CARD.STRIKE_IRONCLAD'}], 'player': {'deck': []}}
    _, command = InteractionFlow(ROOT, deck_profile=PROFILE).choose(state, shadow)
    assert command.client.action == ('skip_reward_cards' if skippable else 'choose_reward_card')
    if not skippable:
        assert command.shadow_args == {'card_index': 4}
        assert command.client.params == {'option_index': 9}
        assert command.telemetry['choice_reason'] == 'mandatory_reward_fallback'


def test_sandbox_generation_is_strictly_whitelist_and_old_scene_rejected(tmp_path):
    manager = SandboxManager(ROOT, tmp_path, ROOT / 'data/deck_profiles/ironclad_self_damage.json')
    try:
        allowed = {row['id'] for row in manager.catalog()['whitelist']}
        assert manager.catalog()['excluded_scenarios'] > 0
        with pytest.raises(ValueError, match='STALE_FIXTURE'):
            manager._cmd_load({'id': 'case_rupture_before_self_damage'})
        with pytest.raises(ValueError, match='whitelist'):
            manager._cmd_generate({'hand': ['STRIKE_IRONCLAD']})
        manager._cmd_generate({'stage': 'mid', 'seed': 'strict-white-test',
                               'hand': ['RUPTURE', 'HEMOKINESIS', 'BREAKTHROUGH']})
        assert set(manager.scene['parameters']['deck']) <= allowed
        assert not manager._outside_profile(manager.scene, {'cards': [{'id': card} for card in allowed]})
    finally:
        manager.close()


def test_mandatory_bundle_selects_a_legal_minimal_exception():
    state = {'screen': 'BUNDLE_SELECTION', 'available_actions': ['choose_bundle'],
             'bundles': [{'index': 5, 'cards': [{'card_id': 'HAVOC'}]},
                         {'index': 9, 'cards': [{'card_id': 'STRIKE_IRONCLAD'}, {'card_id': 'DEFEND_IRONCLAD'}]}]}
    shadow = {'decision': 'bundle_select', 'player': {'deck': []}, 'bundles': [
        {'index': 0, 'cards': [{'card_id': 'HAVOC'}]},
        {'index': 1, 'cards': [{'card_id': 'STRIKE_IRONCLAD'}, {'card_id': 'DEFEND_IRONCLAD'}]}]}
    _, command = InteractionFlow(ROOT, deck_profile=PROFILE).choose(state, shadow)
    assert command.client.params == {'option_index': 5}
    assert command.shadow_args == {'bundle_index': 0}
    assert command.telemetry['bundle_audit']['reason'] == 'visible_bundle_fallback'
