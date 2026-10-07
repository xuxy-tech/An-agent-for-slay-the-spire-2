from pathlib import Path

import pytest

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter


ROOT = Path(__file__).resolve().parents[1]
RELEASE = Path('third_party/sts2-cli/src/Sts2Headless/bin/Release/net9.0/Sts2Headless.dll')


def test_default_runtime_starts_the_verified_release(monkeypatch):
    monkeypatch.delenv('STS2_HEADLESS_DLL', raising=False)
    config = CliConfig(ROOT)
    assert config.dll_relpath == RELEASE
    if not (ROOT / RELEASE).is_file():
        pytest.skip('Release headless runtime is not built')

    cli = Sts2CliAdapter(config)
    try:
        cli.start()
        assert Path(cli._proc.args[1]).resolve() == (ROOT / RELEASE).resolve()
        cli.require_capabilities({'exact_save_json', 'native_reward_items_v1'})
    finally:
        cli.stop()


def test_explicit_runtime_override_is_preserved(monkeypatch):
    override = 'third_party/sts2-cli/src/Sts2Headless/bin/Debug/net9.0/Sts2Headless.dll'
    monkeypatch.setenv('STS2_HEADLESS_DLL', override)
    assert CliConfig(ROOT).dll_relpath == Path(override)
