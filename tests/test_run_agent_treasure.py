import pytest

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.run_agent import NoncombatProgressGuard, choose_treasure_action
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_treasure_decisions_require_the_native_action_sequence():
    assert choose_treasure_action({'decision': 'treasure', 'opened': False}) == ('open_chest', {})
    assert choose_treasure_action({'decision': 'treasure_relic',
                                   'relics': [{'index': 2, 'id': 'EXAMPLE'}]}) == (
        'choose_treasure_relic', {'relic_index': 2})
    assert choose_treasure_action({'decision': 'treasure_complete', 'empty': True}) == ('leave_room', {})
    with pytest.raises(ValueError, match='stable engine index'):
        choose_treasure_action({'decision': 'treasure_relic', 'relics': []})


def test_noncombat_no_progress_fails_before_long_loop():
    guard = NoncombatProgressGuard(limit=3)
    treasure = {'type': 'decision', 'decision': 'treasure', 'opened': False}
    for _ in range(2):
        guard.check(treasure, dict(treasure), 'treasure', 'proceed', {})
    with pytest.raises(RuntimeError, match='made no progress'):
        guard.check(treasure, dict(treasure), 'treasure', 'proceed', {})
    guard.check(treasure, {'type': 'decision', 'decision': 'treasure_relic'},
                'treasure', 'open_chest', {})
    assert guard.count == 0
    for _ in range(4):
        guard.check(treasure, dict(treasure), 'combat_play', 'end_turn', {})


def test_headless_treasure_policy_reaches_map():
    cli = Sts2CliAdapter(CliConfig(ROOT))
    try:
        cli.start()
        cli.start_run(seed='headless-treasure-policy-regression')
        state = cli.enter_room('treasure')
        decisions = []
        for _ in range(3):
            decisions.append(state['decision'])
            action, payload = choose_treasure_action(state)
            state = cli.action(action, payload, timeout_s=20)
            assert state.get('type') != 'error', state
        assert decisions == ['treasure', 'treasure_relic', 'treasure_complete']
        assert state['decision'] == 'map_select'
    finally:
        cli.stop()
