import json
from argparse import Namespace

from controller.human_capture import CAPTURE_PROTOCOL, AUTHORITATIVE_SNAPSHOT_SCHEMA
from scripts.generate_human_counterfactuals import generate


def test_failed_capture_reports_native_reason_without_starting_engine(tmp_path, monkeypatch):
    session = tmp_path / 'human_failed'
    session.mkdir()
    (session / 'manifest.json').write_text(json.dumps({
        'status': 'failed', 'capture_health': {
            'protocol_version': CAPTURE_PROTOCOL,
            'snapshot_schema': AUTHORITATIVE_SNAPSHOT_SCHEMA}}), encoding='utf-8')
    (session / 'events.jsonl').write_text(json.dumps({
        'record_type': 'capture_snapshot_failure', 'event_id': 1523,
        'snapshot_metadata': {'error': 'Detached historical power'}}), encoding='utf-8')
    def no_engine(*args, **kwargs):
        raise AssertionError('No engine should start without eligible sessions')
    monkeypatch.setattr('scripts.generate_turn_preferences.CombatSearcher', no_engine)
    report = tmp_path / 'report.json'
    result = generate(Namespace(human_input=tmp_path, report=report,
                                workers=1, max_nodes=10, max_seconds=1, max_actions=10))
    assert result['success'] is False
    assert result['excluded_sessions'][0]['last_failure']['event_id'] == 1523
    assert json.loads(report.read_text(encoding='utf-8')) == result
