import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.verify_combat_replay_gate import compare_history, compare_replays, load_snapshot


def _step(action="play_card", card="BASH"):
    return {
        "action": action,
        "payload": {"card_index": 2, "target_index": 0} if action == "play_card" else {},
        "chosen": {"action_type": action, "metadata": {"card_id": card} if card else {}},
        "reused_plan": False,
        "score": 12.0,
        "decision_reason": "highest_score",
        "before_state_hash": "before",
        "after_state_hash": "after",
        "rng_before_sha256": "before-rng",
        "rng_after_sha256": "after-rng",
        "after_decision": "combat_play",
        "time_budget_exhausted": False,
    }


def test_aa_gate_reports_first_action_or_rng_difference():
    left = {"status": "COMPLETED", "terminal": "combat_reward", "actions": [_step()]}
    right = json.loads(json.dumps(left))
    assert compare_replays(left, right)["status"] == "PASS"
    right["actions"][0]["rng_after_sha256"] = "different"
    result = compare_replays(left, right)
    assert result["status"] == "FAILED"
    assert result["first_difference"] == 1
    assert result["fields"] == ["rng_after_sha256"]


def test_aa_gate_rejects_identical_but_incomplete_runs():
    incomplete = {"status": "FAILED", "error": "fallback", "actions": [_step()]}
    result = compare_replays(incomplete, incomplete)
    assert result["status"] == "FAILED"
    assert result["left_completed_actions"] == 1


def test_snapshot_loader_uses_unsanitized_restore_and_checks_both_hashes(tmp_path, monkeypatch):
    import scripts.verify_combat_replay_gate as gate

    monkeypatch.setattr(gate, "snapshot_compatibility", lambda *_: {"contract": 3})
    restore = b'{"PlayerJson":"potion-present"}'
    search = b'{"PlayerJson":"potion-removed"}'
    (tmp_path / "restore_snapshot.json").write_bytes(restore)
    (tmp_path / "search_snapshot.json").write_bytes(search)
    metadata = {
        "schema": "sts2.combat_snapshot.v2", "status": "RESTORE_VERIFIED", "reusable": True,
        "compatibility": {"contract": 3},
        "restore_sha256": hashlib.sha256(restore).hexdigest(),
        "search_sha256": hashlib.sha256(search).hexdigest(),
    }
    (tmp_path / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    _, actual = load_snapshot(tmp_path, SimpleNamespace())
    assert actual == restore.decode("utf-8")
    (tmp_path / "search_snapshot.json").write_text("modified", encoding="utf-8")
    with pytest.raises(ValueError, match="search_snapshot.json"):
        load_snapshot(tmp_path, SimpleNamespace())


def test_history_gate_compares_shadow_command_and_rng(tmp_path):
    from scripts.verify_combat_replay_gate import _digest

    run_report = {
        "config": {"depth": 12, "chance_depth": 1, "search_budget_ms": 20000},
        "actions": [{
            "decision": "combat.play_card", "timestamp_utc": "2026-10-02T10:00:01+00:00",
            "headless_action": "play_card", "headless_args": {"card_index": 2, "target_index": 0},
            "decision_telemetry": {"combat_number": 1, "reused_plan": False,
                                   "chosen": {"action_type": "play_card", "metadata": {"card_id": "BASH"}},
                                   "score_explanation": {"scorer": {"weights_sha256": "model"}}},
            "shadow_rng_before": {"counter": 1}, "shadow_rng_after": {"counter": 2},
        }],
    }
    (tmp_path / "run_report.json").write_text(json.dumps(run_report), encoding="utf-8")
    step = _step()
    step["rng_before_sha256"] = _digest({"counter": 1})
    step["rng_after_sha256"] = _digest({"counter": 2})
    sources = ("controller/search/combat_search.py", "controller/run_agent.py")
    metadata = {"created_at_utc": 1790935200, "context": {"session_dir": str(tmp_path)},
                "source_sha256": {name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
                                  for name in sources}}
    replay = {"status": "COMPLETED", "actions": [step]}
    settings = {"depth": 12, "chance_depth": 1, "max_search_ms": 20000}
    assert compare_history(metadata, replay, {"weights_sha256": "model"}, settings)["status"] == "PASS"
    run_report["actions"][0]["headless_args"]["card_index"] = 3
    (tmp_path / "run_report.json").write_text(json.dumps(run_report), encoding="utf-8")
    result = compare_history(metadata, replay, {"weights_sha256": "model"}, settings)
    assert result["reason"] == "shadow_action_mismatch"
    assert result["first_difference"] == 1
