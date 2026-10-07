from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from controller.human_capture_dashboard import HumanCaptureDashboard


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    monkeypatch.setattr("controller.human_capture_dashboard.threading.Thread.start", lambda self: None)
    value = HumanCaptureDashboard(tmp_path, game_root=tmp_path / "game")
    yield value
    if value.output_handle is not None:
        value.output_handle.close()


def test_status_separates_workspace_build_from_game_install(dashboard, tmp_path):
    built = tmp_path / "mod" / "STS2HumanCapture" / "bin" / "Release" / "net9.0" / "STS2HumanCapture.dll"
    built.parent.mkdir(parents=True)
    built.write_bytes(b"current-build")

    status = dashboard._installation_status()

    assert status["built_sha256"]
    assert status["installed"] is False
    assert status["matches_build"] is False


def test_session_path_cannot_escape_raw_root(dashboard, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "events.jsonl").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Unknown human-capture session"):
        dashboard._session_path("../outside")


def test_start_uses_read_only_capture_ports_and_stop_file(dashboard, monkeypatch):
    launched = []
    monkeypatch.setattr(dashboard, "_require_capture_ready", lambda: None)
    monkeypatch.setattr(
        "controller.human_capture_dashboard.subprocess.Popen",
        lambda command, **kwargs: launched.append(command) or SimpleNamespace(poll=lambda: None),
    )

    status = dashboard.command("start", {"max_actions": 12})

    command = launched[0]
    assert command[command.index("--observer-url") + 1] == "http://127.0.0.1:8080"
    assert command[command.index("--capture-url") + 1] == "http://127.0.0.1:9878"
    assert command[command.index("--max-actions") + 1] == "12"
    assert Path(command[command.index("--stop-file") + 1]).parent == dashboard.task_dir
    assert status["running"] is True
    assert status["job"] == "collect"


def test_stop_requests_graceful_session_close(dashboard):
    dashboard.process = SimpleNamespace(poll=lambda: None)
    dashboard.job = "collect"
    dashboard.stop_file = dashboard.repo_root / "control.json"

    status = dashboard.command("stop", {})

    assert json.loads(dashboard.stop_file.read_text(encoding="utf-8"))["desired"] == "stop"
    assert status["mode"] == "stopping"


def test_sessions_are_sorted_and_report_manifest_status(dashboard):
    first = dashboard.raw_root / "human_a"
    second = dashboard.raw_root / "human_b"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (second / "events.jsonl").write_text("{}\n{}\n", encoding="utf-8")
    (first / "manifest.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    (second / "manifest.json").write_text(json.dumps({"status": "stopped"}), encoding="utf-8")
    first.joinpath("events.jsonl").touch()
    second.joinpath("events.jsonl").touch()

    sessions = dashboard.sessions()

    assert {row["id"] for row in sessions} == {"human_a", "human_b"}
    assert {row["status"] for row in sessions} == {"completed", "stopped"}


def test_delete_session_requires_explicit_confirmation(dashboard, tmp_path):
    session = dashboard.raw_root / "human_bad"
    session.mkdir(parents=True)
    (session / "events.jsonl").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="需要确认"):
        dashboard.command("delete_session", {"session_id": "human_bad"})

    assert session.is_dir()


def test_delete_session_removes_only_selected_raw_session(dashboard, tmp_path):
    selected = dashboard.raw_root / "human_bad"
    sibling = dashboard.raw_root / "human_keep"
    selected.mkdir(parents=True)
    sibling.mkdir(parents=True)
    (selected / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (sibling / "events.jsonl").write_text("{}\n", encoding="utf-8")
    dashboard.experiment_root.mkdir(parents=True)
    manifest = dashboard.experiment_root / "report.json"
    manifest.write_text(json.dumps({"schema": "preserve"}), encoding="utf-8")

    status = dashboard.command("delete_session", {
        "session_id": "human_bad",
        "confirm": True,
    })

    assert not selected.exists()
    assert sibling.is_dir()
    assert json.loads(manifest.read_text(encoding="utf-8"))["schema"] == "preserve"
    assert "human_bad" in status["message"]
