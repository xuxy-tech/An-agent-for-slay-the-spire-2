from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import copy
import threading
from types import MethodType

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.search.actions import available_actions_from_search_state, cli_payload_for_action
from controller.search.combat_search import RecordedAction, SearchResult, _SharedDagCache
from controller.search.state_cache import hash_search_state, hash_search_state_for_subtree_cache


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_batch_expansion_is_strictly_repeatable_and_restorable():
    cli = Sts2CliAdapter(CliConfig(repo_root=REPO_ROOT))
    cli.start()
    try:
        started = cli.start_test_combat(
            encounter="SHRINKER_BEETLE_WEAK", seed="batch-expansion-test"
        )
        assert started.get("decision") == "combat_play"
        root_a = cli.capture_combat_snapshot("batch_test_root_a")
        root_b = cli.capture_combat_snapshot("batch_test_root_b")
        assert root_a.get("state_fingerprint") == root_b.get("state_fingerprint")
        assert root_a.get("semantic_state_fingerprint") == root_b.get(
            "semantic_state_fingerprint"
        )

        search_state = cli.get_search_state()["combat_state_for_search"]
        action = next(
            action
            for action in available_actions_from_search_state(search_state)
            if action.action_type != "end_turn"
        )
        action_name, args = cli_payload_for_action(action)
        expanded = cli.expand_combat_children(
            "batch_test_root_a",
            [
                {"action": action_name, "args": args, "snapshot_id": "batch_test_child_a"},
                {"action": action_name, "args": args, "snapshot_id": "batch_test_child_b"},
            ],
        )
        rows = expanded.get("children") or []
        assert expanded.get("success") is True
        assert len(rows) == 2
        assert all(row.get("success") is True for row in rows)
        assert all(
            (row.get("restore_result") or {}).get("restore_mode") == "in_place"
            for row in rows
        )
        assert rows[0].get("state_fingerprint") == rows[1].get("state_fingerprint")
        assert rows[0].get("semantic_state_fingerprint") == rows[1].get(
            "semantic_state_fingerprint"
        )
        assert hash_search_state(rows[0]["combat_state_for_search"]) == hash_search_state(
            rows[1]["combat_state_for_search"]
        )

        restored = cli.restore_combat_snapshot("batch_test_child_a", compact=True)
        assert restored.get("type") != "error"
        assert cli.get_search_state().get("success") is True
    finally:
        cli.stop()


def test_snapshot_capture_can_defer_and_lazily_compute_fingerprints():
    cli = Sts2CliAdapter(CliConfig(repo_root=REPO_ROOT))
    cli.start()
    try:
        started = cli.start_test_combat(
            encounter="SHRINKER_BEETLE_WEAK", seed="lazy-fingerprint-test"
        )
        assert started.get("decision") == "combat_play"

        captured = cli.capture_combat_snapshot(
            "lazy_fingerprint_root", fingerprint_mode="none"
        )
        assert captured.get("success") is True
        assert captured.get("fingerprint_mode") == "none"
        assert "state_fingerprint" not in captured
        assert "semantic_state_fingerprint" not in captured

        strict = cli.fingerprint_combat_snapshot(
            "lazy_fingerprint_root", fingerprint_mode="strict"
        )
        assert strict.get("success") is True
        assert strict.get("state_fingerprint", "").startswith(
            "sts2-combat-snapshot-v1:"
        )
        assert "semantic_state_fingerprint" not in strict

        complete = cli.fingerprint_combat_snapshot(
            "lazy_fingerprint_root", fingerprint_mode="all"
        )
        exported = cli.export_combat_snapshot("lazy_fingerprint_root")
        assert complete.get("state_fingerprint") == strict.get("state_fingerprint")
        assert complete.get("state_fingerprint") == exported.get("state_fingerprint")
        assert complete.get("semantic_state_fingerprint") == exported.get(
            "semantic_state_fingerprint"
        )
    finally:
        cli.stop()


