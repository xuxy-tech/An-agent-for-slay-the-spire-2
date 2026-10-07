from __future__ import annotations

import json
import locale
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from cli.runtime_paths import resolve_dotnet


@dataclass
class CliConfig:
    repo_root: Path
    dotnet_path: Optional[Path] = None
    dll_relpath: Path = field(default_factory=lambda: Path(os.environ.get(
        "STS2_HEADLESS_DLL", "third_party/sts2-cli/src/Sts2Headless/bin/Release/net9.0/Sts2Headless.dll")))


class Sts2CliAdapter:
    def __init__(self, config: CliConfig):
        self.config = config
        self._proc: Optional[subprocess.Popen[bytes]] = None
        self.protocol_capabilities: frozenset[str] = frozenset()
        self.last_call_ms: float = 0.0
        # Per-response transport probes. queue_wait_ms covers the interval from
        # beginning to wait for stdout until a complete JSON line is available;
        # json_parse_ms measures Python decoding only. Combined with the
        # engine-side timings in each response, this isolates serialization and
        # pipe-transfer overhead without changing the wire protocol.
        self.last_response_bytes: int = 0
        self.last_response_queue_wait_ms: float = 0.0
        self.last_json_parse_ms: float = 0.0
        self._stderr_thread: Optional[threading.Thread] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stdout_queue: queue.Queue[Optional[str]] = queue.Queue()
        self._stdout_error: Optional[str] = None
        self._stderr_tail: deque[str] = deque(maxlen=40)

    def start(self) -> Dict[str, Any]:
        if self._proc is not None:
            raise RuntimeError("CLI process already started")
        started = time.perf_counter()
        dotnet_path = resolve_dotnet(self.config.dotnet_path)
        dll_path = self.config.repo_root / self.config.dll_relpath
        if not dll_path.is_file():
            raise FileNotFoundError(
                f"Headless runtime not found: {dll_path}. Build Sts2Headless before running the agent."
            )
        cmd = [str(dotnet_path), str(dll_path)]
        env = os.environ.copy()
        env.setdefault("STS2_STRICT_RESTORE", "1")
        self._stdout_queue = queue.Queue()
        self._stdout_error = None
        self._stderr_tail.clear()
        self._proc = subprocess.Popen(
            cmd,
            cwd=str(self.config.repo_root / "third_party/sts2-cli"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            # Search-state responses are multi-kilobyte single-line JSON. An
            # unbuffered binary pipe makes FileIO.readline advance in tiny reads;
            # with one reader thread per worker this creates severe GIL and pipe
            # contention on Windows. A 64 KiB buffer keeps each response within
            # one buffered read while stdin.flush() still preserves request order.
            bufsize=64 * 1024,
            env=env,
        )
        self._start_stderr_drain()
        self._start_stdout_reader()
        result = self._read_json(timeout_s=20.0)
        self.protocol_capabilities = frozenset(result.get("protocol_capabilities") or [])
        self.last_call_ms = (time.perf_counter() - started) * 1000.0
        return result

    def require_capabilities(self, required) -> None:
        missing = set(required) - self.protocol_capabilities
        if missing:
            raise RuntimeError(f"Headless runtime lacks protocol capabilities {sorted(missing)}; "
                               "rebuild Sts2Headless and restart the runner")

    def send(self, payload: Dict[str, Any], timeout_s: Optional[float] = None) -> Dict[str, Any]:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("CLI process not started")
        started = time.perf_counter()
        request = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self._proc.stdin.write(request)
        self._proc.stdin.flush()
        result = self._read_json(timeout_s=timeout_s)
        self.last_call_ms = (time.perf_counter() - started) * 1000.0
        return result

    def start_run(
        self,
        character: str = "Ironclad",
        seed: str = "42",
        ascension: int = 0,
        lang: str = "en",
        unlock_mode: str = "all",
        progress_path: Optional[Path] = None,
        progress_json: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "cmd": "start_run",
            "character": character,
            "seed": seed,
            "ascension": ascension,
            "lang": lang,
            "unlock_mode": unlock_mode,
        }
        if progress_path is not None:
            payload["progress_path"] = str(progress_path)
        if progress_json is not None:
            payload["progress_json"] = progress_json
        return self.send(payload)

    def start_test_combat(
        self,
        character: str = "Ironclad",
        encounter: str = "CORPSE_SLUGS_WEAK",
        seed: str = "42",
        ascension: int = 0,
        lang: str = "en",
        *,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        return self.send(
            {
                "cmd": "start_test_combat",
                "character": character,
                "encounter": encounter,
                "seed": seed,
                "ascension": ascension,
                "lang": lang,
            },
            timeout_s=timeout_s,
        )

    def action(
        self,
        action: str,
        args: Optional[Dict[str, Any]] = None,
        with_snapshot: bool = False,
        timeout_s: Optional[float] = None,
        compact: bool = False,
    ) -> Dict[str, Any]:
        cmd = "action_with_engine_snapshot" if with_snapshot else "action"
        payload: Dict[str, Any] = {"cmd": cmd, "action": action}
        if args:
            payload["args"] = args
        if compact and not with_snapshot:
            payload["compact"] = True
        return self.send(payload, timeout_s=timeout_s)

    def get_search_state(self, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        return self.send({"cmd": "get_search_state"}, timeout_s=timeout_s)

    def get_map(self, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        return self.send({"cmd": "get_map"}, timeout_s=timeout_s)

    def capture_combat_snapshot(
        self,
        snapshot_id: str,
        *,
        fingerprint_mode: str = "all",
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        return self.send({
            "cmd": "capture_combat_snapshot",
            "snapshot_id": snapshot_id,
            "fingerprint_mode": fingerprint_mode,
        }, timeout_s=timeout_s)

    def fingerprint_combat_snapshot(
        self,
        snapshot_id: str,
        *,
        fingerprint_mode: str = "all",
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        return self.send({
            "cmd": "fingerprint_combat_snapshot",
            "snapshot_id": snapshot_id,
            "fingerprint_mode": fingerprint_mode,
        }, timeout_s=timeout_s)

    def expand_combat_children(
        self,
        parent_snapshot_id: str,
        children: Sequence[Dict[str, Any]],
        *,
        lang: str = "en",
        fingerprint_mode: str = "all",
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        return self.send(
            {
                "cmd": "expand_combat_children",
                "parent_snapshot_id": parent_snapshot_id,
                "children": list(children),
                "lang": lang,
                "fingerprint_mode": fingerprint_mode,
            },
            timeout_s=timeout_s,
        )

    def reseed_rng_stream(self, streams: Dict[str, int]) -> Dict[str, Any]:
        """Surgically reseed named RNG streams (e.g. {"Shuffle": 12345}) on the
        live run/player RNG sets, leaving all other streams untouched. Used for
        CRN / draw-order marginalization variance-reduction experiments."""
        return self.send({"cmd": "reseed_rng_stream", "streams": streams})

    def set_player(self, **fields: Any) -> Dict[str, Any]:
        """Overwrite player state on the live run (requires a run in progress).
        Recognized fields: hp, max_hp, gold (ints); deck, relics, potions (lists
        of id strings). deck is rebuilt by card_id via RunState.CreateCard, so
        UPGRADE LEVELS ARE LOST (card_id carries no upgrade) — acceptable only
        for coarse counterfactuals (e.g. junk-deck vs strong-deck contrast), not
        fidelity-critical eval. Mirrors engine SetPlayer (RunSimulator.cs:703)."""
        payload: Dict[str, Any] = {"cmd": "set_player"}
        payload.update(fields)
        return self.send(payload)

    def enter_room(self, room_type: str, encounter: Optional[str] = None,
                   event: Optional[str] = None) -> Dict[str, Any]:
        """Walk the live run into a room (e.g. room_type='combat',
        encounter='THE_KIN_BOSS'). Shuffle+opening-hand deal happen inside this
        call, so a reseed_rng_stream({Shuffle}) BEFORE it re-randomizes the hand."""
        payload: Dict[str, Any] = {"cmd": "enter_room", "type": room_type}
        if encounter is not None:
            payload["encounter"] = encounter
        if event is not None:
            payload["event"] = event
        return self.send(payload)

    def restore_combat_snapshot(
        self,
        snapshot_id: str,
        lang: str = "en",
        allow_full: bool = True,
        compact: bool = False,
        *,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        return self.send({
            "cmd": "restore_combat_snapshot",
            "snapshot_id": snapshot_id,
            "lang": lang,
            "allow_full": bool(allow_full),
            "compact": bool(compact),
        }, timeout_s=timeout_s)

    def export_combat_snapshot(
        self, snapshot_id: str, *, timeout_s: Optional[float] = None
    ) -> Dict[str, Any]:
        return self.send(
            {"cmd": "export_combat_snapshot", "snapshot_id": snapshot_id},
            timeout_s=timeout_s,
        )

    def import_combat_snapshot(
        self,
        snapshot_json: str,
        snapshot_id: Optional[str] = None,
        *,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"cmd": "import_combat_snapshot", "snapshot_json": snapshot_json}
        if snapshot_id is not None:
            payload["snapshot_id"] = snapshot_id
        return self.send(payload, timeout_s=timeout_s)

    def write_continue_save(self, path: str) -> Dict[str, Any]:
        return self.send({"cmd": "write_continue_save", "path": path})

    def write_exact_save(self, path: str) -> Dict[str, Any]:
        return self.send({"cmd": "write_exact_save", "path": path})

    def load_save(self, path: str, lang: str = "en", resume_room: bool = False) -> Dict[str, Any]:
        return self.send({"cmd": "load_save", "path": path, "lang": lang, "resume_room": resume_room}, timeout_s=20.0)

    def load_save_json(self, save_json: str, lang: str = "en", resume_room: bool = False) -> Dict[str, Any]:
        return self.send({"cmd": "load_save", "json": save_json, "lang": lang,
                          "resume_room": resume_room}, timeout_s=20.0)

    def get_rng_snapshot(self) -> Dict[str, Any]:
        result = self.send({"cmd": "get_rng_snapshot"})
        if result.get("success") is not True or not isinstance(result.get("rng"), dict):
            raise RuntimeError(f"Incomplete headless RNG snapshot: {result!r}")
        return result["rng"]

    def is_alive(self) -> bool:
        """True if the child process is started and has not exited.

        Used by the cross-step worker pool to detect a crashed CLI process so it
        can be discarded and rebuilt without taking down the other workers.
        """
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            self.send({"cmd": "quit"}, timeout_s=2.0)
        except Exception:
            pass
        try:
            self._proc.kill()
            self._proc.wait(timeout=3.0)
        except Exception:
            pass
        self._proc = None
        self._stderr_thread = None
        self._stdout_thread = None

    def _start_stderr_drain(self) -> None:
        # The CLI child is spawned with stderr=PIPE. If nothing reads that pipe,
        # a long-lived (reused) worker eventually fills the OS pipe buffer
        # (~64KB on Linux), blocks on its next stderr write, and therefore never
        # emits its stdout response — while we block forever in _read_json's
        # readline(). That is the reused-process deadlock. So we ALWAYS drain
        # stderr on a daemon thread; STS2_CLI_STDERR_TEE optionally mirrors it.
        if self._proc is None or self._proc.stderr is None:
            return
        tee_target = os.environ.get("STS2_CLI_STDERR_TEE")

        def _pump() -> None:
            stream = self._proc.stderr
            if stream is None:
                return
            target = None
            if tee_target:
                target = sys.stderr if tee_target == "stderr" else open(tee_target, "a", encoding="utf-8")
            try:
                for raw_line in stream:  # drains bytes regardless of text encoding
                    line = self._decode_stderr_line(raw_line.rstrip(b"\r\n"))
                    self._stderr_tail.append(line)
                    if target is not None:
                        try:
                            target.write(line + "\n")
                            target.flush()
                        except (OSError, UnicodeError):
                            target = None
            finally:
                if target is not None and target is not sys.stderr:
                    target.close()

        self._stderr_thread = threading.Thread(target=_pump, name="sts2-cli-stderr", daemon=True)
        self._stderr_thread.start()

    @staticmethod
    def _decode_stderr_line(raw: bytes) -> str:
        encodings = ("utf-8", locale.getpreferredencoding(False), "gb18030")
        tried = set()
        for encoding in encodings:
            normalized = encoding.lower()
            if normalized in tried:
                continue
            tried.add(normalized)
            try:
                return raw.decode(encoding, errors="strict")
            except (LookupError, UnicodeDecodeError):
                continue
        return raw.decode("utf-8", errors="backslashreplace")

    def _start_stdout_reader(self) -> None:
        """Drain stdout on a thread so timeouts work for Windows pipe handles.

        ``select.select`` supports Unix pipes but only sockets on Windows. A
        queue-backed reader gives both platforms the same timeout behavior.
        """
        if self._proc is None or self._proc.stdout is None:
            return

        def _pump() -> None:
            stream = self._proc.stdout
            if stream is None:
                self._stdout_queue.put(None)
                return
            try:
                for raw_line in stream:
                    try:
                        line = raw_line.decode("utf-8", errors="strict")
                    except UnicodeDecodeError as exc:
                        self._stdout_error = f"CLI stdout is not UTF-8: {exc}"
                        break
                    self._stdout_queue.put(line)
            finally:
                self._stdout_queue.put(None)

        self._stdout_thread = threading.Thread(target=_pump, name="sts2-cli-stdout", daemon=True)
        self._stdout_thread.start()

    def _read_json(self, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        if self._proc is None:
            raise RuntimeError("CLI process not started")

        self.last_response_bytes = 0
        self.last_response_queue_wait_ms = 0.0
        self.last_json_parse_ms = 0.0
        wait_started = time.perf_counter()
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        while True:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            try:
                line = self._stdout_queue.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(f"Timed out waiting for CLI response after {timeout_s:.1f}s") from exc
            if line is None:
                err = "\n".join(self._stderr_tail)
                stdout_error = f" {self._stdout_error}" if self._stdout_error else ""
                raise RuntimeError(
                    f"CLI terminated unexpectedly.{stdout_error} stderr: {err[-800:]}"
                )
            line = line.strip()
            if line.startswith("{"):
                parse_started = time.perf_counter()
                self.last_response_queue_wait_ms = (parse_started - wait_started) * 1000.0
                self.last_response_bytes = len(line.encode("utf-8"))
                result = json.loads(line)
                self.last_json_parse_ms = (time.perf_counter() - parse_started) * 1000.0
                return result
