import sys
from pathlib import Path

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter


def test_utf8_stderr_larger_than_pipe_does_not_kill_reader(monkeypatch):
    import cli.sts2_cli_adapter as adapter
    monkeypatch.setattr(adapter, 'resolve_dotnet', lambda explicit: Path(sys.executable))
    cli = Sts2CliAdapter(CliConfig(Path(__file__).resolve().parents[1],
                                dll_relpath=Path('tests/fixtures/utf8_engine.py')))
    try:
        ready = cli.start()
        assert ready['text'] == '\u5b9d\u7bb1'
        assert any('\u2014' in line for line in cli._stderr_tail)
        assert cli.send({'cmd': 'state'}, timeout_s=2)['type'] == 'ok'
    finally:
        cli.stop()


def test_non_utf8_stderr_larger_than_pipe_does_not_kill_reader(monkeypatch):
    import cli.sts2_cli_adapter as adapter
    monkeypatch.setattr(adapter, "resolve_dotnet", lambda explicit: Path(sys.executable))
    cli = Sts2CliAdapter(
        CliConfig(
            Path(__file__).resolve().parents[1],
            dll_relpath=Path("tests/fixtures/non_utf8_stderr_engine.py"),
        )
    )
    try:
        assert cli.start()["text"] == "ok"
        assert cli._stderr_thread is not None and cli._stderr_thread.is_alive()
        assert cli._stderr_tail
        assert cli.send({"cmd": "state"}, timeout_s=2)["type"] == "ok"
    finally:
        cli.stop()