def test_batch_children_match_independent_parent_restores():
    cli = Sts2CliAdapter(CliConfig(repo_root=REPO_ROOT))
    cli.start()
    try:
        started = cli.start_test_combat(
            encounter="EXOSKELETONS_NORMAL", seed="batch-parity-test"
        )
        assert started.get("decision") == "combat_play"
        assert cli.capture_combat_snapshot("batch_parity_root").get("success")
        root_state = cli.get_search_state()["combat_state_for_search"]
        actions = [
            action
            for action in available_actions_from_search_state(root_state)
            if action.action_type != "end_turn"
        ][:6]
        requests = []
        payloads = []
        for index, action in enumerate(actions):
            action_name, args = cli_payload_for_action(action)
            payloads.append((action_name, args))
            requests.append({
                "action": action_name,
                "args": args,
                "snapshot_id": f"batch_parity_child_{index}",
            })

        expanded = cli.expand_combat_children("batch_parity_root", requests)
        rows = expanded.get("children") or []
        assert len(rows) == len(requests)
        assert all(row.get("success") is True for row in rows)
        assert all(
            (row.get("restore_result") or {}).get("restore_mode") == "in_place"
            for row in rows
        )

        for index, ((action_name, args), batch_row) in enumerate(zip(payloads, rows)):
            restored = cli.restore_combat_snapshot("batch_parity_root", compact=True)
            assert restored.get("type") != "error"
            action_result = cli.action(action_name, args=args, compact=True)
            assert action_result.get("type") != "error"
            expected_snapshot = cli.capture_combat_snapshot(
                f"batch_parity_expected_{index}"
            )
            expected_state = cli.get_search_state()["combat_state_for_search"]

            assert hash_search_state(
                batch_row["combat_state_for_search"]
            ) == hash_search_state(expected_state)
            assert batch_row.get("state_fingerprint") == expected_snapshot.get(
                "state_fingerprint"
            )
            assert batch_row.get(
                "semantic_state_fingerprint"
            ) == expected_snapshot.get("semantic_state_fingerprint")
    finally:
        cli.stop()


def test_default_batch_search_matches_sequential_oracle(monkeypatch):
    from controller.search.combat_search import (
        CombatSearcher,
        CombatSpec,
        CombatWorkerPool,
    )

    snapshot_path = (
        REPO_ROOT / "data" / "human_play" / "raw"
        / "human_b561826d7f994592a1564785bb95363f"
        / "snapshots" / "combat_d043a0010a2042ccac1b1646205f60d9.json"
    )
    snapshot_json = snapshot_path.read_text(encoding="utf-8")

    def run(*, disable_batch):
        if disable_batch:
            monkeypatch.setenv("STS2_DISABLE_BATCH_EXPANSION", "1")
        else:
            monkeypatch.delenv("STS2_DISABLE_BATCH_EXPANSION", raising=False)
        monkeypatch.delenv("STS2_SEARCH_SNAPSHOT_FINGERPRINT_MODE", raising=False)
        cfg = CliConfig(repo_root=REPO_ROOT)
        pool = CombatWorkerPool(cfg)
        pool.prewarm(1)
        searcher = CombatSearcher(
            cfg,
            CombatSpec("Ironclad", "ImportedSnapshot", "batch-search-parity"),
            reuse_cli_processes=True,
            root_snapshot_id=snapshot_path.stem,
            root_snapshot_json=snapshot_json,
            worker_pool=pool,
        )
        try:
            result = searcher.search_from_history([], depth=2, chance_depth=1)
            timing = searcher.timing_summary()
            return result, timing
        finally:
            searcher.close()
            pool.close()

    batch, batch_timing = run(disable_batch=False)
    sequential, sequential_timing = run(disable_batch=True)

    assert batch.score == sequential.score
    assert batch.sequence == sequential.sequence
    assert batch.stats["nodes"] == sequential.stats["nodes"]
    assert batch.stats["root_coverage"] == sequential.stats["root_coverage"]
    assert hash_search_state(batch.leaf_state) == hash_search_state(
        sequential.leaf_state
    )
    assert batch_timing["batch_expand_calls"] > 0
    assert batch_timing["batch_expand_fallbacks"] == 0
    assert sequential_timing["batch_expand_calls"] == 0


