import json
import threading
from pathlib import Path

import pytest

from controller.live_session import (
    ManagedControl, SessionJournal, ThrottledReportWriter,
    recover_report_tail, write_json, read_json,
)
from controller.live_client_bridge import JsonlSessionLog


def test_journal_skips_full_report_for_durable_telemetry_but_saves_action_boundaries():
    class Control:
        state = {'mode': 'running'}

        def publish(self, *args, **kwargs):
            pass

    report = {'actions': [], 'parity_checkpoints': []}
    saved = []
    journal = SessionJournal(report, lambda: saved.append(json.loads(json.dumps(report))), Control())
    journal.record({'event': 'interaction_state', 'scene': 'combat'})
    journal.record({'event': 'rng_parity', 'status': 'PASS'})
    journal.record({'event': 'parity_checkpoint', 'status': 'PASS'})
    assert saved == []

    journal.record({'event': 'action_pending', 'sequence': 1, 'status': 'pending'})
    assert saved[-1]['pending_action']['sequence'] == 1
    journal.record({'event': 'action_started', 'sequence': 1, 'status': 'executing'})
    assert saved[-1]['pending_action']['status'] == 'executing'
    assert saved[-1]['pending_action'] is not saved[-1]['actions'][0]
    journal.record({'event': 'live_action', 'sequence': 1, 'status': 'completed'})
    assert saved[-1]['pending_action'] is None
    journal.record({'event': 'shadow_applied', 'sequence': 1})
    assert len(saved) == 4


def test_managed_status_keeps_action_summary_without_full_state(tmp_path):
    control = ManagedControl(None, tmp_path / 'status.json')
    action = {
        'sequence': 7, 'status': 'executing', 'client_action': 'play_card',
        'client_params': {'card_index': 2}, 'screen_before': 'COMBAT',
        'client_before': {'turn': 3, 'run': {'floor': 30}, 'large': 'x' * 100_000},
        'decision_telemetry': {'root_candidates': ['y' * 100_000]},
    }
    control.before_action(action)
    control.publish('running', latest_action=action)
    status = read_json(tmp_path / 'status.json')
    assert status['pending_action']['sequence'] == 7
    assert status['latest_action']['floor'] == 30
    assert status['latest_action']['turn'] == 3
    assert len((tmp_path / 'status.json').read_bytes()) < 1000
    assert action['client_before']['large'] == 'x' * 100_000


def test_compact_lifecycle_log_keeps_full_callback_and_failure_evidence(tmp_path):
    log = JsonlSessionLog(tmp_path / 'session.jsonl', compact_lifecycle=True)
    received = []
    log.on_event = received.append
    action = {
        'sequence': 1, 'status': 'executing', 'client_action': 'play_card',
        'client_params': {'card_index': 0}, 'client_before': {
            'turn': 2, 'run': {'floor': 30}, 'large': 'x' * 100_000,
        },
    }
    log.write({'event': 'action_started', **action})
    log.write({'event': 'action_failed', **action, 'error': 'settlement failed'})
    persisted = [json.loads(line) for line in (tmp_path / 'session.jsonl').read_text(encoding='utf-8').splitlines()]
    assert 'client_before' not in persisted[0]
    assert persisted[0]['floor'] == 30
    assert received[0]['client_before']['large'] == 'x' * 100_000
    assert persisted[1]['client_before']['large'] == 'x' * 100_000


def test_compact_json_preserves_document(tmp_path):
    path = tmp_path / 'run_report.json'
    value = {'actions': [{'text': '药水', 'state': {'turn': 1}}]}
    write_json(path, value, compact=True)
    assert read_json(path) == value
    assert b'\n' not in path.read_bytes()


