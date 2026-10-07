"""Native history and continuation regressions for visual attack callbacks."""
import json
from pathlib import Path

import pytest

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.rng_parity import compare_rng_snapshots


ROOT = Path(__file__).resolve().parents[1]
SESSION = ROOT / 'logs/live_dashboard/20260928_120226_287675'
RAW = SESSION / 'combat_snapshots/combat_9_turn_1/raw_snapshot.json'
POTION_SESSION = ROOT / 'logs/live_dashboard/20260928_153558_856765'
POTION_RAW = POTION_SESSION / 'combat_snapshots/combat_4_turn_1/raw_snapshot.json'


def _checked(value):
    assert value.get('type') != 'error', value
    return value


def _capture(cli, name):
    value = _checked(cli.capture_combat_snapshot(name))
    assert value['success'] is True, value
    return value


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
                return path + '.' + key, left.get(key), right.get(key)
            diff = _first_diff(left[key], right[key], path + '.' + key)
            if diff:
                return diff
    elif isinstance(left, list):
        if len(left) != len(right):
            return path + '.length', len(left), len(right)
        for index, (lvalue, rvalue) in enumerate(zip(left, right)):
            diff = _first_diff(lvalue, rvalue, path + f'[{index}]')
            if diff:
                return diff
    elif left != right:
        return path, left, right
    return None


@pytest.mark.skipif(not POTION_RAW.is_file(), reason='Potion live combat snapshot is unavailable')
def test_consumed_potion_history_rebinds_player_without_refilling_slot():
    source = Sts2CliAdapter(CliConfig(ROOT))
    worker = Sts2CliAdapter(CliConfig(ROOT))
    source.start()
    worker.start()
    try:
        _checked(source.import_combat_snapshot(POTION_RAW.read_text(encoding='utf-8'), 'potion_root'))
        assert _checked(source.restore_combat_snapshot('potion_root'))['restore_mode'] == 'full'
        _capture(source, 'before_potion')
        assert _checked(source.action('use_potion', {'potion_index': 2}, timeout_s=30))['decision'] == 'combat_play'
        captured = _capture(source, 'after_potion')
        envelope = source.export_combat_snapshot('after_potion')['snapshot_json']
        snapshot = json.loads(envelope)
        history = snapshot['CombatHistoryEntries']
        values = list(_walk(history))
        assert any(value.get('Kind') == 'player' for value in values)
        assert any(value.get('Kind') == 'object' and 'BloodPotion' in str(value.get('TypeName'))
                   for value in values)
        assert not any(value.get('Kind') == 'potion' and value.get('ModelId') == 'BLOOD_POTION'
                       for value in values)

        _checked(worker.import_combat_snapshot(envelope, 'potion_after'))
        assert _checked(worker.restore_combat_snapshot('potion_after'))['restore_mode'] == 'full'
        recaptured = _capture(worker, 'potion_roundtrip')
        restored_history = json.loads(worker.export_combat_snapshot('potion_roundtrip')['snapshot_json'])[
            'CombatHistoryEntries']
        assert restored_history == history, _first_diff(restored_history, history)
        assert recaptured['semantic_state_fingerprint'] == captured['semantic_state_fingerprint']
        for engine in (source, worker):
            _capture(engine, 'verify_potion_slots')
            player = json.loads(json.loads(engine.export_combat_snapshot('verify_potion_slots')[
                'snapshot_json'])['PlayerJson'])
            assert not any(potion['slot_index'] == 2 for potion in player['potions'])
        assert source.get_search_state()['combat_state_for_search'] == worker.get_search_state()['combat_state_for_search']
        assert compare_rng_snapshots(source.get_rng_snapshot(), worker.get_rng_snapshot()).passed
        for action, args in [('play_card', {'card_index': 1, 'target_index': 0}), ('end_turn', {})]:
            direct = _checked(source.action(action, args, timeout_s=30))
            restored = _checked(worker.action(action, args, timeout_s=30))
            assert direct['decision'] == restored['decision']
            assert source.get_search_state()['combat_state_for_search'] == worker.get_search_state()['combat_state_for_search']
            assert compare_rng_snapshots(source.get_rng_snapshot(), worker.get_rng_snapshot()).passed
    finally:
        source.stop()
        worker.stop()


