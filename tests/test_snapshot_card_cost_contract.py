"""Card-instance state must survive an independent combat snapshot restore."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.combat_snapshot import _runtime_roundtrip_differences
from controller.human_capture import validate_authoritative_snapshot
from controller.search.combat_search import RootStateMismatchError, _require_matching_root_cards
from test_human_capture import snapshot_bundle


ROOT = Path(__file__).resolve().parents[1]
TAINTED_ARCHIVE = ROOT / (
    'logs/live_dashboard/20261001_015443_283587/combat_snapshots/'
    'combat_9_turn_1/search_snapshot.json'
)


def checked(result):
    assert result.get('type') != 'error' and result.get('success') is not False, result
    return result


def hand_cards(snapshot):
    return next(pile for pile in snapshot['NetState']['Players'][0]['piles']
                if pile['pileType'] == 2)['cards']


def test_root_contract_rejects_affliction_and_cost_drift():
    root = {'combat': {'hand': [{
        'card_id': 'BASH', 'upgrade': 0, 'current_cost': None,
        'display_cost': 2, 'display_costs_x': False,
        'keywords': [], 'affliction': 'TAINTED', 'affliction_count': 2,
    }], 'available_actions': [{'action_type': 'play_card', 'card_index': 0,
                               'target_index': 0, 'metadata': {'card_id': 'BASH'}}]}}
    _require_matching_root_cards(root, copy.deepcopy(root))
    changed = copy.deepcopy(root)
    changed['combat']['hand'][0]['affliction_count'] = 0
    with pytest.raises(RootStateMismatchError, match='hand'):
        _require_matching_root_cards(root, changed)
    changed = copy.deepcopy(root)
    changed['combat']['hand'][0]['display_cost'] = 0
    with pytest.raises(RootStateMismatchError, match='hand'):
        _require_matching_root_cards(root, changed)


def test_authoritative_capture_rejects_missing_runtime_cost():
    _, payload = snapshot_bundle()
    snapshot = json.loads(payload['snapshot_json'])
    snapshot['NetState']['Players'][0]['piles'] = [
        {'pileType': 2, 'cards': [{'card': {'id': {'Entry': 'BASH'}}}]}
    ]
    raw = json.dumps(snapshot)
    payload.update(snapshot_json=raw, sha256=hashlib.sha256(raw.encode()).hexdigest(),
                   bytes=len(raw.encode()))
    with pytest.raises(ValueError, match='runtimeEnergyCost'):
        validate_authoritative_snapshot(payload)


def test_existing_tainted_snapshot_restores_count():
    if not TAINTED_ARCHIVE.is_file():
        pytest.skip('Recorded Tainted snapshot is unavailable')
    cli = Sts2CliAdapter(CliConfig(ROOT))
    cli.start()
    try:
        checked(cli.import_combat_snapshot(TAINTED_ARCHIVE.read_text(encoding='utf-8'), 'tainted'))
        checked(cli.restore_combat_snapshot('tainted'))
        hand = checked(cli.get_search_state())['combat_state_for_search']['combat']['hand']
        assert any(card['affliction'] == 'TAINTED' and card['affliction_count'] == 2
                   for card in hand)
    finally:
        cli.stop()


def test_turn_only_cost_modifier_round_trips_and_expires():
    source = Sts2CliAdapter(CliConfig(ROOT))
    worker = Sts2CliAdapter(CliConfig(ROOT))
    source.start()
    worker.start()
    try:
        checked(source.start_test_combat(encounter='PHANTASMAL_GARDENERS_ELITE'))
        checked(source.capture_combat_snapshot('cost_source'))
        snapshot = json.loads(checked(source.export_combat_snapshot('cost_source'))['snapshot_json'])
        card = hand_cards(snapshot)[0]
        assert card['runtimeEnergyCost']['Base'] == 1
        card['energyCost'] = {'ResolvedValue': 0}
        card['runtimeEnergyCost']['LocalModifiers'] = [
            {'Amount': 0, 'Type': 1, 'Expiration': 1, 'IsReduceOnly': False}
        ]
        checked(worker.import_combat_snapshot(json.dumps(snapshot), 'turn_cost'))
        checked(worker.restore_combat_snapshot('turn_cost'))
        root = checked(worker.get_search_state())['combat_state_for_search']
        assert root['combat']['hand'][0]['display_cost'] == 0
        checked(worker.capture_combat_snapshot('turn_cost_roundtrip'))
        roundtrip = json.loads(checked(worker.export_combat_snapshot('turn_cost_roundtrip'))['snapshot_json'])
        assert _runtime_roundtrip_differences(snapshot, roundtrip) == []
        checked(worker.action('end_turn', {}, timeout_s=30))
        after = checked(worker.get_search_state())['combat_state_for_search']
        assert all(card['display_cost'] == 1 for card in after['combat']['hand'])
        assert checked(worker.restore_combat_snapshot('turn_cost_roundtrip'))['restore_mode'] == 'in_place'
        restored_again = checked(worker.get_search_state())['combat_state_for_search']
        assert restored_again['combat']['hand'][0]['display_cost'] == 0
        checked(worker.action('end_turn', {}, timeout_s=30))
        after_again = checked(worker.get_search_state())['combat_state_for_search']
        assert all(card['display_cost'] == 1 for card in after_again['combat']['hand'])
    finally:
        source.stop()
        worker.stop()
