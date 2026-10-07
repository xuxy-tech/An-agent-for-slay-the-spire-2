import json
import os
import time

from scripts import test_workspace


def test_prune_only_expired_test_outputs_and_preserves_active_and_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(test_workspace, 'ROOT', tmp_path)
    monkeypatch.setattr(test_workspace, 'OUTPUT_ROOT', tmp_path / 'artifacts' / 'test_runs')
    managed = test_workspace.OUTPUT_ROOT / '20260101T000000Z-abcdef'
    active = test_workspace.OUTPUT_ROOT / '20260101T000001Z-abcdef'
    legacy = tmp_path / '.pytest_old'
    fixed = tmp_path / 'data' / 'combat_regressions' / 'case'
    snapshot = tmp_path / 'logs' / 'live_dashboard' / 'combat_validation_set' / 'sample'
    for path in (managed, active, legacy, fixed, snapshot):
        path.mkdir(parents=True)
    (managed / 'manifest.json').write_text(json.dumps({'status': 'passed'}), encoding='utf-8')
    (active / 'manifest.json').write_text(json.dumps({'status': 'running'}), encoding='utf-8')
    past = time.time() - 9 * 86400
    for path in (managed, active, legacy, fixed, snapshot):
        os.utime(path, (past, past))

    eligible, removed, failed = test_workspace.prune(7, apply=False)
    assert (eligible, removed, failed) == (2, 0, 0)
    assert managed.exists() and legacy.exists()

    eligible, removed, failed = test_workspace.prune(7, apply=True)
    assert (eligible, removed, failed) == (2, 2, 0)
    assert not managed.exists() and not legacy.exists()
    assert active.exists() and fixed.exists() and snapshot.exists()
