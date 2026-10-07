import json
from pathlib import Path

import pytest

from controller.combat_comparison import ModelLibrary, _anchor_model, compare_outcomes, run_job
from controller.combat_scoring import CombatScoring, active_model, stage_for_floor


def _replay(terminal="combat_reward", hp=40, potions=()):
    return {"status": "COMPLETED", "terminal": terminal,
            "outcome": {"hp": hp, "potions": list(potions)}}


def test_paired_outcomes_keep_resource_tradeoffs_separate():
    assert compare_outcomes(_replay(hp=50, potions=["A"]),
                            _replay(hp=45, potions=["A", "B"]))["category"] == "resource_tradeoff"
    assert compare_outcomes(_replay(hp=50, potions=["A"]),
                            _replay(hp=45, potions=[]))["category"] == "a_resources"
    assert compare_outcomes(_replay(), _replay("defeat"))["category"] == "a_win"
    assert compare_outcomes(_replay("defeat"), _replay("game_over"))["category"] == "both_lost"
    assert compare_outcomes({"status": "FAILED"}, _replay())["category"] == "invalid"


def test_model_library_saves_immutable_schema_checked_versions(tmp_path):
    library = ModelLibrary(tmp_path)
    weights = active_model()["weights"].copy()
    saved = library.save("more damage", {**weights, "enemy_hp_removed": 25})
    assert saved["id"].startswith("saved:")
    assert library.get(saved["id"])["weights"]["enemy_hp_removed"] == 25
    assert library.save("new name", {**weights, "enemy_hp_removed": 25})["id"] == saved["id"]
    renamed = library.rename(saved["id"], "保血策略")
    assert renamed["name"] == "保血策略"
    assert next(row for row in library.catalog()["models"] if row["id"] == saved["id"])["name"] == "保血策略"
    assert library.get(saved["id"])["weights"]["enemy_hp_removed"] == 25
    with pytest.raises(ValueError, match="current scoring features"):
        library.save("broken", {"hp_change": 1})
    with pytest.raises(ValueError, match="between 0 and 1000"):
        library.save("broken", {**weights, "hp_change": float("nan")})
    with pytest.raises(ValueError, match="Only saved"):
        library.delete("active")
    library.delete(saved["id"])
    with pytest.raises(ValueError, match="Unknown"):
        library.get(saved["id"])


def test_historical_anchor_selects_recorded_scorer(tmp_path):
    active = active_model()
    changed = {**active, "weights": {**active["weights"], "hp_change": 80}}
    scorer = CombatScoring(stage_for_floor(2), active).identity
    report = {"actions": [{"decision": "combat.play_card",
                           "timestamp_utc": "2026-10-02T10:00:01+00:00",
                           "decision_telemetry": {"score_explanation": {"scorer": scorer}}}]}
    (tmp_path / "run_report.json").write_text(json.dumps(report), encoding="utf-8")
    metadata = {"created_at_utc": 1790935200,
                "context": {"session_dir": str(tmp_path), "floor": 2}}
    assert _anchor_model(metadata, [changed, active]) == active
    assert _anchor_model(metadata, [changed]) is None
    (tmp_path / 'scoring_model.json').write_text(json.dumps(active), encoding='utf-8')
    assert _anchor_model(metadata, [changed]) == active


def test_job_keeps_exclusions_out_of_valid_counts(tmp_path, monkeypatch):
    import controller.combat_comparison as comparison

    job_dir = tmp_path / "jobs" / "test_job"
    job_dir.mkdir(parents=True)
    config = {"snapshots": [str(tmp_path / "good"), str(tmp_path / "bad")],
              "a_id": "active", "b_id": "saved:test", "a_name": "A", "b_name": "B",
              "a_model": {}, "b_model": {}, "anchor_models": [], "parallel_pairs": 1}
    (job_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")

    def fake_compare(_root, _cache, snapshot: Path, *_args):
        if snapshot.name == "bad":
            return {"snapshot_id": "bad", "status": "EXCLUDED", "reason": "baseline_gate"}
        return {"snapshot_id": "good", "status": "VALID",
                "comparison": {"category": "a_win"}, "a": _replay(), "b": _replay("defeat")}

    monkeypatch.setattr(comparison, "_compare_one", fake_compare)
    run_job(tmp_path, job_dir)
    status = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "completed"
    assert status["counts"] == {"a_win": 1, "excluded": 1}
    assert len(status["pairs"]) == 2
