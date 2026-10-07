"""A consumed potion from a completed live run survives cross-process history restore."""
import json
from pathlib import Path

import pytest

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.rng_parity import compare_rng_snapshots


ROOT = Path(__file__).resolve().parents[1]
SESSION = ROOT / 'logs/live_dashboard/20260929_145432_520698'
RAW = SESSION / 'combat_snapshots/combat_4_turn_1/raw_snapshot.json'


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _first_diff(left, right, path='root'):
    if type(left) is not type(right):
        return path, left, right
    if isinstance(left, dict):
        for key in left.keys() | right.keys():
            if key not in left or key not in right:
                return f'{path}.{key}', left.get(key), right.get(key)
            diff = _first_diff(left[key], right[key], f'{path}.{key}')
            if diff:
                return diff
    elif isinstance(left, list):
        if len(left) != len(right):
            return f'{path}.length', len(left), len(right)
        for index, (a, b) in enumerate(zip(left, right)):
            diff = _first_diff(a, b, f'{path}[{index}]')
            if diff:
                return diff
    elif left != right:
        return path, left, right
    return None


@pytest.mark.skipif(not RAW.is_file(), reason='Live potion combat anchor unavailable')
def test_consumed_live_potion_history_roundtrip_and_continue():
    report = json.loads((SESSION / 'run_report.json').read_text(encoding='utf-8'))
    source, restored = (Sts2CliAdapter(CliConfig(ROOT)) for _ in range(2))
    source.start()
    restored.start()
    try:
        assert source.import_combat_snapshot(RAW.read_text(encoding='utf-8'), 'root')['success']
        assert source.restore_combat_snapshot('root').get('type') != 'error'
        row = next(row for row in report['actions'] if row['sequence'] == 66)
        assert row['headless_action'] == 'use_potion'
        state = source.action(row['headless_action'], row['headless_args'], timeout_s=30)
        assert state.get('type') != 'error', state.get('message')
        assert compare_rng_snapshots(row['client_rng_after'], source.get_rng_snapshot()).passed
        captured = source.capture_combat_snapshot('after_potion')
        assert captured.get('success'), captured.get('message')
        envelope = source.export_combat_snapshot('after_potion')['snapshot_json']
        payload = json.loads(envelope)
        history = payload['CombatHistoryEntries']
        assert any(value.get('Kind') == 'player' for value in _walk(history))
        assert any(value.get('Kind') == 'object' and 'Potion' in str(value.get('TypeName'))
                   for value in _walk(history))
        player = json.loads(payload['PlayerJson'])
        assert not any(potion['slot_index'] == 1 for potion in player['potions'])
        assert restored.import_combat_snapshot(envelope, 'after_potion')['success']
        assert restored.restore_combat_snapshot('after_potion').get('type') != 'error'
        recaptured = restored.capture_combat_snapshot('roundtrip')
        assert recaptured.get('success'), recaptured.get('message')
        restored_payload = json.loads(restored.export_combat_snapshot('roundtrip')['snapshot_json'])
        assert restored_payload['CombatHistoryEntries'] == history
        assert restored_payload['ActivePowerRefs'] == payload['ActivePowerRefs']
        restored_player = json.loads(restored_payload['PlayerJson'])
        assert not any(potion['slot_index'] == 1 for potion in restored_player['potions'])
        assert recaptured['semantic_state_fingerprint'] == captured['semantic_state_fingerprint']
        assert restored.get_search_state()['combat_state_for_search'] == source.get_search_state()['combat_state_for_search']
        assert compare_rng_snapshots(source.get_rng_snapshot(), restored.get_rng_snapshot()).passed
        for row in report['actions']:
            if not 67 <= row['sequence'] <= 70:
                continue
            for engine in (source, restored):
                state = engine.action(row['headless_action'], row['headless_args'], timeout_s=30)
                assert state.get('type') != 'error', (row['sequence'], state.get('message'))
                assert compare_rng_snapshots(row['client_rng_after'], engine.get_rng_snapshot()).passed
            direct_state = source.get_search_state()['combat_state_for_search']
            restored_state = restored.get_search_state()['combat_state_for_search']
            assert restored_state == direct_state, (row['sequence'],
                                                    _first_diff(direct_state, restored_state))
    finally:
        source.stop()
        restored.stop()
