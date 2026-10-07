from pathlib import Path

import pytest

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('session', ['20260923_221201_994993', '20260923_215758_484363'])
def test_recorded_unicode_save_pipe_matches_file(session):
    source = ROOT / 'logs/live_dashboard' / session / 'reanchor_segment_1.save'
    if not source.is_file():
        pytest.skip('Local failure snapshot unavailable')
    results = []
    for inline in (False, True):
        cli = Sts2CliAdapter(CliConfig(ROOT))
        cli.start()
        try:
            state = (cli.load_save_json(source.read_text(encoding='utf-8')) if inline
                     else cli.load_save(str(source)))
            assert state.get('decision') == 'map_select', state
            results.append((state, cli.get_rng_snapshot()))
        finally:
            cli.stop()
    assert results[0] == results[1]
