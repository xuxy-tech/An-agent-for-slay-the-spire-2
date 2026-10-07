import hashlib
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from controller.live_session import write_json
from controller.live_client_bridge import JsonlSessionLog
from controller.run_lifecycle import menu_action, run_summary
from scripts.live_dashboard import DashboardManager


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(threading.Thread, 'start', lambda self: None)
    value = DashboardManager(tmp_path, tmp_path / 'logs' / 'live_dashboard', 'http://localhost:9999')
    value.mod = value.observer = SimpleNamespace(state=lambda: {'screen': 'MAIN_MENU'})
    return value


def test_timeline_claims_only_obtained_not_completed():
    state = {'available_actions': ['choose_timeline_epoch', 'close_main_menu_submenu'],
             'timeline': {'slots': [
                 {'index': 1, 'state': 'Complete', 'is_actionable': True},
                 {'index': 7, 'state': 'Obtained', 'is_actionable': True}]}}
    assert menu_action(state) == ('choose_timeline_epoch', {'option_index': 7})
    state['timeline']['slots'].pop()
    assert menu_action(state) == ('close_main_menu_submenu', {})
    state['available_actions'].append('confirm_timeline_overlay')
    assert menu_action(state)[0] == 'confirm_timeline_overlay'


def test_unknown_lifecycle_does_not_guess_action():
    assert menu_action({'screen': 'UNKNOWN', 'available_actions': ['confirm_modal']}) is None


def test_pause_auto_between_runs_blocks_prepare_and_resumes(manager):
    manager.auto_start = True
    manager.command('pause', {})
    assert manager.auto_paused
    manager._start_prepare_thread()
    assert not manager.preparing
    manager.command('resume', {})
    assert not manager.auto_paused and manager.auto_start


def test_stop_clears_auto_pause(manager):
    manager.auto_start = True
    manager.command('pause', {})
    manager.command('stop', {})
    assert not manager.auto_paused and not manager.auto_start


def test_menu_with_continue_save_is_ready_before_abandon_step(manager):
    state = {'screen': 'MAIN_MENU', 'available_actions': ['continue_run', 'abandon_run', 'open_timeline']}
    assert manager._ensure_main_menu_for_new_run(state) is state
    assert menu_action(state) is None


def test_auto_waits_for_potion_fix_without_creating_failed_runs(manager):
    manager.auto_start = True
    manager.mod.capture_health = lambda: {'recovery_main_menu': True}
    assert not manager._automatic_runtime_ready()
    assert manager.auto_start and manager.manager_mode == 'waiting_mod'
    assert not list(manager.log_root.glob('*/run_report.json'))
    manager.mod.capture_health = lambda: {'recovery_main_menu': True,
                                         'potion_target_contract': 'native-no-creature-v1'}
    assert manager._automatic_runtime_ready()


def test_auto_waits_for_stale_capture_dll(manager, tmp_path):
    manager.repo_root = tmp_path
    built = manager.repo_root / 'mod' / 'STS2HumanCapture' / 'bin' / 'Release' / 'net9.0'
    built.mkdir(parents=True, exist_ok=True)
    dll = built / 'STS2HumanCapture.dll'
    dll.write_bytes(b'workspace-build')
    manager.mod = SimpleNamespace(
        capture_health=lambda: {'recovery_main_menu': True,
                                'potion_target_contract': 'native-no-creature-v1'},
        capture_identity=lambda: {'capture_assembly': {'sha256': 'stale'}},
    )
    assert not manager._automatic_runtime_ready()
    assert manager.manager_mode == 'waiting_mod'

    manager.mod.capture_identity = lambda: {
        'capture_assembly': {'sha256': hashlib.sha256(b'workspace-build').hexdigest()}
    }
    assert manager._automatic_runtime_ready()


def test_dashboard_exposes_loaded_runtime_version(manager):
    assert manager.status()['runtime_version'] == 'storage-evidence-20261003-1'


