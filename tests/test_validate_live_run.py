import json

from controller.action_transaction import make_transaction
from scripts.validate_live_run import validate_live_run


def action(sequence=1, *, mirrored=True, status="completed"):
    transaction = make_transaction(
        "play_card",
        {"card_index": 0, "target_index": 0},
        shadow_action="play_card" if mirrored else None,
        shadow_args={"card_index": 0, "target_index": 0} if mirrored else None,
    ).to_dict()
    return {
        "sequence": sequence,
        "status": status,
        "event": "live_action" if mirrored else "client_only_action",
        "transaction": transaction,
        "client_action": "play_card",
        "client_params": {"card_index": 0, "target_index": 0},
        "headless_action": "play_card" if mirrored else None,
        "headless_args": (
            {"card_index": 0, "target_index": 0} if mirrored else None
        ),
        "client_digest_before": "before",
        "client_digest_after": "after",
        "flow_managed": mirrored,
        "shadow_action_applied": mirrored,
        "rng_parity": {"status": "PASS"} if mirrored else None,
    }


def report(actions, **overrides):
    value = {
        "schema_version": 3,
        "transaction_schema_version": 1,
        "status": "COMPLETED",
        "target_combat_count": 1,
        "completed_combat_count": 1,
        "actions": actions,
        "pending_action": None,
    }
    value.update(overrides)
    return value


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def test_completed_report_with_valid_transactions_passes(tmp_path):
    path = tmp_path / "run_report.json"
    write(path, report([action()]))
    result = validate_live_run(path)
    assert result.verdict == "PASS"
    assert result.reports["run"]["mirror_modes"] == {"mirrored": 1}
    assert result.coverage["families"] == {"combat": 1}


def test_transaction_timeline_order_is_validated(tmp_path):
    path = tmp_path / "run_report.json"
    row = action()
    row["transaction_timeline"] = {
        "client_settled_ms": 10.0,
        "shadow_started_ms": 8.0,
        "shadow_finished_ms": 7.0,
        "shadow_elapsed_ms": -1.0,
    }
    write(path, report([row]))
    result = validate_live_run(path)
    codes = {issue.code for issue in result.issues}
    assert result.verdict == "FAIL"
    assert "invalid_transaction_timeline" in codes
    assert "transaction_timeline_order" in codes


def test_client_only_timeline_does_not_require_shadow_fields(tmp_path):
    path = tmp_path / "run_report.json"
    row = action(mirrored=False)
    row["transaction"] = make_transaction(
        "confirm_bundle", {}, shadow_action=None, shadow_args=None
    ).to_dict()
    row["client_action"] = "confirm_bundle"
    row["client_params"] = {}
    row["transaction_timeline"] = {"client_settled_ms": 12.5}
    write(path, report([row]))
    result = validate_live_run(path)
    assert result.verdict == "PASS"


def test_client_only_timeline_rejects_invalid_client_duration(tmp_path):
    path = tmp_path / "run_report.json"
    row = action(mirrored=False)
    row["transaction"] = make_transaction(
        "confirm_bundle", {}, shadow_action=None, shadow_args=None
    ).to_dict()
    row["client_action"] = "confirm_bundle"
    row["client_params"] = {}
    row["transaction_timeline"] = {"client_settled_ms": -1.0}
    write(path, report([row]))
    result = validate_live_run(path)
    assert result.verdict == "FAIL"
    assert any(issue.code == "invalid_client_transaction_timeline" for issue in result.issues)


def test_transaction_timeline_budget_overrun_is_a_warning(tmp_path):
    path = tmp_path / "run_report.json"
    row = action()
    row["transaction"]["telemetry"] = {"shadow_timeout_ms": 100.0}
    row["transaction_timeline"] = {
        "client_settled_ms": 10.0,
        "shadow_started_ms": 11.0,
        "shadow_finished_ms": 500.0,
        "shadow_elapsed_ms": 489.0,
    }
    write(path, report([row]))
    result = validate_live_run(path)
    assert result.verdict == "WARN"
    assert any(issue.code == "shadow_timeout_overrun" for issue in result.issues)


