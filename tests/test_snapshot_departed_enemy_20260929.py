"""Action 304: a departed enemy remains in combat history after its power fires."""
import json
from pathlib import Path

import pytest

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.rng_parity import compare_rng_snapshots


ROOT = Path(__file__).resolve().parents[1]
SESSION = ROOT / 'logs/live_dashboard/20260929_094651_954494'
RAW = SESSION / 'combat_snapshots/combat_10_turn_1/raw_snapshot.json'


@pytest.mark.skipif(not RAW.is_file(), reason='Original action 304 anchor is unavailable')
def test_departed_enemy_history_roundtrip_and_next_turn():
    report = json.loads((SESSION / 'run_report.json').read_text(encoding='utf-8'))
    source, restored = (Sts2CliAdapter(CliConfig(ROOT)) for _ in range(2))
    source.start()
    restored.start()
    try:
        assert source.import_combat_snapshot(RAW.read_text(encoding='utf-8'), 'root')['success']
        initial_restore = source.restore_combat_snapshot('root')
        assert initial_restore.get('type') != 'error', initial_restore.get('message')
        for row in report['actions']:
            if row['sequence'] < 294:
                continue
            state = source.action(row['headless_action'], row['headless_args'], timeout_s=30)
            assert state.get('type') != 'error', (row['sequence'], state)
            parity = compare_rng_snapshots(row['client_rng_after'], source.get_rng_snapshot())
            assert parity.passed, (row['sequence'], parity.differences[:5])
        captured = source.capture_combat_snapshot('after_304')
        assert captured.get('success'), captured.get('message')
        envelope = source.export_combat_snapshot('after_304')['snapshot_json']
        original_payload = json.loads(envelope)
        history = original_payload['CombatHistoryEntries']
        def walk(value):
            if isinstance(value, dict):
                yield value
                for child in value.values():
                    yield from walk(child)
            elif isinstance(value, list):
                for child in value:
                    yield from walk(child)
        assert any(item.get('Kind') == 'historical_creature' for item in walk(history))
        assert restored.import_combat_snapshot(envelope, 'after_304')['success']
        restored_result = restored.restore_combat_snapshot('after_304')
        assert restored_result.get('type') != 'error', restored_result.get('message')
        recaptured = restored.capture_combat_snapshot('roundtrip')
        assert recaptured.get('success'), recaptured
        restored_payload = json.loads(restored.export_combat_snapshot('roundtrip')['snapshot_json'])
        restored_history = restored_payload['CombatHistoryEntries']
        def first_diff(a, b, path='history'):
            if type(a) is not type(b):
                return path, a, b
            if isinstance(a, dict):
                for key in a.keys() | b.keys():
                    if key not in a or key not in b:
                        return path + '.' + key, a.get(key), b.get(key)
                    diff = first_diff(a[key], b[key], path + '.' + key)
                    if diff:
                        return diff
            elif isinstance(a, list):
                if len(a) != len(b):
                    return path + '.length', len(a), len(b)
                for index, (left, right) in enumerate(zip(a, b)):
                    diff = first_diff(left, right, path + f'[{index}]')
                    if diff:
                        return diff
            elif isinstance(a, str) and a.startswith('{') and isinstance(b, str) and b.startswith('{'):
                try:
                    return first_diff(json.loads(a), json.loads(b), path + '.decoded')
                except json.JSONDecodeError:
                    pass
            elif a != b:
                return path, a, b
            return None
        assert restored_history == history, first_diff(history, restored_history)
        assert restored_payload['HookStates'] == original_payload['HookStates'], first_diff(
            original_payload['HookStates'], restored_payload['HookStates'])
        assert recaptured['semantic_state_fingerprint'] == captured['semantic_state_fingerprint']
        assert compare_rng_snapshots(source.get_rng_snapshot(), restored.get_rng_snapshot()).passed
        assert restored_payload['ActivePowerRefs'] == original_payload['ActivePowerRefs']
        direct_search = source.get_search_state()['combat_state_for_search']
        restored_search = restored.get_search_state()['combat_state_for_search']
        assert direct_search == restored_search, first_diff(direct_search, restored_search)
        for engine in (source, restored):
            state = engine.action('end_turn', {}, timeout_s=30)
            assert state.get('type') != 'error', state
        direct_search = source.get_search_state()['combat_state_for_search']
        restored_search = restored.get_search_state()['combat_state_for_search']
        post_parity = compare_rng_snapshots(source.get_rng_snapshot(), restored.get_rng_snapshot())
        assert direct_search == restored_search, (first_diff(direct_search, restored_search),
                                                  direct_search['combat']['player'],
                                                  restored_search['combat']['player'],
                                                  direct_search['combat']['enemies'],
                                                  restored_search['combat']['enemies'],
                                                  post_parity.differences[:5])
        assert post_parity.passed
    finally:
        source.stop()
        restored.stop()
