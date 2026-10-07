"""Replay a real skipped card reward through every controller decision."""
import json
from pathlib import Path

import pytest

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.deck_profile import load_deck_profile
from controller.interaction_flow import InteractionFlow
from controller.rng_parity import compare_rng_snapshots


ROOT = Path(__file__).resolve().parents[1]
SESSIONS = (
    '20260928_223509_172037', '20260928_223719_554654',
    '20260928_223852_658276', '20260928_224038_683850',
    '20260928_224148_242150', '20260928_224635_233527',
)


@pytest.mark.parametrize('session_name', SESSIONS)
def test_skipped_card_remains_unselected_and_controller_finishes_rewards(session_name):
    session = ROOT / 'logs/live_dashboard' / session_name
    anchor = session / 'official_map_anchor.save'
    if not anchor.is_file():
        pytest.skip('Original native anchor is unavailable')
    report = json.loads((session / 'run_report.json').read_text(encoding='utf-8'))
    rows = report['actions']
    start = len(rows) - 1
    while start > 0 and rows[start - 1].get('client_action') in {
            'claim_reward', 'choose_reward_card', 'skip_reward_cards'}:
        start -= 1
    assert rows[-1]['client_action'] == 'skip_reward_cards'
    cli = Sts2CliAdapter(CliConfig(ROOT))
    cli.start()
    try:
        state = cli.load_save(str(anchor), resume_room=True)
        for row in rows[:start]:
            if row.get('headless_action') is None:
                continue
            state = cli.action(row['headless_action'], row['headless_args'], timeout_s=30)
            assert state.get('type') != 'error', (row['sequence'], state.get('message'))
            assert compare_rng_snapshots(row['client_rng_after'], cli.get_rng_snapshot()).passed
        flow = InteractionFlow(ROOT, deck_profile=load_deck_profile(
            report['config'].get('deck_profile')))
        for row in rows[start:]:
            client_before = row['client_before']
            _, command = flow.choose(client_before, state)
            assert command.client.action == row['client_action']
            assert command.shadow_action == row['headless_action']
            assert command.shadow_args == row['headless_args']
            shadow_before = state
            state = cli.action(command.shadow_action, command.shadow_args, timeout_s=30)
            assert state.get('type') != 'error', (row['sequence'], state.get('message'))
            assert compare_rng_snapshots(row['client_rng_after'], cli.get_rng_snapshot()).passed
            client_after = row['client_after']
            flow.verify_native_reward_transition(
                command, client_before, shadow_before, client_after, state)
            flow.complete(command, client_before, client_after)
        assert state['decision'] == 'combat_reward'
        assert flow.context.resolved_reward_items
        skipped_index = int(flow.context.resolved_reward_items[-1].rsplit(':', 1)[1])
        assert next(item for item in state['offered_rewards']
                    if item['index'] == skipped_index)['successfully_selected'] is False
        client_offer = rows[-1]['client_after']['reward']['offered_rewards']
        assert next(item for item in client_offer
                    if item['native_index'] == skipped_index)['successfully_selected'] is False
        _, finish = flow.choose(rows[-1]['client_after'], state)
        assert finish.client.action == 'collect_rewards_and_proceed'
        assert finish.shadow_action == 'finish_combat_rewards'
        state = cli.action(finish.shadow_action, finish.shadow_args, timeout_s=30)
        assert state['decision'] == 'map_select'
    finally:
        cli.stop()
