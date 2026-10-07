"""Live action 150: historical WRIGGLER move survives snapshot and continuation."""
import json
from pathlib import Path

import pytest

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.rng_parity import compare_rng_snapshots


ROOT = Path(__file__).resolve().parents[1]
SESSION = ROOT / 'logs/live_dashboard/20260929_150202_958629'
RAW = SESSION / 'combat_snapshots/combat_7_turn_1/raw_snapshot.json'


@pytest.mark.skipif(not RAW.is_file(), reason='Action 150 original anchor unavailable')
def test_wriggler_history_roundtrip_and_continuation():
    report = json.loads((SESSION / 'run_report.json').read_text(encoding='utf-8'))
    source, restored = (Sts2CliAdapter(CliConfig(ROOT)) for _ in range(2))
    source.start()
    restored.start()
    try:
        assert source.import_combat_snapshot(RAW.read_text(encoding='utf-8'), 'root')['success']
        assert source.restore_combat_snapshot('root').get('type') != 'error'
        for row in report['actions']:
            if row['sequence'] < 140:
                continue
            state = source.action(row['headless_action'], row['headless_args'], timeout_s=30)
            assert state.get('type') != 'error', (row['sequence'], state.get('message'))
            parity = compare_rng_snapshots(row['client_rng_after'], source.get_rng_snapshot())
            assert parity.passed, (row['sequence'], parity.differences[:5])
        captured = source.capture_combat_snapshot('after_150')
        assert captured.get('success'), captured.get('message')
        envelope = source.export_combat_snapshot('after_150')['snapshot_json']
        original_payload = json.loads(envelope)
        historical_move = next(field['Value'] for field in
                               original_payload['CombatHistoryEntries'][62]['Fields']
                               if field['Name'] == '<Move>k__BackingField')
        assert historical_move['Kind'] == 'completed_move_state'
        assert historical_move['ModelId'] == 'SPAWNED_MOVE'
        assert restored.import_combat_snapshot(envelope, 'after_150')['success']
        assert restored.restore_combat_snapshot('after_150').get('type') != 'error'
        recaptured = restored.capture_combat_snapshot('roundtrip')
        assert recaptured.get('success'), recaptured.get('message')
        restored_payload = json.loads(restored.export_combat_snapshot('roundtrip')['snapshot_json'])
        assert restored_payload['CombatHistoryEntries'] == original_payload['CombatHistoryEntries']
        assert recaptured['semantic_state_fingerprint'] == captured['semantic_state_fingerprint']
        assert restored.get_search_state()['combat_state_for_search'] == source.get_search_state()['combat_state_for_search']
        assert compare_rng_snapshots(source.get_rng_snapshot(), restored.get_rng_snapshot()).passed
        for engine in (source, restored):
            state = engine.action('end_turn', {}, timeout_s=30)
            assert state.get('type') != 'error', state.get('message')
        assert restored.get_search_state()['combat_state_for_search'] == source.get_search_state()['combat_state_for_search']
        assert compare_rng_snapshots(source.get_rng_snapshot(), restored.get_rng_snapshot()).passed
    finally:
        source.stop()
        restored.stop()