def test_batch_counts_current_run_and_every_terminal_result_once(manager):
    manager.session_dir = manager.log_root / 'first'
    manager.session_dir.mkdir(parents=True)
    manager.process = SimpleNamespace(poll=lambda: None)
    manager.command('set_run_plan', {'run_mode': 'batch', 'batch_target': 3,
                                     'expected_session_id': 'live_dashboard/first'})
    assert manager.auto_start and manager.batch_completed == 0
    for index, status in enumerate(('DEFEAT', 'FAIL', 'VICTORY'), start=1):
        directory = manager.log_root / str(index)
        write_json(directory / 'run_report.json', {'status': status})
        manager._record_batch_completion(directory)
        manager._record_batch_completion(directory)
        assert manager.batch_completed == index
    assert not manager.auto_start and manager.batch_state == 'completed'
    assert manager.status()['run_plan']['completed'] == 3


def test_batch_pause_resume_preserves_progress_and_stop_cancels_remaining(manager):
    manager.command('set_run_plan', {'run_mode': 'batch', 'batch_target': 10})
    directory = manager.log_root / 'first'
    write_json(directory / 'run_report.json', {'status': 'DEFEAT'})
    manager._record_batch_completion(directory)
    manager.command('pause', {})
    assert manager.batch_completed == 1 and manager.batch_state == 'paused'
    manager.command('resume', {})
    assert manager.batch_completed == 1 and manager.batch_state == 'running'
    manager.command('stop', {})
    assert manager.batch_state == 'cancelled' and not manager.auto_start
    second = manager.log_root / 'second'
    write_json(second / 'run_report.json', {'status': 'FAIL'})
    manager._record_batch_completion(second)
    assert manager.batch_completed == 1


def test_batch_applied_to_paused_current_run_stays_paused(manager):
    manager.process = SimpleNamespace(poll=lambda: None)
    manager.status_file = manager.log_root / 'current' / 'status.json'
    write_json(manager.status_file, {'mode': 'paused'})
    manager.command('set_run_plan', {'run_mode': 'batch', 'batch_target': 4})
    assert manager.auto_paused and manager.batch_state == 'paused'
    manager.command('resume', {})
    assert not manager.auto_paused and manager.batch_state == 'running'


def test_stale_current_run_control_is_rejected(manager):
    manager.session_dir = manager.log_root / 'new'
    with pytest.raises(ValueError, match='Current run changed'):
        manager.command('stop', {'expected_session_id': 'live_dashboard/old'})


def test_batch_state_survives_dashboard_restart_without_auto_running(manager):
    manager.command('set_run_plan', {'run_mode': 'batch', 'batch_target': 5})
    directory = manager.log_root / 'first'
    write_json(directory / 'run_report.json', {'status': 'BLOCKED'})
    manager._record_batch_completion(directory)
    other = DashboardManager(manager.repo_root, manager.log_root, manager.mod_url)
    assert (other.batch_target, other.batch_completed, other.batch_state) == (5, 1, 'interrupted')
    assert not other.auto_start


def test_initial_anchor_uses_matching_exact_save_without_disk_save(manager, tmp_path):
    state = {'screen': 'EVENT', 'run_id': 'CURRENT', 'event': {'event_id': 'NEOW'},
             'available_actions': ['choose_event_option']}
    save_json = json.dumps({'rng': {'seed': 'CURRENT'}, 'current_act_index': 0,
                            'extra_fields': {'started_with_neow': True},
                            'visited_map_coords': [{'col': 3, 'row': 0}]}, indent=2).replace('\n', '\r\n')
    payload = {'save_json': save_json, 'sha256': hashlib.sha256(save_json.encode('utf-8')).hexdigest()}
    manager.mod = SimpleNamespace(exact_save=lambda: payload, state=lambda: state)
    anchor = tmp_path / 'official_map_anchor.save'
    result = manager._capture_initial_anchor(state, anchor)
    assert anchor.read_bytes() == save_json.encode('utf-8')
    assert result == {'source': 'client_exact_save', 'sha256': payload['sha256'],
                      'run_id': 'CURRENT', 'visited_map_coords': 1}


