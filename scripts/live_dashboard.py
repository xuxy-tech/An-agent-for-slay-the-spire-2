from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
import uuid
import zipfile
from urllib.parse import urlparse, parse_qs
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

from cli.sts2_mod_adapter import ModApiError, ModClientConfig, Sts2ModAdapter
from controller.live_session import write_json, read_json, read_shared_bytes, recover_report_tail, SessionJournal, ManagedControl, ControlledStop
from controller.live_client_bridge import LiveClientBridge, JsonlSessionLog
from controller.interaction_state import classify_interaction
from controller.deck_profile import load_deck_profile, save_deck_profile
from controller.card_catalog import load_card_catalog
from controller.card_art import CardArtStore
from controller.combat_step import DEFAULT_LIVE_SEARCH_BUDGET_MS, DEFAULT_TURN_ACTION_CAP
from controller.combat_snapshot import (capture_client_combat_snapshot, snapshot_index,
                                        snapshot_compatibility, VALIDATION_SET_LIMIT)
from controller.combat_comparison import ComparisonManager
from controller.engine_consistency import load_gate, run_check, require_pass
from controller.snapshot_evidence import seal_session_snapshots, read_evidence
from scripts.archive_live_dashboard_logs import archive_session, SESSION_RE
from cli.sts2_cli_adapter import CliConfig
from controller.search.worker_scaling import recommended_hardware_workers
from controller.sandbox import SandboxManager
from controller.human_capture_dashboard import HumanCaptureDashboard
from controller.headless_run_batch import HeadlessRunBatch
from controller.run_lifecycle import menu_action, run_summary

RUNTIME_VERSION = 'headless-learning-20261005-1'


