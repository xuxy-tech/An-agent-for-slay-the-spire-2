import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import threading
from types import SimpleNamespace
import pytest

from controller.combat_scoring import CombatScoring, active_model, stage_for_floor
from controller.snapshot_evidence import read_evidence, seal_evidence, seal_session_snapshots
from scripts.archive_live_dashboard_logs import archive_session
from scripts.verify_combat_replay_gate import _digest, _recorded_config, compare_history
from scripts.live_dashboard import DashboardManager


def _fixture(tmp_path):
    session = tmp_path / 'logs' / 'live_dashboard' / '20261003_010203_123456'
    snapshot = session.parent / 'combat_validation_set' / 'snapshot_one'
    session.mkdir(parents=True)
    snapshot.mkdir(parents=True)
    model = active_model()
    scorer = CombatScoring(stage_for_floor(12), model).identity
    stamp = datetime(2026, 10, 3, 1, 2, 3, tzinfo=timezone.utc)
    metadata = {
        'schema': 'sts2.combat_snapshot.v2', 'snapshot_id': snapshot.name,
        'artifact_dir': str(snapshot), 'status': 'RESTORE_VERIFIED', 'reusable': True,
        'created_at_utc': stamp.timestamp(),
        'context': {'session_dir': str(session), 'combat_number': 2, 'floor': 12},
        'source_sha256': {name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
                          for name in ('controller/search/combat_search.py', 'controller/run_agent.py')},
    }
    (snapshot / 'metadata.json').write_text(json.dumps(metadata), encoding='utf-8')
    report = {
        'status': 'DEFEAT', 'config': {'depth': 12, 'chance_depth': 1, 'search_budget_ms': 20000},
        'combats': [{'combat_number': 2, 'status': 'COMPLETED'}],
        'actions': [{
            'decision': 'combat.play_card', 'timestamp_utc': (stamp + timedelta(seconds=1)).isoformat(),
            'headless_action': 'play_card', 'headless_args': {'card_index': 2, 'target_index': 0},
            'decision_telemetry': {'combat_number': 2, 'reused_plan': False,
                                   'worker_runtime': {'active_workers': 3},
                                   'chosen': {'action_type': 'play_card', 'metadata': {'card_id': 'BASH'}},
                                   'score_explanation': {'scorer': scorer}},
            'shadow_rng_before': {'counter': 1}, 'shadow_rng_after': {'counter': 2},
        }],
    }
    (session / 'run_report.json').write_text(json.dumps(report), encoding='utf-8')
    (session / 'scoring_model.json').write_text(json.dumps(model), encoding='utf-8')
    (session / 'deck_profile.json').write_text(json.dumps({'combat_coefficients': {}}), encoding='utf-8')
    return session, snapshot, metadata, report, model, scorer


def _replay():
    return {'status': 'COMPLETED', 'actions': [{
        'action': 'play_card', 'payload': {'card_index': 2, 'target_index': 0},
        'chosen': {'action_type': 'play_card', 'metadata': {'card_id': 'BASH'}},
        'rng_before_sha256': _digest({'counter': 1}),
        'rng_after_sha256': _digest({'counter': 2}),
    }]}


def test_sealed_evidence_keeps_history_gate_after_report_removal(tmp_path):
    session, snapshot, metadata, report, model, scorer = _fixture(tmp_path)
    assert seal_evidence(snapshot, report, model, {})
    (session / 'run_report.json').unlink()
    config, workers = _recorded_config(metadata)
    assert config['depth'] == 12 and workers == 3
    result = compare_history(metadata, _replay(), scorer,
                             {'depth': 12, 'chance_depth': 1, 'max_search_ms': 20000, 'workers': 3})
    assert result['status'] == 'PASS'
    path = snapshot / 'live_combat_evidence.json'
    evidence = json.loads(path.read_text(encoding='utf-8'))
    evidence['payload']['actions'][0]['headless_action'] = 'end_turn'
    path.write_text(json.dumps(evidence), encoding='utf-8')
    assert compare_history(metadata, _replay(), scorer,
                           {'depth': 12, 'chance_depth': 1, 'max_search_ms': 20000})['reason'] == 'live_evidence_corrupt'