def test_initial_anchor_rejects_stale_run_seed(manager, tmp_path, monkeypatch):
    state = {'screen': 'EVENT', 'run_id': 'CURRENT', 'event': {'event_id': 'NEOW'},
             'available_actions': ['choose_event_option']}
    save_json = json.dumps({'rng': {'seed': 'PREVIOUS'}, 'current_act_index': 0,
                            'extra_fields': {'started_with_neow': True},
                            'visited_map_coords': [{'col': 3, 'row': 0}]})
    payload = {'save_json': save_json, 'sha256': hashlib.sha256(save_json.encode('utf-8')).hexdigest()}
    manager.mod = SimpleNamespace(exact_save=lambda: payload, state=lambda: state)
    times = iter([0.0, 1.0, 16.0])
    monkeypatch.setattr('scripts.live_dashboard.time.monotonic', lambda: next(times))
    monkeypatch.setattr('scripts.live_dashboard.time.sleep', lambda _: None)
    anchor = tmp_path / 'official_map_anchor.save'
    with pytest.raises(RuntimeError, match='不属于当前涅奥开局'):
        manager._capture_initial_anchor(state, anchor)
    assert not anchor.exists()


def test_initial_anchor_rejects_unsettled_event(manager, tmp_path):
    state = {'screen': 'CHARACTER_SELECT', 'run_id': 'CURRENT'}
    with pytest.raises(RuntimeError, match='没有停在可选择的涅奥事件'):
        manager._capture_initial_anchor(state, tmp_path / 'anchor.save')


def test_summary_preserves_unknown_deck_and_max_floor():
    report = {'status': 'FAIL', 'config': {'scoring_model_id': 'trained:example'},
              'actions': [{'client_before': {'run': {'floor': 5, 'deck': [1, 2]}}}],
              'terminal_client': {'run': {'floor': 0}}}
    result = run_summary(report, 'x', 12)
    assert (result['floor'], result['deck_size'], result['error']) == (5, 2, True)
    assert result['scoring_model_id'] == 'trained:example'
    assert run_summary({}, 'x', 12)['deck_size'] is None


def test_auto_launch_runs_without_neow_pause(manager, monkeypatch):
    captured = []
    monkeypatch.setattr('scripts.live_dashboard.subprocess.Popen',
                        lambda cmd, **kw: captured.append(cmd) or SimpleNamespace(poll=lambda: None))
    manager.auto_start = True
    directory = manager.log_root / 'run'
    directory.mkdir(parents=True)
    manager._launch_runner(directory, directory / 'anchor.save')
    try:
        assert json.loads((directory / 'control.json').read_text())['desired'] == 'running'
        assert captured[0][captured[0].index('--target-combats') + 1] == '0'
        assert '--verify-checkpoints' in captured[0]
    finally:
        manager.output_handle.close()


def test_selected_strategy_is_frozen_for_run_and_recovery(manager, monkeypatch):
    from controller.combat_scoring import active_model
    commands = []
    monkeypatch.setattr('scripts.live_dashboard.subprocess.Popen',
                        lambda cmd, **kw: commands.append(cmd) or SimpleNamespace(poll=lambda: None))
    weights = {**active_model()['weights'], 'enemy_hp_removed': 35}
    saved = manager.comparison.models.save('保血版本', weights)
    manager.command('save_config', {'scoring_model_id': saved['id']})
    first = manager.log_root / 'first'
    first.mkdir(parents=True)
    manager._launch_runner(first, first / 'anchor.save')
    try:
        frozen = (first / 'scoring_model.json').read_bytes()
        assert json.loads(frozen)['weights']['enemy_hp_removed'] == 35
        assert commands[0][commands[0].index('--scoring-model-id') + 1] == saved['id']
        manager.comparison.models.rename(saved['id'], '新名字')
        assert (first / 'scoring_model.json').read_bytes() == frozen
        manager.output_handle.close()
        second = manager.log_root / 'second'
        second.mkdir(parents=True)
        manager._launch_runner(second, second / 'anchor.save',
                               resume_report=first / 'run_report.json')
        assert (second / 'scoring_model.json').read_bytes() == frozen
    finally:
        manager.output_handle.close()


