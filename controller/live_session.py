from __future__ import annotations

import json
import time
import tempfile
import os
from pathlib import Path
from typing import Any, Callable


def read_shared_bytes(path: Path) -> bytes:
    if os.name != 'nt':
        return path.read_bytes()
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    # FILE_SHARE_DELETE keeps concurrent atomic writers from failing on Windows.
    handle = create(str(path.resolve()), 0x80000000, 0x7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close(handle)
        raise
    with os.fdopen(descriptor, 'rb') as stream:
        return stream.read()


def read_json(path: Path) -> Any:
    return json.loads(read_shared_bytes(path).decode('utf-8'))


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and (value != value or value in (float('inf'), float('-inf'))):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def write_json(path: Path, value: Any, *, compact: bool = False,
               timing: dict[str, Any] | None = None) -> None:
    started = time.perf_counter()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Windows readers can temporarily deny rename/delete sharing. Keep the old
    # complete document visible while retrying; never truncate it in place.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
        temporary = Path(stream.name)
        phase_started = time.perf_counter()
        safe_value = _json_safe(value)
        if timing is not None:
            timing['json_safe_ms'] = round((time.perf_counter() - phase_started) * 1000, 3)
        phase_started = time.perf_counter()
        if compact:
            serialized = json.dumps(safe_value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        else:
            serialized = json.dumps(safe_value, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write(serialized)
        if timing is not None:
            timing['json_dump_ms'] = round((time.perf_counter() - phase_started) * 1000, 3)
        close_started = time.perf_counter()
    try:
        if timing is not None:
            timing['temp_close_ms'] = round((time.perf_counter() - close_started) * 1000, 3)
            timing['bytes'] = temporary.stat().st_size
        deadline = time.monotonic() + 3.0
        phase_started = time.perf_counter()
        retries = 0
        while True:
            try:
                temporary.replace(path)
                break
            except PermissionError:
                retries += 1
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.025)
        if timing is not None:
            timing['replace_ms'] = round((time.perf_counter() - phase_started) * 1000, 3)
            timing['replace_retries'] = retries
    finally:
        temporary.unlink(missing_ok=True)
    if timing is not None:
        timing['total_ms'] = round((time.perf_counter() - started) * 1000, 3)


class ControlledStop(RuntimeError):
    pass


class RunDefeat(RuntimeError):
    pass


class ManagedControl:
    # Step tokens are consumed only at action gates, never during observation.
    def __init__(self, control_file: Path | None, status_file: Path | None):
        self.control_file = control_file
        self.status_file = status_file
        self.state: dict[str, Any] = {}
        self.consumed_step: str | None = None

    def checkpoint(self, mode: str, **fields: Any) -> None:
        if self._read_command().get('desired') == 'stop':
            raise ControlledStop('Stop requested at a safe decision boundary')
        self.publish(mode, **fields)

    def before_action(self, pending: dict[str, Any]) -> None:
        timing = pending.get('timing_breakdown')
        publish_started = time.perf_counter()
        self.publish('running', phase='awaiting_permission', pending_action=pending)
        if isinstance(timing, dict):
            timing['control_awaiting_publish_ms'] = round((time.perf_counter() - publish_started) * 1000, 3)
        wait_started = time.perf_counter()
        paused_publish_ms = 0.0
        while True:
            command = self._read_command()
            desired = command.get('desired', 'running')
            if desired == 'stop':
                raise ControlledStop('Stop requested before client action')
            token = str(command.get('command_id') or '')
            if desired == 'running':
                break
            if desired == 'step' and token and token != self.consumed_step:
                self.consumed_step = token
                break
            publish_started = time.perf_counter()
            self.publish('paused', phase='awaiting_permission', pending_action=pending)
            paused_publish_ms += (time.perf_counter() - publish_started) * 1000
            time.sleep(0.1)
        if isinstance(timing, dict):
            timing['control_permission_wait_ms'] = round((time.perf_counter() - wait_started) * 1000, 3)
            timing['control_paused_publish_ms'] = round(paused_publish_ms, 3)
        publish_started = time.perf_counter()
        self.publish('running', phase='executing', pending_action=pending)
        if isinstance(timing, dict):
            timing['control_executing_publish_ms'] = round((time.perf_counter() - publish_started) * 1000, 3)

    def publish(self, mode: str, **fields: Any) -> None:
        for key, value in fields.items():
            if key in {'pending_action', 'latest_action'} and isinstance(value, dict):
                before = value.get('client_before') or {}
                run = before.get('run') or {}
                self.state[key] = {
                    name: value.get(name) for name in (
                        'sequence', 'status', 'client_action', 'client_params',
                        'screen_before', 'screen_after', 'verification', 'error',
                    ) if name in value
                }
                self.state[key].update(floor=run.get('floor'), turn=before.get('turn'))
            elif key == 'latest_checkpoint' and isinstance(value, dict):
                self.state[key] = {
                    name: value.get(name) for name in (
                        'label', 'sequence', 'status', 'scope', 'difference_count',
                    ) if name in value
                }
                self.state[key]['differences'] = (value.get('differences') or [])[:3]
            else:
                self.state[key] = value
        self.state.update(mode=mode, updated_at=time.time())
        if self.status_file is not None:
            write_json(self.status_file, self.state)

    def _read_command(self) -> dict[str, Any]:
        if self.control_file is None:
            return {'desired': 'running'}
        try:
            value = read_json(self.control_file)
            if not isinstance(value, dict) or value.get('desired') not in {'running', 'paused', 'step', 'stop'}:
                return {'desired': 'paused'}
            return value
        except (OSError, json.JSONDecodeError):
            return {'desired': 'paused'}


class SessionJournal:
    # Persist the lifecycle even when a sent command has an unknown outcome.
    def __init__(self, report: dict[str, Any], save: Callable[[], dict[str, Any] | None], control: ManagedControl):
        self.report = report
        self.save = save
        self.control = control
        report.setdefault('actions', [])

    def record(self, event: dict[str, Any]) -> None:
        kind = event.get('event')
        action_timing = event.get('timing_breakdown')
        if kind == 'interaction_state':
            self.control.publish(self.control.state.get('mode', 'running'), interaction=event)
        if kind == 'parity_checkpoint':
            self.control.publish(self.control.state.get('mode', 'running'), latest_checkpoint=event)
        if kind == 'selection_subaction':
            row = next((r for r in self.report.get('actions', []) if r['sequence'] == event['sequence']), None)
            if row is not None:
                progress = row.setdefault('selection_progress', {'submitted_indices': [], 'remaining_indices': []})
                progress['submitted_indices'].append(event['option_index'])
                progress['remaining_indices'] = [i for i in (row.get('client_params') or {}).get('indices', [])
                                                 if i not in progress['submitted_indices']]
                self.control.publish(self.control.state.get('mode', 'running'), latest_action=row)
        if kind in {'parent_action_settled', 'parent_action_verified'}:
            parent = event.get('parent_record') or {}
            row = next((item for item in self.report.get('actions', [])
                        if item.get('sequence') == parent.get('sequence')), None)
            if row is None:
                raise ValueError('Parent settlement has no recorded action')
            row.update(parent)
        if kind in {'action_pending', 'action_started', 'action_awaiting_input',
                    'live_action', 'client_only_action', 'action_failed'}:
            sequence = event['sequence']
            rows = self.report['actions']
            row = next((row for row in rows if row['sequence'] == sequence), None)
            if row is None:
                row = {'sequence': sequence}
                rows.append(row)
            row.update(event)
            self.report['pending_action'] = (
                {
                    key: row.get(key) for key in (
                        'sequence', 'status', 'client_action', 'client_params',
                        'screen_before', 'headless_action', 'headless_args',
                    ) if key in row
                }
                if row['status'] in {'pending', 'executing', 'awaiting_input'} else None
            )
            publish_started = time.perf_counter()
            self.control.publish(self.control.state.get('mode', 'running'), latest_action=row)
            if isinstance(action_timing, dict):
                action_timing.setdefault('persistence', []).append({
                    'event': kind,
                    'session_log_serialize_ms': action_timing.get('session_log_serialize_ms'),
                    'session_log_append_ms': action_timing.get('session_log_append_ms'),
                    'status_publish_ms': round((time.perf_counter() - publish_started) * 1000, 3),
                })
        # Persist every lifecycle boundary: paused dashboards and crash recovery
        # need the pending and sent states before later outcomes are known.
        if kind in {
            'action_pending', 'action_started', 'action_awaiting_input',
            'live_action', 'client_only_action',
            'action_failed', 'selection_subaction', 'shadow_applied',
            'parent_action_settled', 'parent_action_verified',
            'shadow_failed', 'root_rng_parity',
            'reward_receipt', 'terminal_outcome', 'worker_pool_closed',
            'reanchor', 'shadow_recovery',
        }:
            save_started = time.perf_counter()
            details = self.save()
            elapsed_ms = round((time.perf_counter() - save_started) * 1000, 3)
            if isinstance(action_timing, dict) and kind in {
                'action_pending', 'action_started', 'action_awaiting_input',
                'live_action', 'client_only_action', 'action_failed',
            }:
                entry = action_timing.setdefault('persistence', [])[-1]
                entry.update(report_write_ms=elapsed_ms if details is not None else 0.0,
                             report_write=details, report_write_deferred=details is None)
            elif details is not None:
                summary = self.report.setdefault('persistence_summary', {}).setdefault(kind, {
                    'count': 0, 'report_write_total_ms': 0.0, 'report_write_max_ms': 0.0,
                })
                summary['count'] += 1
                summary['report_write_total_ms'] = round(summary['report_write_total_ms'] + elapsed_ms, 3)
                summary['report_write_max_ms'] = max(summary['report_write_max_ms'], elapsed_ms)


class ThrottledReportWriter:
    def __init__(self, session_dir: Path, report: dict[str, Any], log_path: Path,
                 write: Callable[[Path, dict[str, Any]], dict[str, Any]],
                 interval_s: float = 60.0):
        self.session_dir = session_dir
        self.report = report
        self.log_path = log_path
        self.write_report = write
        self.interval_s = interval_s
        self.last_write = 0.0

    def save(self, *, force: bool = False) -> dict[str, Any] | None:
        now = time.monotonic()
        if not force and now - self.last_write < self.interval_s:
            return None
        self.report['session_log_offset'] = self.log_path.stat().st_size if self.log_path.exists() else 0
        timing = self.write_report(self.session_dir, self.report)
        self.last_write = time.monotonic()
        return timing


def recover_report_tail(report: dict[str, Any], log_path: Path) -> dict[str, Any]:
    """Apply durable events written after the last complete report snapshot."""
    if 'session_log_offset' not in report:
        return report
    offset = int(report.get('session_log_offset') or 0)
    if not log_path.is_file():
        if offset:
            raise ValueError('Session log is missing after report snapshot')
        return report
    if offset > log_path.stat().st_size:
        raise ValueError('Session log is shorter than the report snapshot offset')
    actions = {row['sequence']: row for row in report.setdefault('actions', [])}
    applied_offset = offset
    with log_path.open('rb') as stream:
        stream.seek(offset)
        for raw in stream:
            if not raw.endswith(b'\n'):
                break
            event = _json_safe(json.loads(raw.decode('utf-8')))
            kind = event.get('event')
            sequence = event.get('sequence')
            if kind in {'action_pending', 'action_started', 'action_awaiting_input', 'live_action',
                        'client_only_action', 'action_failed'}:
                if type(sequence) is not int:
                    raise ValueError('Action journal entry lacks an integer sequence')
                row = actions.setdefault(sequence, {'sequence': sequence})
                row.update(event)
                if row.get('status') in {'pending', 'executing', 'awaiting_input'}:
                    report['pending_action'] = {
                        key: row[key] for key in (
                            'sequence', 'status', 'client_action', 'client_params',
                            'screen_before', 'headless_action', 'headless_args',
                        ) if key in row
                    }
                else:
                    report['pending_action'] = None
            elif kind == 'shadow_applied' and sequence in actions:
                record = event.get('transaction_record')
                if isinstance(record, dict):
                    actions[sequence].update(record)
                else:
                    actions[sequence].update(
                        shadow_action_applied=True,
                        transaction_status=event.get('transaction_status') or 'completed',
                        shadow_completed=event.get('shadow_completed', True),
                    )
            elif kind in {'parent_action_settled', 'parent_action_verified'}:
                parent = event.get('parent_record') or {}
                parent_sequence = parent.get('sequence')
                if parent_sequence not in actions:
                    raise ValueError('Parent settlement has no recorded action')
                actions[parent_sequence].update(parent)
            elif kind == 'shadow_failed' and sequence in actions:
                actions[sequence].update(transaction_status='shadow_failed',
                                         shadow_error=event.get('error'))
            elif kind == 'parity_checkpoint':
                report.setdefault('parity_checkpoints', []).append(event)
                report['latest_checkpoint'] = event
                if sequence in actions:
                    actions[sequence]['verification'] = (
                        'COVERED_FIELDS_MATCH' if event.get('status') == 'PASS'
                        else event.get('status')
                    )
            elif kind == 'terminal_outcome' and sequence in actions:
                actions[sequence]['verification'] = event.get('verification')
            elif kind == 'interaction_state':
                report['interaction'] = event
                if 'flow_context' in event:
                    report['flow_context'] = event['flow_context']
            elif kind == 'flow_context':
                report['flow_context'] = event['value']
            elif kind == 'combat_count':
                report['completed_combat_count'] = event['count']
            elif kind == 'reanchor':
                report.setdefault('reanchors', []).append(event)
            elif kind == 'reward_receipt' and sequence in actions:
                actions[sequence]['reward_receipt'] = event
            elif kind in {
                'pre_action_map_reanchor_started', 'pre_action_map_reanchor_verified',
                'client_only_segment_started', 'client_only_segment_verified',
                'client_only_segment_failed',
            }:
                segment_id = event.get('segment_id')
                segments = report.setdefault('client_only_segments', [])
                segment = next((item for item in segments
                                if item.get('segment_id') == segment_id), None)
                if segment is None:
                    segment = {'segment_id': segment_id}
                    segments.append(segment)
                segment.update(event)
            applied_offset += len(raw)
    report['actions'] = [actions[key] for key in sorted(actions)]
    report['session_log_offset'] = applied_offset
    return report
