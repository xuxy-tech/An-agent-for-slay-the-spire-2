import json
from pathlib import Path

import pytest

from controller.engine_parity import (
    client_reward_checkpoint, headless_reward_checkpoint, compare_checkpoints,
)
from controller.live_client_bridge import LiveClientBridge, JsonlSessionLog
from controller.live_session import ManagedControl, ControlledStop, write_json
from tests.test_live_observer import FakeMod


def reward_overview():
    # Shape recorded at the first-combat failure, before any manual click.
    return {'state_version': 9, 'run_id': 'test', 'screen': 'REWARD', 'in_combat': False,
            'available_actions': ['resolve_rewards', 'collect_rewards_and_proceed', 'claim_reward'],
            'reward': {'pending_card_choice': False, 'can_proceed': True,
                       'rewards': [{'index': 0, 'reward_type': 'Gold', 'claimable': True},
                                   {'index': 1, 'reward_type': 'Card', 'claimable': True}],
                       'card_options': [], 'alternatives': []}}


class RewardMod(FakeMod):
    def __init__(self):
        super().__init__()
        self.value = reward_overview()

    def action(self, action, **params):
        self.actions.append((action, params))
        if action == 'claim_reward':
            assert params == {'option_index': 1}
            self.value['screen'] = 'CARD_SELECTION'
            self.value['reward'].update(pending_card_choice=True, card_options=[
                {'index': i, 'card_id': card}
                for i, card in enumerate(['RAGE', 'ASHEN_STRIKE', 'SETUP_STRIKE'])])
            self.value['available_actions'] = ['resolve_rewards', 'choose_reward_card', 'skip_reward_cards']
        elif action == 'resolve_rewards':
            self.value.update(screen='MAP', available_actions=['choose_map_node'], reward=None)
        else:
            raise AssertionError(action)
        return {'state': self.state()}


def test_overview_opens_before_comparison_and_resolution(tmp_path):
    mod = RewardMod()
    bridge = LiveClientBridge(mod, JsonlSessionLog(tmp_path / 'session.jsonl'))
    overview = mod.state()
    assert compare_checkpoints(client_reward_checkpoint(overview), client_reward_checkpoint(overview)).status == 'INCOMPLETE'
    visible = bridge.reveal_card_reward(overview)
    assert visible['screen'] == 'CARD_SELECTION'
    assert mod.actions == [('claim_reward', {'option_index': 1})]
    shadow = {'decision': 'card_reward', 'cards': [
        {'index': i, 'id': card} for i, card in enumerate(['RAGE', 'ASHEN_STRIKE', 'SETUP_STRIKE'])]}
    assert compare_checkpoints(client_reward_checkpoint(visible), headless_reward_checkpoint(shadow)).status == 'PASS'
    after = bridge.execute_headless_action('card_reward', 'select_card_reward', {'card_index': 2}, visible)
    assert after['screen'] == 'MAP'
    assert mod.actions[-1] == ('resolve_rewards', {'card_index': 2})
    events = [json.loads(line) for line in (tmp_path / 'session.jsonl').read_text().splitlines()]
    assert any(e.get('decision_telemetry', {}).get('policy') == 'open_card_reward' for e in events)


def test_open_reward_is_a_controlled_action(tmp_path):
    mod = RewardMod()
    path = tmp_path / 'control.json'
    write_json(path, {'desired': 'stop'})
    bridge = LiveClientBridge(mod)
    bridge.before_action = ManagedControl(path, None).before_action
    with pytest.raises(ControlledStop):
        bridge.reveal_card_reward(mod.state())
    assert mod.actions == []


def test_already_open_does_not_click_again():
    mod = RewardMod()
    bridge = LiveClientBridge(mod)
    visible = bridge.reveal_card_reward(mod.state())
    bridge.reveal_card_reward(visible)
    assert len(mod.actions) == 1


def test_real_candidate_difference_is_still_rejected():
    mod = RewardMod()
    visible = LiveClientBridge(mod).reveal_card_reward(mod.state())
    shadow = {'decision': 'card_reward', 'cards': [{'index': 0, 'id': 'OTHER'}]}
    assert compare_checkpoints(client_reward_checkpoint(visible), headless_reward_checkpoint(shadow)).status == 'FAIL'


@pytest.mark.parametrize('skip', [False, True])
def test_recorded_card_selection_can_resolve_to_map(skip):
    # Reduced observation from 20260915_222841_824487, action 15.
    mod = RewardMod()
    mod.value = json.loads((Path(__file__).parent / 'fixtures/card_reward_selection.json').read_text())
    bridge = LiveClientBridge(mod)
    visible = bridge.reveal_card_reward(mod.state())
    assert mod.actions == []
    shadow = {'decision': 'card_reward', 'cards': [
        {'index': i, 'id': card} for i, card in enumerate(['HAVOC', 'BLOOD_WALL', 'BLOODLETTING'])]}
    assert compare_checkpoints(client_reward_checkpoint(visible), headless_reward_checkpoint(shadow)).status == 'PASS'
    result = bridge.execute_headless_action('card_reward', 'skip_card_reward' if skip else 'select_card_reward',
                                           {} if skip else {'card_index': 1}, visible)
    assert result['screen'] == 'MAP'
    assert mod.actions == [('resolve_rewards', {'option_index': -1} if skip else {'card_index': 1})]


@pytest.mark.parametrize('kind', ['deck_card_select', 'deck_remove_select', 'deck_upgrade_select'])
def test_generic_card_selection_is_not_reward(kind):
    from controller.engine_parity import is_card_reward_selection
    state = {'screen': 'CARD_SELECTION', 'selection': {'kind': kind},
             'available_actions': ['select_deck_card'], 'reward': None}
    assert not is_card_reward_selection(state)
    mod = RewardMod()
    mod.value = state
    with pytest.raises(RuntimeError, match='claimable card reward'):
        LiveClientBridge(mod).reveal_card_reward(state)
    assert not mod.actions