class DashboardManager:
    def __init__(self, repo_root: Path, log_root: Path, mod_url: str,
                 sandbox_dir: Optional[Path] = None,
                 capture_url: str = 'http://127.0.0.1:9878',
                 game_root: Optional[Path] = None):
        self.repo_root = repo_root
        self.log_root = log_root
        self.mod = Sts2ModAdapter(ModClientConfig(base_url=mod_url, timeout_s=20.0, capture_base_url=capture_url))
        # Status is advisory and polled every second.  Keep a failed probe
        # cheap while the game is still warming up; action paths use the
        # separate 20-second client and transaction deadlines.
        self.observer = Sts2ModAdapter(ModClientConfig(base_url=mod_url, timeout_s=0.35))
        self.mod_url = mod_url
        self.lock = threading.RLock()
        self.process: Optional[subprocess.Popen[str]] = None
        self.output_handle: Any = None
        self.session_dir: Optional[Path] = None
        self.control_file: Optional[Path] = None
        self.status_file: Optional[Path] = None
        self.output_file: Optional[Path] = None
        self.manager_mode = "idle"
        self.message = "控制台已就绪"
        self.preparing = False
        self.auto_start = False
        self.auto_paused = False
        self.restart_pending = False
        self.run_plan_mode = 'single'
        self.batch_target = 0
        self.batch_completed = 0
        self.batch_state = 'idle'
        self.batch_counted_sessions: set[str] = set()
        self.cancel_prepare = threading.Event()
        self.recovering = False
        self.retry_at = 0.0
        self.failure_streak = 0
        self.progress_token = None
        self.progress_at = time.monotonic()
        self.config_path = self.log_root / 'runner_settings.json'
        self.batch_path = self.log_root / 'run_batch.json'
        saved_batch = self._read_json(self.batch_path)
        if saved_batch.get('mode') == 'batch':
            self.run_plan_mode = 'batch'
            self.batch_target = int(saved_batch.get('target') or 0)
            self.batch_completed = int(saved_batch.get('completed') or 0)
            self.batch_counted_sessions = set(saved_batch.get('counted_sessions') or [])
            self.batch_state = ('interrupted' if saved_batch.get('state') in {'running', 'paused'}
                                else str(saved_batch.get('state') or 'idle'))
        self._summary_cache = {}
        self._summary_lock = threading.RLock()
        self._client_status_cache: Optional[Dict[str, Any]] = None
        self._client_status_at = 0.0
        self.config = {
            "target_combats": 0,
            "depth": DEFAULT_TURN_ACTION_CAP,
            "search_ms": int(DEFAULT_LIVE_SEARCH_BUDGET_MS),
            "delay_ms": 700,
            "stall_seconds": 900,
        }
        self.config['max_workers'] = recommended_hardware_workers()
        self.config['worker_mode'] = 'adaptive'
        self.config['scoring_model_id'] = 'active'
        self._comparison = None
        self._update_config(self._read_json(self.config_path))
        self.deck_profile_path = self.log_root / 'deck_profile.json'
        profile = load_deck_profile(self.deck_profile_path) if self.deck_profile_path.is_file() else load_deck_profile()
        save_deck_profile(self.deck_profile_path, profile)
        self._sandbox = None
        self._human_capture = None
        self._headless_batch = None
        self._sandbox_directory = sandbox_dir or repo_root / 'data' / 'sandbox'
        self._capture_url = capture_url
        self._game_root = game_root
        self._snapshot_busy = False
        self._command_lock = threading.RLock()
        self._closed = False
        self.monitor = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor.start()

    @property
    def sandbox(self):
        with self.lock:
            if self._sandbox is None:
                self._sandbox = SandboxManager(self.repo_root, self._sandbox_directory, self.deck_profile_path)
            return self._sandbox

    @property
    def human_capture(self):
        with self.lock:
            if self._human_capture is None:
                self._human_capture = HumanCaptureDashboard(
                    self.repo_root, self.mod_url, self._capture_url, self._game_root
                )
            return self._human_capture

    @property
    def comparison(self):
        with self.lock:
            if self._comparison is None:
                self._comparison = ComparisonManager(self.repo_root, self.log_root)
            return self._comparison

    @property
    def headless_batch(self):
        with self.lock:
            if self._headless_batch is None:
                self._headless_batch = HeadlessRunBatch(
                    self.repo_root, self.log_root, self.comparison.models, self.deck_profile_path)
            return self._headless_batch

    def _headless_gate_allowed(self) -> bool:
        try:
            require_pass(self.log_root, snapshot_compatibility(CliConfig(repo_root=self.repo_root)))
            return True
        except (ValueError, OSError):
            return False

    def command(self, command: str, options: Dict[str, Any]) -> Dict[str, Any]:
        # Serialize runner controls with capture, including simultaneous HTTP requests.
        if not self._command_lock.acquire(blocking=False):
            raise ValueError('Another dashboard command is in progress; wait for it to finish')
        try:
            if command == 'capture_combat_snapshot':
                return self.capture_combat_snapshot()
            return self._command(command, options)
        finally:
            self._command_lock.release()

    def capture_combat_snapshot(self) -> Dict[str, Any]:
        with self.lock:
            if self._closed or self.preparing or self.recovering or self.restart_pending:
                raise ValueError('Wait for preparation/recovery to finish')
            if self.auto_start and not self.auto_paused:
                raise ValueError('Pause automatic running before capture')
            if self._is_running():
                worker = self._read_json(self.status_file)
                control = self._read_json(self.control_file)
                if worker.get('mode') != 'paused' or control.get('desired') != 'paused':
                    raise ValueError('Pause the runner and wait for its paused acknowledgement')
                if worker.get('updated_at', 0) < getattr(self, 'command_time', 0):
                    raise ValueError('Waiting for the runner to acknowledge pause')
            self._snapshot_busy = True
        try:
            report = capture_client_combat_snapshot(
                self.mod, self.log_root / 'combat_snapshots',
                cli_config=CliConfig(repo_root=self.repo_root,
                    dll_relpath=Path('third_party/sts2-cli/src/Sts2Headless/bin/Release/net9.0/Sts2Headless.dll')),
                context={'source': 'dashboard_manual',
                         'session_dir': str(self.session_dir) if self.session_dir else None,
                         'search_config': dict(self.config),
                         'deck_profile': load_deck_profile(self.deck_profile_path)})
            return {'snapshot': report}
        finally:
            with self.lock:
                self._snapshot_busy = False

    def _command(self, command: str, options: Dict[str, Any]) -> Dict[str, Any]:
        if self._closed:
            raise ValueError('Dashboard is closing')
        if command in {'start', 'restart', 'continue_session'} and self.headless_batch.status()['status'] in {'running', 'stopping'}:
            raise ValueError('请先结束仅后端对局批次')
        if command in {'pause', 'resume', 'step', 'stop', 'restart', 'return_to_menu',
                       'continue_session', 'set_run_plan'} and 'expected_session_id' in options:
            actual = self.session_id(self.session_dir) if self.session_dir else None
            if options['expected_session_id'] != actual:
                raise ValueError('Current run changed; refresh status before controlling it')
        if command in {'save_deck_profile', 'reset_deck_profile'}:
            if self._is_running() or self.preparing:
                raise ValueError('Stop the runner before changing the deck profile')
            profile = load_deck_profile() if command == 'reset_deck_profile' else options.get('profile')
            if not isinstance(profile, dict):
                raise ValueError('Deck profile payload is required')
            save_deck_profile(self.deck_profile_path, profile)
            self._set('idle', 'Deck profile updated')
            return self.status()
        if command in {'step', 'resume'} and not self._is_running() and not (command == 'resume' and self.auto_paused):
            raise ValueError('No active runner')
        if command in {'restart', 'return_to_menu', 'start', 'continue_session'} and self.recovering:
            raise ValueError('Client recovery is still in progress')
        if command in {'restart', 'return_to_menu'} and self.preparing:
            raise ValueError('New-run preparation is in progress; stop it first')
        if command in {"start", "restart", "save_config", "set_auto_start"}:
            self._update_config(options)
        if command in {'start', 'set_run_plan'} and 'run_mode' in options:
            self._set_run_plan(options)
            if command == 'start':
                self.auto_paused = False
                if self.run_plan_mode == 'batch':
                    self.batch_state = 'running'
                    self._save_batch_state()
        elif "auto_start" in options and command in {'set_auto_start', 'start', 'restart'}:
            with self.lock:
                self.auto_start = bool(options["auto_start"])
                self.run_plan_mode = 'unlimited' if self.auto_start else 'single'
                self.batch_state = 'idle'
        if command == 'save_config':
            write_json(self.config_path, self.config)
            self.message = '参数已保存，下次开局生效'
        elif command == "start":
            if self._is_running():
                self._write_control("running")
                self._set("running", "继续运行请求已发送")
            else:
                self._start_prepare_thread()
        elif command == 'continue_session':
            if self._is_running() or self.preparing or self.session_dir is None:
                raise ValueError('No stopped session available to continue')
            source_dir = self.session_dir
            source = self._read_json(source_dir / 'run_report.json')
            if source.get('status') in {'DEFEAT', 'VICTORY'}:
                raise ValueError('This game has already ended')
            if not (source_dir / 'official_map_anchor.save').is_file():
                raise ValueError('Session has no replay anchor')
            session_dir = self.log_root / datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            session_dir.mkdir(parents=True, exist_ok=False)
            anchor = session_dir / 'official_map_anchor.save'
            shutil.copy2(source_dir / 'official_map_anchor.save', anchor)
            self._update_config(options)
            self._launch_runner(session_dir, anchor, resume_report=source_dir / 'run_report.json')
            self._set('running', '正在恢复已记录的会话，首个动作前暂停')
        elif command == "pause":
            self.auto_paused = True
            if self.run_plan_mode == 'batch' and self.batch_state == 'running':
                self.batch_state = 'paused'
                self._save_batch_state()
            if self._is_running():
                self._write_control("paused")
                self._set("pausing", "将在下一个安全决策点暂停")
            else:
                self._set('paused', '自动循环已暂停；不会开始下一局')
        elif command == "resume":
            self.auto_paused = False
            if self.run_plan_mode == 'batch' and self.batch_state == 'paused':
                self.batch_state = 'running'
                self._save_batch_state()
            self._write_control("running")
            self._set("running", "继续运行请求已发送")
        elif command == "step":
            worker = self._read_json(self.status_file)
            if worker.get('mode') != 'paused':
                raise ValueError('Single step requires a paused runner')
            self._write_control('step')
            self._set('stepping', '单步执行请求已发送')
        elif command == "stop":
            self.auto_paused = False
            self.cancel_prepare.set()
            self.auto_start = False
            self.restart_pending = False
            if self.run_plan_mode == 'batch' and self.batch_state in {'running', 'paused'}:
                self.batch_state = 'cancelled'
                self._save_batch_state()
            if self._is_running():
                self._write_control("stop")
                self._set("stopping", "将在下一个安全决策点停止 Agent，游戏保持当前界面")
            else:
                self._set("stopped", "Agent 已停止，游戏保持当前界面")
        elif command == "restart":
            if self.restart_pending:
                raise ValueError('Restart already pending')
            self.cancel_prepare.clear()
            if self.run_plan_mode == 'batch' and self.batch_state in {'running', 'paused'}:
                self.batch_state = 'cancelled'
                self.auto_start = False
                self._save_batch_state()
            self.restart_pending = True
            self._write_control("stop")
            self._set("restarting", "正在安全停止；回到主菜单后会开始新局")
        elif command == "return_to_menu":
            if self._is_running():
                raise ValueError('Stop the Agent before returning to the main menu')
            self.cancel_prepare.clear()
            threading.Thread(target=self._return_to_menu, daemon=True).start()
        elif command == "set_auto_start":
            self.message = "自动开局设置已更新"
            if self.auto_start and self._is_running():
                self._write_control('running')
            if self.auto_start:
                self.cancel_prepare.clear()
        elif command == 'set_run_plan':
            self.message = '运行计划已应用到当前局'
            if self.auto_start:
                self.cancel_prepare.clear()
        else:
            raise ValueError(f"unknown command: {command}")
        return self.status()

    def _save_batch_state(self) -> None:
        write_json(self.batch_path, {
            'mode': self.run_plan_mode, 'target': self.batch_target,
            'completed': self.batch_completed, 'state': self.batch_state,
            'counted_sessions': sorted(self.batch_counted_sessions),
        })

    def _set_run_plan(self, options: Dict[str, Any]) -> None:
        mode = str(options.get('run_mode') or '')
        if mode not in {'single', 'batch', 'unlimited'}:
            raise ValueError('Run mode must be single, batch, or unlimited')
        target = 0
        if mode == 'batch':
            value = options.get('batch_target')
            if type(value) is not int or value < 1:
                raise ValueError('Batch size must be a positive integer')
            target = value
        paused_now = self.auto_paused or (self._is_running()
                         and self._read_json(self.status_file).get('mode') == 'paused')
        with self.lock:
            self.run_plan_mode = mode
            self.auto_start = mode != 'single'
            if mode != 'single':
                self.auto_paused = paused_now
            self.batch_target = target
            self.batch_completed = 0
            self.batch_counted_sessions.clear()
            self.batch_state = ('paused' if self.auto_paused else 'running') if mode == 'batch' else 'idle'
            self._save_batch_state()

    def _record_batch_completion(self, session_dir: Path) -> None:
        session_id = self.session_id(session_dir)
        report = self._read_json(session_dir / 'run_report.json')
        if report.get('status') not in {'VICTORY', 'DEFEAT', 'FAIL', 'BLOCKED', 'WATCHDOG',
                                        'INTERRUPTED', 'STOPPED'}:
            return
        with self.lock:
            if (self.run_plan_mode != 'batch' or self.batch_state not in {'running', 'paused'}
                    or session_id in self.batch_counted_sessions):
                return
            self.batch_counted_sessions.add(session_id)
            self.batch_completed += 1
            if self.batch_completed >= self.batch_target:
                self.batch_state = 'completed'
                self.auto_start = False
                self.auto_paused = False
                self._set('completed', f'已运行 {self.batch_completed} / {self.batch_target} 局，连续运行结束')
            self._save_batch_state()

    def status(self) -> Dict[str, Any]:
        worker = self._read_json(self.status_file)
        client: Dict[str, Any]
        now = time.monotonic()
        with self.lock:
            cached_client = self._client_status_cache
            cached_at = self._client_status_at
        cache_ttl = 0.5 if cached_client and cached_client.get('connected') else 2.0
        if cached_client is not None and now - cached_at < cache_ttl:
            client = dict(cached_client)
        else:
            try:
                raw = self.observer.state()
                run = raw.get("run") or {}
                client = {
                    "connected": True,
                    "screen": raw.get("screen"),
                    "run_id": raw.get("run_id"),
                    "floor": run.get("floor"),
                    "hp": run.get("current_hp"),
                    "max_hp": run.get("max_hp"),
                    "gold": run.get("gold"),
                    "turn": raw.get("turn"),
                    "available_actions": raw.get("available_actions") or [],
                    "interaction": classify_interaction(raw).to_dict(),
                }
            except Exception as exc:
                client = {"connected": False, "error": str(exc)}
            with self.lock:
                self._client_status_cache = dict(client)
                self._client_status_at = now
        with self.lock:
            running = self.process is not None and self.process.poll() is None
            mode = self.manager_mode
            if running and not self.preparing and mode not in {'stopping', 'restarting'}:
                mode = str(worker.get('mode') or 'running')
                if self.manager_mode == 'pausing' and mode != 'paused':
                    mode = 'pausing'
                if self.manager_mode == 'stepping' and worker.get('updated_at', 0) < getattr(self, 'command_time', 0):
                    mode = 'stepping'
            return {
                "snapshot_busy": self._snapshot_busy,
                "runtime_version": RUNTIME_VERSION,
                "mode": mode,
                "message": self.message,
                "worker_running": running,
                "preparing": self.preparing,
                "recovering": self.recovering,
                "auto_start": self.auto_start,
                "auto_paused": self.auto_paused,
                "run_plan": {"mode": self.run_plan_mode, "target": self.batch_target,
                             "completed": self.batch_completed, "state": self.batch_state},
                "config": dict(self.config),
                "engine_consistency": load_gate(self.log_root),
                "headless_only_allowed": self._headless_gate_allowed(),
                "headless_batch": self.headless_batch.status(),
                "active_scoring_model_id": (self._read_json(self.session_dir / 'config.json').get('scoring_model_id')
                                            if self.session_dir and running else None),
                'deck_profile': load_deck_profile(self.deck_profile_path),
                "session_dir": str(self.session_dir) if self.session_dir else None,
                "worker": worker,
                "session_id": self.session_id(self.session_dir) if self.session_dir else None,
                "report_revision": self._report_revision(),
                "client": client,
                "recent_log": self._tail(self.output_file, 45),
                "capabilities": {
                    "safe_pause": True,
                    "main_menu_start": True,
                    "game_over_return": "return_to_main_menu" in client.get("available_actions", []),
                    "active_run_return": 'requires_recovery_bridge',
                },
            }

    def close(self) -> None:
        self._closed = True
        if self._headless_batch is not None:
            self._headless_batch.close()
        if self._sandbox is not None:
            self._sandbox.close()
        if self._human_capture is not None:
            self._human_capture.close()
        self.cancel_prepare.set()
        self.auto_start = False
        self._write_control("stop")

    def _report_revision(self):
        if self.session_dir is None:
            return None
        try:
            stat = (self.session_dir / 'run_report.json').stat()
            return f'{self.session_dir.name}:{stat.st_mtime_ns}:{stat.st_size}'
        except OSError:
            return None

    def session_id(self, path: Path) -> str:
        return path.resolve().relative_to((self.repo_root / 'logs').resolve()).as_posix()

    def session_path(self, session_id: str) -> Path:
        root = (self.repo_root / 'logs').resolve()
        path = (root / session_id).resolve()
        if not path.is_relative_to(root) or path == root or not (path / 'run_report.json').is_file():
            raise ValueError('Unknown session')
        return path

    def delete_session(self, session_id: str) -> None:
        path = self.session_path(session_id)
        root = self.log_root.resolve()
        if path.parent != root or path.is_symlink():
            raise ValueError('Only dashboard sessions can be deleted')
        with self.lock:
            if self.session_dir is not None and path == self.session_dir.resolve():
                raise ValueError('The current session cannot be deleted')
            report = self._read_json(path / 'run_report.json')
            if report.get('status') not in {
                'VICTORY', 'DEFEAT', 'FAIL', 'BLOCKED', 'WATCHDOG', 'INTERRUPTED', 'STOPPED'
            }:
                raise ValueError('Only finished sessions can be deleted')
            self._seal_session_evidence(path, report)
            shutil.rmtree(path)
            with self._summary_lock:
                self._summary_cache.pop(path / 'run_report.json', None)

    def _seal_session_evidence(self, path: Path, report: Dict[str, Any]) -> None:
        for root in (self.log_root / 'combat_validation_set', self.log_root / 'combat_snapshots'):
            result = seal_session_snapshots(root, path, report)
            if result['incomplete']:
                raise ValueError(f"{result['incomplete']} 个关联快照的战斗证据未完整保存，不能清理该局")

    def archive_session(self, session_id: str) -> Dict[str, Any]:
        path = self.session_path(session_id)
        if path.parent != self.log_root.resolve() or path.is_symlink():
            raise ValueError('Only dashboard sessions can be archived')
        with self.lock:
            if self.session_dir is not None and path == self.session_dir.resolve():
                raise ValueError('The current session cannot be archived')
            report = self._read_json(path / 'run_report.json')
            if report.get('status') not in {
                'VICTORY', 'DEFEAT', 'FAIL', 'BLOCKED', 'WATCHDOG', 'INTERRUPTED', 'STOPPED'
            }:
                raise ValueError('Only finished sessions can be archived')
            target = self.log_root / 'archive'
            target.mkdir(exist_ok=True)
            result = archive_session(path, target)
            write_json(target / f'{path.name}.summary.json',
                       run_summary(report, f'live_dashboard/archive/{path.name}', time.time()))
            with self._summary_lock:
                self._summary_cache.pop(path / 'run_report.json', None)
            return result

    def archive_path(self, session_id: str) -> Path:
        prefix = 'live_dashboard/archive/'
        name = session_id[len(prefix):] if session_id.startswith(prefix) else ''
        if not SESSION_RE.fullmatch(name):
            raise ValueError('Unknown archive')
        root = (self.log_root / 'archive').resolve()
        path = (root / (name + '.zip')).resolve()
        if path.parent != root or not path.is_file() or path.is_symlink():
            raise ValueError('Unknown archive')
        return path

    def delete_archive(self, session_id: str) -> None:
        path = self.archive_path(session_id)
        with self.lock:
            source = self.log_root / path.stem
            for root in (self.log_root / 'combat_validation_set', self.log_root / 'combat_snapshots'):
                for metadata_path in root.glob('*/metadata.json'):
                    metadata = self._read_json(metadata_path)
                    if (metadata.get('status') != 'RESTORE_VERIFIED'
                            or metadata.get('reusable') is not True
                            or Path(str((metadata.get('context') or {}).get('session_dir') or '')).resolve() != source.resolve()):
                        continue
                    try:
                        sealed = read_evidence(metadata) is not None
                    except (OSError, ValueError, TypeError, json.JSONDecodeError):
                        sealed = False
                    if not sealed:
                        raise ValueError('归档仍是关联快照的唯一战斗记录；先保留该归档或删除过期快照')
            path.unlink()
            path.with_suffix('.summary.json').unlink(missing_ok=True)
            with self._summary_lock:
                self._summary_cache.pop(path, None)

    def _archived_file(self, path: Path, name: str) -> bytes:
        if name not in {'run_report.json', 'session.jsonl'}:
            raise ValueError('Unknown archived file')
        with zipfile.ZipFile(path) as archive:
            return archive.read(name)

    def storage_status(self) -> Dict[str, Any]:
        def bytes_in(directory: Path) -> int:
            return sum(path.stat().st_size for path in directory.rglob('*')
                       if path.is_file() and not path.is_symlink())
        archives = []
        for path in sorted((self.log_root / 'archive').glob('*.zip'), reverse=True):
            if SESSION_RE.fullmatch(path.stem) and not path.is_symlink():
                archives.append({'id': f'live_dashboard/archive/{path.stem}',
                                 'bytes': path.stat().st_size})
        active = [path for path in self.log_root.iterdir()
                  if path.is_dir() and SESSION_RE.fullmatch(path.name) and not path.is_symlink()]
        compatibility = snapshot_compatibility(CliConfig(repo_root=self.repo_root))
        snapshots = snapshot_index(self.log_root)
        stale = [row for row in snapshots if row.get('compatibility') != compatibility]
        snapshot_bytes = {row['artifact_dir']: bytes_in(Path(row['artifact_dir'])) for row in snapshots}
        cache = self.log_root / 'comparisons' / 'cache'
        cache_files = list(cache.glob('*.json'))
        return {'active_sessions': len(active), 'active_session_bytes': sum(bytes_in(path) for path in active),
                'archives': archives,
                'archive_bytes': sum(row['bytes'] for row in archives),
                'snapshots': len(snapshots), 'snapshot_bytes': sum(snapshot_bytes.values()),
                'stale_snapshots': len(stale),
                'stale_snapshot_bytes': sum(snapshot_bytes[row['artifact_dir']] for row in stale),
                'replay_cache_files': len(cache_files),
                'replay_cache_bytes': sum(path.stat().st_size for path in cache_files)}

    def delete_stale_snapshots(self) -> int:
        if self.comparison._running_job_ids():
            raise ValueError('A/B 回放正在运行，稍后清理快照')
        compatibility = snapshot_compatibility(CliConfig(repo_root=self.repo_root))
        removed = 0
        for root in (self.log_root / 'combat_validation_set', self.log_root / 'combat_snapshots'):
            for path in root.glob('*/metadata.json'):
                artifact = path.parent
                if artifact.is_symlink() or artifact.resolve().parent != root.resolve():
                    continue
                row = self._read_json(path)
                if row.get('schema') != 'sts2.combat_snapshot.v2' or row.get('compatibility') == compatibility:
                    continue
                shutil.rmtree(artifact)
                removed += 1
        return removed

    def clear_replay_cache(self) -> int:
        if self.comparison._running_job_ids():
            raise ValueError('A/B 回放正在运行，稍后清理缓存')
        root = self.log_root / 'comparisons' / 'cache'
        removed = 0
        for path in root.glob('*.json'):
            if not path.is_symlink() and path.resolve().parent == root.resolve():
                path.unlink()
                removed += 1
        return removed

    def _history_candidates(self, recent: bool = False) -> list[Path]:
        # Only inspect direct children.  Recursive scans also walk large pytest
        # fixtures and copied dashboard trees unrelated to game history.
        roots = {self.log_root.resolve()}
        if not recent:
            roots.add((self.repo_root / 'logs').resolve())
        candidates = [path for root in roots for path in root.glob('*/run_report.json')]
        if not recent:
            candidates.extend(path for path in (self.log_root / 'archive').glob('*.zip')
                              if SESSION_RE.fullmatch(path.stem) and not path.is_symlink())
        def modified(path: Path) -> tuple[int, str]:
            try:
                if path.suffix == '.zip':
                    return int(path.stem[:8] + path.stem[9:15]), str(path)
                if SESSION_RE.fullmatch(path.parent.name):
                    return int(path.parent.name[:8] + path.parent.name[9:15]), str(path)
                # Legacy reports outside live_dashboard still participate in
                # history.  Compare their modification times in the same
                # YYYYMMDDHHMMSS scale as session directory names.
                return int(datetime.fromtimestamp(path.stat().st_mtime).strftime('%Y%m%d%H%M%S')), str(path)
            except OSError:
                return -1, str(path)
        candidates.sort(key=modified, reverse=True)
        if recent:
            return candidates[:16]
        return candidates

    def _history_row(self, path: Path) -> Dict[str, Any] | None:
        try:
            stamp = path.stat()
            key = (stamp.st_mtime_ns, stamp.st_size)
            with self._summary_lock:
                cached = self._summary_cache.get(path)
            if cached is None or cached[0] != key:
                if path.suffix == '.zip':
                    session_id = f'live_dashboard/archive/{path.stem}'
                    summary_path = path.with_suffix('.summary.json')
                    if summary_path.is_file():
                        row = self._read_json(summary_path)
                        if row.get('id') == session_id:
                            cached = (key, row)
                        else:
                            raise ValueError('Archive summary does not match archive')
                    else:
                        data = json.loads(self._archived_file(path, 'run_report.json'))
                        cached = (key, run_summary(data, session_id, stamp.st_mtime))
                        write_json(summary_path, cached[1])
                else:
                    data = self._read_json(path)
                    session_id = self.session_id(path.parent)
                    cached = (key, run_summary(data, session_id, stamp.st_mtime))
                if path.suffix == '.zip':
                    cached[1]['archived'] = True
                with self._summary_lock:
                    if path.is_file():
                        self._summary_cache[path] = cached
            return cached[1]
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, json.JSONDecodeError):
            return None

    def sessions(self, recent: bool = False) -> list[Dict[str, Any]]:
        result = [row for path in self._history_candidates(recent) if (row := self._history_row(path))]
        return sorted(result, key=lambda row: row.get('finished_at') or row['created_at'], reverse=True)

    def history_sessions(self, offset: int = 0, limit: int = 20,
                         summary_n: int = 10) -> Dict[str, Any]:
        """Read newest reports first, and only enough for this page and summary."""
        if offset < 0 or limit < 0 or limit > 100 or (limit == 0 and offset != 0) or summary_n not in {10, 20, 30, 50}:
            raise ValueError('Invalid history pagination')
        candidates = self._history_candidates()
        page = []
        next_offset = offset
        while next_offset < len(candidates) and len(page) < limit:
            row = self._history_row(candidates[next_offset])
            next_offset += 1
            if row and (row.get('finished_at') or row['status'] not in {'RUNNING', 'PREPARING'}):
                page.append(row)
        # Starting at the newest file also keeps the summary bounded to the
        # requested sample rather than parsing every historical full report.
        recent_rows = []
        for path in candidates:
            row = self._history_row(path)
            if row and (row.get('finished_at') or row['status'] not in {'RUNNING', 'PREPARING'}):
                recent_rows.append(row)
            if len(self._history_games(recent_rows)) >= summary_n:
                break
        games = self._history_games(recent_rows)[:summary_n]
        counts = {key: 0 for key in ('victory', 'defeat', 'stopped', 'error')}
        errors: dict[str, dict[str, Any]] = {}
        defeats = []
        for row in games:
            category = ('error' if row.get('error') and row['status'] != 'STOPPED' else
                        'victory' if row['status'] == 'VICTORY' else
                        'defeat' if row['status'] == 'DEFEAT' else
                        'stopped' if row['status'] == 'STOPPED' else 'error')
            counts[category] += 1
            if category == 'error':
                reason = row.get('error_reason') or row['status']
                group = errors.setdefault(reason, {'reason': reason, 'count': 0,
                                                   'example': row.get('error_example'), 'records': []})
                group['count'] += 1
                group['records'].append(row)
            if category == 'defeat':
                defeats.append(row)
        defeated_count = len(defeats)
        return {
            'sessions': page,
            'total': len(candidates), 'next_offset': next_offset,
            'has_more': next_offset < len(candidates),
            'loading': False, 'error': None,
            'summary': {'requested': summary_n, 'actual': len(games), 'counts': counts,
                        'errors': sorted(errors.values(), key=lambda item: (-item['count'], item['reason'])),
                        'defeat': {'count': defeated_count,
                                   'average_floor': (sum(row.get('floor') or 0 for row in defeats) / defeated_count
                                                     if defeated_count else None),
                                   'act1_passed': sum(bool(row.get('act1_passed')) for row in defeats),
                                   'act2_passed': sum(bool(row.get('act2_passed')) for row in defeats),
                                   'top': sorted(defeats, key=lambda row: row.get('floor') or 0, reverse=True)[:3]}},
        }

    @staticmethod
    def _history_games(rows: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
        """Collapse only explicit resume chains, never independent same-seed runs."""
        def canonical(session_id: str) -> str:
            return session_id.replace('live_dashboard/archive/', 'live_dashboard/', 1)
        by_id = {canonical(row['id']): row for row in rows}
        grouped: dict[str, Dict[str, Any]] = {}
        for row in rows:
            if not row.get('started'):
                continue
            root = canonical(row['id'])
            seen = {root}
            while by_id.get(root, {}).get('resume_from') in by_id:
                parent = by_id[root]['resume_from']
                if parent in seen:
                    break
                seen.add(parent)
                root = parent
            earlier = grouped.get(root)
            if earlier is None or (row.get('finished_at') or row['created_at']) > (earlier.get('finished_at') or earlier['created_at']):
                grouped[root] = row
        return sorted(grouped.values(), key=lambda row: row.get('finished_at') or row['created_at'], reverse=True)

    def _start_prepare_thread(self) -> None:
        with self.lock:
            if self._snapshot_busy or self.preparing or self.recovering or self._is_running() or self._closed or self.auto_paused:
                return
            self.cancel_prepare.clear()
            self.preparing = True
            self.manager_mode = "preparing"
            self.message = "正在检查客户端并准备新局"
        threading.Thread(target=self._prepare_and_launch, daemon=True).start()

    def _prepare_and_launch(self) -> None:
        preparation = {'schema_version': 2, 'status': 'PREPARING', 'created_at': time.time(),
                       'actions': [], 'config': dict(self.config)}
        try:
            session_dir = self.log_root / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            session_dir.mkdir(parents=True, exist_ok=False)
            with self.lock:
                self.session_dir = session_dir
                self.control_file = None
                self.status_file = session_dir / 'status.json'
                self.output_file = session_dir / 'runner.log'
            log = JsonlSessionLog(session_dir / 'opening.jsonl')
            journal = SessionJournal(preparation, lambda: write_json(session_dir / 'run_report.json', preparation),
                                     ManagedControl(None, self.status_file))
            log.on_event = journal.record
            self.preparation_live = LiveClientBridge(self.mod, log)
            self.preparation_live.before_action = lambda pending: self._check_preparation_cancelled()
            write_json(session_dir / 'run_report.json', preparation)
            state = self.mod.state()
            state = self._ensure_main_menu_for_new_run(state)
            state = self._abandon_continue_if_present(state)
            if "open_character_select" not in (state.get("available_actions") or []):
                raise RuntimeError(
                    f"主菜单暂时不能打开角色选择：{state.get('available_actions')!r}"
                )
            state = self._prepare_action("open_character_select")["state"]
            state = self._wait(lambda value: value.get("screen") == "CHARACTER_SELECT", state)
            state = self._configure_character(state, "IRONCLAD", 0)
            state = self._prepare_action("embark")["state"]
            anchor = session_dir / "official_map_anchor.save"
            preparation['opening_anchor'] = self._capture_initial_anchor(state, anchor)
            write_json(session_dir / 'run_report.json', preparation)
            if self.cancel_prepare.is_set() or self._closed:
                raise RuntimeError('Preparation stopped before launching Agent')
            preparation['status'] = 'COMPLETED'
            write_json(session_dir / 'opening_report.json', preparation)
            self._launch_runner(session_dir, anchor, anchor_room=True)
            self.restart_pending = False
            self._set("running", "新局已建立，Agent 正在可视运行")
        except Exception as exc:
            preparation.update(status='STOPPED' if self.cancel_prepare.is_set() else 'FAIL', error=str(exc))
            preparation['finished_at'] = time.time()
            try:
                preparation['terminal_client'] = self.observer.state()
            except Exception as observation_error:
                preparation['observation_error'] = str(observation_error)
            if self.session_dir is not None:
                write_json(self.session_dir / 'run_report.json', preparation)
            self._set("stopped" if self.cancel_prepare.is_set() else "error", str(exc))
            self.failure_streak += 1
            self.retry_at = time.monotonic() + min(60, 5 * self.failure_streak)
        finally:
            with self.lock:
                self.preparing = False

    def _ensure_main_menu_for_new_run(self, state: Dict[str, Any]) -> Dict[str, Any]:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            self._check_preparation_cancelled()
            command = menu_action(state)
            if command:
                action, params = command
                state = self._prepare_action(action, **params)['state']
                continue
            if state.get('screen') == 'MAIN_MENU' and set(state.get('available_actions') or []).intersection(
                    {'open_character_select', 'abandon_run', 'continue_run'}):
                return state
            if state.get('in_combat') or state.get('screen') in {'MAP', 'EVENT', 'REST', 'SHOP'}:
                raise RuntimeError('Active run must be archived and recovered before starting a new run')
            time.sleep(0.2)
            state = self.mod.state()
        raise RuntimeError('Main menu or timeline did not reach an actionable boundary')

    def _abandon_continue_if_present(self, state: Dict[str, Any]) -> Dict[str, Any]:
        actions = set(state.get("available_actions") or [])
        if "abandon_run" not in actions:
            return state
        state = self._prepare_action("abandon_run")["state"]
        state = self._wait(lambda value: value.get("screen") == "MODAL", state)
        if "confirm_modal" not in (state.get("available_actions") or []):
            raise RuntimeError("放弃旧局确认框没有可用的确认动作")
        state = self._prepare_action("confirm_modal")["state"]
        return self._wait(
            lambda value: value.get("screen") == "MAIN_MENU"
            and "abandon_run" not in (value.get("available_actions") or []),
            state,
        )

    def _configure_character(
        self, state: Dict[str, Any], character_id: str, ascension: int
    ) -> Dict[str, Any]:
        select = state.get("character_select") or {}
        choices = list(select.get("characters") or [])
        match = next(
            (row for row in choices if str(row.get("character_id") or "").upper() == character_id),
            None,
        )
        if match is None or match.get("is_locked"):
            raise RuntimeError(f"角色 {character_id} 不可用")
        if not match.get("is_selected"):
            state = self._prepare_action("select_character", option_index=int(match["index"]))["state"]
        while True:
            select = state.get("character_select") or {}
            current = int(select.get("ascension") or 0)
            if current == ascension:
                break
            action = "increase_ascension" if current < ascension else "decrease_ascension"
            if action not in (state.get("available_actions") or []):
                raise RuntimeError(f"无法把进阶从 {current} 调整到 {ascension}")
            state = self._prepare_action(action)["state"]
        if "embark" not in (state.get("available_actions") or []):
            raise RuntimeError("角色选择完成但无法启程")
        return state


    def _capture_initial_anchor(self, state: Dict[str, Any], anchor: Path) -> Dict[str, Any]:
        if (state.get('screen') != 'EVENT' or (state.get('event') or {}).get('event_id') != 'NEOW'
                or 'choose_event_option' not in (state.get('available_actions') or [])):
            raise RuntimeError('启程后客户端没有停在可选择的涅奥事件，不能建立开局锚点')
        run_id = state.get('run_id')
        if not isinstance(run_id, str) or not run_id:
            raise RuntimeError('启程后客户端缺少 run_id，不能建立开局锚点')
        deadline = time.monotonic() + 15.0
        last_error = ''
        while time.monotonic() < deadline:
            self._check_preparation_cancelled()
            try:
                captured = self.mod.exact_save()
                save_json = captured['save_json']
                saved = json.loads(save_json)
                seed = (saved.get('rng') or {}).get('seed')
                visited = saved.get('visited_map_coords')
                if (seed != run_id or saved.get('current_act_index') != 0
                        or not (saved.get('extra_fields') or {}).get('started_with_neow')
                        or not isinstance(visited, list) or len(visited) > 1
                        or (visited and (not isinstance(visited[0], dict)
                                         or visited[0].get('row') != 0))):
                    raise ValueError('exact-save 不属于当前涅奥开局')
                observed = self.mod.state()
                if (observed.get('run_id') != run_id or observed.get('screen') != 'EVENT'
                        or (observed.get('event') or {}).get('event_id') != 'NEOW'):
                    raise ValueError('获取 exact-save 时客户端已离开当前涅奥事件')
                anchor.write_bytes(save_json.encode('utf-8'))
                if hashlib.sha256(anchor.read_bytes()).hexdigest() != captured['sha256']:
                    anchor.unlink(missing_ok=True)
                    raise RuntimeError('开局锚点落盘后摘要与客户端不一致')
                return {'source': 'client_exact_save', 'sha256': captured['sha256'],
                        'run_id': run_id, 'visited_map_coords': len(visited)}
            except (ModApiError, OSError, ValueError, KeyError, TypeError) as exc:
                last_error = str(exc)
            time.sleep(0.2)
        raise RuntimeError(f'未能取得与当前涅奥开局匹配的客户端精确存档：{last_error}')

    def _prepare_action(self, action, **params):
        self._check_preparation_cancelled()
        return {'state': self.preparation_live.execute_client_action(action, params)}

    def _check_preparation_cancelled(self):
        if self.cancel_prepare.is_set() or self._closed:
            raise ControlledStop('New-run preparation stopped')

    def _launch_runner(self, session_dir: Path, anchor: Path, completed_combats: int = 0,
                       resume_report: Path | None = None, recover_pending_selection: bool = False,
                       anchor_room: bool = False, replay_room_report: Path | None = None) -> None:
        control = session_dir / "control.json"
        status = session_dir / "status.json"
        output = session_dir / "runner.log"
        write_json(control, {"desired": "running" if self.auto_start and not self.auto_paused and resume_report is None else "paused",
                             "command_id": uuid.uuid4().hex})
        model_path = session_dir / 'scoring_model.json'
        if resume_report is not None and (resume_report.parent / 'scoring_model.json').is_file():
            shutil.copy2(resume_report.parent / 'scoring_model.json', model_path)
            model_id = str(self._read_json(resume_report.parent / 'config.json').get('scoring_model_id') or 'active')
        else:
            model_id = str(self.config['scoring_model_id'])
            write_json(model_path, self.comparison.models.get(model_id))
        frozen_profile = session_dir / 'deck_profile.json'
        prior_profile = resume_report.parent / 'deck_profile.json' if resume_report else None
        shutil.copy2(prior_profile if prior_profile and prior_profile.is_file()
                     else self.deck_profile_path, frozen_profile)
        write_json(session_dir / 'config.json', {**self.config, 'scoring_model_id': model_id,
                                                'auto_start': self.auto_start})
        self.progress_at = time.monotonic()
        self.progress_token = None
        command = [
            sys.executable,
            '-X', 'utf8',
            "-m",
            "scripts.live_run_demo",
            "--url", self.mod_url,
            "--anchor-map-save",
            str(anchor),
            "--session-dir",
            str(session_dir),
            "--target-combats",
            '0',
            "--completed-combats", str(completed_combats),
            "--depth",
            str(self.config["depth"]),
            "--max-search-ms",
            str(self.config["search_ms"]),
            "--visible-delay-ms",
            str(self.config["delay_ms"]),
            "--control-file",
            str(control),
            "--status-file",
            str(status),
            "--verify-checkpoints",
        ]
        command.extend(['--max-workers', str(self.config['max_workers'])])
        command.extend(['--worker-mode', str(self.config['worker_mode'])])
        command.extend(['--deck-profile', str(frozen_profile.resolve())])
        command.extend(['--scoring-model', str(model_path.resolve()), '--scoring-model-id', model_id])
        if resume_report is not None:
            command.extend(['--resume-report', str(resume_report.resolve())])
        if recover_pending_selection:
            command.append('--recover-pending-selection')
        if anchor_room:
            command.append('--anchor-room')
        if replay_room_report:
            command.extend(['--replay-room-report', str(replay_room_report.resolve())])
        handle = output.open("w", encoding="utf-8")
        process = subprocess.Popen(
            command,
            cwd=self.repo_root,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        with self.lock:
            self.session_dir = session_dir
            self.control_file = control
            self.status_file = status
            self.output_file = output
            self.output_handle = handle
            self.process = process

    def _restart_after_stop(self) -> None:
        while self._is_running() and not self._closed and self.restart_pending:
            time.sleep(0.2)
        if not self._closed and self.restart_pending:
            self._return_to_menu(start_after=True)

    def _return_to_menu(self, start_after: bool = False) -> None:
        with self.lock:
            if self.recovering or self._is_running() or self.preparing:
                return
            self.recovering = True
        try:
            self._set('recovering', '正在归档并返回主菜单')
            directory = self.session_dir or self.log_root / ('recovery_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
            directory.mkdir(parents=True, exist_ok=True)
            recovery_log = JsonlSessionLog(directory / 'recovery.jsonl')
            self.preparation_live = LiveClientBridge(self.mod, recovery_log)
            self.preparation_live.before_action = lambda pending: self._check_preparation_cancelled()
            state = self.mod.state()
            recovery_log.write({'event': 'recovery_before', 'state': state, 'worker': self._read_json(self.status_file)})
            try:
                exact = self.mod.exact_save()
                write_json(directory / ('recovery_snapshot_' + uuid.uuid4().hex + '.json'), exact, compact=True)
            except Exception as exc:
                recovery_log.write({'event': 'recovery_snapshot_unavailable', 'error': str(exc)})
            if state.get('screen') not in {'MAIN_MENU', 'GAME_OVER'} and not menu_action(state):
                health = self.mod.capture_health()
                if health.get('recovery_main_menu') is not True:
                    raise RuntimeError('Client capture Mod needs recovery_main_menu support; install the rebuilt Mod and restart the game')
                self._check_preparation_cancelled()
                recovery_log.write({'event': 'native_recovery_started', 'state': state})
                self.mod.recover_to_menu()
                state = self.mod.state()
            state = self._ensure_main_menu_for_new_run(state)
            recovery_log.write({'event': 'recovery_completed', 'state': state})
            self._set('stopped' if self.cancel_prepare.is_set() else 'idle', '已返回主菜单')
            self.restart_pending = False
        except Exception as exc:
            self._set("error", f"返回主菜单失败：{exc}")
            self.failure_streak += 1
            self.retry_at = time.monotonic() + min(60, 5 * self.failure_streak)
            if 'recovery_log' in locals():
                recovery_log.write({'event': 'recovery_failed', 'error': str(exc)})
        finally:
            with self.lock:
                self.recovering = False
        if (start_after or self.auto_start) and self.manager_mode == 'idle' and not self.cancel_prepare.is_set():
            self._start_prepare_thread()

    def _watchdog(self, process) -> None:
        worker = self._read_json(self.status_file)
        command = self._read_json(self.control_file)
        if worker.get('mode') == 'paused' or command.get('desired') == 'paused':
            self.progress_at = time.monotonic()
            return
        latest = worker.get('latest_action') or {}
        token = (worker.get('phase'), latest.get('sequence'), latest.get('status'),
                 worker.get('completed_combat_count'))
        if token != self.progress_token:
            self.progress_token, self.progress_at = token, time.monotonic()
        if time.monotonic() - self.progress_at < self.config['stall_seconds']:
            return
        # This aborts an invalid experiment, never produces a cheaper search result.
        evidence = {'status': 'WATCHDOG', 'at': time.time(), 'worker': worker,
                    'reason': 'No lifecycle progress before watchdog deadline'}
        try:
            evidence['client'] = self.observer.state()
        except Exception as exc:
            evidence['client_error'] = str(exc)
        write_json(self.session_dir / 'watchdog.json', evidence)
        self._write_control('stop')
        if os.name == 'nt':
            result = subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                    capture_output=True, timeout=15)
            if result.returncode and process.poll() is None:
                raise RuntimeError('Could not terminate stalled runner tree')
        else:
            process.terminate()
        process.wait(timeout=10)
        report = recover_report_tail(
            self._read_json(self.session_dir / 'run_report.json'),
            self.session_dir / 'session.jsonl',
        )
        report.update(status='WATCHDOG', error=evidence['reason'], finished_at=time.time())
        write_json(self.session_dir / 'run_report.json', report, compact=True)
        write_json(self.status_file, {'mode': 'error', 'error': evidence['reason']})

    def _monitor_loop(self) -> None:
        while not self._closed:
            time.sleep(0.5)
            with self.lock:
                process = self.process
                if self._snapshot_busy:
                    continue
            if process is not None and process.poll() is None and (self.auto_start or self.restart_pending):
                try:
                    self._watchdog(process)
                except Exception as exc:
                    self._set('error', str(exc))
            if process is not None and process.poll() is not None:
                with self.lock:
                    if self.process is not process:
                        continue
                    if self.output_handle is not None:
                        self.output_handle.close()
                    self.output_handle = None
                    self.process = None
                worker = self._read_json(self.status_file)
                worker_mode = worker.get("mode")
                if worker_mode == "completed":
                    self._set("completed", "本局战败，运行正常结束" if worker.get('outcome') == 'DEFEAT'
                              else "本局运行结束")
                elif worker_mode == "stopped":
                    self._set("stopped", "Agent 已安全停止，游戏保持当前界面")
                elif worker_mode == "error":
                    self._set("error", str(worker.get("error") or "运行器异常退出"))
                elif worker_mode == 'blocked':
                    self._set('blocked', str(worker.get('error') or '交互需要处理'))
                else:
                    self._set('error', f'运行器提前退出，退出码 {process.returncode}')
                try:
                    self._archive_exit(process.returncode)
                    if self.session_dir is not None:
                        self._record_batch_completion(self.session_dir)
                except Exception as exc:
                    self.auto_start = False
                    self.restart_pending = False
                    if self.run_plan_mode == 'batch' and self.batch_state in {'running', 'paused'}:
                        self.batch_state = 'interrupted'
                        self._save_batch_state()
                    self._set('error', f'Could not archive run; automatic recovery stopped: {exc}')
            if ((self.auto_start or self.restart_pending) and not self.auto_paused and not self._is_running()
                    and not self.preparing and not self.recovering and time.monotonic() >= self.retry_at):
                try:
                    if not self._automatic_runtime_ready():
                        continue
                    state = self.mod.state()
                    if state.get("screen") == "MAIN_MENU" and not menu_action(state):
                        self._start_prepare_thread()
                    else:
                        self._return_to_menu(start_after=True)
                except Exception as exc:
                    self._set('waiting_client', f'Waiting for client: {exc}')
                    self.retry_at = time.monotonic() + 5

    def _automatic_runtime_ready(self):
        health = self.mod.capture_health()
        if (health.get('recovery_main_menu') is not True
                or health.get('potion_target_contract') != 'native-no-creature-v1'):
            self._set('waiting_mod', '等待安装 STS2HumanCapture 0.4.1 并重启游戏；自动运行请求已保留')
            self.retry_at = time.monotonic() + 5
            return False
        built_capture = self.repo_root / 'mod' / 'STS2HumanCapture' / 'bin' / 'Release' / 'net9.0' / 'STS2HumanCapture.dll'
        identity_reader = getattr(self.mod, 'capture_identity', None)
        if built_capture.is_file() and callable(identity_reader):
            try:
                expected = hashlib.sha256(built_capture.read_bytes()).hexdigest()
                loaded = identity_reader().get('capture_assembly') or {}
                actual = str(loaded.get('sha256') or '').lower()
            except Exception as exc:
                self._set('waiting_mod', f'Capture Mod identity unavailable: {exc}')
                self.retry_at = time.monotonic() + 5
                return False
            if actual != expected:
                self._set('waiting_mod', 'Capture Mod DLL differs from the current workspace build; install and restart the game')
                self.retry_at = time.monotonic() + 5
                return False
        return True

    def _archive_exit(self, returncode):
        if self.session_dir is None:
            return
        path = self.session_dir / 'run_report.json'
        report = recover_report_tail(
            self._read_json(path), self.session_dir / 'session.jsonl'
        )
        if report.get('status') in {None, 'RUNNING', 'PREPARING'}:
            report.update(status='INTERRUPTED', error=f'Runner exited with code {returncode}')
        evidence = {'returncode': returncode, 'worker': self._read_json(self.status_file), 'at': time.time()}
        try:
            state = self.observer.state()
            evidence['client'] = state
            report.setdefault('terminal_client', state)
        except Exception as exc:
            evidence['client_error'] = str(exc)
        report.setdefault('finished_at', time.time())
        write_json(self.session_dir / 'exit_snapshot.json', evidence)
        write_json(path, report, compact=True)
        self.failure_streak = self.failure_streak + 1 if report.get('error') else 0
        self.retry_at = time.monotonic() + min(60, 3 + 5 * self.failure_streak)

    def _wait(self, predicate: Any, initial: Dict[str, Any], timeout: float = 20.0) -> Dict[str, Any]:
        state = initial
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(state):
                return state
            time.sleep(0.1)
            state = self.mod.state()
        raise RuntimeError(
            f"客户端状态等待超时：screen={state.get('screen')!r}, "
            f"actions={state.get('available_actions')!r}"
        )

    def _write_control(self, desired: str) -> None:
        with self.lock:
            path = self.control_file
            if path is None:
                return
            self.command_time = time.time()
            write_json(path, {"desired": desired, "updated_at": self.command_time,
                              "command_id": uuid.uuid4().hex})

    def _update_config(self, options: Dict[str, Any]) -> None:
        with self.lock:
            for key, low, high in (
                ("stall_seconds", 120, 7200),
                ("depth", 1, 16),
                ("search_ms", 100, 60000),
                ("delay_ms", 0, 5000),
            ):
                if key in options:
                    self.config[key] = max(low, min(high, int(options[key])))
            if 'max_workers' in options:
                self.config['max_workers'] = max(1, min(32, int(options['max_workers'])))
            if 'worker_mode' in options:
                worker_mode = str(options['worker_mode'])
                if worker_mode not in {'fixed', 'adaptive'}:
                    raise ValueError(f'Unsupported worker mode: {worker_mode}')
                self.config['worker_mode'] = worker_mode
            if 'scoring_model_id' in options:
                model_id = str(options['scoring_model_id'])
                self.comparison.models.get(model_id)
                self.config['scoring_model_id'] = model_id

    def _is_running(self) -> bool:
        with self.lock:
            return self.process is not None and self.process.poll() is None

    def _set(self, mode: str, message: str) -> None:
        with self.lock:
            self.manager_mode = mode
            self.message = message

    @staticmethod
    def _read_json(path: Optional[Path]) -> Dict[str, Any]:
        if path is None or not path.exists():
            return {}
        try:
            value = read_json(path)
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _tail(path: Optional[Path], limit: int) -> list[str]:
        if path is None or not path.exists():
            return []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                return list(deque(stream, maxlen=limit))
        except OSError:
            return []


def make_handler(manager: DashboardManager, ui_path: Path, server: ThreadingHTTPServer,
                 history_only: bool = False):
    card_art = CardArtStore.discover()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            assets = {'/assets/dashboard.css': ('dashboard.css', 'text/css; charset=utf-8'),
                      '/assets/shared_ui.css': ('shared_ui.css', 'text/css; charset=utf-8'),
                      '/assets/sandbox.css': ('sandbox.css', 'text/css; charset=utf-8'),
                      '/assets/sandbox.js': ('sandbox.js', 'text/javascript; charset=utf-8'),
                      '/assets/human_capture.css': ('human_capture.css', 'text/css; charset=utf-8'),
                      '/assets/human_capture.js': ('human_capture.js', 'text/javascript; charset=utf-8'),
                      '/assets/combat_comparison.css': ('combat_comparison.css', 'text/css; charset=utf-8'),
                      '/assets/combat_comparison.js': ('combat_comparison.js', 'text/javascript; charset=utf-8'),
                      '/assets/combat_snapshots.js': ('combat_snapshots.js', 'text/javascript; charset=utf-8'),
                      '/assets/storage.js': ('storage.js', 'text/javascript; charset=utf-8'),
                      '/assets/dashboard.js': ('dashboard.js', 'text/javascript; charset=utf-8'),
                      '/assets/deck_profile.css': ('deck_profile.css', 'text/css; charset=utf-8'),
                      '/assets/deck_profile_editor.js': ('deck_profile_editor.js', 'text/javascript; charset=utf-8'),
                      '/assets/lucide.min.js': ('lucide.min.js', 'text/javascript; charset=utf-8')}
            if parsed.path == '/favicon.ico':
                self._send(204, b'', 'image/x-icon')
                return
            if parsed.path == '/sandbox':
                self._send(200, (ui_path.parent / 'sandbox.html').read_bytes(), 'text/html; charset=utf-8')
                return
            if parsed.path == '/human-capture':
                self._send(200, (ui_path.parent / 'human_capture.html').read_bytes(), 'text/html; charset=utf-8')
                return
            if parsed.path == '/sandbox/guide':
                import html
                guide = (manager.repo_root / 'docs' / 'SANDBOX_USER_GUIDE.md').read_text(encoding='utf-8')
                page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>沙盒使用说明</title><style>body{max-width:900px;margin:32px auto;padding:0 20px;font:15px/1.9 system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}a{color:#167153}</style><a href="/sandbox">返回沙盒</a><pre>' + html.escape(guide) + '</pre></html>'
                self._send(200, page.encode('utf-8'), 'text/html; charset=utf-8')
                return
            if parsed.path.startswith('/api/sandbox/'):
                try:
                    if parsed.path == '/api/sandbox/catalog':
                        self._json(200, manager.sandbox.catalog())
                    elif parsed.path == '/api/sandbox/state':
                        self._json(200, manager.sandbox.status())
                    elif parsed.path == '/api/sandbox/export':
                        self._send(200, json.dumps(manager.sandbox.store.dataset(), ensure_ascii=False).encode('utf-8'), 'application/json; charset=utf-8')
                    else:
                        self._json(404, {'error': 'not found'})
                except (ValueError, OSError, KeyError) as exc:
                    self._json(400, {'error': str(exc)})
                return
            if parsed.path.startswith('/api/human-capture/'):
                try:
                    if parsed.path == '/api/human-capture/status':
                        self._json(200, manager.human_capture.status())
                    elif parsed.path == '/api/human-capture/sessions':
                        self._json(200, {'sessions': manager.human_capture.sessions()})
                    elif parsed.path == '/api/human-capture/session':
                        self._json(200, manager.human_capture.session(query.get('id', [''])[0]))
                    else:
                        self._json(404, {'error': 'not found'})
                except (ValueError, OSError, json.JSONDecodeError) as exc:
                    self._json(400, {'error': str(exc)})
                return
            if parsed.path.startswith('/api/comparison/'):
                try:
                    if parsed.path == '/api/comparison/catalog':
                        self._json(200, manager.comparison.catalog())
                    elif parsed.path == '/api/comparison/models':
                        self._json(200, {'models': [{key: row[key] for key in ('id', 'name', 'source')}
                                                     for row in manager.comparison.models.catalog()['models']]})
                    elif parsed.path == '/api/comparison/job':
                        self._json(200, manager.comparison.job(query.get('id', [''])[0]))
                    elif parsed.path == '/api/comparison/pair':
                        self._json(200, manager.comparison.pair(query.get('id', [''])[0],
                                                               query.get('snapshot', [''])[0]))
                    else:
                        self._json(404, {'error': 'not found'})
                except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
                    self._json(400, {'error': str(exc)})
                return
            if parsed.path == '/api/engine-consistency':
                try:
                    self._json(200, {**load_gate(manager.log_root),
                                     'current_version_allowed': manager._headless_gate_allowed()})
                except (ValueError, OSError, json.JSONDecodeError) as exc:
                    self._json(400, {'error': str(exc)})
                return
            if parsed.path == '/api/headless-runs':
                self._json(200, manager.headless_batch.status())
                return
            if parsed.path in assets:
                name, content_type = assets[parsed.path]
                self._send(200, (ui_path.parent / name).read_bytes(), content_type)
                return
            if parsed.path == '/api/card-art':
                try:
                    result = card_art.get(query.get('id', [''])[0])
                    if result is None:
                        raise ValueError('Card art is unavailable')
                    body, content_type = result
                    self._send(200, body, content_type, 'private, max-age=86400')
                except (KeyError, OSError, UnicodeError, ValueError) as exc:
                    self._json(404, {'error': str(exc)})
                return
            if parsed.path in {'/api/sessions', '/api/session', '/api/download', '/api/card-catalog'}:
                try:
                    if parsed.path == '/api/card-catalog':
                        profile = load_deck_profile(manager.deck_profile_path)
                        self._json(200, {'cards': load_card_catalog(
                            manager.repo_root, str(profile.get('character') or 'IRONCLAD'))})
                        return
                    if parsed.path == '/api/sessions':
                        self._json(200, manager.history_sessions(
                            int(query.get('offset', ['0'])[0]),
                            int(query.get('limit', ['20'])[0]),
                            int(query.get('summary_n', ['10'])[0])))
                        return
                    session_id = query.get('id', [''])[0]
                    archived = session_id.startswith('live_dashboard/archive/')
                    directory = manager.archive_path(session_id) if archived else manager.session_path(session_id)
                    if parsed.path == '/api/session':
                        self._json(200, json.loads(manager._archived_file(directory, 'run_report.json'))
                                   if archived else manager._read_json(directory / 'run_report.json'))
                        return
                    name = query.get('file', ['run_report.json'])[0]
                    if name not in {'run_report.json', 'session.jsonl'}:
                        raise ValueError('Unknown download')
                    body = manager._archived_file(directory, name) if archived else read_shared_bytes(directory / name)
                    self._send(200, body, 'application/octet-stream')
                except (ValueError, OSError, KeyError, zipfile.BadZipFile) as exc:
                    self._json(404, {'error': str(exc)})
                return
            if parsed.path == '/api/storage':
                try:
                    self._json(200, manager.storage_status())
                except (ValueError, OSError) as exc:
                    self._json(400, {'error': str(exc)})
                return
            if self.path == "/" or self.path.startswith("/?"):
                self._send(200, ui_path.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/combat-snapshots":
                rows = snapshot_index(manager.log_root)
                compatibility = snapshot_compatibility(CliConfig(repo_root=manager.repo_root))
                auto = sorted((row for row in rows if (row.get('context') or {}).get('source')
                               == 'automatic_validation_set'),
                              key=lambda row: (float(row.get('created_at_utc') or 0),
                                               str(row.get('snapshot_id') or '')))
                for row in auto:
                    if row.get('compatibility') != compatibility:
                        row.update(status='STALE_REVALIDATION', reusable=False)
                allocated = []
                reserve = []
                for row in auto:
                    act = (row.get('context') or {}).get('act')
                    act_limit = min(VALIDATION_SET_LIMIT, {1: 24, 2: 16, 3: 8}.get(act, 8))
                    if (len(allocated) >= VALIDATION_SET_LIMIT or
                            sum((item.get('context') or {}).get('act') == act
                                for item in allocated) >= act_limit):
                        reserve.append(row)
                        if row.get('status') == 'RESTORE_VERIFIED':
                            row['status'] = 'RESERVE_VERIFIED'
                    else:
                        allocated.append(row)
                valid_rows = [row for row in allocated if row.get('status') == 'RESTORE_VERIFIED']
                valid = len(valid_rows)
                acts = {str(act): sum((row.get('context') or {}).get('act') == act
                                      for row in valid_rows) for act in (1, 2, 3)}
                self._json(200, {"snapshots": rows, "history_only": history_only,
                                 "validation_set": {"valid": valid,
                                                    "target": VALIDATION_SET_LIMIT,
                                                    "stale": sum(row.get('status') == 'STALE_REVALIDATION'
                                                                 for row in allocated),
                                                    "reserve": len(reserve),
                                                    "acts": acts,
                                                    "encounters": len({(row.get('context') or {}).get('encounter_id')
                                                                       for row in valid_rows})}})
            elif self.path.startswith("/api/status"):
                status = manager.status()
                status['history_only'] = history_only
                self._json(200, status)
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path in {'/api/headless-runs/start', '/api/headless-runs/stop'}:
                if history_only:
                    self._json(403, {'error': 'History-only dashboard cannot start runs'})
                    return
                try:
                    length = int(self.headers.get('Content-Length') or 0)
                    if not 0 < length <= 4096:
                        raise ValueError('Invalid headless run request size')
                    payload = json.loads(self.rfile.read(length).decode('utf-8'))
                    if not isinstance(payload, dict):
                        raise ValueError('Request must be an object')
                    if self.path.endswith('/start'):
                        if manager._is_running() or manager.preparing:
                            raise ValueError('请先结束当前可视化对局')
                        result = manager.headless_batch.start(
                            int(payload.get('count')), int(payload.get('parallel')),
                            str(payload.get('scoring_model_id') or 'active'),
                            int(manager.config['depth']), int(manager.config['search_ms']),
                            int(manager.config['max_workers']))
                    else:
                        result = manager.headless_batch.stop()
                    self._json(200, result)
                except (ValueError, OSError, TypeError, json.JSONDecodeError) as exc:
                    self._json(400, {'error': str(exc)})
                return
            if self.path == '/api/engine-consistency':
                try:
                    length = int(self.headers.get('Content-Length') or 0)
                    if not 0 < length <= 65536:
                        raise ValueError('Invalid consistency request size')
                    payload = json.loads(self.rfile.read(length).decode('utf-8'))
                    ids = payload.get('snapshot_ids') if isinstance(payload, dict) else None
                    if ids is not None and (not isinstance(ids, list) or not all(isinstance(item, str) for item in ids)):
                        raise ValueError('snapshot_ids must be a list of strings')
                    self._json(200, run_check(manager.repo_root, manager.log_root, ids))
                except (ValueError, OSError, json.JSONDecodeError) as exc:
                    self._json(400, {'error': str(exc)})
                return
            if self.path.startswith('/api/comparison/'):
                try:
                    length = int(self.headers.get('Content-Length') or 0)
                    if not 0 < length <= 65536:
                        raise ValueError('Invalid comparison request size')
                    payload = json.loads(self.rfile.read(length).decode('utf-8'))
                    if not isinstance(payload, dict):
                        raise ValueError('Comparison request must be an object')
                    if self.path == '/api/comparison/model':
                        result = manager.comparison.models.save(payload.get('name', ''), payload.get('weights'), payload.get('ability_config'))
                    elif self.path == '/api/comparison/model/delete':
                        manager.comparison.models.delete(str(payload.get('id') or ''))
                        result = {'ok': True}
                    elif self.path == '/api/comparison/model/rename':
                        result = manager.comparison.models.rename(str(payload.get('id') or ''),
                                                                  payload.get('name', ''))
                    elif self.path == '/api/comparison/start':
                        result = manager.comparison.start(str(payload.get('a_id') or ''),
                                                          str(payload.get('b_id') or ''),
                                                          payload.get('snapshots'))
                    elif self.path == '/api/comparison/stop':
                        result = manager.comparison.stop(str(payload.get('id') or ''))
                    else:
                        self._json(404, {'error': 'not found'})
                        return
                    self._json(200, result)
                except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
                    self._json(400, {'error': str(exc)})
                return
            if self.path == '/api/sessions/delete':
                try:
                    length = int(self.headers.get('Content-Length') or 0)
                    if not 0 < length <= 4096:
                        raise ValueError('Invalid delete request size')
                    payload = json.loads(self.rfile.read(length).decode('utf-8'))
                    if not isinstance(payload, dict) or not isinstance(payload.get('id'), str):
                        raise ValueError('Session id is required')
                    manager.delete_session(payload['id'])
                    self._json(200, {'ok': True})
                except (ValueError, OSError, json.JSONDecodeError) as exc:
                    self._json(400, {'error': str(exc)})
                return
            if self.path in {'/api/storage/archive', '/api/storage/delete',
                             '/api/storage/stale-snapshots/delete', '/api/storage/replay-cache/clear'}:
                try:
                    length = int(self.headers.get('Content-Length') or 0)
                    if length < 1 or length > 4096:
                        raise ValueError('Invalid storage request size')
                    payload = json.loads(self.rfile.read(length).decode('utf-8'))
                    if not isinstance(payload, dict):
                        raise ValueError('Invalid storage request')
                    if self.path == '/api/storage/archive':
                        result = manager.archive_session(str(payload.get('id') or ''))
                    elif self.path == '/api/storage/delete':
                        manager.delete_archive(str(payload.get('id') or ''))
                        result = {'ok': True}
                    elif self.path == '/api/storage/stale-snapshots/delete':
                        result = {'removed': manager.delete_stale_snapshots()}
                    else:
                        result = {'removed': manager.clear_replay_cache()}
                    self._json(200, result)
                except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
                    self._json(400, {'error': str(exc)})
                return
            if self.path == '/api/human-capture/command':
                try:
                    length = int(self.headers.get('Content-Length') or 0)
                    if not 0 < length <= 16384:
                        raise ValueError('Invalid human-capture command size')
                    payload = json.loads(self.rfile.read(length).decode('utf-8'))
                    self._json(200, manager.human_capture.command(
                        str(payload.get('command') or ''), payload.get('options') or {}
                    ))
                except Exception as exc:
                    self._json(400, {'error': str(exc)})
                return
            if self.path == '/api/sandbox/command':
                try:
                    length = int(self.headers.get('Content-Length') or 0)
                    if not 0 < length <= 65536:
                        raise ValueError('Invalid sandbox command size')
                    payload = json.loads(self.rfile.read(length).decode('utf-8'))
                    self._json(202, manager.sandbox.submit(payload['command'], payload.get('options') or {},
                                                         payload['revision'], payload['request_id']))
                except (ValueError, KeyError, OSError) as exc:
                    self._json(409, {'error': str(exc)})
                return
            if self.path != "/api/command":
                self._json(404, {"error": "not found"})
                return
            if history_only:
                self._json(403, {'error': 'History-only dashboard cannot control the runner'})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length < 1 or length > 16384:
                    raise ValueError('Invalid command size')
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                command = str(payload.get("command") or "")
                if command == "shutdown":
                    manager.close()
                    self._json(200, {"ok": True, "message": "控制台正在退出"})
                    def stop_after_runner():
                        while manager._is_running() or manager.preparing:
                            time.sleep(0.1)
                        server.shutdown()
                    threading.Thread(target=stop_after_runner, daemon=True).start()
                    return
                result = manager.command(command, payload.get("options") or {})
                self._json(200, result)
            except Exception as exc:
                self._json(400, {"error": str(exc)})

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _json(self, status: int, value: Dict[str, Any]) -> None:
            self._send(
                status,
                json.dumps(value, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _send(self, status: int, body: bytes, content_type: str,
                  cache_control: str = 'no-store') -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header('Cache-Control', cache_control)
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return

    return Handler


class DashboardServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main() -> None:
    parser = argparse.ArgumentParser(description="Local control panel for visible STS2 Agent runs")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--mod-url", default="http://127.0.0.1:8080")
    parser.add_argument("--log-root", type=Path, default=Path("logs/live_dashboard"))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument('--continue-anchor', type=Path)
    parser.add_argument('--resume-report', type=Path)
    parser.add_argument('--recover-pending-selection', action='store_true')
    parser.add_argument('--anchor-room', action='store_true')
    parser.add_argument('--replay-room-report', type=Path)
    parser.add_argument('--completed-combats', type=int, default=0)
    parser.add_argument('--target-combats', type=int, default=0, help='Deprecated; dashboard runs until game over')
    parser.add_argument('--sandbox-dir', type=Path)
    parser.add_argument('--capture-url', default='http://127.0.0.1:9878')
    parser.add_argument('--game-root', type=Path)
    parser.add_argument('--history-only', action='store_true', help='Review and delete history without runner controls')
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("The dashboard intentionally binds to localhost only")
    repo_root = Path(__file__).resolve().parents[1]
    manager = DashboardManager(repo_root, (repo_root / args.log_root).resolve(), args.mod_url,
                               args.sandbox_dir.resolve() if args.sandbox_dir else None,
                               args.capture_url,
                               args.game_root.resolve() if args.game_root else None)
    try:
        server = DashboardServer((args.host, args.port), lambda *a, **kw: None)
    except OSError:
        manager.close()
        raise
    server.RequestHandlerClass = make_handler(
        manager, repo_root / "ui" / "live_dashboard.html", server, args.history_only
    )
    if args.continue_anchor:
        session_dir = manager.log_root / datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        session_dir.mkdir(parents=True, exist_ok=False)
        anchor = session_dir / 'official_map_anchor.save'
        shutil.copy2(args.continue_anchor.resolve(), anchor)
        manager.config['target_combats'] = args.target_combats
        manager._launch_runner(session_dir, anchor, args.completed_combats, args.resume_report,
                               args.recover_pending_selection, args.anchor_room, args.replay_room_report)
        manager._set('running', '从已验证地图存档继续；首个动作前暂停')
    url = f"http://{args.host}:{args.port}/"
    print(json.dumps({"status": "ready", "url": url}, ensure_ascii=False), flush=True)
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        manager.close()
        server.server_close()


if __name__ == "__main__":
    main()