def test_repeated_enemy_snapshot_restore_round_trips_exact_state():
    cli = Sts2CliAdapter(CliConfig(repo_root=REPO_ROOT))
    cli.start()
    try:
        started = cli.start_test_combat(
            encounter="EXOSKELETONS_NORMAL", seed="repeated-enemy-restore-test"
        )
        assert started.get("decision") == "combat_play"
        root = cli.capture_combat_snapshot("repeated_enemy_root")
        assert root.get("success") is True

        search_state = cli.get_search_state()["combat_state_for_search"]
        action = next(
            candidate
            for candidate in available_actions_from_search_state(search_state)
            if candidate.action_type == "play_card" and candidate.target_index == 2
        )
        action_name, args = cli_payload_for_action(action)
        played = cli.action(action_name, args=args, compact=True)
        assert played.get("type") != "error"

        expected = cli.get_search_state()["combat_state_for_search"]
        child = cli.capture_combat_snapshot("repeated_enemy_child")
        assert child.get("success") is True

        restored_root = cli.restore_combat_snapshot("repeated_enemy_root", compact=True)
        assert restored_root.get("type") != "error"
        restored_child = cli.restore_combat_snapshot("repeated_enemy_child", compact=True)
        assert restored_child.get("type") != "error"

        actual = cli.get_search_state()["combat_state_for_search"]
        round_trip = cli.capture_combat_snapshot("repeated_enemy_round_trip")
        assert round_trip.get("state_fingerprint") == child.get("state_fingerprint")
        assert round_trip.get("semantic_state_fingerprint") == child.get(
            "semantic_state_fingerprint"
        )
        assert hash_search_state(actual) == hash_search_state(expected)
    finally:
        cli.stop()


def test_semantic_fingerprint_keeps_hidden_combat_history_distinct():
    cli = Sts2CliAdapter(CliConfig(repo_root=REPO_ROOT))
    cli.start()
    try:
        started = cli.start_test_combat(
            encounter="EXOSKELETONS_NORMAL", seed="semantic-commutative-test"
        )
        assert started.get("decision") == "combat_play"
        root = cli.capture_combat_snapshot("semantic_commutative_root")
        assert root.get("success") is True

        def play(card_id, target_index=None):
            state = cli.get_search_state()["combat_state_for_search"]
            action = next(
                candidate
                for candidate in available_actions_from_search_state(state)
                if candidate.action_type == "play_card"
                and (candidate.metadata or {}).get("card_id") == card_id
                and (target_index is None or candidate.target_index == target_index)
            )
            action_name, args = cli_payload_for_action(action)
            result = cli.action(action_name, args=args, compact=True)
            assert result.get("type") != "error"

        play("STRIKE_IRONCLAD", target_index=2)
        play("DEFEND_IRONCLAD")
        state_ab = cli.get_search_state()["combat_state_for_search"]
        snapshot_ab = cli.capture_combat_snapshot("semantic_commutative_ab")

        restored = cli.restore_combat_snapshot("semantic_commutative_root", compact=True)
        assert restored.get("type") != "error"
        play("DEFEND_IRONCLAD")
        play("STRIKE_IRONCLAD", target_index=2)
        state_ba = cli.get_search_state()["combat_state_for_search"]
        snapshot_ba = cli.capture_combat_snapshot("semantic_commutative_ba")

        assert snapshot_ab.get("state_fingerprint") != snapshot_ba.get("state_fingerprint")
        # The public search state is identical, but CombatHistory preserves the
        # order of damage, block, and card-finished entries. Engine effects query
        # that hidden history, so treating these states as a commutative DAG hit
        # would reintroduce the same class of semantic drift as root replay.
        assert snapshot_ab.get("semantic_state_fingerprint") != snapshot_ba.get(
            "semantic_state_fingerprint"
        )
        assert hash_search_state_for_subtree_cache(
            state_ab
        ) == hash_search_state_for_subtree_cache(state_ba)
    finally:
        cli.stop()