def test_settings_survive_restart_without_enabling_automation(manager):
    manager.command('save_config', {'search_ms': 3456, 'delay_ms': 123, 'auto_start': True})
    other = DashboardManager(manager.repo_root, manager.log_root, manager.mod_url)
    assert other.config['search_ms'] == 3456
    assert other.config['delay_ms'] == 123
    assert not other.auto_start


def test_stop_cancels_pending_auto_restart(manager):
    manager.auto_start = True
    manager.restart_pending = True
    manager.command('stop', {})
    assert not manager.auto_start and not manager.restart_pending
    assert manager.cancel_prepare.is_set()


def test_archive_preserves_full_error_and_pending_action(manager):
    manager.session_dir = manager.log_root / 'run'
    report = {'status': 'RUNNING', 'actions': [{'status': 'outcome_unknown', 'client_params': {'x': 4}}]}
    write_json(manager.session_dir / 'run_report.json', report)
    manager._archive_exit(9)
    archived = json.loads((manager.session_dir / 'run_report.json').read_text())
    assert archived['status'] == 'INTERRUPTED'
    assert archived['actions'] == report['actions']
    assert (manager.session_dir / 'exit_snapshot.json').is_file()


def test_watchdog_never_aborts_user_pause(manager):
    manager.status_file = manager.log_root / 'status.json'
    write_json(manager.status_file, {'mode': 'paused'})
    manager.progress_at = time.monotonic() - 10000
    manager._watchdog(SimpleNamespace())
    assert time.monotonic() - manager.progress_at < 1


def test_history_limits_display_only(manager):
    for i in range(12):
        write_json(manager.log_root / str(i) / 'run_report.json',
                   {'created_at': i + 1, 'status': 'FAIL', 'actions': [{'client_before': {'run': {'floor': i}}}]})
    assert 'history' not in manager.status()
    assert len(manager.sessions(recent=True)) == 12


def test_history_endpoint_reads_no_report_when_empty(manager):
    result = manager.history_sessions()

    assert result['sessions'] == []
    assert result['loading'] is False


def test_history_reads_newest_first_and_only_requested_pages(manager):
    for i in range(60):
        path = manager.log_root / f'{i:03d}' / 'run_report.json'
        write_json(path, {'created_at': i, 'finished_at': i + 1, 'status': 'DEFEAT',
                          'identity': {'run_id': str(i)}})
        os.utime(path, (i + 1, i + 1))
    original = manager._read_json
    read = []
    def counted(path):
        if path.name == 'run_report.json':
            read.append(path.parent.name)
        return original(path)
    manager._read_json = counted
    first = manager.history_sessions(0, 20, 10)
    assert [row['id'].split('/')[-1] for row in first['sessions']] == [f'{i:03d}' for i in range(59, 39, -1)]
    assert first['next_offset'] == 20 and first['has_more']
    assert set(read) == {f'{i:03d}' for i in range(40, 60)}
    second = manager.history_sessions(first['next_offset'], 20, 10)
    assert second['sessions'][0]['id'] == 'live_dashboard/039'
    assert set(read) == {f'{i:03d}' for i in range(20, 60)}


def test_history_places_current_dated_sessions_before_legacy_paths(manager):
    current = manager.log_root / '20261003_120000_000000' / 'run_report.json'
    legacy = manager.repo_root / 'logs' / 'legacy_run' / 'run_report.json'
    write_json(current, {'status': 'DEFEAT', 'finished_at': 1791000000})
    write_json(legacy, {'status': 'FAIL', 'finished_at': 1789500000})
    os.utime(legacy, (1789500000, 1789500000))
    rows = manager.history_sessions(0, 2)['sessions']
    assert [row['id'] for row in rows] == [
        'live_dashboard/20261003_120000_000000', 'legacy_run']


