import copy
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.action_transaction import transaction_from_dict
from controller.interaction_flow import FlowBlocked, InteractionFlow
from controller.interaction_state import FlowContext
from controller.rng_parity import compare_rng_snapshots


ROOT = Path(__file__).resolve().parents[1]
TRIAL_RUNS = ('20261001_194037_447221', '20261001_202659_860950')
REWARD_RUN = '20261001_201954_102692'


def _session(name):
    path = ROOT / 'logs' / 'live_dashboard' / name
    if not (path / 'run_report.json').is_file():
        pytest.skip(f'Original session {name} is unavailable')
    return json.loads((path / 'run_report.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('name', TRIAL_RUNS)
def test_trial_accept_replays_original_floor_37_page_and_rng(name):
    report = _session(name)
    anchor = report['reanchors'][-1]
    cli = Sts2CliAdapter(CliConfig(ROOT))
    try:
        cli.start()
        state = cli.load_save(anchor['save_path'])
        assert state['decision'] == 'map_select'
        for row in report['actions']:
            if row['sequence'] <= anchor['action_sequence'] or not row.get('headless_action'):
                continue
            state = cli.action(row['headless_action'], row.get('headless_args') or {}, timeout_s=20)
            assert state.get('type') != 'error', state
        observed = report['actions'][-1]['client_after']['event']['options']
        assert state['decision'] == 'event_choice'
        assert [row['text_key'] for row in state['options']] == [
            row['text_key'] for row in observed]
        assert compare_rng_snapshots(
            report['actions'][-1]['client_rng_after'], cli.get_rng_snapshot()).passed
    finally:
        cli.stop()


def test_final_card_reward_pairs_visible_proceed_with_shadow_finish():
    report = _session(REWARD_RUN)
    last = report['actions'][-1]
    cli = Sts2CliAdapter(CliConfig(ROOT))
    try:
        cli.start()
        shadow = cli.load_save(report['anchor_save'], resume_room=True)
        for row in report['actions'][:-1]:
            shadow = cli.action(row['headless_action'], row['headless_args'])
        shadow_before = shadow
        shadow_after = cli.action(last['headless_action'], last['headless_args'])
        assert shadow_after['decision'] == 'combat_reward'
        assert shadow_after['rewards'] == []
        assert compare_rng_snapshots(last['client_rng_after'], cli.get_rng_snapshot()).passed

        flow = InteractionFlow(ROOT)
        flow.context.location = ('0VWFE9JNLE', '0', 1)
        flow.context.reward_set_mapping = copy.deepcopy(
            report['flow_context']['reward_set_mapping'])
        flow.context.active_reward_item = report['flow_context']['active_reward_item']
        command = transaction_from_dict(last['transaction'])
        flow.verify_native_reward_transition(
            command, last['client_before'], shadow_before,
            last['client_after'], shadow_after)
        assert flow.context.pending_reward_finish == {
            'run_id': '0VWFE9JNLE', 'shadow_set_id': 0}

        incomplete = copy.deepcopy(shadow_after)
        incomplete['offered_rewards'][0]['successfully_selected'] = False
        with pytest.raises(FlowBlocked, match='verified shadow finish'):
            flow.verify_native_reward_transition(
                command, last['client_before'], shadow_before,
                last['client_after'], incomplete)

        flow.complete(command, last['client_before'], last['client_after'])
        saved = json.loads(json.dumps(asdict(flow.context)))
        saved['location'] = tuple(saved['location'])
        resumed = InteractionFlow(ROOT, FlowContext(**saved))
        wrong_set = copy.deepcopy(shadow_after)
        wrong_set['reward_set_id'] = 99
        with pytest.raises(FlowBlocked, match='matching proceed'):
            resumed.choose(last['client_after'], wrong_set)
        _, finish = resumed.choose(last['client_after'], shadow_after)
        assert finish.client.action == 'choose_event_option'
        assert finish.shadow_action == 'finish_combat_rewards'
        assert cli.action(finish.shadow_action, finish.shadow_args)['decision'] == 'map_select'
        resumed.complete(finish, last['client_after'], {'screen': 'MAP'})
        assert resumed.context.pending_reward_finish is None
    finally:
        cli.stop()