def test_strict_dag_cache_wakes_waiters_with_a_cloned_result():
    cache = _SharedDagCache()
    key = ("sts2-combat-snapshot-v1:test", 3, 1, 3)
    status, _ = cache.reserve(key)
    assert status == "owner"

    waiting = threading.Event()

    def wait_for_result():
        waiter_status, event = cache.reserve(key)
        assert waiter_status == "wait"
        waiting.set()
        assert event.wait(timeout=2.0)
        return cache.completed(key)

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(wait_for_result)
    assert waiting.wait(timeout=2.0)
    result = SearchResult(
        score=7.0,
        sequence=[],
        leaf_state={"success": True},
        stats={"nodes": 2},
    )
    cache.publish(key, result)
    waited = future.result(timeout=2.0)
    executor.shutdown(wait=True)
    assert waited is not None
    assert waited.score == 7.0
    assert waited.stats["dag_cache_hit"] is True

    status, cached = cache.reserve(key)
    assert status == "cached"
    assert cached is not result
    assert cached.score == 7.0
    assert cached.stats["dag_cache_hit"] is True


def test_strict_dag_is_opt_in(monkeypatch):
    from controller.search.combat_search import CombatSearcher, CombatSpec

    monkeypatch.delenv("STS2_ENABLE_STRICT_DAG", raising=False)
    disabled = CombatSearcher(
        None, CombatSpec("Ironclad", "TEST", "seed"), score_mode="balanced"
    )
    assert disabled.strict_dag_enabled is False

    monkeypatch.setenv("STS2_ENABLE_STRICT_DAG", "1")
    enabled = CombatSearcher(
        None, CombatSpec("Ironclad", "TEST", "seed"), score_mode="balanced"
    )
    assert enabled.strict_dag_enabled is True

    monkeypatch.setenv("STS2_DISABLE_STRICT_DAG", "1")
    forced_off = CombatSearcher(
        None, CombatSpec("Ironclad", "TEST", "seed"), score_mode="balanced"
    )
    assert forced_off.strict_dag_enabled is False


def test_strict_dag_reuses_exact_state_across_different_histories():
    from controller.search.combat_search import CombatSearcher, CombatSpec

    searcher = CombatSearcher(
        None, CombatSpec("Ironclad", "TEST", "seed"), score_mode="balanced"
    )
    searcher.strict_dag_enabled = True
    state = {
        "success": True,
        "engine_state_fingerprint": "sts2-combat-snapshot-v1:shared",
        "combat": {"player": {"hp": 40}, "enemies": [], "available_actions": []},
    }
    calls = []

    def fake_impl(self, current, history, action_budget, chance_depth,
                  pre_chance_budget, is_root=False):
        calls.append(tuple(row.action for row in history))
        return SearchResult(
            score=7.0,
            sequence=[],
            leaf_state=current,
            stats={"nodes": 9, "semantic_reuse_safe": True},
        )

    searcher._search_from_state_impl = MethodType(fake_impl, searcher)
    first_history = [RecordedAction("play_card", {"card_index": 0})]
    second_history = [RecordedAction("play_card", {"card_index": 1})]

    searcher._search_from_state(state, first_history, 3, 1, 3)
    reused = searcher._search_from_state(state, second_history, 3, 1, 3)

    assert calls == [("play_card",)]
    assert reused.stats["dag_cache_hit"] is True
    assert searcher.timing["dag_cache_hits"] == 1
    assert searcher.timing["strict_dag_nodes_avoided"] == 8