def test_history_summary_pagination_and_explicit_resume(manager):
    def add(name, status, finished, floor, *, seed=None, source=None, error=None):
        row = {'status': status, 'created_at': finished - 5, 'finished_at': finished,
               'identity': {'run_id': seed} if seed else {},
               'terminal_client': {'run': {'floor': floor, 'act_id': str(0 if floor is None or floor < 18 else 1 if floor < 35 else 2),
                                           'deck': list(range(floor)) if floor is not None else []}},
               'resume_report': source, 'error': error}
        write_json(manager.log_root / name / 'run_report.json', row)
    add('a', 'STOPPED', 10, 17, seed='SHARED')
    add('b', 'DEFEAT', 20, 39, seed='SHARED', source=str(manager.log_root / 'a' / 'run_report.json'))
    add('c', 'DEFEAT', 30, 20, seed='SHARED')
    add('d', 'FAIL', 40, 1, seed='OTHER', error='Snapshot mismatch at floor 1')
    add('prep', 'FAIL', 50, None, error='Cannot launch game')
    manager.sessions()
    result = manager.history_sessions(0, 2, 10)
    assert result['total'] == 5 and result['has_more']
    assert len(result['sessions']) == 2
    assert result['summary']['actual'] == 3
    assert result['summary']['counts'] == {'victory': 0, 'defeat': 2, 'stopped': 0, 'error': 1}
    assert result['summary']['defeat']['average_floor'] == 29.5
    assert result['summary']['defeat']['act1_passed'] == 2
    assert result['summary']['defeat']['act2_passed'] == 1
    assert result['summary']['errors'][0]['reason'] == '快照或恢复失败'
    assert result['summary']['errors'][0]['example'] == 'Snapshot mismatch at floor 1'
    assert [row['seed'] for row in result['summary']['defeat']['top']] == ['SHARED', 'SHARED']
    with pytest.raises(ValueError, match='pagination'):
        manager.history_sessions(-1)


def test_delete_finished_history_removes_full_session_and_refreshes_cache(manager):
    directory = manager.log_root / 'old'
    write_json(directory / 'run_report.json', {'status': 'FAIL', 'created_at': 1})
    (directory / 'session.jsonl').write_text('failure evidence', encoding='utf-8')
    (directory / 'recovery_snapshot.json').write_text('{}', encoding='utf-8')
    assert len(manager.sessions(recent=True)) == 1
    manager.delete_session('live_dashboard/old')
    assert not directory.exists()
    assert manager.sessions(recent=True) == []


def test_delete_history_rejects_active_and_unfinished_sessions(manager):
    active = manager.log_root / 'active'
    write_json(active / 'run_report.json', {'status': 'FAIL'})
    manager.session_dir = active
    with pytest.raises(ValueError, match='current session'):
        manager.delete_session('live_dashboard/active')
    assert active.exists()
    manager.session_dir = None
    write_json(active / 'run_report.json', {'status': 'RUNNING'})
    with pytest.raises(ValueError, match='finished sessions'):
        manager.delete_session('live_dashboard/active')
    assert active.exists()


def test_delete_history_rejects_other_log_roots_and_traversal(manager):
    other = manager.repo_root / 'logs' / 'other' / 'run'
    write_json(other / 'run_report.json', {'status': 'FAIL'})
    with pytest.raises(ValueError, match='dashboard sessions'):
        manager.delete_session('other/run')
    with pytest.raises(ValueError, match='Unknown session'):
        manager.delete_session('../outside')
    assert other.exists()


