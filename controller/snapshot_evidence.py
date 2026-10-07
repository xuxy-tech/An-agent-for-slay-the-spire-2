"""Seal the small live combat trace needed to audit a replay snapshot.

The trace is deliberately stored beside the snapshot, rather than retaining a
whole run report merely because one combat was sampled from it.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVIDENCE_SCHEMA = 'sts2.live_combat_evidence.v1'
EVIDENCE_FILE = 'live_combat_evidence.json'
SESSION_RE = re.compile(r'^\d{8}_\d{6}_\d{6}$')


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode('utf-8')).hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    data = (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _later_rows(metadata: dict[str, Any], report: dict[str, Any]) -> tuple[int | None, list[dict[str, Any]]]:
    start = datetime.fromtimestamp(float(metadata['created_at_utc']), tz=timezone.utc)
    selected = []
    for row in report.get('actions') or []:
        if not str(row.get('decision') or '').startswith('combat.'):
            continue
        try:
            when = datetime.fromisoformat(row['timestamp_utc']).astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        if when > start:
            selected.append(row)
    if not selected:
        return None, []
    number = (selected[0].get('decision_telemetry') or {}).get('combat_number')
    return number, [row for row in selected
                    if (row.get('decision_telemetry') or {}).get('combat_number') == number]


def evidence_for_report(metadata: dict[str, Any], report: dict[str, Any],
                        model: dict[str, Any] | None = None,
                        evaluator_coefficients: dict[str, Any] | None = None) -> dict[str, Any] | None:
    number, rows = _later_rows(metadata, report)
    if not rows or number is None:
        return None
    expected = (metadata.get('context') or {}).get('combat_number')
    if expected is not None and expected != number:
        return None
    combat = next((item for item in report.get('combats') or []
                   if item.get('combat_number') == number), None)
    if not combat or combat.get('status') not in {'COMPLETED', 'DEFEAT'}:
        return None
    if model is None or evaluator_coefficients is None:
        return None
    from controller.combat_scoring import CombatScoring, stage_for_floor, validate_model
    validate_model(model)
    floor = (metadata.get('context') or {}).get('floor')
    digest = (((rows[0].get('decision_telemetry') or {}).get('score_explanation') or {})
              .get('scorer') or {}).get('weights_sha256')
    if digest != CombatScoring(stage_for_floor(int(floor) if floor is not None else None), model).identity['weights_sha256']:
        return None
    trace = []
    for row in rows:
        telemetry = row.get('decision_telemetry') or {}
        trace.append({
            'decision': row.get('decision'), 'timestamp_utc': row.get('timestamp_utc'),
            'headless_action': row.get('headless_action'),
            'headless_args': row.get('headless_args'),
            'decision_telemetry': {
                key: telemetry.get(key) for key in
                ('combat_number', 'reused_plan', 'worker_runtime', 'score_explanation', 'chosen')
            },
            'shadow_rng_before': row.get('shadow_rng_before'),
            'shadow_rng_after': row.get('shadow_rng_after'),
        })
    payload = {'snapshot_id': metadata.get('snapshot_id'),
               'created_at_utc': metadata.get('created_at_utc'),
               'combat_number': number, 'combat_status': combat['status'],
               'config': report.get('config') or {}, 'model': model,
               'evaluator_coefficients': evaluator_coefficients,
               'actions': trace}
    return {'schema': EVIDENCE_SCHEMA, 'payload': payload, 'sha256': _digest(payload)}


def seal_evidence(artifact: Path, report: dict[str, Any],
                  model: dict[str, Any] | None = None,
                  evaluator_coefficients: dict[str, Any] | None = None) -> bool:
    metadata_path = artifact / 'metadata.json'
    if not metadata_path.is_file():
        return False
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    if metadata.get('status') != 'RESTORE_VERIFIED' or metadata.get('reusable') is not True:
        return False
    evidence = evidence_for_report(metadata, report, model, evaluator_coefficients)
    if evidence is None:
        return False
    _atomic_json(artifact / EVIDENCE_FILE, evidence)
    return True


def read_evidence(metadata: dict[str, Any]) -> dict[str, Any] | None:
    artifact = Path(str(metadata.get('artifact_dir') or ''))
    path = artifact / EVIDENCE_FILE
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding='utf-8'))
    payload = value.get('payload')
    if (value.get('schema') != EVIDENCE_SCHEMA or not isinstance(payload, dict)
            or value.get('sha256') != _digest(payload)
            or payload.get('snapshot_id') != metadata.get('snapshot_id')
            or payload.get('created_at_utc') != metadata.get('created_at_utc')):
        raise ValueError('Snapshot live combat evidence is corrupt or belongs to another snapshot')
    return payload


def read_original_report(metadata: dict[str, Any]) -> dict[str, Any] | None:
    """Legacy fallback: read a live report or its verified full-session ZIP."""
    session = Path(str((metadata.get('context') or {}).get('session_dir') or ''))
    report_path = session / 'run_report.json'
    if report_path.is_file():
        return json.loads(report_path.read_text(encoding='utf-8'))
    if not SESSION_RE.fullmatch(session.name) or session.parent.name != 'live_dashboard':
        return None
    archive_path = session.parent / 'archive' / (session.name + '.zip')
    if not archive_path.is_file() or archive_path.is_symlink():
        return None
    with zipfile.ZipFile(archive_path) as archive:
        return json.loads(archive.read('run_report.json'))


def seal_session_snapshots(snapshot_root: Path, session_dir: Path,
                           report: dict[str, Any]) -> dict[str, int]:
    """Backfill every snapshot of one finished session before moving its logs."""
    source = session_dir.resolve()
    model_path = source / 'scoring_model.json'
    model = (json.loads(model_path.read_text(encoding='utf-8')) if model_path.is_file()
             else (report.get('config') or {}).get('scoring_model'))
    profile_path = source / 'deck_profile.json'
    coefficients = None
    if profile_path.is_file():
        coefficients = dict((json.loads(profile_path.read_text(encoding='utf-8'))
                             .get('combat_coefficients') or {}))
    result = {'total': 0, 'sealed': 0, 'incomplete': 0}
    for metadata_path in snapshot_root.glob('*/metadata.json'):
        try:
            metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
            if Path(str((metadata.get('context') or {}).get('session_dir') or '')).resolve() != source:
                continue
            if metadata.get('status') != 'RESTORE_VERIFIED' or metadata.get('reusable') is not True:
                continue
            result['total'] += 1
            try:
                if read_evidence(metadata) is not None:
                    result['sealed'] += 1
                    continue
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
            if seal_evidence(metadata_path.parent, report, model, coefficients):
                result['sealed'] += 1
            else:
                result['incomplete'] += 1
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            result['incomplete'] += 1
    return result
