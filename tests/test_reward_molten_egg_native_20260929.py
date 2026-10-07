"""Replay the live Molten Egg claim that upgrades untouched card candidates."""
import json
from pathlib import Path

import pytest

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.interaction_flow import InteractionFlow
from controller.rng_parity import compare_rng_snapshots


ROOT = Path(__file__).resolve().parents[1]
SESSION = ROOT / 'logs/live_dashboard/20260929_163737_065473'


@pytest.mark.skipif(not (SESSION / 'official_map_anchor.save').is_file(),
                    reason='Molten Egg native map anchor unavailable')
def test_molten_egg_claim_updates_other_offer_and_next_controller_decision():
    report = json.loads((SESSION / 'run_report.json').read_text(encoding='utf-8'))
    cli = Sts2CliAdapter(CliConfig(ROOT))
    cli.start()
    try:
        state = cli.load_save(str(SESSION / 'official_map_anchor.save'),
                              resume_room=report.get('anchor_room', False))
        assert state.get('type') != 'error', state.get('message')
        flow = InteractionFlow(ROOT)
        for row in report['actions']:
            if row['sequence'] > 162:
                break
            if row['sequence'] >= 159:
                _, command = flow.choose(row['client_before'], state)
                assert command.client.action == row['client_action']
                assert command.shadow_action == row['headless_action']
                assert command.shadow_args == row['headless_args']
                before = state
            else:
                command = before = None
            if row.get('headless_action'):
                state = cli.action(row['headless_action'], row.get('headless_args') or {},
                                   timeout_s=30)
                assert state.get('type') != 'error', (row['sequence'], state.get('message'))
                assert compare_rng_snapshots(row['client_rng_after'], cli.get_rng_snapshot()).passed
            if command is not None:
                flow.verify_native_reward_transition(command, row['client_before'], before,
                                                     row['client_after'], state)
                flow.complete(command, row['client_before'], row['client_after'])
        assert state['offered_rewards'][3]['model_id'] == 'MOLTEN_EGG'
        assert state['offered_rewards'][3]['successfully_selected'] is True
        assert state['offered_rewards'][4]['cards'] == [
            {'id': 'AGGRESSION', 'upgraded': False},
            {'id': 'BULLY', 'upgraded': True},
            {'id': 'UPPERCUT', 'upgraded': True},
        ]
        assert (report['actions'][161]['client_after']['reward']['offered_rewards'][4]['cards']
                == state['offered_rewards'][4]['cards'])
        _, next_command = flow.choose(report['actions'][161]['client_after'], state)
        assert next_command.client.action == 'claim_reward'
        assert next_command.shadow_action == 'claim_combat_reward'
        assert next_command.telemetry['reward_native_index'] == 4
    finally:
        cli.stop()
