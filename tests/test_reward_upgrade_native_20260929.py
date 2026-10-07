"""Replay a real upgraded reward with the native engine and controller loop."""
import json
from pathlib import Path

import pytest

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.deck_profile import load_deck_profile
from controller.interaction_flow import InteractionFlow
from controller.rng_parity import compare_rng_snapshots


ROOT = Path(__file__).resolve().parents[1]
SESSION = ROOT / 'logs/live_dashboard/20260929_094231_226058'


@pytest.mark.skipif(not (SESSION / 'official_map_anchor.save').is_file(),
                    reason='Original upgraded reward anchor is unavailable')
def test_upgraded_card_reward_reaches_controller_finish():
    report = json.loads((SESSION / 'run_report.json').read_text(encoding='utf-8'))
    cli = Sts2CliAdapter(CliConfig(ROOT))
    cli.start()
    try:
        state = cli.load_save(str(SESSION / 'official_map_anchor.save'), resume_room=True)
        assert state.get('type') != 'error', state
        flow = InteractionFlow(ROOT, deck_profile=load_deck_profile(
            report['config'].get('deck_profile')))
        for row in report['actions']:
            if row['sequence'] < 13:
                if row.get('headless_action'):
                    state = cli.action(row['headless_action'], row['headless_args'], timeout_s=30)
                    assert state.get('type') != 'error', (row['sequence'], state)
                assert compare_rng_snapshots(row['client_rng_after'], cli.get_rng_snapshot()).passed
                continue
            client_before = row['client_before']
            _, command = flow.choose(client_before, state)
            assert command.client.action == row['client_action']
            assert command.shadow_action == row['headless_action']
            assert command.shadow_args == row['headless_args']
            shadow_before = state
            state = cli.action(command.shadow_action, command.shadow_args, timeout_s=30)
            assert state.get('type') != 'error', (row['sequence'], state)
            assert compare_rng_snapshots(row['client_rng_after'], cli.get_rng_snapshot()).passed
            flow.verify_native_reward_transition(
                command, client_before, shadow_before, row['client_after'], state)
            flow.complete(command, client_before, row['client_after'])
        assert state['decision'] == 'combat_reward'
        assert state['offered_rewards'][1]['successfully_selected'] is True
        assert state['player']['deck'][-1]['upgraded'] is True
        _, finish = flow.choose(report['actions'][-1]['client_after'], state)
        assert finish.shadow_action == 'finish_combat_rewards'
        state = cli.action(finish.shadow_action, finish.shadow_args, timeout_s=30)
        assert state['decision'] == 'map_select'
    finally:
        cli.stop()
