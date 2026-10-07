import json
from pathlib import Path

import pytest

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.rng_parity import compare_rng_snapshots


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('session', [
    '20260926_115655_785603', '20260926_123521_224527',
])
def test_live_event_combat_snapshot_roundtrip(session):
    path = ROOT / 'logs/live_dashboard' / session / 'run_report.json'
    if not path.exists():
        pytest.skip('Local failure evidence unavailable')
    report = json.loads(path.read_text(encoding='utf-8'))
    source = Sts2CliAdapter(CliConfig(ROOT))
    worker = Sts2CliAdapter(CliConfig(ROOT))
    source.start()
    worker.start()
    try:
        anchors = report.get('reanchors') or []
        anchor = anchors[-1] if anchors else None
        state = source.load_save(
            anchor['save_path'] if anchor else report['anchor_save'],
            # Native room loading also initializes the map vote synchronizer.
            resume_room=True if anchor else report.get('anchor_room', False),
        )
        assert state.get('type') != 'error', state
        for row in report['actions']:
            if anchor and row['sequence'] <= anchor['action_sequence']:
                continue
            assert row['status'] == 'completed', row
            command = (row.get('transaction') or {}).get('shadow')
            if command:
                state = source.action(command['action'], command['params'], timeout_s=30)
                assert state.get('type') != 'error', state
        assert state['decision'] == 'combat_play'
        before = source.get_rng_snapshot()
        assert compare_rng_snapshots(report['actions'][-1]['client_rng_after'], before).passed
        captured = source.capture_combat_snapshot('event_root')
        assert captured.get('success') is True, captured
        assert compare_rng_snapshots(before, source.get_rng_snapshot()).passed
        exported = source.export_combat_snapshot('event_root')
        envelope = json.loads(exported['snapshot_json'])
        room = json.loads(envelope['RoomJson'])
        assert room['parent_event_id']['Entry'] == 'DENSE_VEGETATION'
        assert not room.get('is_pre_finished', False)
        imported = worker.import_combat_snapshot(exported['snapshot_json'], 'event_root')
        assert imported.get('success') is True, imported
        restored = worker.restore_combat_snapshot('event_root')
        assert restored.get('type') != 'error', restored
        assert restored.get('restore_mode') == 'full', restored
        assert compare_rng_snapshots(before, worker.get_rng_snapshot()).passed
        worker_capture = worker.capture_combat_snapshot('worker_root')
        assert worker_capture.get('success') is True, worker_capture
        assert worker_capture['semantic_state_fingerprint'] == captured['semantic_state_fingerprint']
        worker_room = json.loads(json.loads(worker.export_combat_snapshot('worker_root')['snapshot_json'])['RoomJson'])
        assert worker_room == room
        direct = source.action('end_turn', {}, timeout_s=30)
        cold = worker.action('end_turn', {}, timeout_s=30)
        assert direct.get('type') != 'error', direct
        assert cold.get('type') != 'error', cold
        assert direct['decision'] == cold['decision']
        after = source.get_rng_snapshot()
        assert compare_rng_snapshots(after, worker.get_rng_snapshot()).passed
        expected_turn = source.capture_combat_snapshot('after_turn')
        actual_turn = worker.capture_combat_snapshot('after_turn')
        assert actual_turn['semantic_state_fingerprint'] == expected_turn['semantic_state_fingerprint']
        hot = worker.restore_combat_snapshot('event_root', allow_full=False)
        assert hot.get('restore_mode') == 'in_place', hot
        assert compare_rng_snapshots(before, worker.get_rng_snapshot()).passed
        repeated = worker.action('end_turn', {}, timeout_s=30)
        assert repeated.get('type') != 'error', repeated
        assert compare_rng_snapshots(after, worker.get_rng_snapshot()).passed
        repeated_turn = worker.capture_combat_snapshot('repeated_turn')
        assert repeated_turn['semantic_state_fingerprint'] == expected_turn['semantic_state_fingerprint']

        # Force a short victory line only in isolated test processes, then
        # compare native event/reward settlement against the restored worker.
        source.restore_combat_snapshot('event_root', allow_full=False)
        count = len(envelope['EnemyCreatureStates'])
        configured = source.send({
            'cmd': 'configure_sandbox', 'hp': 80, 'energy': 10,
            'hand': ['STRIKE_IRONCLAD'] * count, 'enemy_hp': [1] * count,
        })
        assert configured.get('decision') == 'combat_play', configured
        source.capture_combat_snapshot('lethal')
        lethal = source.export_combat_snapshot('lethal')['snapshot_json']
        worker.import_combat_snapshot(lethal, 'lethal')
        restored = worker.restore_combat_snapshot('lethal', allow_full=False)
        assert restored.get('restore_mode') == 'in_place', restored
        outcomes = []
        for cli in (source, worker):
            for _ in range(count):
                actions = cli.get_search_state()['combat_state_for_search']['combat']['available_actions']
                strike = next(a for a in actions if (a.get('metadata') or {}).get('card_id') == 'STRIKE_IRONCLAD')
                result = cli.action('play_card', {
                    'card_index': strike['card_index'], 'target_index': strike['target_index'],
                }, timeout_s=30)
                assert result.get('type') != 'error', result
            outcomes.append(result)
        assert outcomes[0]['decision'] == outcomes[1]['decision']
        assert outcomes[0]['decision'] == 'combat_reward', outcomes
        assert outcomes[0].get('rewards') == outcomes[1].get('rewards')
        assert compare_rng_snapshots(source.get_rng_snapshot(), worker.get_rng_snapshot()).passed
        # Search workers stop at victory and have no overworld map. Only the
        # original run owns continuation through the native event/reward flow.
        finished = source.action('finish_combat_rewards', timeout_s=30)
        assert finished.get('decision') == 'map_select', finished
    finally:
        source.stop()
        worker.stop()
def test_recorded_battle_dummy_empty_intent_is_complete_parity():
    import json
    from pathlib import Path

    import pytest

    from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
    from controller.engine_parity import (
        client_combat_checkpoint, headless_combat_checkpoint, compare_checkpoints,
    )

    root = Path(__file__).resolve().parents[1]
    report_path = root / 'logs/live_dashboard/20261003_210340_252576/run_report.json'
    if not report_path.is_file():
        pytest.skip('Recorded Battle Dummy live evidence is unavailable')
    report = json.loads(report_path.read_text(encoding='utf-8'))
    anchor = [row for row in report['reanchors'] if row.get('status') == 'REANCHORED_PASS'][-1]
    cli = Sts2CliAdapter(CliConfig(root))
    assert cli.start().get('type') != 'error'
    try:
        assert cli.load_save(anchor['save_path'], resume_room=False).get('type') != 'error'
        for row in report['actions']:
            if not anchor['action_sequence'] < row['sequence'] <= 396:
                continue
            command = (row.get('transaction') or {}).get('shadow')
            if command:
                state = cli.action(command['action'], command.get('params'), timeout_s=30)
                assert state.get('type') != 'error'
        search = cli.get_search_state()['combat_state_for_search']
        assert search['combat']['enemies'][0]['intent']['intent_types'] == []
        client = report['terminal_client']
        result = compare_checkpoints(
            client_combat_checkpoint(client),
            headless_combat_checkpoint(state, search, client['run_id']),
        )
        assert result.status == 'PASS', result.differences
    finally:
        cli.stop()
