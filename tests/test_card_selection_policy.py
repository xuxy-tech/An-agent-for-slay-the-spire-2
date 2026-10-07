from pathlib import Path

import pytest

from controller.live_noncombat import (
    UnsupportedClientDecision,
    choose_client_selection,
)
from controller.run_agent import (
    choose_card_selection,
    infer_card_selection_mode,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE = {
    'schema_version': 2,
    'id': 'test',
    'ports': [{'id': 'defense', 'name': 'Defense', 'target': 2, 'maximum': 3}],
    'cards': [{'id': 'FLAME_BARRIER', 'max_copies': 2, 'ports': ['defense']}],
}


def _cards():
    return [
        {
            "index": 0,
            "card_id": "STRIKE_IRONCLAD",
            "card_type": "Attack",
            "rarity": "Basic",
            "upgraded": False,
        },
        {
            "index": 1,
            "card_id": "BASH",
            "card_type": "Attack",
            "rarity": "Basic",
            "upgraded": False,
        },
        {
            "index": 2,
            "card_id": "FLAME_BARRIER",
            "card_type": "Skill",
            "rarity": "Uncommon",
            "upgraded": False,
        },
    ]


def _policy_state():
    cards = _cards()
    return {
        "cards": cards,
        "min_select": 1,
        "max_select": 1,
        "player": {"hp": 80, "max_hp": 80, "gold": 99, "deck": cards},
        "context": {"floor": 1},
    }


def test_remove_and_transform_do_not_use_best_card_policy():
    state = _policy_state()
    assert choose_card_selection(state, REPO_ROOT, "pick", PROFILE)["card_id"] == "FLAME_BARRIER"
    assert choose_card_selection(state, REPO_ROOT, "remove")["card_id"] == "STRIKE_IRONCLAD"
    assert choose_card_selection(state, REPO_ROOT, "transform")["card_id"] == "STRIKE_IRONCLAD"


def test_optional_acquisition_is_strict_and_mandatory_acquisition_falls_back():
    state = _policy_state()
    outside = {**state, 'cards': [{'index': 0, 'card_id': 'HAVOC'}]}
    assert choose_card_selection({**outside, 'can_skip': True}, REPO_ROOT, 'copy', PROFILE) is None
    assert choose_card_selection(outside, REPO_ROOT, 'copy', PROFILE)['card_id'] == 'HAVOC'
    assert choose_card_selection(state, REPO_ROOT, 'upgrade', PROFILE)['card_id'] == 'FLAME_BARRIER'
    assert choose_card_selection(state, REPO_ROOT, 'enchant', PROFILE)['card_id'] == 'FLAME_BARRIER'


def test_selection_kind_overrides_stale_parent_context():
    cards = _cards()
    visible = {
        "screen": "CARD_SELECTION",
        "available_actions": ["select_deck_card"],
        "selection": {
            "kind": "deck_transform_select",
            "min_select": 0,
            "max_select": 0,
            "cards": cards,
        },
        "run": {
            "current_hp": 80,
            "max_hp": 80,
            "gold": 99,
            "floor": 1,
            "deck": cards,
        },
    }
    decision = choose_client_selection(visible, REPO_ROOT, "upgrade")
    assert decision.operation == "transform"
    assert decision.params == {"option_index": 0}


def test_unknown_visible_selection_fails_closed():
    visible = {
        "screen": "CARD_SELECTION",
        "available_actions": ["select_deck_card"],
        "selection": {"kind": "mystery", "cards": _cards()},
        "run": {"deck": _cards()},
    }
    with pytest.raises(UnsupportedClientDecision, match="无法识别选牌目的"):
        choose_client_selection(visible, REPO_ROOT)


def test_multi_select_uses_required_count_in_headless_state():
    state = {**_policy_state(), "min_select": 2, "max_select": 2}
    indices = choose_card_selection(state, REPO_ROOT, "remove")["indices"].split(",")
    assert len(indices) == 2
    assert len(set(indices)) == 2


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("deck_remove_select", "remove"),
        ("deck_transform_select", "transform"),
        ("deck_upgrade_select", "upgrade"),
        ("deck_copy_select", "copy"),
    ],
)
def test_visible_selection_kinds_are_operation_aware(kind, expected):
    assert infer_card_selection_mode(selection_kind=kind) == expected


def test_forced_upgrade_and_enchant_fall_back_to_starter_cards():
    from controller.run_agent import choose_card_select_upgrade, choose_card_select_enchant
    state = {'cards': _cards()[:1], 'min_select': 1, 'max_select': 1,
             'selection_count': 1, 'run': {'floor': 1},
             'player': {'deck': _cards()[:1]}}
    upgrade = choose_card_select_upgrade(state, REPO_ROOT)
    enchant = choose_card_select_enchant(state, REPO_ROOT, PROFILE)
    assert upgrade and enchant
    assert upgrade['indices'] == enchant['indices'] == '0'