def test_action_io_probes_are_recorded_without_changing_durable_boundaries(tmp_path):
    report = {'actions': []}
    control = ManagedControl(None, tmp_path / 'status.json')
    report_path = tmp_path / 'run_report.json'
    log = JsonlSessionLog(tmp_path / 'session.jsonl', compact_lifecycle=True)

    def save():
        timing = {}
        write_json(report_path, report, compact=True, timing=timing)
        return timing

    log.on_event = SessionJournal(report, save, control).record
    timing = {}
    action = {
        'sequence': 1, 'status': 'pending', 'client_action': 'play_card',
        'client_before': {'turn': 2, 'run': {'floor': 30}},
        'timing_breakdown': timing,
    }
    log.write({'event': 'action_pending', **action})
    control.before_action(action)
    log.write({'event': 'action_started', **action, 'status': 'executing'})
    log.write({'event': 'action_failed', **action, 'status': 'outcome_unknown', 'error': 'settlement failed'})
    save()

    saved = read_json(report_path)
    entries = saved['actions'][0]['timing_breakdown']['persistence']
    assert [entry['event'] for entry in entries] == ['action_pending', 'action_started', 'action_failed']
    for entry in entries:
        assert entry['report_write_ms'] >= 0
        assert entry['status_publish_ms'] >= 0
        assert entry['session_log_serialize_ms'] >= 0
        assert entry['session_log_append_ms'] >= 0
        assert entry['report_write']['json_safe_ms'] >= 0
        assert entry['report_write']['json_dump_ms'] >= 0
        assert entry['report_write']['replace_ms'] >= 0
        assert entry['report_write']['bytes'] > 0
    assert timing['control_awaiting_publish_ms'] >= 0
    assert timing['control_permission_wait_ms'] >= 0
    assert timing['control_executing_publish_ms'] >= 0
    assert saved['actions'][0]['status'] == 'outcome_unknown'
    assert saved['actions'][0]['error'] == 'settlement failed'
    durable = [json.loads(line) for line in (tmp_path / 'session.jsonl').read_text(encoding='utf-8').splitlines()]
    assert 'client_before' not in durable[0]
    assert 'client_before' not in durable[1]
    assert durable[2]['client_before']['turn'] == 2


def test_throttled_report_rebuilds_unsaved_action_and_parity_after_crash(tmp_path, monkeypatch):
    import controller.live_session as session

    clock = [100.0]
    monkeypatch.setattr(session.time, 'monotonic', lambda: clock[0])
    report = {'schema_version': 3, 'actions': [], 'parity_checkpoints': []}
    log = JsonlSessionLog(tmp_path / 'session.jsonl', compact_lifecycle=True)
    writes = []

    def save(path, value):
        writes.append(len(value['actions']))
        write_json(path / 'run_report.json', value, compact=True)
        return {'total_ms': 0.0}

    writer = ThrottledReportWriter(tmp_path, report, log.path, save)
    control = ManagedControl(None, None)
    log.on_event = SessionJournal(report, writer.save, control).record
    writer.save(force=True)
    before = {'screen': 'COMBAT', 'turn': 2, 'run': {'floor': 7}}
    common = {
        'sequence': 1, 'client_action': 'play_card',
        'client_params': {'card_index': 0}, 'client_before': before,
        'headless_action': 'play_card', 'headless_args': {'card_index': 0},
        'transaction': {'client': {'action': 'play_card', 'params': {'card_index': 0}},
                        'telemetry': {'score': float('-inf')}},
    }
    log.write({'event': 'action_pending', 'status': 'pending', **common})
    log.write({'event': 'action_started', 'status': 'executing', **common})
    log.write({'event': 'live_action', 'status': 'completed',
               'client_after': {'screen': 'COMBAT', 'turn': 2}, **common})
    log.write({'event': 'shadow_applied', 'sequence': 1, 'action': 'play_card'})
    log.write({'event': 'parity_checkpoint', 'sequence': 1, 'status': 'PASS',
               'label': 'after_action_1'})
    log.write({'event': 'flow_context', 'value': {'scene': 'COMBAT', 'operation': None}})
    assert writes == [0]

    recovered = recover_report_tail(read_json(tmp_path / 'run_report.json'), log.path)
    assert recovered['actions'][0]['status'] == 'completed'
    assert recovered['actions'][0]['shadow_action_applied'] is True
    assert recovered['actions'][0]['verification'] == 'COVERED_FIELDS_MATCH'
    assert recovered['actions'][0]['client_after']['turn'] == 2
    assert recovered['actions'][0]['transaction']['telemetry']['score'] is None
    assert recovered['pending_action'] is None
    assert recovered['parity_checkpoints'][-1]['label'] == 'after_action_1'
    assert recovered['flow_context']['scene'] == 'COMBAT'
    assert len(recover_report_tail(recovered, log.path)['parity_checkpoints']) == 1

    clock[0] += 61.0
    writer.save()
    assert writes == [0, 1]
    log.write({'event': 'action_pending', 'status': 'pending', 'sequence': 2,
               'client_action': 'end_turn', 'client_params': {}, 'client_before': before})
    log.write({'event': 'action_started', 'status': 'executing', 'sequence': 2,
               'client_action': 'end_turn', 'client_params': {}, 'client_before': before})
    recovered = recover_report_tail(read_json(tmp_path / 'run_report.json'), log.path)
    assert recovered['actions'][-1]['status'] == 'executing'
    assert recovered['pending_action']['sequence'] == 2
    assert 'transaction' not in recovered['actions'][-1]


