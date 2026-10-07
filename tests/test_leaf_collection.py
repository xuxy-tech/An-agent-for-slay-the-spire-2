import json

from controller.run_agent import LeafCollector
from controller.search.actions import SearchAction
from controller.search.combat_search import CombatSearcher, CombatSpec, RecordedAction, SearchResult


def visible_state():
    return {
        "success": True,
        "combat": {
            "turn_number": 1,
            "round_number": 1,
            "is_player_turn": True,
            "player": {
                "hp": 40,
                "max_hp": 40,
                "block": 0,
                "energy": 3,
                "powers": [],
            },
            "enemies": [{
                "monster_id": "TEST",
                "hp": 20,
                "max_hp": 20,
                "block": 0,
                "powers": [],
                "intent": {"intent_types": ["Attack"], "display_damage": 8},
            }],
            "hand": [{"card_id": "STRIKE_IRONCLAD", "upgrade": 0, "display_cost": 1}],
            "draw_pile": [{"card_id": "HIDDEN_CARD", "upgrade": 0}],
            "discard_pile": [{"card_id": "DISCARD_CARD", "upgrade": 0}],
            "exhaust_pile": [],
            "available_actions": [{"action_type": "end_turn"}],
        },
    }


def test_preference_leaf_collection_uses_visible_state_and_trace():
    samples = []
    searcher = CombatSearcher(
        None,
        CombatSpec("Ironclad", "TEST", "fixed"),
        score_mode="preference",
        leaf_dump_sink=samples.append,
        leaf_dump_rate=1,
    )
    root = visible_state()
    searcher._preference_root = root
    searcher._root_summary = searcher._combat_summary(root)
    searcher.chance_depth = 1
    try:
        searcher._maybe_dump_leaf(
            root,
            [RecordedAction("play_card", {"card_index": 0})],
            score=12.5,
        )
    finally:
        searcher.close()

    assert len(samples) == 1
    sample = samples[0]
    assert sample["schema"] == "sts2.combat_search.leaf.v3"
    assert sample["record_type"] == "leaf"
    assert sample["root_action"]["action_type"] == "play_card"
    assert sample["score"] == 12.5
    assert sample["information_boundary"] == "simulated_hand_unordered_piles_v1"
    assert sample["leaf_visible_state"]["combat"]["draw_pile"]["kind"] == "card_multiset"
    assert sample["leaf_visible_state"]["combat"]["discard_pile"]["kind"] == "card_multiset"
    assert "draw_cards" not in sample
    assert "draw_pool_strength" not in sample
    # Leaf samples carry their own complete scoring root even though controller
    # decision rows intentionally keep the old projection for matching.
    from controller.combat_scoring import CombatScoring
    rebuilt = CombatScoring().features(sample['root_scoring_state'], sample['leaf_visible_state'],
        [{'action': {'action_type': 'play_card'}, 'before': {}}])
    assert rebuilt == sample['features']


def test_leaf_collector_writes_explicit_counterfactual_decision_record(tmp_path):
    path = tmp_path / "leaves.jsonl"
    collector = LeafCollector(str(path), seed="test")
    root = visible_state()
    chosen = SearchAction("end_turn")
    result = SearchResult(
        score=2.0,
        sequence=[chosen],
        leaf_state=root,
        stats={"root_coverage": {"bounded_tree_complete": True}},
        root_candidates=[{
            "action": {"action_type": "end_turn", "card_index": None,
                        "target_index": None, "metadata": {}},
            "line": [{"action_type": "end_turn", "card_index": None,
                      "target_index": None, "metadata": {}}],
            "score": 2.0,
            "base_score": 2.0,
            "comparable": True,
            "leaf_settlement": {"phase": "post_enemy_turn"},
        }],
    )
    collector.record_search(root, result, {"depth": 12, "chance_depth": 1})
    collector.finish_combat("TEST", 1.0, True)
    collector.close()

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    decision = next(row for row in records if row["record_type"] == "decision")
    assert decision["counterfactual_source"] == "headless_search"
    assert decision["human_label_status"] == "unlabeled"
    assert decision["counterfactual_candidates"][0]["label"]["human_preference"] is None
    assert decision["counterfactual_candidates"][0]["label"]["policy_selected"] is True
    assert "draw_pile" not in decision["root_visible_state"]["combat"]
