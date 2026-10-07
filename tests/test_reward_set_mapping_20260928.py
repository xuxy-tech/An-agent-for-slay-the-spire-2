"""Replay the native reward boundary after an event map re-anchor."""
import json
from pathlib import Path

import pytest

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.engine_parity import compare_checkpoints, reward_lifecycle_checkpoint
from controller.interaction_flow import InteractionFlow
from controller.rng_parity import compare_rng_snapshots


ROOT = Path(__file__).resolve().parents[1]
SESSION = ROOT / 'logs/live_dashboard/20260928_152648_472747'


@pytest.mark.skipif(not (SESSION / 'reanchor_segment_1.save').is_file(),
                    reason='Native map re-anchor save is unavailable')
def test_reward_after_map_reanchor_uses_each_native_set_id_and_returns_to_map():
    report = json.loads((SESSION / 'run_report.json').read_text(encoding='utf-8'))
    cli = Sts2CliAdapter(CliConfig(ROOT))
    cli.start()
    try:
        state = cli.load_save(str(SESSION / 'reanchor_segment_1.save'))
        assert state['decision'] == 'map_select'
        for row in report['actions']:
            if row['sequence'] < 6:
                continue
            state = cli.action(row['headless_action'], row['headless_args'], timeout_s=30)
            assert state.get('type') != 'error', (row['sequence'], state)
        client = report['actions'][-1]['client_after']
        assert state['decision'] == 'combat_reward'
        assert client['reward']['reward_set_id'] == 1
        assert state['reward_set_id'] == 0
        flow = InteractionFlow(ROOT)
        current, command = flow.choose(client, state)
        assert command.shadow_args == {'reward_index': 0, 'reward_set_id': 0}
        mapping = flow.context.reward_set_mapping
        identity = f"{mapping['run_id']}:{mapping['reanchor_generation']}:{mapping['occurrence']}"
        parity = compare_checkpoints(
            reward_lifecycle_checkpoint(client, client=True, mapped_set_identity=identity),
            reward_lifecycle_checkpoint(state, client=False, mapped_set_identity=identity))
        assert parity.status == 'PASS', parity.differences
        assert compare_rng_snapshots(report['actions'][-1]['client_rng_after'],
                                     cli.get_rng_snapshot()).passed

        state = cli.action('claim_combat_reward', command.shadow_args)
        assert state['decision'] == 'combat_reward'
        assert [row['index'] for row in state['rewards']] == [1]
        state = cli.action('claim_combat_reward', {'reward_index': 1, 'reward_set_id': 0})
        assert state['decision'] == 'card_reward'
        assert [row['id'].split('.')[-1] for row in state['cards']] == [
            'BODY_SLAM', 'IRON_WAVE', 'FORGOTTEN_RITUAL']
        state = cli.action('select_card_reward', {'card_index': 2})
        assert state['decision'] == 'combat_reward' and not state['rewards']
        state = cli.action('finish_combat_rewards', {})
        assert state['decision'] == 'map_select'
    finally:
        cli.stop()
