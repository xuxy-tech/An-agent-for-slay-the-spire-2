import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from controller.combat_snapshot import (CombatValidationSet, capture_client_combat_snapshot,
                                        snapshot_index, _runtime_roundtrip_differences)
from controller.human_capture import validate_authoritative_snapshot
from test_human_capture import snapshot_bundle, state
from test_run_lifecycle import manager


def test_runtime_roundtrip_accepts_sparse_false_but_rejects_real_reference_change():
    original = {'CombatHistoryEntries': [{'Kind': 'object', 'Fields': [
        {'Value': {'Kind': 'creature', 'IsPlayerCreature': True}},
        {'Value': {'Kind': 'scalar', 'ScalarJson': '1'}}]}],
        'ActivePowerRefs': [{'Applier': {'Kind': 'null'},
                             'Target': {'Kind': 'creature', 'IsPlayerCreature': True}}]}
    roundtrip = {'CombatHistoryEntries': [{'Kind': 'object', 'IsPlayerCreature': False,
                                          'Fields': [
        {'Value': {'Kind': 'creature', 'IsPlayerCreature': True}},
        {'Value': {'Kind': 'scalar', 'ScalarJson': '1', 'IsPlayerCreature': False}}]}],
        'ActivePowerRefs': [{'Applier': {'Kind': 'null', 'IsPlayerCreature': False},
                             'Target': {'Kind': 'creature', 'IsPlayerCreature': True}}]}
    assert _runtime_roundtrip_differences(original, roundtrip) == []
    roundtrip['ActivePowerRefs'][0]['Target']['IsPlayerCreature'] = False
    assert _runtime_roundtrip_differences(original, roundtrip) == ['ActivePowerRefs']
    roundtrip['ActivePowerRefs'][0]['Target']['IsPlayerCreature'] = True
    roundtrip['CombatHistoryEntries'][0]['Fields'][1]['Value']['ScalarJson'] = '2'
    assert _runtime_roundtrip_differences(original, roundtrip) == ['CombatHistoryEntries']


def rng(counter=0):
    return {'schema_version': 1, 'complete': True, 'players': [],
            'run_streams': {'card': dict(counter=counter, seed=1, s0=1, s1=2, s2=3, s3=4)}}


class Mod:
    def __init__(self, changed=False, corrupt=False):
        self.observations = 0
        self.changed = changed
        self.corrupt = corrupt

    def state(self):
        self.observations += 1
        return {**state(2 if self.changed and self.observations > 1 else 3),
                'run_id': 'test-seed', 'run': {'act_id': 0, 'floor': 2}}

    def rng_snapshot(self):
        return rng()

    def current_combat_snapshot_raw(self):
        _, payload = snapshot_bundle()
        if self.corrupt:
            payload['sha256'] = 'wrong'
        return payload


@pytest.fixture(autouse=True)
def fixture_client_checkpoint(monkeypatch):
    import controller.engine_parity as parity
    monkeypatch.setattr(parity, 'client_combat_checkpoint',
                        lambda *_: {'run': {'floor': 2, 'boss': 'FIXTURE_BOSS'}})


def test_standalone_authoritative_validation_still_checks_digest():
    metadata, payload = snapshot_bundle()
    assert validate_authoritative_snapshot(payload)[2] == metadata['sha256']
    with pytest.raises(ValueError, match='digest'):
        validate_authoritative_snapshot({**payload, 'sha256': 'bad'})
    with pytest.raises(ValueError, match='schema'):
        validate_authoritative_snapshot(payload, {})
    old = json.loads(payload['snapshot_json'])
    del old['CombatHistoryEntries']
    old_raw = json.dumps(old)
    old_payload = {**payload, 'snapshot_json': old_raw,
                   'sha256': hashlib.sha256(old_raw.encode()).hexdigest(),
                   'bytes': len(old_raw.encode())}
    with pytest.raises(ValueError, match='CombatHistoryEntries'):
        validate_authoritative_snapshot(old_payload)