def test_strict_dag_keeps_preference_potion_cost_in_path_context():
    from controller.search.combat_search import CombatSearcher, CombatSpec

    searcher = CombatSearcher(
        None, CombatSpec("Ironclad", "TEST", "seed"), score_mode="preference"
    )
    searcher.strict_dag_enabled = True
    state = {
        "success": True,
        "engine_state_fingerprint": "sts2-combat-snapshot-v1:shared",
        "combat": {"player": {"hp": 40}, "enemies": [], "available_actions": []},
    }
    calls = []

    def fake_impl(self, current, history, action_budget, chance_depth,
                  pre_chance_budget, is_root=False):
        calls.append(tuple(row.action for row in history))
        return SearchResult(
            score=7.0,
            sequence=[],
            leaf_state=current,
            stats={"nodes": 2, "semantic_reuse_safe": True},
        )

    searcher._search_from_state_impl = MethodType(fake_impl, searcher)
    searcher._search_from_state(
        state, [RecordedAction("play_card", {"card_index": 0})], 3, 1, 3
    )
    searcher._search_from_state(
        state, [RecordedAction("use_potion", {"potion_index": 0})], 3, 1, 3
    )

    assert calls == [("play_card",), ("use_potion",)]
    assert searcher.timing["dag_cache_hits"] == 0


def test_strict_dag_does_not_merge_different_engine_fingerprints():
    from controller.search.combat_search import CombatSearcher, CombatSpec

    searcher = CombatSearcher(
        None, CombatSpec("Ironclad", "TEST", "seed"), score_mode="balanced"
    )
    searcher.strict_dag_enabled = True
    first = {
        "success": True,
        "engine_state_fingerprint": "sts2-combat-snapshot-v1:first",
        "combat": {"player": {"hp": 40}, "enemies": [], "available_actions": []},
    }
    second = copy.deepcopy(first)
    second["engine_state_fingerprint"] = "sts2-combat-snapshot-v1:second"
    calls = []

    def fake_impl(self, current, history, action_budget, chance_depth,
                  pre_chance_budget, is_root=False):
        calls.append(current["engine_state_fingerprint"])
        return SearchResult(
            score=7.0,
            sequence=[],
            leaf_state=current,
            stats={"nodes": 2, "semantic_reuse_safe": True},
        )

    searcher._search_from_state_impl = MethodType(fake_impl, searcher)
    searcher._search_from_state(first, [], 3, 1, 3)
    searcher._search_from_state(second, [], 3, 1, 3)

    assert calls == [
        "sts2-combat-snapshot-v1:first",
        "sts2-combat-snapshot-v1:second",
    ]
    assert searcher.timing["dag_cache_hits"] == 0


def test_semantic_transition_proof_rejects_shuffle_and_accepts_append_only_piles():
    from controller.search.combat_search import CombatSearcher

    parent = {
        "success": True,
        "combat": {
            "draw_pile": [{"card_id": "A"}, {"card_id": "B"}, {"card_id": "C"}],
            "discard_pile": [{"card_id": "D"}, {"card_id": "E"}],
            "exhaust_pile": [],
            "play_pile": [],
        },
    }
    append_only = copy.deepcopy(parent)
    append_only["combat"]["draw_pile"] = [{"card_id": "B"}, {"card_id": "C"}]
    append_only["combat"]["discard_pile"].append({"card_id": "A"})
    assert CombatSearcher._semantic_transition_preserves_pile_order(parent, append_only)

    shuffled = copy.deepcopy(append_only)
    shuffled["combat"]["draw_pile"] = [{"card_id": "D"}, {"card_id": "B"}]
    shuffled["combat"]["discard_pile"] = []
    assert not CombatSearcher._semantic_transition_preserves_pile_order(parent, shuffled)


