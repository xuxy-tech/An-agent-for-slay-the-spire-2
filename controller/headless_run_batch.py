"""Dashboard-managed, gate-controlled full headless games with normal history artifacts."""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from cli.sts2_cli_adapter import CliConfig
from controller.combat_snapshot import snapshot_compatibility
from controller.engine_consistency import require_pass
from controller.live_session import write_json


class HeadlessRunBatch:
    def __init__(self, repo_root: Path, log_root: Path, model_library, deck_profile_path: Path):
        self.repo_root = repo_root
        self.log_root = log_root
        self.model_library = model_library
        self.deck_profile_path = deck_profile_path
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.processes: dict[str, subprocess.Popen] = {}
        self.state = {'status': 'idle', 'target': 0, 'completed': 0, 'running': 0,
                      'parallel': 0, 'sessions': [], 'message': '尚未启动'}

    def start(self, count: int, parallel: int, model_id: str, depth: int,
              search_ms: int, max_workers: int) -> dict:
        if type(count) is not int or not 1 <= count <= 1000:
            raise ValueError('局数必须在 1–1000 之间')
        if type(parallel) is not int or not 1 <= parallel <= 4:
            raise ValueError('并行对局数必须在 1–4 之间')
        if not 1 <= depth <= 16 or not 100 <= search_ms <= 60000:
            raise ValueError('搜索参数超出允许范围')
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                raise ValueError('已有仅后端对局正在运行')
            gate = require_pass(self.log_root, snapshot_compatibility(CliConfig(repo_root=self.repo_root)))
            model = self.model_library.get(model_id)
            if not self.deck_profile_path.is_file():
                raise ValueError('当前牌组配置不存在')
            # Freeze both policy inputs for the entire batch, including games
            # that start after a dashboard edit.
            self.stop_event = threading.Event()
            batch = self.log_root / 'headless_batches' / datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            batch.mkdir(parents=True, exist_ok=False)
            write_json(batch / 'scoring_model.json', model)
            shutil.copy2(self.deck_profile_path, batch / 'deck_profile.json')
            self.state = {'status': 'running', 'target': count, 'completed': 0,
                          'running': 0, 'parallel': parallel, 'sessions': [],
                          'model_id': model_id, 'batch_id': batch.name,
                          'gate_checked_at': gate['checked_at_utc'],
                          'message': '仅后端对局正在运行'}
            workers = max(1, min(max_workers, max(1, (os.cpu_count() or 4) // parallel)))
            self.thread = threading.Thread(target=self._schedule,
                args=(batch, count, parallel, model_id, depth, search_ms, workers), daemon=True)
            self.thread.start()
            return dict(self.state)

    def stop(self) -> dict:
        with self.lock:
            self.stop_event.set()
            for process in self.processes.values():
                if process.poll() is None:
                    process.terminate()
            if self.state['status'] == 'running':
                self.state['status'] = 'stopping'
                self.state['message'] = '正在结束已启动对局；剩余局数已取消'
            return dict(self.state)

    def status(self) -> dict:
        with self.lock:
            return {**self.state, 'sessions': list(self.state['sessions'])}

    def close(self) -> None:
        self.stop()
        thread = self.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)

    def _schedule(self, batch: Path, count: int, parallel: int, model_id: str,
                  depth: int, search_ms: int, workers: int) -> None:
        threads: list[threading.Thread] = []
        semaphore = threading.Semaphore(parallel)
        for _ in range(count):
            if self.stop_event.is_set():
                break
            semaphore.acquire()
            if self.stop_event.is_set():
                semaphore.release()
                break
            thread = threading.Thread(target=self._game,
                args=(batch, model_id, depth, search_ms, workers, semaphore), daemon=True)
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()
        with self.lock:
            self.state['status'] = 'stopped' if self.stop_event.is_set() else 'completed'
            self.state['message'] = ('已停止' if self.stop_event.is_set() else '全部对局已完成')

    def _game(self, batch: Path, model_id: str, depth: int,
              search_ms: int, workers: int, semaphore: threading.Semaphore) -> None:
        session = self.log_root / datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        seed = str(secrets.randbits(63))
        process = None
        created_at = time.time()
        event_count = 0
        summary: dict = {}
        report = {'status': 'RUNNING', 'created_at': created_at,
                  'execution_mode': 'headless_only', 'verification_scope': 'ENGINE_GATE',
                  'identity': {'run_id': seed, 'character': 'Ironclad'},
                  'config': {'scoring_model_id': model_id, 'depth': depth,
                             'search_budget_ms': search_ms, 'worker_count': workers,
                             'deck_profile': str(batch / 'deck_profile.json'),
                             'engine_gate_checked_at': self.state['gate_checked_at']},
                  'actions': [], 'completed_combat_count': 0}
        try:
            session.mkdir(parents=True, exist_ok=False)
            shutil.copy2(batch / 'scoring_model.json', session / 'scoring_model.json')
            shutil.copy2(batch / 'deck_profile.json', session / 'deck_profile.json')
            write_json(session / 'config.json', report['config'])
            write_json(session / 'run_report.json', report)
            gate = require_pass(self.log_root, snapshot_compatibility(CliConfig(repo_root=self.repo_root)))
            if gate.get('checked_at_utc') != self.state['gate_checked_at']:
                raise ValueError('一致性门禁在批次运行期间发生变化；批次已停止')
            command = [sys.executable, '-u', '-X', 'utf8', '-m', 'controller.run_agent',
                       '--character', 'Ironclad', '--seed', seed, '--depth', str(depth),
                       '--max-search-ms', str(search_ms), '--max-workers', str(workers),
                       '--max-steps', '2000', '--score-mode', 'preference',
                       '--scoring-model', str(session / 'scoring_model.json'),
                       '--deck-profile', str(session / 'deck_profile.json')]
            if workers > 1:
                command.append('--parallel-top-level')
            with self.lock:
                self.state['running'] += 1
                self.state['sessions'].append({'id': f'live_dashboard/{session.name}',
                                               'seed': seed, 'status': 'RUNNING'})
            if self.stop_event.is_set():
                report.update(status='STOPPED', finished_at=time.time())
                write_json(session / 'run_report.json', report, compact=True)
                return
            with (session / 'session.jsonl').open('w', encoding='utf-8') as log, \
                    (session / 'runner.log').open('w', encoding='utf-8') as raw_log:
                process = subprocess.Popen(command, cwd=self.repo_root, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True, encoding='utf-8',
                                           errors='replace', bufsize=1)
                with self.lock:
                    self.processes[session.name] = process
                if self.stop_event.is_set() and process.poll() is None:
                    process.terminate()
                assert process.stdout is not None
                for line in process.stdout:
                    raw_log.write(line)
                    raw_log.flush()
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    log.write(json.dumps(row, ensure_ascii=False) + '\n')
                    log.flush()
                    if row.get('run_summary'):
                        summary = row
                    elif 'step_id' in row:
                        event_count += 1
                        report['actions'].append(self._action(row))
                        if row.get('combat_end'):
                            report['completed_combat_count'] += 1
                        if event_count % 5 == 0:
                            write_json(session / 'run_report.json', report, compact=True)
                process.wait()
            if self.stop_event.is_set() and process.returncode != 0:
                status = 'STOPPED'
            elif not summary or process.returncode != 0:
                status = 'FAIL'
            else:
                status = {'victory': 'VICTORY', 'game_over': 'DEFEAT',
                          'error': 'FAIL'}.get(summary.get('outcome'), 'FAIL')
            report.update(status=status, finished_at=time.time(), headless_summary=summary)
            if status == 'FAIL':
                report['error'] = ((summary.get('error_context') or {}).get('message')
                                   or f"Headless run ended without victory/defeat: {summary.get('outcome') or process.returncode}")
            write_json(session / 'run_report.json', report, compact=True)
        except Exception as exc:
            report.update(status='FAIL', finished_at=time.time(), error=str(exc))
            if '一致性' in str(exc):
                self.stop_event.set()
            if session.is_dir():
                write_json(session / 'run_report.json', report, compact=True)
        finally:
            with self.lock:
                self.processes.pop(session.name, None)
                self.state['running'] = max(0, self.state['running'] - 1)
                self.state['completed'] += 1
                for row in self.state['sessions']:
                    if row['id'].endswith(session.name):
                        row['status'] = report['status']
            semaphore.release()

    @staticmethod
    def _action(row: dict) -> dict:
        action = row.get('combat_action') or {}
        applied = row.get('applied') or {}
        return {'sequence': row.get('step_id'), 'status': 'completed',
                'verification': 'ENGINE_GATE',
                'headless_before': row.get('headless_before') or {
                    'screen': row.get('decision'),
                    'run': {'floor': row.get('floor'), 'act_id': row.get('act')}},
                'headless_after': {'screen': (row.get('post_state') or {}).get('decision'),
                                   'run': {'floor': (row.get('post_state') or {}).get('floor'),
                                           'act_id': (row.get('post_state') or {}).get('act'),
                                           'current_hp': (row.get('post_state') or {}).get('current_hp'),
                                           'max_hp': (row.get('post_state') or {}).get('max_hp'),
                                           'gold': (row.get('post_state') or {}).get('gold')}},
                'client_action': applied.get('action'), 'client_params': applied.get('payload') or {},
                'decision_telemetry': {'chosen': action.get('chosen'),
                                       'search_ms': action.get('search_wall_ms'),
                                       'nodes': action.get('nodes'),
                                       'fell_back': action.get('search_failed', False),
                                       'reused_plan': action.get('reused_plan', False),
                                       'decision_audit': {'ran_search': bool(action.get('nodes'))}}}
