import io
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from cli.sts2_cli_adapter import CliConfig
from controller.combat_comparison import ModelLibrary
from controller.combat_snapshot import snapshot_compatibility
from controller.engine_consistency import SCHEMA, gate_path
from controller.headless_run_batch import HeadlessRunBatch
from controller.run_lifecycle import run_summary
from controller.sandbox_features import FEATURE_VERSION


class _FinishedProcess:
    def __init__(self, *args, **kwargs):
        event = {'step_id': 1, 'decision': 'event_choice', 'floor': 1, 'act': 1,
                 'post_state': {'decision': 'map_select', 'floor': 1, 'act': 1},
                 'applied': {'action': 'choose_option', 'payload': {'option_index': 1}}}
        summary = {'run_summary': True, 'outcome': 'game_over', 'last_floor': 12,
                   'max_act': 1, 'final_deck': {'deck_size': 19}}
        self.stdout = io.StringIO(json.dumps(event) + '\n' + json.dumps(summary) + '\n')
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self):
        return self.returncode

    def terminate(self):
        self.returncode = 1


def test_headless_batch_requires_current_gate_and_writes_history(tmp_path):
    root = Path(__file__).resolve().parents[1]
    profile = root / 'logs' / 'live_dashboard' / 'deck_profile.json'
    if not profile.is_file():
        pytest.skip('No live deck profile')
    log_root = tmp_path / 'live_dashboard'
    log_root.mkdir()
    batch = HeadlessRunBatch(root, log_root, ModelLibrary(root), profile)
    with pytest.raises(ValueError):
        batch.start(2, 2, 'active', 2, 1000, 1)
    gate_path(log_root).write_text(json.dumps({
        'schema': SCHEMA, 'status': 'PASS', 'feature_version': FEATURE_VERSION,
        'compatibility': snapshot_compatibility(CliConfig(repo_root=root)),
        'checked_at_utc': time.time(), 'snapshot_count': 1,
    }), encoding='utf-8')
    with patch('controller.headless_run_batch.subprocess.Popen', _FinishedProcess):
        batch.start(2, 2, 'active', 2, 1000, 1)
        batch.thread.join(timeout=10)
    status = batch.status()
    assert status['status'] == 'completed'
    assert status['completed'] == 2
    for row in status['sessions']:
        report = json.loads((log_root / row['id'].split('/')[-1] / 'run_report.json').read_text(encoding='utf-8'))
        history = run_summary(report, row['id'], time.time())
        assert history['status'] == 'DEFEAT'
        assert history['deck_size'] == 19
        assert history['seed']
        assert report['actions'][0]['client_action'] == 'choose_option'