def test_failed_recovery_preserves_auto_mode_and_backs_off(manager):
    manager.auto_start = True
    manager.session_dir = manager.log_root / 'failed'
    manager.mod = SimpleNamespace(
        state=lambda: {'screen': 'MAP', 'available_actions': ['choose_map_node']},
        capture_health=lambda: {})
    manager._return_to_menu(start_after=True)
    assert manager.auto_start
    assert not manager.recovering and not manager.preparing
    assert manager.retry_at > time.monotonic()
    assert 'recovery_main_menu' in manager.message
    assert 'recovery_failed' in (manager.session_dir / 'recovery.jsonl').read_text()


def test_native_recovery_observes_menu_before_launch(manager, monkeypatch):
    state = {'screen': 'MAP', 'available_actions': ['choose_map_node']}
    calls = []
    def recover():
        calls.append('recover')
        state.update(screen='MAIN_MENU', available_actions=['open_character_select'])
    manager.mod = SimpleNamespace(state=lambda: dict(state),
                                 capture_health=lambda: {'recovery_main_menu': True},
                                 recover_to_menu=recover)
    manager.session_dir = manager.log_root / 'failed'
    monkeypatch.setattr(manager, '_start_prepare_thread', lambda: calls.append('start'))
    manager._return_to_menu(start_after=True)
    assert calls == ['recover', 'start']


def test_stop_during_recovery_does_not_launch_next_run(manager, monkeypatch):
    state = {'screen': 'MAP', 'available_actions': ['choose_map_node']}
    def recover():
        manager.cancel_prepare.set()
        state.update(screen='MAIN_MENU', available_actions=['open_character_select'])
    manager.mod = SimpleNamespace(state=lambda: dict(state),
                                 capture_health=lambda: {'recovery_main_menu': True},
                                 recover_to_menu=recover)
    calls = []
    monkeypatch.setattr(manager, '_start_prepare_thread', lambda: calls.append('start'))
    manager._return_to_menu(start_after=True)
    assert not calls


def test_watchdog_archives_before_killing_and_keeps_unknown_action(manager, monkeypatch):
    manager.session_dir = manager.log_root / 'stalled'
    manager.status_file = manager.session_dir / 'status.json'
    manager.control_file = manager.session_dir / 'control.json'
    worker = {'mode': 'running', 'phase': 'executing', 'latest_action': {'sequence': 5, 'status': 'executing'}}
    write_json(manager.status_file, worker)
    write_json(manager.session_dir / 'run_report.json', {'status': 'RUNNING', 'actions': [worker['latest_action']]})
    manager.progress_token = ('executing', 5, 'executing', None)
    manager.progress_at = time.monotonic() - 10000
    process = SimpleNamespace(pid=123, poll=lambda: None, wait=lambda timeout: 0)
    def kill(*args, **kwargs):
        assert (manager.session_dir / 'watchdog.json').is_file()
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr('scripts.live_dashboard.subprocess.run', kill)
    process.terminate = lambda: kill()
    manager._watchdog(process)
    report = json.loads((manager.session_dir / 'run_report.json').read_text())
    assert report['status'] == 'WATCHDOG'
    assert report['actions'][0]['status'] == 'executing'


def test_crashed_runner_archives_action_started_after_last_report_snapshot(manager):
    manager.session_dir = manager.log_root / 'crashed'
    write_json(manager.session_dir / 'run_report.json',
               {'status': 'RUNNING', 'actions': [], 'session_log_offset': 0})
    log = JsonlSessionLog(manager.session_dir / 'session.jsonl', compact_lifecycle=True)
    action = {'sequence': 1, 'status': 'executing', 'client_action': 'end_turn',
              'client_params': {}, 'client_before': {'turn': 3, 'run': {'floor': 17}}}
    log.write({'event': 'action_started', **action})

    manager._archive_exit(1)

    report = json.loads((manager.session_dir / 'run_report.json').read_text(encoding='utf-8'))
    assert report['status'] == 'INTERRUPTED'
    assert report['actions'][0]['status'] == 'executing'
    assert report['pending_action']['sequence'] == 1
    assert (manager.session_dir / 'exit_snapshot.json').is_file()
