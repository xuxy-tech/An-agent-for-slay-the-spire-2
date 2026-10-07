"""Replay a previously normal run against the patched native engine."""
import json
from pathlib import Path

import pytest

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.rng_parity import compare_rng_snapshots


ROOT = Path(__file__).resolve().parents[1]
SESSIONS = (
    ('20260927_165202_469345', {'choose_reward_card', 'claim_reward', 'choose_event_option'}),
    ('20260927_170208_738913', {'select_deck_card', 'claim_reward', 'choose_event_option'}),
    ('20260927_175906_905994', {'skip_reward_cards', 'claim_reward', 'choose_event_option'}),
)


@pytest.mark.parametrize('session_id,required_actions', SESSIONS)
def test_normal_multicombat_reward_event_run_replays(session_id, required_actions):
    session = ROOT / 'logs/live_dashboard' / session_id
    if not (session / 'official_map_anchor.save').is_file():
        pytest.skip('Normal baseline anchor is unavailable')
    report = json.loads((session / 'run_report.json').read_text(encoding='utf-8'))
    assert report['status'] == 'DEFEAT'
    assert required_actions <= {row.get('client_action') for row in report['actions']}
    cli = Sts2CliAdapter(CliConfig(ROOT))
    cli.start()
    try:
        state = cli.load_save(str(session / 'official_map_anchor.save'),
                              resume_room=report.get('anchor_room', False))
        assert state.get('type') != 'error', state.get('message')
        for row in report['actions']:
            if row.get('status') != 'completed':
                continue
            action = row.get('headless_action')
            if not action:
                continue
            state = cli.action(action, row.get('headless_args') or {}, timeout_s=30)
            assert state.get('type') != 'error', (row['sequence'], action, state.get('message'))
            parity = compare_rng_snapshots(row['client_rng_after'], cli.get_rng_snapshot())
            assert parity.passed, (row['sequence'], action, parity.differences[:5])
        assert state['decision'] == 'game_over'
    finally:
        cli.stop()