@pytest.mark.skipif(not RAW.is_file(), reason='Original live combat snapshot is unavailable')
def test_perfected_strike_consumed_vigor_roundtrips_with_history_and_continuation():
    report = json.loads((SESSION / 'run_report.json').read_text(encoding='utf-8'))
    row = next(row for row in report['actions'] if row['sequence'] == 194)
    source = Sts2CliAdapter(CliConfig(ROOT))
    worker = Sts2CliAdapter(CliConfig(ROOT))
    source.start()
    worker.start()
    try:
        _checked(source.import_combat_snapshot(RAW.read_text(encoding='utf-8'), 'tunneler_root'))
        assert _checked(source.restore_combat_snapshot('tunneler_root'))['restore_mode'] == 'full'
        after = _checked(source.action('play_card', {'card_index': 3, 'target_index': 0}))
        assert after['decision'] == 'combat_play'
        assert after['enemies'][0]['hp'] == row['client_after']['combat']['enemies'][0]['current_hp'] == 62
        assert after['energy'] == 1
        assert compare_rng_snapshots(row['client_rng_after'], source.get_rng_snapshot()).passed

        captured = _capture(source, 'tunneler_after_194')
        envelope = json.loads(source.export_combat_snapshot('tunneler_after_194')['snapshot_json'])
        history = envelope['CombatHistoryEntries']
        assert len(history) >= 12
        values = list(_walk(history))
        assert any(value.get('Kind') == 'card_calculated_damage_var'
                   and value.get('ModelId') == 'PERFECTED_STRIKE' for value in values)
        object_ids = {value['ObjectId'] for value in values if value.get('ObjectId') is not None}
        assert any(value.get('Kind') == 'ref' and value.get('RefId') in object_ids
                   for value in values)

        _checked(worker.import_combat_snapshot(
            source.export_combat_snapshot('tunneler_after_194')['snapshot_json'], 'tunneler_after_194'))
        assert _checked(worker.restore_combat_snapshot('tunneler_after_194'))['restore_mode'] == 'full'
        recaptured = _capture(worker, 'tunneler_roundtrip')
        restored_history = json.loads(worker.export_combat_snapshot('tunneler_roundtrip')['snapshot_json'])[
            'CombatHistoryEntries']
        assert restored_history == history
        assert recaptured['semantic_state_fingerprint'] == captured['semantic_state_fingerprint']
        assert source.get_search_state()['combat_state_for_search'] == worker.get_search_state()['combat_state_for_search']
        assert compare_rng_snapshots(source.get_rng_snapshot(), worker.get_rng_snapshot()).passed

        for step, (action, args) in enumerate([
            ('play_card', {'card_index': 0}), ('end_turn', {}),
        ]):
            direct = _checked(source.action(action, args, timeout_s=30))
            restored = _checked(worker.action(action, args, timeout_s=30))
            assert direct['decision'] == restored['decision']
            assert source.get_search_state()['combat_state_for_search'] == worker.get_search_state()['combat_state_for_search']
            assert compare_rng_snapshots(source.get_rng_snapshot(), worker.get_rng_snapshot()).passed
            expected = _capture(source, f'tunneler_direct_{step}')
            actual = _capture(worker, f'tunneler_restored_{step}')
            assert actual['semantic_state_fingerprint'] == expected['semantic_state_fingerprint']
    finally:
        source.stop()
        worker.stop()


def test_other_visual_callback_attack_claw_roundtrips():
    source = Sts2CliAdapter(CliConfig(ROOT))
    worker = Sts2CliAdapter(CliConfig(ROOT))
    source.start()
    worker.start()
    try:
        _checked(source.start_run(seed='VFX-CALLBACK-REGRESSION'))
        _checked(source.set_player(deck=['CLAW', 'DEFEND_IRONCLAD']))
        _checked(source.enter_room('combat', encounter='TUNNELER_WEAK'))
        _checked(source.send({'cmd': 'configure_sandbox', 'energy': 10, 'hand': ['CLAW']}))
        assert _checked(source.action('play_card', {'card_index': 0, 'target_index': 0}))['decision'] == 'combat_play'
        captured = _capture(source, 'claw_vfx')
        _checked(worker.import_combat_snapshot(
            source.export_combat_snapshot('claw_vfx')['snapshot_json'], 'claw_vfx'))
        assert _checked(worker.restore_combat_snapshot('claw_vfx'))['restore_mode'] == 'full'
        recaptured = _capture(worker, 'claw_vfx_roundtrip')
        assert recaptured['semantic_state_fingerprint'] == captured['semantic_state_fingerprint']
        assert source.get_search_state()['combat_state_for_search'] == worker.get_search_state()['combat_state_for_search']
        assert compare_rng_snapshots(source.get_rng_snapshot(), worker.get_rng_snapshot()).passed
    finally:
        source.stop()
        worker.stop()
