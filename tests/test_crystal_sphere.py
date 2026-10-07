from pathlib import Path

import pytest

from controller.crystal_sphere import board_identity, choose_divination
from controller.interaction_flow import FlowBlocked, InteractionFlow, transition_completed
from cli.sts2_mod_adapter import Sts2ModAdapter


ROOT = Path(__file__).resolve().parents[1]


def sphere(remaining=3, cells=None, phase='divining'):
    return {'contract': 'sts2.crystal_sphere.v1', 'active': True, 'phase': phase,
            'width': 11, 'height': 11, 'remaining': remaining,
            'visible_cells': cells or []}


def client(board, actions=None):
    return {'screen': 'CRYSTAL_SPHERE', 'run_id': 'run',
            'available_actions': actions or ['crystal_sphere_divine'],
            'crystal_sphere': board}


def shadow(board):
    return {'decision': 'crystal_sphere', **{key: board[key] for key in
            ('width', 'height', 'remaining', 'visible_cells')}}


def test_divination_is_mirrored_and_uses_only_visible_cells():
    board = sphere(cells=[{'x': 5, 'y': 5, 'item_kind': 'CrystalSphereCurse'}])
    flow = InteractionFlow(ROOT)
    _, tx = flow.choose(client(board), shadow(board))
    expected = choose_divination(board)
    assert tx.client.action == tx.shadow.action == 'crystal_sphere_divine'
    assert tx.client.params == expected
    assert tx.shadow.params == {key: expected[key] for key in ('x', 'y', 'tool')}
    assert transition_completed('crystal_sphere_divine', client(board),
                                client(sphere(2, [{'x': 0, 'y': 0, 'item_kind': None}])))


def test_board_mismatch_blocks_before_submission():
    board = sphere()
    other = sphere(cells=[{'x': 1, 'y': 1, 'item_kind': None}])
    with pytest.raises(FlowBlocked, match='board differs'):
        InteractionFlow(ROOT).choose(client(board), shadow(other))
    with pytest.raises(ValueError, match='duplicate'):
        board_identity(sphere(cells=[{'x': 1, 'y': 1}, {'x': 1, 'y': 1}]))


def test_native_proceed_finishes_unclaimed_shadow_reward_boundary():
    board = sphere(0, phase='proceed')
    state = client(board, ['proceed'])
    flow = InteractionFlow(ROOT)
    pending_finish = {'decision': 'combat_reward', 'rewards': [],
                      'offered_rewards': [{'successfully_selected': True}]}
    _, tx = flow.choose(state, pending_finish)
    assert tx.client.action == 'proceed'
    assert tx.shadow.action == 'finish_combat_rewards'
    _, already_finished = flow.choose(state, {'decision': 'map_select'})
    assert already_finished.shadow is None
    with pytest.raises(FlowBlocked, match='not fully claimed'):
        flow.choose(state, {'decision': 'combat_reward', 'rewards': [
            {'reward_type': 'Gold'}], 'offered_rewards': [
            {'successfully_selected': False}]})
    flow.verify_crystal_sphere_transition(
        tx, client(sphere(1)), shadow(sphere(1)), state,
        pending_finish)


def test_recorded_sphere_proceed_reaches_matching_map():
    import json

    from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
    from controller.engine_parity import (
        client_map_checkpoint, headless_map_checkpoint, compare_checkpoints,
    )

    path = ROOT / 'logs/live_dashboard/20261003_113718_866615/run_report.json'
    if not path.is_file():
        pytest.skip('Recorded Crystal Sphere live evidence is unavailable')
    report = json.loads(path.read_text(encoding='utf-8'))
    anchor = [row for row in report['reanchors'] if row.get('status') == 'REANCHORED_PASS'][-1]
    cli = Sts2CliAdapter(CliConfig(ROOT))
    assert cli.start().get('type') != 'error'
    try:
        assert cli.load_save(anchor['save_path'], resume_room=False).get('type') != 'error'
        for row in report['actions']:
            if not anchor['action_sequence'] < row['sequence'] <= 258:
                continue
            command = (row.get('transaction') or {}).get('shadow')
            if command:
                state = cli.action(command['action'], command.get('params'), timeout_s=30)
                assert state.get('type') != 'error'
        assert state['decision'] == 'combat_reward'
        assert state['rewards'] == []
        assert all(item['successfully_selected'] for item in state['offered_rewards'])
        after = cli.action('finish_combat_rewards', {}, timeout_s=30)
        assert after['decision'] == 'map_select'
        client_after = report['actions'][-1]['client_after']
        result = compare_checkpoints(
            client_map_checkpoint(client_after),
            headless_map_checkpoint(after, cli.get_map(), client_after['run_id']),
        )
        assert result.status == 'PASS', result.differences
    finally:
        cli.stop()


def test_mod_adapter_promotes_native_sphere_and_routes_action(monkeypatch):
    mod = Sts2ModAdapter()
    calls = []
    board = sphere()

    def request(method, path, body=None, **kwargs):
        calls.append((method, path, body))
        if path == '/state':
            return {'state_version': 1, 'run_id': 'run', 'screen': 'UNKNOWN',
                    'available_actions': ['discard_potion']}
        if path == '/crystal-sphere/state':
            return board
        if path == '/crystal-sphere/action':
            return {'action': body['action'], 'status': 'submitted'}
        raise AssertionError(path)

    monkeypatch.setattr(mod, '_request', request)
    state = mod.state()
    assert state['screen'] == 'CRYSTAL_SPHERE'
    assert state['available_actions'] == ['crystal_sphere_divine', 'discard_potion']
    move = choose_divination(board)
    response = mod.submit_action('crystal_sphere_divine', **move)
    assert response['status'] == 'completed'
    assert ('POST', '/crystal-sphere/action', {'action': 'crystal_sphere_divine', **move}) in calls