def test_missing_transaction_and_unknown_outcome_fail(tmp_path):
    path = tmp_path / "run_report.json"
    row = action(status="outcome_unknown")
    row.pop("transaction")
    row["event"] = "action_failed"
    write(path, report([row], status="FAIL", error="transport timeout"))
    result = validate_live_run(path)
    codes = {issue.code for issue in result.issues}
    assert result.verdict == "FAIL"
    assert {"failed_report", "report_error", "failed_action", "missing_transaction"} <= codes


def test_persisted_commands_must_match_transaction(tmp_path):
    path = tmp_path / "run_report.json"
    row = action()
    row["client_params"] = {"card_index": 9}
    row["headless_action"] = "end_turn"
    write(path, report([row]))
    result = validate_live_run(path)
    codes = {issue.code for issue in result.issues}
    assert result.verdict == "FAIL"
    assert {"client_params_mismatch", "shadow_action_mismatch"} <= codes


def test_shadow_failure_is_a_transaction_failure(tmp_path):
    path = tmp_path / "run_report.json"
    row = action()
    row["shadow_action_applied"] = False
    row["transaction_status"] = "shadow_failed"
    row["shadow_error"] = "engine timeout"
    write(path, report([row], status="FAIL", error="engine timeout"))
    result = validate_live_run(path)
    codes = {issue.code for issue in result.issues}
    assert {"shadow_failed", "shadow_not_applied"} <= codes


def test_nonterminal_pending_report_warns(tmp_path):
    path = tmp_path / "run_report.json"
    row = action(status="pending")
    row["event"] = "action_pending"
    write(path, report([row], status="RUNNING", pending_action=row))
    result = validate_live_run(path)
    assert result.verdict == "WARN"
    assert {issue.code for issue in result.issues} == {
        "report_not_terminal",
        "incomplete_action",
    }


def test_terminal_report_cannot_leave_native_choice_pending(tmp_path):
    path = tmp_path / "run_report.json"
    row = action(status="awaiting_input")
    row.update(event="action_awaiting_input", transaction_status="both_awaiting_input",
               native_action_phase="awaiting_input")
    write(path, report([row], pending_action=row))
    result = validate_live_run(path)
    codes = {issue.code for issue in result.issues}
    assert result.verdict == "FAIL"
    assert {"pending_native_choice", "terminal_pending_action"} <= codes


def test_nonterminal_native_choice_pause_is_explicit_warning(tmp_path):
    path = tmp_path / "run_report.json"
    row = action(status="awaiting_input")
    row.update(event="action_awaiting_input", transaction_status="both_awaiting_input",
               native_action_phase="awaiting_input")
    write(path, report([row], status="RUNNING", pending_action=row))
    result = validate_live_run(path)
    assert result.verdict == "WARN"
    assert any(issue.code == "pending_native_choice" for issue in result.issues)


def test_session_log_action_failure_is_reported(tmp_path):
    path = tmp_path / "run_report.json"
    session = tmp_path / "session.jsonl"
    write(path, report([action()]))
    session.write_text(
        json.dumps(
            {"event": "action_failed", "sequence": 3, "status": "outcome_unknown"}
        )
        + "\n",
        encoding="utf-8",
    )
    result = validate_live_run(path)
    assert result.verdict == "FAIL"
    assert result.session_log["failed_sequences"] == [3]
    assert any(issue.code == "session_action_failed" for issue in result.issues)


def test_opening_report_is_included_in_coverage(tmp_path):
    run_path = tmp_path / "run_report.json"
    opening_path = tmp_path / "opening_report.json"
    write(run_path, report([action()]))
    ui = action(mirrored=False)
    ui['transaction'] = make_transaction('open_character_select').to_dict()
    ui['client_action'] = 'open_character_select'
    ui['client_params'] = {}
    write(opening_path, {"schema_version": 2, "status": "COMPLETED", "actions": [ui]})
    result = validate_live_run(run_path)
    assert result.verdict == "PASS"
    assert result.coverage["mirror_modes"] == {"client_only": 1, "mirrored": 1}