@pytest.mark.parametrize('changed,corrupt', [(True, False), (False, True)])
def test_unstable_or_corrupt_capture_preserves_evidence_without_restore(tmp_path, changed, corrupt):
    report = capture_client_combat_snapshot(Mod(changed, corrupt), tmp_path,
        cli_config=SimpleNamespace(repo_root=tmp_path, dll_relpath=Path('absent.dll')))
    assert report['status'] == 'CAPTURE_FAILED'
    assert report['reusable'] is False
    assert (Path(report['artifact_dir']) / 'raw_snapshot.json').is_file()


def test_missing_runtime_is_not_reusable_and_captures_are_unique(tmp_path):
    config = SimpleNamespace(repo_root=tmp_path, dll_relpath=Path('absent.dll'))
    reports = [capture_client_combat_snapshot(Mod(), tmp_path / 'combat_snapshots', cli_config=config)
               for _ in range(2)]
    assert reports[0]['snapshot_id'] != reports[1]['snapshot_id']
    assert len(snapshot_index(tmp_path)) == 2
    for report in reports:
        assert report['status'] == 'VALIDATION_FAILED'
        assert report['reusable'] is False
        raw = (Path(report['artifact_dir']) / 'search_snapshot.json').read_bytes()
        assert hashlib.sha256(raw).hexdigest() == report['search_sha256']


@pytest.mark.parametrize('parity_status,rng_mismatch,runtime_mismatch,expected', [
    ('PASS', False, False, 'RESTORE_VERIFIED'),
    ('FAIL', False, False, 'VALIDATION_FAILED'),
    ('PASS', True, False, 'VALIDATION_FAILED'),
    ('PASS', False, True, 'VALIDATION_FAILED')])
def test_independent_verifier_requires_both_fields_and_rng(tmp_path, monkeypatch,
                                                         parity_status, rng_mismatch,
                                                         runtime_mismatch, expected):
    import cli.sts2_cli_adapter as adapter
    import controller.engine_parity as parity
    stopped = []

    class Verifier:
        def __init__(self, config): pass
        def start(self): pass
        def import_combat_snapshot(self, raw, *args, **kwargs):
            self.raw = raw
            return {'success': True}
        def restore_combat_snapshot(self, *args, **kwargs): return {'decision': 'combat_play'}
        def capture_combat_snapshot(self, *args, **kwargs): return {'success': True}
        def export_combat_snapshot(self, *args, **kwargs):
            restored = json.loads(self.raw)
            if runtime_mismatch:
                restored['CombatHistoryEntries'] = [{'Kind': 'object'}]
            return {'success': True, 'snapshot_json': json.dumps(restored)}
        def get_search_state(self, **kwargs): return {'combat_state_for_search': {'combat': {'turn': 2}}}
        def send(self, payload, **kwargs): return {'success': True, 'rng': rng(1 if rng_mismatch else 0)}
        def stop(self): stopped.append(True)

    monkeypatch.setattr(adapter, 'Sts2CliAdapter', Verifier)
    monkeypatch.setattr(parity, 'client_combat_checkpoint',
                        lambda *a: {'run': {'floor': 2, 'boss': 'FIXTURE_BOSS'}})
    monkeypatch.setattr(parity, 'headless_combat_checkpoint', lambda *a: {})
    monkeypatch.setattr(parity, 'compare_checkpoints', lambda *a:
                        SimpleNamespace(status=parity_status, differences=[]))
    (tmp_path / 'fake.dll').write_bytes(b'fixture')
    report = capture_client_combat_snapshot(Mod(), tmp_path / 'combat_snapshots',
        cli_config=SimpleNamespace(repo_root=tmp_path, dll_relpath=Path('fake.dll')))
    assert report['status'] == expected
    assert report['reusable'] is (expected == 'RESTORE_VERIFIED')
    assert stopped == [True]