def test_archive_backfills_evidence_and_keeps_full_history_readable(tmp_path):
    session, snapshot, metadata, report, model, scorer = _fixture(tmp_path)
    archive_dir = session.parent / 'archive'
    archive_dir.mkdir()
    archived = archive_session(session, archive_dir)
    assert archived['session'] == session.name
    assert not session.exists()
    assert read_evidence(metadata)['model'] == model
    import zipfile
    with zipfile.ZipFile(archive_dir / f'{session.name}.zip') as bundle:
        assert json.loads(bundle.read('run_report.json')) == report


def test_archive_keeps_unsealed_combat_report_in_zip(tmp_path):
    session, snapshot, metadata, report, model, scorer = _fixture(tmp_path)
    report['combats'][0]['status'] = 'RUNNING'
    (session / 'run_report.json').write_text(json.dumps(report), encoding='utf-8')
    archive_dir = session.parent / 'archive'
    archive_dir.mkdir()
    result = archive_session(session, archive_dir)
    assert result['incomplete_snapshot_evidence'] == 1
    assert not session.exists()
    from controller.snapshot_evidence import read_original_report
    assert read_original_report(metadata) == report


def test_dashboard_archived_history_remains_browsable_and_deletable(tmp_path, monkeypatch):
    session, snapshot, metadata, report, model, scorer = _fixture(tmp_path)
    monkeypatch.setattr(threading.Thread, 'start', lambda self: None)
    manager = DashboardManager(tmp_path, session.parent, 'http://localhost:9999')
    manager.mod = manager.observer = SimpleNamespace(state=lambda: {'screen': 'MAIN_MENU'})
    session_id = manager.session_id(session)
    manager.archive_session(session_id)
    archive_id = f'live_dashboard/archive/{session.name}'
    row = manager._history_row(manager.archive_path(archive_id))
    assert row['id'] == archive_id and row['archived']
    assert json.loads(manager._archived_file(manager.archive_path(archive_id), 'run_report.json')) == report
    assert read_evidence(metadata)['actions'][0]['headless_action'] == 'play_card'
    manager.delete_archive(archive_id)
    assert not (manager.log_root / 'archive' / (session.name + '.zip')).exists()
    assert read_evidence(metadata) is not None


def test_dashboard_keeps_archive_when_snapshot_has_no_sealed_evidence(tmp_path, monkeypatch):
    session, snapshot, metadata, report, model, scorer = _fixture(tmp_path)
    report['combats'][0]['status'] = 'RUNNING'
    (session / 'run_report.json').write_text(json.dumps(report), encoding='utf-8')
    monkeypatch.setattr(threading.Thread, 'start', lambda self: None)
    manager = DashboardManager(tmp_path, session.parent, 'http://localhost:9999')
    manager.mod = manager.observer = SimpleNamespace(state=lambda: {'screen': 'MAIN_MENU'})
    manager.archive_session(manager.session_id(session))
    archive_id = f'live_dashboard/archive/{session.name}'
    with pytest.raises(ValueError, match='唯一战斗记录'):
        manager.delete_archive(archive_id)
    assert manager.archive_path(archive_id).is_file()


def test_dashboard_deletion_seals_snapshot_before_removing_session(tmp_path, monkeypatch):
    session, snapshot, metadata, report, model, scorer = _fixture(tmp_path)
    monkeypatch.setattr(threading.Thread, 'start', lambda self: None)
    manager = DashboardManager(tmp_path, session.parent, 'http://localhost:9999')
    manager.mod = manager.observer = SimpleNamespace(state=lambda: {'screen': 'MAIN_MENU'})
    manager.delete_session(manager.session_id(session))
    assert not session.exists()
    assert read_evidence(metadata)['model'] == model
