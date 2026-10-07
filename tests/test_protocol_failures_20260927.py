"""The eight interrupted sessions are immutable regression inputs."""
from pathlib import Path

import pytest

from scripts.verify_protocol_failures import MAP_SAMPLES, verify_map, verify_replay
from cli.sts2_cli_adapter import CliConfig

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    not (ROOT / CliConfig(ROOT).dll_relpath).is_file(),
    reason='Build the configured headless DLL before running historical replays',
)


@pytest.mark.parametrize('suffix', MAP_SAMPLES)
def test_native_map_vote_after_historical_exact_reanchor(suffix):
    evidence = verify_map(suffix)
    assert evidence['before_state'] == evidence['before_rng'] == 'PASS', evidence
    assert evidence['after_state'] == evidence['after_rng'] == 'PASS', evidence
    assert evidence['decision'] != 'map_select', evidence


def test_centipede_last_segment_death_reaches_native_rewards():
    evidence = verify_replay('171219_961079')
    assert evidence.get('error') is None, evidence
    assert evidence['replayed_through'] == 289
    assert evidence['decision'] == 'combat_reward'
    assert evidence['state'] == evidence['rng'] == 'PASS', evidence
    assert evidence['intermediate_segments']['sequence'] == 287
    assert evidence['intermediate_segments']['living_segments'] == 1
    assert evidence['intermediate_segments']['state'] == evidence['intermediate_segments']['rng'] == 'PASS'


def test_full_potion_slots_allow_native_reward_abandonment():
    evidence = verify_replay('183637_194961')
    assert evidence.get('error') is None, evidence
    assert evidence['offered_types'] == ['Potion', 'Potion']
    assert evidence['abandon_decision'] == 'map_select'
    assert evidence['potions_preserved'] and evidence['abandon_rng_unchanged']


def test_kaleidoscope_skip_first_group_keeps_second_group():
    evidence = verify_replay('190418_025553')
    assert evidence.get('error') is None, evidence
    assert evidence['remaining_card_groups_after_skip'] == 1
    assert evidence['second_group_opened'] and evidence['second_group_resolved']


def test_glass_eye_handles_all_five_groups():
    evidence = verify_replay('194142_678037')
    assert evidence.get('error') is None, evidence
    assert evidence['state'] == evidence['rng'] == 'PASS', evidence
    assert evidence['processed_groups'] == 5
    assert [row['remaining_after'] for row in evidence['group_receipts']] == [3, 2, 1, 0]
    assert [row['deck_delta'] for row in evidence['group_receipts']] == [1, 1, 0, 0]
    assert evidence['remaining_items'] == 0
    assert evidence['continuation'] == 'map_select'