@pytest.mark.parametrize('lose_supported_potion', [False, True])
def test_sanitized_potion_does_not_mask_raw_restore_mismatch(tmp_path, monkeypatch,
                                                              lose_supported_potion):
    import cli.sts2_cli_adapter as adapter
    import controller.engine_parity as parity

    class PotionMod(Mod):
        def current_combat_snapshot_raw(self):
            _, payload = snapshot_bundle()
            snapshot = json.loads(payload['snapshot_json'])
            player = json.loads(snapshot['PlayerJson'])
            player['potions'] = [
                {'id': {'Entry': name}}
                for name in ('COLORLESS_POTION', 'VULNERABLE_POTION')
            ]
            snapshot['PlayerJson'] = json.dumps(player)
            raw = json.dumps(snapshot)
            payload.update(snapshot_json=raw, sha256=hashlib.sha256(raw.encode()).hexdigest(),
                           bytes=len(raw.encode()))
            return payload

    starts = []
    class Verifier:
        def __init__(self, config): self.potions = []
        def start(self): starts.append(True)
        def import_combat_snapshot(self, raw, *args, **kwargs):
            self.raw = raw
            player = json.loads(json.loads(raw)['PlayerJson'])
            self.potions = [item['id']['Entry'] for item in player['potions']]
            return {'success': True}
        def restore_combat_snapshot(self, *args, **kwargs): return {'decision': 'combat_play'}
        def capture_combat_snapshot(self, *args, **kwargs): return {'success': True}
        def export_combat_snapshot(self, *args, **kwargs):
            return {'success': True, 'snapshot_json': self.raw}
        def get_search_state(self, **kwargs):
            potions = list(self.potions)
            if lose_supported_potion:
                potions = [name for name in potions if name != 'VULNERABLE_POTION']
            return {'combat_state_for_search': {'potions': potions}}
        def send(self, *args, **kwargs): return {'success': True, 'rng': rng()}
        def stop(self): pass

    monkeypatch.setattr(adapter, 'Sts2CliAdapter', Verifier)
    monkeypatch.setattr(parity, 'client_combat_checkpoint', lambda *_: {
        'run': {'floor': 2, 'boss': 'FIXTURE_BOSS',
                'potions': ['COLORLESS_POTION', 'VULNERABLE_POTION']}})
    monkeypatch.setattr(parity, 'headless_combat_checkpoint',
                        lambda _restored, search, _run_id: {
                            'run': {'floor': 2, 'boss': 'FIXTURE_BOSS',
                                    'potions': search['potions']}})
    monkeypatch.setattr(parity, 'compare_checkpoints', lambda expected, actual:
                        SimpleNamespace(status='PASS' if expected == actual else 'FAIL',
                                        differences=[] if expected == actual else [{'path': 'run.potions'}]))
    (tmp_path / 'fake.dll').write_bytes(b'fixture')
    report = capture_client_combat_snapshot(PotionMod(), tmp_path / 'captures',
        cli_config=SimpleNamespace(repo_root=tmp_path, dll_relpath=Path('fake.dll')))
    artifact = Path(report['artifact_dir'])
    assert report['sanitize']['removed_potion_ids'] == ['COLORLESS_POTION']
    assert [x['id']['Entry'] for x in json.loads(json.loads(
        (artifact / 'restore_snapshot.json').read_text(encoding='utf-8'))['PlayerJson'])['potions']] == [
            'COLORLESS_POTION', 'VULNERABLE_POTION']
    assert [x['id']['Entry'] for x in json.loads(json.loads(
        (artifact / 'search_snapshot.json').read_text(encoding='utf-8'))['PlayerJson'])['potions']] == [
            'VULNERABLE_POTION']
    if lose_supported_potion:
        assert report['status'] == 'VALIDATION_FAILED'
        assert report['validation']['raw']['fields_status'] == 'FAIL'
        assert len(starts) == 1
    else:
        assert report['status'] == 'RESTORE_VERIFIED'
        assert report['validation']['raw']['fields_status'] == 'PASS'
        assert report['validation']['search']['fields_status'] == 'PASS'
        assert len(starts) == 2