def test_semantic_dag_reuses_certified_equivalent_state():
    from controller.search.combat_search import CombatSearcher, CombatSpec

    searcher = CombatSearcher(None, CombatSpec("Ironclad", "TEST", "seed"), score_mode="balanced")
    first = {
        "success": True,
        "engine_state_fingerprint": "sts2-combat-snapshot-v1:first",
        "engine_semantic_state_fingerprint": "sts2-combat-semantic-v1:shared",
        "combat": {
            "player": {"hp": 40, "energy": 1},
            "enemies": [],
            "hand": [],
            "draw_pile": [{"card_id": "C"}],
            "discard_pile": [{"card_id": "A"}, {"card_id": "B"}],
            "exhaust_pile": [],
            "play_pile": [],
            "available_actions": [],
        },
    }
    second = copy.deepcopy(first)
    second["engine_state_fingerprint"] = "sts2-combat-snapshot-v1:second"
    second["combat"]["discard_pile"].reverse()
    assert hash_search_state_for_subtree_cache(first) == hash_search_state_for_subtree_cache(second)

    calls = []

    def fake_impl(self, state, history, action_budget, chance_depth, pre_chance_budget, is_root=False):
        calls.append(state["engine_state_fingerprint"])
        return SearchResult(
            score=7.0,
            sequence=[],
            leaf_state=state,
            stats={"nodes": 9, "semantic_reuse_safe": True},
        )

    def fake_replay(self, state, history, cached):
        return SearchResult(
            score=cached.score,
            sequence=list(cached.sequence),
            leaf_state=state,
            stats={**cached.stats, "semantic_dag_cache_hit": True},
        )

    searcher._search_from_state_impl = MethodType(fake_impl, searcher)
    searcher._replay_semantic_result = MethodType(fake_replay, searcher)
    searcher._search_from_state(first, [], 3, 1, 3)
    reused = searcher._search_from_state(second, [], 3, 1, 3)

    assert calls == ["sts2-combat-snapshot-v1:first"]
    assert reused.stats["semantic_dag_cache_hit"] is True
    assert searcher.timing["semantic_dag_hits"] == 1


def test_batch_request_preserves_exact_target_indices(monkeypatch):
    from controller.search.actions import SearchAction
    from controller.search.combat_search import CombatSearcher, CombatSpec, _CliWorkerContext

    class FakeCli:
        last_call_ms = 0.0
        last_response_bytes = 0
        last_response_queue_wait_ms = 0.0
        last_json_parse_ms = 0.0

        def expand_combat_children(self, parent_snapshot_id, children, **kwargs):
            self.children = children
            self.kwargs = kwargs
            return {
                "type": "combat_children_expanded",
                "success": True,
                "children": [{"success": False} for _ in children],
            }

    monkeypatch.delenv("STS2_ENABLE_BATCH_EXPANSION", raising=False)
    monkeypatch.delenv("STS2_DISABLE_BATCH_EXPANSION", raising=False)
    searcher = CombatSearcher(None, CombatSpec("Ironclad", "TEST", "seed"), reuse_cli_processes=True)
    fake = FakeCli()
    searcher._cli_by_thread[threading.get_ident()] = _CliWorkerContext(
        cli=fake, snapshot_ids_by_history={tuple(): "root"}
    )
    actions = [
        SearchAction(
            "play_card", card_index=1, target_index=index,
            metadata={"card_id": "STRIKE_IRONCLAD", "target_monster_id": "EXOSKELETON"},
        )
        for index in range(4)
    ]
    searcher._try_batch_expand_children({"success": True, "combat": {}}, [], actions)
    assert [row["args"]["target_index"] for row in fake.children] == [0, 1, 2, 3]
    assert fake.kwargs["fingerprint_mode"] == "none"


def test_batch_timeout_discards_worker_before_fallback(monkeypatch):
    from controller.search.actions import SearchAction
    from controller.search.combat_search import CombatSearcher, CombatSpec, _CliWorkerContext

    class FakeCli:
        last_call_ms = 0.0
        last_response_bytes = 0
        last_response_queue_wait_ms = 0.0
        last_json_parse_ms = 0.0

        def expand_combat_children(self, parent_snapshot_id, children, **kwargs):
            self.kwargs = kwargs
            raise TimeoutError("synthetic RPC timeout")

        def stop(self):
            self.stopped = True

    class FakePool:
        def __init__(self):
            self.discarded = []

        def discard(self, cli, reason=""):
            self.discarded.append((cli, reason))

    monkeypatch.delenv("STS2_ENABLE_BATCH_EXPANSION", raising=False)
    monkeypatch.delenv("STS2_DISABLE_BATCH_EXPANSION", raising=False)
    pool = FakePool()
    searcher = CombatSearcher(
        None,
        CombatSpec("Ironclad", "TEST", "seed"),
        reuse_cli_processes=True,
        worker_pool=pool,
        max_search_ms=100,
    )
    searcher._reset_search_deadline()
    fake = FakeCli()
    ctx = _CliWorkerContext(cli=fake, snapshot_ids_by_history={tuple(): "root"}, owned=False)
    searcher._cli_by_thread[threading.get_ident()] = ctx
    searcher._pooled_borrowed.append(fake)
    actions = [
        SearchAction(
            "play_card",
            card_index=1,
            target_index=0,
            metadata={"card_id": "STRIKE_IRONCLAD", "target_monster_id": "EXOSKELETON"},
        )
    ]

    candidates = searcher._prepare_action_candidates(
        {"success": True, "combat": {"player": {"hp": 80, "max_hp": 80}}},
        [],
        actions,
    )

    assert candidates == []
    assert fake.kwargs["timeout_s"] >= 0.25
    assert pool.discarded == [(fake, "batch_expand_timeout")]
    assert fake not in searcher._pooled_borrowed
    assert searcher._cli_by_thread.get(threading.get_ident()) is None
    assert searcher.timing["engine_rpc_timeouts"] == 1
    assert searcher.timing["time_budget_exhausted"] == 1
    assert searcher.timing["known_unexpanded_action_edges"] == 1


