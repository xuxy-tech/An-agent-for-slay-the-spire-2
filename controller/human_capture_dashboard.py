"""Dashboard process control for real-player capture and training tasks."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from cli.sts2_mod_adapter import ModClientConfig, Sts2ModAdapter
from controller.human_capture import AUTHORITATIVE_SNAPSHOT_SCHEMA, CAPTURE_PROTOCOL, audit_capture_session
from controller.live_session import write_json


CORE_CASES = (
    "play_card",
    "end_turn",
    "use_potion:no_manual_target",
    "use_potion:manual_target",
    "source:player_choice",
)


class HumanCaptureDashboard:
    def __init__(
        self,
        repo_root: Path,
        observer_url: str = "http://127.0.0.1:8080",
        capture_url: str = "http://127.0.0.1:9878",
        game_root: Path | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.observer_url = observer_url
        self.capture_url = capture_url
        self.raw_root = self.repo_root / "data" / "human_play" / "raw"
        self.leaf_root = self.repo_root / "data" / "human_play" / "leaf"
        self.experiment_root = self.repo_root / "data" / "human_play" / "experiments" / "current_turn_join"
        self.model_root = self.repo_root / "data" / "human_play" / "models"
        self.task_root = self.repo_root / "data" / "human_play" / "dashboard_tasks"
        self.game_root = game_root or Path(os.environ.get(
            "STS2_GAME_ROOT", r"D:\SteamLibrary\steamapps\common\Slay the Spire 2"
        ))
        self.lock = threading.RLock()
        self.process: subprocess.Popen[str] | None = None
        self.output_handle: Any = None
        self.output_file: Path | None = None
        self.stop_file: Path | None = None
        self.result_file: Path | None = None
        self.task_dir: Path | None = None
        self.job: str | None = None
        self.mode = "idle"
        self.message = "采集控制已就绪"
        self.returncode: int | None = None
        self._closed = False
        self.monitor = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor.start()

    def command(self, command: str, options: dict[str, Any]) -> dict[str, Any]:
        if command == "start":
            self._require_idle()
            self._require_capture_ready()
            task_dir = self._new_task_dir("collect")
            stop_file = task_dir / "control.json"
            command_line = [
                sys.executable, "-X", "utf8", "-m", "scripts.collect_human_play",
                "--observer-url", self.observer_url,
                "--capture-url", self.capture_url,
                "--output", str(self.raw_root),
                "--stop-file", str(stop_file),
            ]
            max_actions = options.get("max_actions")
            if max_actions not in (None, ""):
                value = int(max_actions)
                if not 1 <= value <= 100000:
                    raise ValueError("max_actions must be between 1 and 100000")
                command_line.extend(["--max-actions", str(value)])
            self._launch("collect", command_line, task_dir, stop_file=stop_file)
            self.message = "正在记录玩家动作；后端 Agent 未启动"
        elif command == "stop":
            with self.lock:
                if not self._running() or self.job != "collect" or self.stop_file is None:
                    raise ValueError("没有正在运行的采集任务")
                write_json(self.stop_file, {"desired": "stop", "updated_at": time.time()})
                self.mode = "stopping"
                self.message = "正在完成未决动作归因并关闭会话"
        elif command == "audit":
            self._require_idle()
            session = self._session_path(str(options.get("session_id") or ""))
            task_dir = self._new_task_dir("audit")
            result_file = task_dir / "audit.json"
            command_line = [
                sys.executable, "-X", "utf8", "-m", "scripts.audit_human_capture",
                "--input", str(session), "--output", str(result_file),
            ]
            if options.get("strict", True):
                for case in CORE_CASES:
                    command_line.extend(["--require", case])
            self._launch("audit", command_line, task_dir, result_file=result_file)
            self.message = "正在审计采集完整性与覆盖项"
        elif command == "generate_current":
            self._require_idle()
            if not [row for row in self.sessions() if not row.get("historical") and row.get("status") != "failed"]:
                raise ValueError("\u6ca1\u6709\u53ef\u7528\u4e8e\u5f53\u524d\u7248\u672c\u91cd\u653e\u7684\u4f1a\u8bdd\uff1b\u8bf7\u91cd\u65b0\u91c7\u96c6\u5f53\u524d headless \u5feb\u7167\u534f\u8bae")
            task_dir = self._new_task_dir("generate_current")
            output = self.leaf_root / "current_turns.jsonl"
            report = self.leaf_root / "current_turns.report.json"
            command_line = [
                sys.executable, "-X", "utf8", "-m", "scripts.generate_human_counterfactuals",
                "--human-input", str(self.raw_root), "--output", str(output), "--resume",
                "--report", str(report),
            ]
            self._launch("generate_current", command_line, task_dir, result_file=report)
            self.message = "\u6b63\u5728\u7528\u5f53\u524d\u641c\u7d22\u5668\u91cd\u653e\u91c7\u96c6\u5feb\u7167\u5e76\u751f\u6210\u53f6\u8282\u70b9\u6570\u636e"
        elif command == "join_current":
            self._require_idle()
            leaf = self.leaf_root / "current_turns.jsonl"
            if not leaf.is_file():
                raise ValueError("\u8bf7\u5148\u751f\u6210\u5f53\u524d\u7248\u672c\u53f6\u8282\u70b9\u6570\u636e")
            task_dir = self._new_task_dir("join_current")
            command_line = [
                sys.executable, "-X", "utf8", "-m", "scripts.join_human_leaf_preferences",
                "--human-input", str(self.raw_root), "--leaf-input", str(leaf),
                "--output", str(self.experiment_root),
            ]
            self._launch("join_current", command_line, task_dir, result_file=self.experiment_root / "report.json")
            self.message = "\u6b63\u5728\u628a\u5f53\u524d\u7248\u672c\u7684\u4eba\u7c7b\u9009\u62e9\u4e0e\u641c\u7d22\u53f6\u8282\u70b9\u4e25\u683c\u5339\u914d"
        elif command == "train_current":
            self._require_idle()
            matches = self.experiment_root / "matches.jsonl"
            if not matches.is_file():
                raise ValueError("\u8bf7\u5148\u751f\u6210\u5f53\u524d\u7248\u672c\u5339\u914d\u96c6")
            task_dir = self._new_task_dir("train_current")
            output = self.model_root / datetime.now().strftime("current_%Y%m%d_%H%M%S_%f.json")
            command_line = [
                sys.executable, "-X", "utf8", "-m", "scripts.train_human_leaf_preferences",
                "--input", str(matches), "--output", str(output),
                "--progress", str(task_dir / "progress.json"),
            ]
            self._launch("train_current", command_line, task_dir, result_file=output)
            self.message = "\u6b63\u5728\u8bad\u7ec3\u5f53\u524d\u7248\u672c\u8bc4\u5206\u53c2\u6570\uff1b\u5b8c\u6210\u540e\u4f1a\u51fa\u73b0\u5728\u6218\u6597\u8bc4\u5206\u6a21\u578b\u4e2d"
        elif command == "delete_incompatible":
            self._require_idle()
            if options.get("confirm") is not True:
                raise ValueError("删除不兼容采集会话需要确认")
            incompatible = [row for row in self.sessions() if row.get("historical")]
            for row in incompatible:
                session = self._session_path(str(row["id"]))
                root = self.raw_root.resolve()
                if session.parent != root:
                    raise ValueError("只能删除 raw 根目录下的会话")
                shutil.rmtree(session)
            self.message = f"已删除 {len(incompatible)} 个不兼容采集会话"
        elif command == "delete_session":
            self._require_idle()
            if options.get("confirm") is not True:
                raise ValueError("删除真实对局数据需要确认")
            session_id = str(options.get("session_id") or "")
            session = self._session_path(session_id)
            root = self.raw_root.resolve()
            if session.parent != root:
                raise ValueError("只能删除 raw 根目录下的单个采集会话")
            shutil.rmtree(session)
            self.message = f"已删除真实对局会话 {session_id}；如已构建数据集，请重新构建"
        else:
            raise ValueError(f"unknown human-capture command: {command}")
        return self.status()

    def status(self) -> dict[str, Any]:
        with self.lock:
            running = self._running()
            mode = self.mode
            job = self.job
            message = self.message
            returncode = self.returncode
            output_file = self.output_file
            result_file = self.result_file
            task_dir = self.task_dir
        return {
            "mode": mode,
            "job": job,
            "message": message,
            "running": running,
            "returncode": returncode,
            "observer": self._probe("observer"),
            "capture": self._probe("capture"),
            "installation": self._installation_status(),
            "latest_session_id": self.sessions()[0]["id"] if self.sessions() else None,
            "current_learning": {
                "generation_report": self._read_json(self.leaf_root / "current_turns.report.json"),
                "training_progress": self._read_json(task_dir / "progress.json")
                if task_dir is not None and job == "train_current" else {},
                "leaf_exists": (self.leaf_root / "current_turns.jsonl").is_file()
                and (self.leaf_root / "current_turns.jsonl").stat().st_size > 0,
                "compatible_session_count": sum(1 for row in self.sessions() if not row.get("historical") and row.get("status") != "failed"),
                "join_report": self._read_json(self.experiment_root / "report.json"),
                "model_count": len(list(self.model_root.glob("*.json"))) if self.model_root.is_dir() else 0,
            },
            "last_result": self._result(result_file),
            "recent_log": self._tail(output_file, 60),
        }

    def sessions(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if not self.raw_root.is_dir():
            return result
        for path in self.raw_root.iterdir():
            events = path / "events.jsonl"
            if not path.is_dir() or not events.is_file():
                continue
            manifest = self._read_json(path / "manifest.json")
            protocol = str(((manifest.get("capture_health") or {}).get("protocol_version") or ""))
            snapshot_schema = str(((manifest.get("capture_health") or {}).get("snapshot_schema") or ""))
            result.append({
                "id": path.name,
                "status": manifest.get("status", "unknown"),
                "capture_protocol": protocol or None,
                "historical": protocol != CAPTURE_PROTOCOL or snapshot_schema != AUTHORITATIVE_SNAPSHOT_SCHEMA,
                "snapshot_schema": snapshot_schema or None,
                "started_at_utc": manifest.get("started_at_utc"),
                "closed_at_utc": manifest.get("closed_at_utc"),
                "bytes": events.stat().st_size,
                "updated_at": events.stat().st_mtime,
            })
        return sorted(result, key=lambda row: row["updated_at"], reverse=True)

    def session(self, session_id: str) -> dict[str, Any]:
        path = self._session_path(session_id)
        events = path / "events.jsonl"
        return {
            "id": path.name,
            "manifest": self._read_json(path / "manifest.json"),
            "audit": audit_capture_session(events),
            "recent_records": self._tail_json(events, 20),
        }

    def close(self) -> None:
        self._closed = True
        with self.lock:
            if self._running() and self.job == "collect" and self.stop_file is not None:
                write_json(self.stop_file, {"desired": "stop", "updated_at": time.time()})

    def _launch(
        self,
        job: str,
        command: list[str],
        task_dir: Path,
        *,
        stop_file: Path | None = None,
        result_file: Path | None = None,
    ) -> None:
        output = task_dir / "task.log"
        handle = output.open("w", encoding="utf-8")
        try:
            process = subprocess.Popen(
                command,
                cwd=self.repo_root,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except Exception:
            handle.close()
            raise
        with self.lock:
            self.process = process
            self.output_handle = handle
            self.output_file = output
            self.stop_file = stop_file
            self.result_file = result_file
            self.task_dir = task_dir
            self.job = job
            self.mode = "collecting" if job == "collect" else "working"
            self.returncode = None

    def _monitor_loop(self) -> None:
        while not self._closed:
            time.sleep(0.25)
            with self.lock:
                process = self.process
            if process is None or process.poll() is None:
                continue
            with self.lock:
                if self.process is not process:
                    continue
                if self.output_handle is not None:
                    self.output_handle.close()
                self.output_handle = None
                self.process = None
                self.returncode = process.returncode
                if self.job == "accept" and self.task_dir is not None:
                    reports = sorted((self.task_dir / "acceptance").glob("acceptance_*/acceptance_report.json"))
                    self.result_file = reports[-1] if reports else None
                if process.returncode == 0:
                    self.mode = "completed"
                    self.message = "采集会话已安全关闭" if self.job == "collect" else "任务已完成"
                else:
                    self.mode = "error"
                    self.message = f"任务退出码 {process.returncode}；请查看日志"

    def _require_idle(self) -> None:
        if self._running():
            raise ValueError("已有采集或流水线任务正在运行")

    def _require_capture_ready(self) -> None:
        config = ModClientConfig(
            base_url=self.observer_url,
            capture_base_url=self.capture_url,
            timeout_s=1.0,
        )
        client = Sts2ModAdapter(config)
        client.health()
        client.capture_health()

    def _probe(self, kind: str) -> dict[str, Any]:
        config = ModClientConfig(
            base_url=self.observer_url,
            capture_base_url=self.capture_url,
            timeout_s=0.35,
        )
        client = Sts2ModAdapter(config)
        try:
            data = client.health() if kind == "observer" else client.capture_health()
            return {"connected": True, "data": data}
        except Exception as exc:
            return {"connected": False, "error": str(exc)}

    def _installation_status(self) -> dict[str, Any]:
        built = self.repo_root / "mod" / "STS2HumanCapture" / "bin" / "Release" / "net9.0" / "STS2HumanCapture.dll"
        installed = self.game_root / "mods" / "STS2HumanCapture" / "STS2HumanCapture.dll"
        built_hash = self._sha256(built)
        installed_hash = self._sha256(installed)
        return {
            "built_path": str(built),
            "built_sha256": built_hash,
            "installed_path": str(installed),
            "installed_sha256": installed_hash,
            "installed": installed_hash is not None,
            "matches_build": bool(built_hash and installed_hash and built_hash == installed_hash),
            "install_command": "powershell -ExecutionPolicy Bypass -File scripts/install_human_capture.ps1",
        }

    def _new_task_dir(self, job: str) -> Path:
        path = self.task_root / datetime.now().strftime(f"%Y%m%d_%H%M%S_%f_{job}")
        path.mkdir(parents=True, exist_ok=False)
        return path

    def _session_path(self, session_id: str) -> Path:
        root = self.raw_root.resolve()
        path = (root / session_id).resolve()
        if not session_id or not path.is_relative_to(root) or path == root or not (path / "events.jsonl").is_file():
            raise ValueError("Unknown human-capture session")
        return path

    def _result(self, path: Path | None) -> dict[str, Any] | None:
        return self._read_json(path) if path is not None and path.is_file() else None

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _tail(path: Path | None, limit: int) -> list[str]:
        if path is None or not path.is_file():
            return []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                return [line.rstrip("\r\n") for line in deque(stream, maxlen=limit)]
        except OSError:
            return []

    @staticmethod
    def _tail_json(path: Path, limit: int) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for line in HumanCaptureDashboard._tail(path, limit):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                result.append(value)
        return result

    @staticmethod
    def _sha256(path: Path) -> str | None:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        except OSError:
            return None

    def _running(self) -> bool:
        return self.process is not None and self.process.poll() is None