def test_report_tail_rejects_missing_log_and_ignores_incomplete_line(tmp_path):
    path = tmp_path / 'session.jsonl'
    report = {'session_log_offset': 0, 'actions': []}
    path.write_bytes(b'{"event":"action_started","sequence":3,"status":"executing"}\n'
                     b'{"event":"live_action","sequence":3')
    recovered = recover_report_tail(report, path)
    assert recovered['actions'][0]['status'] == 'executing'
    path.unlink()
    report['session_log_offset'] = 10
    with pytest.raises(ValueError, match='missing'):
        recover_report_tail(report, path)


def test_transient_windows_replace_failure_preserves_previous_document(tmp_path, monkeypatch):
    path = tmp_path / 'status.json'
    write_json(path, {'sequence': 1})
    replace = Path.replace
    attempts = []

    def sharing_violation(source, destination):
        attempts.append(source)
        if len(attempts) <= 3:
            assert json.loads(path.read_text()) == {'sequence': 1}
            raise PermissionError(5, 'sharing violation')
        return replace(source, destination)

    monkeypatch.setattr(Path, 'replace', sharing_violation)
    write_json(path, {'sequence': 2})
    assert len(attempts) == 4
    assert json.loads(path.read_text()) == {'sequence': 2}
    assert not list(tmp_path.glob('*.tmp'))


def test_permanent_failure_is_bounded_and_cleans_temporary_file(tmp_path, monkeypatch):
    import controller.live_session as session
    path = tmp_path / 'status.json'
    write_json(path, {'sequence': 1})
    clock = iter([0.0, 4.0])
    monkeypatch.setattr(session.time, 'monotonic', lambda: next(clock))
    def denied(*args):
        raise PermissionError(5, 'permanent denial')
    monkeypatch.setattr(Path, 'replace', denied)
    with pytest.raises(PermissionError):
        write_json(path, {'sequence': 2})
    assert json.loads(path.read_text()) == {'sequence': 1}
    assert not list(tmp_path.glob('*.tmp'))


def test_concurrent_readers_never_see_partial_json(tmp_path):
    path = tmp_path / 'run_report.json'
    write_json(path, {'sequence': 0, 'data': 'x' * 10000})
    stop = threading.Event()
    errors = []
    def read():
        while not stop.is_set():
            try:
                data = read_json(path)
                assert len(data['data']) == 10000
            except PermissionError:
                continue
            except Exception as exc:
                errors.append(exc)
                break
    readers = [threading.Thread(target=read) for _ in range(3)]
    for thread in readers:
        thread.start()
    try:
        for index in range(50):
            write_json(path, {'sequence': index, 'data': 'x' * 10000})
    finally:
        stop.set()
        for thread in readers:
            thread.join(3)
    assert not errors