def test_semantic_replay_does_not_collapse_duplicate_monster_ids():
    from controller.search.combat_search import CombatSearcher, CombatSpec, RecordedAction

    searcher = CombatSearcher(None, CombatSpec("Ironclad", "TEST", "seed"))
    state = {
        "combat": {
            "available_actions": [
                {
                    "action_type": "play_card",
                    "card_index": 2,
                    "target_index": index,
                    "metadata": {
                        "card_id": "STRIKE_IRONCLAD",
                        "target_monster_id": "EXOSKELETON",
                    },
                }
                for index in range(4)
            ]
        }
    }
    step = RecordedAction(
        "play_card",
        {
            "card_index": 2,
            "target_index": 3,
            "card_id": "STRIKE_IRONCLAD",
            "target_monster_id": "EXOSKELETON",
            "target_type": "AnyEnemy",
        },
    )
    assert searcher._resolve_replay_payload(state, step) == {
        "card_index": 2,
        "target_index": 3,
    }


def test_semantic_replay_preserves_duplicate_card_slot():
    from controller.search.combat_search import CombatSearcher, CombatSpec, RecordedAction

    searcher = CombatSearcher(None, CombatSpec("Ironclad", "TEST", "seed"))
    state = {
        "combat": {
            "available_actions": [
                {
                    "action_type": "play_card",
                    "card_index": card_index,
                    "target_index": 2,
                    "metadata": {
                        "card_id": "STRIKE_IRONCLAD",
                        "target_monster_id": "EXOSKELETON",
                    },
                }
                for card_index in (3, 5)
            ]
        }
    }
    step = RecordedAction(
        "play_card",
        {
            "card_index": 5,
            "target_index": 2,
            "card_id": "STRIKE_IRONCLAD",
            "target_monster_id": "EXOSKELETON",
            "target_type": "AnyEnemy",
        },
    )
    assert searcher._resolve_replay_payload(state, step) == {
        "card_index": 5,
        "target_index": 2,
    }


def test_semantic_replay_preserves_duplicate_potion_slot():
    from controller.search.combat_search import CombatSearcher, CombatSpec, RecordedAction

    searcher = CombatSearcher(None, CombatSpec("Ironclad", "TEST", "seed"))
    state = {
        "combat": {
            "available_actions": [
                {
                    "action_type": "use_potion",
                    "target_index": 1,
                    "metadata": {
                        "potion_id": "FIRE_POTION",
                        "potion_index": potion_index,
                        "target_monster_id": "EXOSKELETON",
                    },
                }
                for potion_index in (0, 2)
            ]
        }
    }
    step = RecordedAction(
        "use_potion",
        {
            "potion_index": 2,
            "target_index": 1,
            "potion_id": "FIRE_POTION",
            "target_monster_id": "EXOSKELETON",
            "target_type": "AnyEnemy",
        },
    )
    assert searcher._resolve_replay_payload(state, step) == {
        "potion_index": 2,
        "target_index": 1,
    }
