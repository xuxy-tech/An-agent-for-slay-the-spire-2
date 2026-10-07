from controller.sandbox_features import FEATURE_VERSION, OBSERVATION_MODE

def features(value):
    return {"version": FEATURE_VERSION, "observation_mode": OBSERVATION_MODE,
            "training_eligible": True, "x": value}

from controller.human_leaf_join import join_records, pairs_from_records, state_fingerprint


def human_state():
    return {
        "in_combat": True, "turn": 2,
        "combat": {
            "player": {"current_hp": 35, "max_hp": 40, "block": 3, "energy": 2, "powers": []},
            "enemies": [{"enemy_id": "1", "current_hp": 20, "max_hp": 30, "block": 0,
                         "powers": [], "intent": {"intent_types": ["Attack"], "display_damage": 8}}],
            "hand": [{"card_id": "CARD.STRIKE", "upgrade": 0, "energy_cost": 1}],
        },
    }


def headless_state():
    return {
        "combat": {
            "turn_number": 2, "round_number": 2, "is_player_turn": True,
            "player": {"hp": 35, "max_hp": 40, "block": 3, "energy": 2, "powers": []},
            "enemies": [{"monster_id": "1", "hp": 20, "max_hp": 30, "block": 0,
                         "powers": [], "intent": {"intent_types": ["Attack"], "total_damage": 8}}],
            "hand": [{"card_id": "STRIKE", "upgrade": 0, "current_cost": 1}],
        },
    }


def test_public_fingerprint_bridges_observer_aliases():
    assert state_fingerprint(human_state()) == state_fingerprint(headless_state())


def test_join_keeps_not_selected_as_explicit_preference_contrast():
    human = {
        "session_id": "human_a", "decision_id": "d1", "run_id": "r1", "turn": 2,
        "observation_before": human_state(), "action": {"type": "play_card", "card_id": "CARD.STRIKE", "target_creature_id": "1"},
        "state_fingerprint": state_fingerprint(human_state()),
    }
    decision = {
        "record_type": "decision", "root_id": "root1", "root_visible_state": headless_state(),
        "counterfactual_candidates": [
            {"candidate_id": "c1", "action": {"action_type": "play_card", "card_index": 0,
              "target_index": 0, "metadata": {}}, "line": [], "comparable": True},
            {"candidate_id": "c2", "action": {"action_type": "end_turn", "card_index": None,
              "target_index": None, "metadata": {}}, "line": [], "comparable": True},
        ], "search_context": {"root_coverage": {"topology_exhaustive": True}},
    }
    leaves = [
        {"record_type": "leaf", "root_id": "root1", "root_action": row["action"],
         "action_sequence": [], "features": features(index), "score": index}
        for index, row in enumerate(decision["counterfactual_candidates"], 1)
    ]
    rows, report = join_records([human], [decision], leaves)
    assert report["matched_rows"] == 1
    assert report["pairwise_examples"] == 1
    assert rows[0]["fit_ready"] is True
    assert {row["human_preference"] for row in rows[0]["candidate_set"]} == {"selected", "not_selected"}
    assert rows[0]["pairwise_examples"][0]["target"] == 1
    leaves[0]['features']['version'] = 'combat-preference-2'
    legacy, _ = join_records([human], [decision], leaves)
    assert legacy[0]['join_status'] == 'matched'
    assert legacy[0]['fit_ready'] is False
    assert legacy[0]['reason'] == 'incompatible_or_incomplete_feature_observations'


def test_join_does_not_mark_incomplete_search_fit_ready():
    human = {"session_id": "human_a", "decision_id": "d1", "observation_before": human_state(),
             "action": {"type": "end_turn"}, "state_fingerprint": state_fingerprint(human_state())}
    decision = {"record_type": "decision", "root_id": "root1", "root_visible_state": headless_state(),
                "counterfactual_candidates": [{"candidate_id": "c1", "action": {"action_type": "end_turn"},
                  "line": [], "comparable": True}], "search_context": {"root_coverage": {"topology_exhaustive": False}}}
    rows, report = join_records([human], [decision], [])
    assert report["matched_rows"] == 1
    assert rows[0]["fit_ready"] is False
    assert rows[0]["reason"] == "incomplete_or_missing_counterfactual_features"


def test_pairs_adapter_only_exports_fit_ready_rows():
    records = [{"fit_ready": True, "pairwise_examples": [{"chosen": {"version": "v", "values": {}},
                   "other": {"version": "v", "values": {}}, "target": 1}]},
               {"fit_ready": False, "pairwise_examples": [{"chosen": {}, "other": {}, "target": 1}]}]
    pairs = pairs_from_records(records)
    assert len(pairs) == 1
    assert pairs[0]["target"] == 1.0


def test_join_matches_leaf_action_indices_nested_under_args():
    human = {
        "session_id": "human_a", "decision_id": "d1", "observation_before": human_state(),
        "action": {"type": "play_card", "card_id": "CARD.STRIKE", "target_creature_id": "1"},
        "state_fingerprint": state_fingerprint(human_state()),
        "authoritative_snapshot_id": "snapshot-1",
    }
    decision = {
        "record_type": "decision", "root_id": "root1", "root_snapshot_id": "snapshot-1",
        "root_visible_state": headless_state(),
        "counterfactual_candidates": [
            {"candidate_id": "c1", "action": {"action_type": "play_card", "card_index": 0,
              "target_index": 0, "metadata": {}}, "line": [], "comparable": True},
            {"candidate_id": "c2", "action": {"action_type": "end_turn"},
             "line": [], "comparable": True},
        ],
        "search_context": {"root_coverage": {"bounded_tree_complete": True}},
    }
    leaves = [
        {"record_type": "leaf", "root_id": "root1",
         "root_action": {"action_type": "play_card", "args": {"card_index": 0, "target_index": 0}},
         "action_sequence": [], "features": features(1), "score": 1},
        {"record_type": "leaf", "root_id": "root1",
         "root_action": {"action_type": "end_turn", "args": {}},
         "action_sequence": [], "features": features(0), "score": 0},
    ]
    rows, report = join_records([human], [decision], leaves)
    assert report["fit_ready_rows"] == 1
    assert report["pairwise_examples"] == 1
    assert rows[0]["join_key"] == "snapshot_id"