def test_old_metadata_cannot_claim_independent_validation(tmp_path):
    directory = tmp_path / 'session' / 'combat_snapshots' / 'root'
    directory.mkdir(parents=True)
    (directory / 'metadata.json').write_text(json.dumps({'status': 'REUSABLE', 'reusable': True}), encoding='utf-8')
    assert snapshot_index(tmp_path, include_legacy=True)[0]['status'] == 'LEGACY_UNVERIFIED'
    assert snapshot_index(tmp_path, include_legacy=True)[0]['reusable'] is False


def test_profile_rejects_modified_artifact_before_launch(tmp_path):
    from scripts.profile_search_snapshot import run
    (tmp_path / 'metadata.json').write_text(json.dumps({
        'schema': 'sts2.combat_snapshot.v2', 'status': 'RESTORE_VERIFIED',
        'search_sha256': 'not-the-file-hash'}), encoding='utf-8')
    (tmp_path / 'search_snapshot.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='digest'):
        run(SimpleNamespace(snapshot=tmp_path, allow_unverified=False))


def test_profile_rejects_stale_runtime_validation(tmp_path):
    from scripts.profile_search_snapshot import run
    (tmp_path / 'metadata.json').write_text(json.dumps({
        'schema': 'sts2.combat_snapshot.v2', 'status': 'RESTORE_VERIFIED',
        'compatibility': {'headless': 'old'}}), encoding='utf-8')
    (tmp_path / 'search_snapshot.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='stale'):
        run(SimpleNamespace(snapshot=tmp_path, allow_unverified=False,
                            dll=Path('third_party/sts2-cli/src/Sts2Headless/bin/Release/net9.0/Sts2Headless.dll')))


def test_dashboard_requires_pause_ack_and_releases_capture_guard(manager, monkeypatch):
    import scripts.live_dashboard as dashboard
    from controller.live_session import write_json
    manager.process = SimpleNamespace(poll=lambda: None)
    manager.control_file = manager.log_root / 'control.json'
    manager.status_file = manager.log_root / 'status.json'
    write_json(manager.control_file, {'desired': 'paused'})
    write_json(manager.status_file, {'mode': 'running'})
    with pytest.raises(ValueError, match='paused acknowledgement'):
        manager.command('capture_combat_snapshot', {})
    write_json(manager.status_file, {'mode': 'paused', 'updated_at': 20})
    manager.command_time = 30
    with pytest.raises(ValueError, match='acknowledge pause'):
        manager.command('capture_combat_snapshot', {})
    manager.command_time = 10
    def capture(*args, **kwargs):
        assert manager._snapshot_busy
        raise OSError('disk error')
    monkeypatch.setattr(dashboard, 'capture_client_combat_snapshot', capture)
    with pytest.raises(OSError, match='disk error'):
        manager.command('capture_combat_snapshot', {})
    assert manager._snapshot_busy is False


def test_profile_passes_beam_settings_and_reports_input_digest(tmp_path, monkeypatch):
    import scripts.profile_search_snapshot as profile
    snapshot = tmp_path / 'root.json'
    snapshot.write_text('{"Seed":"fixture"}', encoding='utf-8')
    dll = tmp_path / 'fake.dll'
    dll.write_bytes(b'fixture')
    received = {}
    class Searcher:
        strict_dag_enabled = True
        def __init__(self, *args, **kwargs): received.update(kwargs)
        def search_from_history(self, history, **kwargs):
            received.update(kwargs)
            return SimpleNamespace(score=1, sequence=[], leaf_state={}, stats={'fixture': True})
        def close(self): received['closed'] = True
        def timing_summary(self): return {}
        def _coverage_snapshot(self): return {}
    monkeypatch.setattr(profile, 'CombatSearcher', Searcher)
    args = SimpleNamespace(snapshot=snapshot, dll=dll, no_worker_pool=True, workers=1,
        parallel=False, character='Ironclad', lang='en', parallel_frontier=False,
        score_mode='preference', max_search_ms=20000, expand_potions=False,
        search_mode='beam', beam_width=16, dominance=False, depth=8, chance_depth=1,
        include_coverage_keys=False)
    report = profile.run(args)
    assert received['search_mode'] == 'beam'
    assert received['beam_width'] == 16
    assert received['beam_dominance'] is False
    assert received['depth'] == 8 and received['closed']
    assert report['snapshot_sha256'] == hashlib.sha256(snapshot.read_bytes()).hexdigest()


def test_validation_set_prefers_new_encounters_and_stops_at_capacity(tmp_path, monkeypatch):
    import controller.combat_snapshot as snapshots
    config = SimpleNamespace(repo_root=tmp_path, dll_relpath=Path('fake.dll'))
    collector = CombatValidationSet(tmp_path / 'combat_validation_set', cli_config=config, limit=2)
    assert collector.wants(act=1, floor=2, encounter='A', room_type='Monster', turn=1)
    def capture(_mod, directory, *, cli_config, context):
        artifact = directory / context['encounter_id']
        artifact.mkdir()
        report = {'schema': 'sts2.combat_snapshot.v2', 'snapshot_id': artifact.name,
                  'artifact_dir': str(artifact), 'context': context,
                  'status': 'RESTORE_VERIFIED', 'reusable': True}
        snapshots._write_json(artifact / 'metadata.json', report)
        return report
    monkeypatch.setattr(snapshots, 'capture_client_combat_snapshot', capture)
    client = {'run_id': 'seed', 'turn': 1, 'run': {'act_id': 0, 'floor': 2}}
    assert collector.capture(object(), client_state=client, encounter='A',
                             room_type='Monster', session_dir=tmp_path)['reusable']
    assert collector.capture(object(), client_state=client, encounter='B',
                             room_type='Elite', session_dir=tmp_path)['reusable']
    assert collector.summary()['valid'] == 2
    assert not collector.wants(act=2, floor=19, encounter='C', room_type='Boss', turn=1)


def test_validation_set_rechecks_stale_and_retires_failed_root(tmp_path, monkeypatch):
    import controller.combat_snapshot as snapshots
    config = SimpleNamespace(repo_root=tmp_path, dll_relpath=Path('fake.dll'))
    root = tmp_path / 'combat_validation_set'
    artifact = root / 'old'
    artifact.mkdir(parents=True)
    snapshots._write_json(artifact / 'metadata.json', {
        'schema': 'sts2.combat_snapshot.v2', 'snapshot_id': 'old',
        'context': {'source': 'automatic_validation_set'},
        'status': 'RESTORE_VERIFIED', 'reusable': True,
        'compatibility': {'contract': snapshots.VALIDATION_CONTRACT, 'headless': 'old'}})
    calls = []
    def revalidate(path, *, cli_config):
        calls.append(path)
        return {'snapshot_id': 'old', 'context': {'source': 'automatic_validation_set'},
                'status': 'VALIDATION_FAILED', 'error': 'native restore failed'}
    monkeypatch.setattr(snapshots, 'revalidate_saved_combat_snapshot', revalidate)
    collector = CombatValidationSet(root, cli_config=config)
    assert calls == [artifact]
    assert collector.summary()['valid'] == 0
    assert not artifact.exists()
    assert 'native restore failed' in (root / 'rejections.jsonl').read_text(encoding='utf-8')
    import zipfile
    with zipfile.ZipFile(root / 'rejected_archive' / 'old.zip') as archive:
        assert archive.testzip() is None
        assert json.loads(archive.read('metadata.json'))['snapshot_id'] == 'old'


def test_previous_contract_is_preserved_but_does_not_block_recollection(tmp_path):
    import controller.combat_snapshot as snapshots
    config = SimpleNamespace(repo_root=tmp_path, dll_relpath=Path('fake.dll'))
    root = tmp_path / 'combat_validation_set'
    artifact = root / 'old'
    snapshots._write_json(artifact / 'metadata.json', {
        'schema': 'sts2.combat_snapshot.v2', 'snapshot_id': 'old',
        'context': {'source': 'automatic_validation_set', 'act': 1, 'encounter_id': 'OLD'},
        'status': 'RESTORE_VERIFIED', 'reusable': True,
        'compatibility': {'contract': 3}})
    collector = CombatValidationSet(root, cli_config=config)
    assert artifact.exists()
    assert collector.summary()['valid'] == 0
    assert collector.wants(act=1, floor=2, encounter='NEW', room_type='Monster', turn=1)
    assert json.loads((artifact / 'metadata.json').read_text(encoding='utf-8'))['status'] == 'LEGACY_UNVERIFIED'


def test_validation_set_reserves_space_for_later_acts(tmp_path):
    config = SimpleNamespace(repo_root=tmp_path, dll_relpath=Path('fake.dll'))
    collector = CombatValidationSet(tmp_path / 'combat_validation_set', cli_config=config)
    collector.rows = [{'context': {'act': 1, 'encounter_id': f'FIRST_{i}',
                                   'floor_band': str(i // 8), 'room_type': 'Monster'}}
                      for i in range(24)]
    assert not collector.wants(act=1, floor=14, encounter='NEW_FIRST',
                               room_type='Elite', turn=1)
    assert collector.wants(act=2, floor=18, encounter='NEW_SECOND',
                           room_type='Monster', turn=1)


def test_validation_set_keeps_excess_valid_samples_as_reserve(tmp_path):
    import controller.combat_snapshot as snapshots
    config = SimpleNamespace(repo_root=tmp_path, dll_relpath=Path('fake.dll'))
    root = tmp_path / 'combat_validation_set'
    compatibility = snapshots.snapshot_compatibility(config)
    for index in range(26):
        artifact = root / f'{index:03d}'
        snapshots._write_json(artifact / 'metadata.json', {
            'schema': 'sts2.combat_snapshot.v2', 'snapshot_id': artifact.name,
            'context': {'source': 'automatic_validation_set', 'act': 1,
                        'encounter_id': f'FIRST_{index}'},
            'status': 'RESTORE_VERIFIED', 'reusable': True,
            'compatibility': compatibility})
    collector = CombatValidationSet(root, cli_config=config)
    assert collector.summary()['valid'] == 24
    assert collector.summary()['reserve'] == 2
    assert not collector.wants(act=1, floor=8, encounter='ANOTHER_FIRST',
                               room_type='Monster', turn=1)
    assert collector.wants(act=2, floor=19, encounter='SECOND',
                           room_type='Monster', turn=1)


def test_stale_samples_reserve_their_act_quota(tmp_path, monkeypatch):
    import controller.combat_snapshot as snapshots
    config = SimpleNamespace(repo_root=tmp_path, dll_relpath=Path('fake.dll'))
    root = tmp_path / 'combat_validation_set'
    for index in range(24):
        artifact = root / f'{index:03d}'
        snapshots._write_json(artifact / 'metadata.json', {
            'schema': 'sts2.combat_snapshot.v2', 'snapshot_id': artifact.name,
            'context': {'source': 'automatic_validation_set', 'act': 1,
                        'encounter_id': f'FIRST_{index}'},
            'status': 'RESTORE_VERIFIED', 'reusable': True,
            'compatibility': {'contract': snapshots.VALIDATION_CONTRACT}})
    def revalidate(path, *, cli_config):
        row = json.loads((path / 'metadata.json').read_text(encoding='utf-8'))
        row['compatibility'] = snapshots.snapshot_compatibility(cli_config)
        return row
    monkeypatch.setattr(snapshots, 'revalidate_saved_combat_snapshot', revalidate)
    collector = CombatValidationSet(root, cli_config=config)
    assert collector.summary()['valid'] == 2
    assert collector.summary()['pending_stale'] == 22
    assert not collector.wants(act=1, floor=8, encounter='ANOTHER_FIRST',
                               room_type='Monster', turn=1)
    assert collector.wants(act=2, floor=19, encounter='SECOND',
                           room_type='Monster', turn=1)
