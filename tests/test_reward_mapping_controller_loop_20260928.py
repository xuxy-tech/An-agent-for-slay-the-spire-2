"""Run six observed reward sequences back through the controller after each action."""
import copy
import json
from pathlib import Path

import pytest

from controller.deck_profile import load_deck_profile
from controller.interaction_flow import InteractionFlow


ROOT = Path(__file__).resolve().parents[1]
SESSIONS = (
    '20260928_202933_867235', '20260928_203013_955156',
    '20260928_203612_612166', '20260928_204058_752206',
    '20260928_204209_994244', '20260928_204300_498989',
)


def _shadow_from_recorded_checkpoint(row, client_state, native_set_id):
    checkpoint = row['checkpoint']
    assert checkpoint['status'] == 'PASS'
    headless = checkpoint['headless']
    rewards = (client_state.get('reward') or {}).get('rewards') or []
    return {
        'decision': 'card_reward' if headless['phase'] == 'card' else 'combat_reward',
        'reward_set_id': native_set_id,
        'offered_rewards': copy.deepcopy(headless['offered']),
        'rewards': [{'index': item['native_index'], 'reward_type': item['reward_type']}
                    for item in rewards if item.get('claimable')],
        'cards': copy.deepcopy(headless['cards']),
        'can_skip': (client_state.get('reward') or {}).get('can_skip'),
    }


@pytest.mark.parametrize('session_name', SESSIONS)
def test_recorded_reward_sequence_reaches_controller_finish_decision(session_name):
    session = ROOT / 'logs/live_dashboard' / session_name
    if not (session / 'run_report.json').is_file():
        pytest.skip(f'Original recorded session {session_name} is unavailable')
    report = json.loads((session / 'run_report.json').read_text(encoding='utf-8'))
    assert report['error'] == 'Native reward set mapping changed within one reward occurrence'
    rows = report['actions']
    start = len(rows) - 1
    while start > 0 and rows[start - 1]['client_action'] in {
            'claim_reward', 'choose_reward_card', 'skip_reward_cards'}:
        start -= 1
    reward_rows = rows[start:]
    assert reward_rows[0]['client_action'] == 'claim_reward'
    assert reward_rows[-1]['client_action'] == 'choose_reward_card'
    mapping = report['flow_context']['reward_set_mapping']
    first_before = reward_rows[0]['client_before']
    shadow = {
        'decision': 'combat_reward',
        'reward_set_id': mapping['shadow_set_id'],
        'offered_rewards': [
            {**copy.deepcopy(item), 'index': item['native_index']}
            for item in first_before['reward']['offered_rewards']],
        'rewards': [{'index': item['native_index'], 'reward_type': item['reward_type']}
                    for item in first_before['reward']['rewards'] if item.get('claimable')],
    }
    profile = load_deck_profile(report['config'].get('deck_profile'))
    flow = InteractionFlow(ROOT, deck_profile=profile)
    flow.context.reward_reanchor_generation = mapping['reanchor_generation']
    flow.context.reward_occurrence = mapping['occurrence'] - 1
    for row in reward_rows:
        client_before = row['client_before']
        _, command = flow.choose(client_before, shadow)
        assert command.client.action == row['client_action']
        assert command.client.params == row['client_params']
        assert command.shadow_action == row['headless_action']
        assert command.shadow_args == row['headless_args']
        client_after = row['client_after']
        shadow_after = _shadow_from_recorded_checkpoint(
            row, client_after, mapping['shadow_set_id'])
        flow.verify_native_reward_transition(
            command, client_before, shadow, client_after, shadow_after)
        flow.complete(command, client_before, client_after)
        assert row['rng_parity']['status'] == 'PASS'
        shadow = shadow_after
    assert flow.context.reward_set_mapping['ordered_offer'] == mapping['ordered_offer']
    _, next_command = flow.choose(reward_rows[-1]['client_after'], shadow)
    assert next_command.client.action in {'proceed', 'collect_rewards_and_proceed'}
    assert next_command.shadow_action == 'finish_combat_rewards'
