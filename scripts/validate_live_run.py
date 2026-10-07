from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from controller.action_transaction import MirrorMode, transaction_from_dict
from controller.live_session import read_json


TERMINAL_STATUSES = {"COMPLETED", "DEFEAT", "VICTORY"}
FAILED_REPORT_STATUSES = {"BLOCKED", "FAIL"}
FAILED_ACTION_STATUSES = {"cancelled", "outcome_unknown"}


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    message: str
    source: str
    sequence: int | None = None


@dataclass
class ValidationResult:
    run_report: str
    verdict: str = "PASS"
    issues: list[ValidationIssue] = field(default_factory=list)
    reports: dict[str, dict[str, Any]] = field(default_factory=dict)
    session_log: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, dict[str, int]] = field(default_factory=dict)

    def issue(self, severity, code, message, source, sequence=None) -> None:
        self.issues.append(
            ValidationIssue(severity, code, message, str(source), sequence)
        )

    def finish(self) -> "ValidationResult":
        severities = {issue.severity for issue in self.issues}
        self.verdict = (
            "FAIL" if "error" in severities
            else "WARN" if "warning" in severities
            else "PASS"
        )
        return self

    def to_dict(self) -> dict[str, Any]:
        counts = Counter(issue.severity for issue in self.issues)
        return {
            "verdict": self.verdict,
            "run_report": self.run_report,
            "issue_counts": {
                "errors": counts["error"],
                "warnings": counts["warning"],
            },
            "reports": self.reports,
            "session_log": self.session_log,
            "coverage": self.coverage,
            "issues": [asdict(issue) for issue in self.issues],
        }


def validate_live_run(
    run_report_path: Path,
    *,
    opening_report_path: Path | None = None,
    session_log_path: Path | None = None,
) -> ValidationResult:
    run_report_path = run_report_path.resolve()
    result = ValidationResult(str(run_report_path))
    report = _load_report(run_report_path, result, "run_report")
    if report is None:
        return result.finish()
    _validate_report(report, run_report_path, result, "run", require_schema3=True)

    opening_report_path = _optional_sibling(
        run_report_path, opening_report_path, "opening_report.json"
    )
    if opening_report_path is not None:
        opening = _load_report(opening_report_path, result, "opening_report")
        if opening is not None:
            _validate_report(
                opening,
                opening_report_path,
                result,
                "opening",
                require_schema3=False,
            )

    session_log_path = _optional_sibling(
        run_report_path, session_log_path, "session.jsonl"
    )
    if session_log_path is not None:
        _validate_session_log(session_log_path, result)

    totals = {
        key: Counter() for key in ("families", "intents", "mirror_modes")
    }
    for summary in result.reports.values():
        for key, counts in totals.items():
            counts.update(summary[key])
    result.coverage = {
        key: dict(sorted(counts.items())) for key, counts in totals.items()
    }
    return result.finish()


def _optional_sibling(base: Path, explicit: Path | None, name: str) -> Path | None:
    if explicit is not None:
        return explicit.resolve()
    candidate = base.parent / name
    return candidate if candidate.is_file() else None


def _load_report(path, result, label):
    try:
        value = read_json(path)
    except FileNotFoundError:
        result.issue("error", "missing_file", "report does not exist", path)
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        result.issue("error", "invalid_json", f"cannot read {label}: {exc}", path)
        return None
    if not isinstance(value, dict):
        result.issue("error", "invalid_report", f"{label} must be an object", path)
        return None
    return value


def _validate_report(
    report: dict[str, Any],
    path: Path,
    result: ValidationResult,
    scope: str,
    *,
    require_schema3: bool,
) -> None:
    status = str(report.get("status") or "UNKNOWN").upper()
    actions = report.get("actions")
    if not isinstance(actions, list):
        result.issue("error", "invalid_actions", "actions must be a list", path)
        actions = []
    if require_schema3 and report.get("schema_version") != 3:
        result.issue("error", "unsupported_schema", "run report schema must be 3", path)
    if require_schema3 and report.get("transaction_schema_version") != 1:
        result.issue(
            "error",
            "unsupported_transaction_schema",
            "transaction schema must be 1",
            path,
        )
    if status in FAILED_REPORT_STATUSES:
        result.issue("error", "failed_report", f"report status is {status}", path)
    elif status not in TERMINAL_STATUSES:
        result.issue("warning", "report_not_terminal", f"report status is {status}", path)
    if report.get("error"):
        result.issue("error", "report_error", str(report["error"]), path)

    for recovery in report.get('shadow_recoveries') or []:
        recovery_status = recovery.get('status')
        if recovery_status not in {'BOUNDARY_VERIFIED', 'REPLAYED_RNG_VERIFIED_AWAITING_CHECKPOINT'}:
            result.issue('error', 'failed_shadow_recovery',
                         f'status={recovery_status}: {recovery.get("error") or "no final verification"}',
                         path, recovery.get('sequence'))
        elif recovery_status != 'BOUNDARY_VERIFIED' and status in TERMINAL_STATUSES:
            result.issue('error', 'unclosed_shadow_recovery',
                         'RNG replay has no covered-field boundary verification',
                         path, recovery.get('sequence'))
    for segment in report.get('client_only_segments') or []:
        if segment.get('status') != 'BOUNDARY_VERIFIED':
            result.issue('error' if status in TERMINAL_STATUSES else 'warning',
                         'unclosed_client_segment',
                         f'segment {segment.get("segment_id")}: {segment.get("status")}', path,
                         segment.get('start_sequence'))
    for checkpoint in report.get('parity_checkpoints') or []:
        if checkpoint.get('status') not in {'PASS'}:
            result.issue('error', 'failed_parity_checkpoint',
                         f'{checkpoint.get("label")}: {checkpoint.get("status")}', path,
                         checkpoint.get('sequence'))

    sequences: set[int] = set()
    action_statuses: Counter[str] = Counter()
    mirror_modes: Counter[str] = Counter()
    intents: Counter[str] = Counter()
    families: Counter[str] = Counter()
    failed_signatures: list[str] = []
    for index, row in enumerate(actions, start=1):
        if not isinstance(row, dict):
            result.issue("error", "invalid_action_row", f"row {index} is not an object", path)
            continue
        sequence = row.get("sequence")
        if not isinstance(sequence, int) or sequence < 1:
            result.issue("error", "invalid_sequence", f"row {index} has invalid sequence", path)
            sequence = None
        elif sequence in sequences:
            result.issue("error", "duplicate_sequence", f"duplicate sequence {sequence}", path, sequence)
        else:
            sequences.add(sequence)

        action_status = str(row.get("status") or "missing")
        action_statuses[action_status] += 1
        if action_status in FAILED_ACTION_STATUSES or row.get("event") == "action_failed":
            result.issue(
                "error",
                "failed_action",
                f"status={action_status}: {row.get('error') or 'no error recorded'}",
                path,
                sequence,
            )
            failed_signatures.append(
                json.dumps(
                    [row.get("client_action"), row.get("client_params") or {}],
                    sort_keys=True,
                )
            )
        elif action_status in {"pending", "executing"}:
            severity = "error" if status in TERMINAL_STATUSES else "warning"
            result.issue(
                severity,
                "incomplete_action",
                f"action is {action_status}",
                path,
                sequence,
            )
        elif action_status == 'awaiting_input':
            if (row.get('event') != 'action_awaiting_input'
                    or row.get('native_action_phase') != 'awaiting_input'
                    or row.get('transaction_status') != 'both_awaiting_input'):
                result.issue('error', 'invalid_native_pause',
                             'Pending choice lacks a matched native and shadow pause',
                             path, sequence)
            else:
                result.issue('error' if status in TERMINAL_STATUSES else 'warning',
                             'pending_native_choice',
                             'Native action still awaits player input', path, sequence)
        elif action_status != "completed":
            result.issue(
                "error",
                "unknown_action_status",
                f"status={action_status!r}",
                path,
                sequence,
            )

        data = row.get("transaction")
        if data is None:
            result.issue(
                "error", "missing_transaction", "action has no transaction", path, sequence
            )
            continue
        try:
            transaction = transaction_from_dict(data)
        except ValueError as exc:
            result.issue("error", "invalid_transaction", str(exc), path, sequence)
            continue
        mirror_modes[transaction.mirror_mode.value] += 1
        intents[transaction.intent] += 1
        families[transaction.family.value] += 1
        _validate_action_row(row, transaction, path, sequence, result)

    if _longest_run(failed_signatures) >= 3:
        result.issue(
            "error",
            "repeated_failed_action",
            "the same failed action occurred at least three consecutive times",
            path,
        )
    if report.get("pending_action") is not None and status in TERMINAL_STATUSES:
        result.issue(
            "error", "terminal_pending_action", "terminal report has pending_action", path
        )
    target = report.get("target_combat_count")
    completed = report.get("completed_combat_count")
    if (
        status == "COMPLETED"
        and isinstance(target, int)
        and isinstance(completed, int)
        and completed < target
    ):
        result.issue(
            "error",
            "target_not_reached",
            f"completed {completed} of {target} combats",
            path,
        )

    result.reports[scope] = {
        "path": str(path),
        "schema_version": report.get("schema_version"),
        "transaction_schema_version": report.get("transaction_schema_version"),
        "status": status,
        "action_count": len(actions),
        "action_statuses": dict(sorted(action_statuses.items())),
        "mirror_modes": dict(sorted(mirror_modes.items())),
        "intents": dict(sorted(intents.items())),
        "families": dict(sorted(families.items())),
    }


def _validate_action_row(row, transaction, path, sequence, result) -> None:
    if isinstance(row.get('transaction_timeline'), dict):
        _validate_transaction_timeline(row, transaction, path, sequence, result)
    if transaction.client is None:
        result.issue(
            "error",
            "shadow_only_live_action",
            "live action has no client command",
            path,
            sequence,
        )
    else:
        if row.get("client_action") != transaction.client.action:
            result.issue(
                "error",
                "client_action_mismatch",
                "client action differs from transaction",
                path,
                sequence,
            )
        if (row.get("client_params") or {}) != transaction.client.params:
            result.issue(
                "error",
                "client_params_mismatch",
                "client params differ from transaction",
                path,
                sequence,
            )
    if transaction.shadow is not None:
        rng = row.get('rng_parity')
        if isinstance(rng, dict) and rng.get('status') != 'PASS':
            result.issue('error', 'rng_parity_failed', str(rng.get('differences') or rng),
                         path, sequence)
        if row.get('shadow_completed') is True and not (row.get('rng_verified') is True
                or isinstance(rng, dict) and rng.get('status') == 'PASS'):
            result.issue('error', 'shadow_rng_unverified',
                         'Shadow completion has no verified post-action RNG', path, sequence)
        if row.get('transaction_status') == 'completed' and row.get('boundary_verified') is not True:
            result.issue('error', 'transaction_boundary_unverified',
                         'Completed transaction has no covered-field checkpoint', path, sequence)
        if row.get("transaction_status") == "shadow_failed":
            result.issue(
                "error",
                "shadow_failed",
                str(row.get("shadow_error") or "shadow action failed"),
                path,
                sequence,
            )
        if row.get("headless_action") != transaction.shadow.action:
            result.issue(
                "error",
                "shadow_action_mismatch",
                "shadow action differs from transaction",
                path,
                sequence,
            )
        if (row.get("headless_args") or {}) != transaction.shadow.params:
            result.issue(
                "error",
                "shadow_params_mismatch",
                "shadow params differ from transaction",
                path,
                sequence,
            )
        if row.get("flow_managed") and row.get("shadow_action_applied") is not True:
            result.issue(
                "error",
                "shadow_not_applied",
                "flow-managed shadow was not applied",
                path,
                sequence,
            )
    elif row.get("headless_action") is not None or row.get("headless_args") is not None:
        result.issue(
            "error",
            "unexpected_shadow_fields",
            "client-only action has shadow fields",
            path,
            sequence,
        )
    elif transaction.client is not None and transaction.client.action in {
            'play_card', 'end_turn', 'use_potion', 'discard_potion',
            'choose_map_node', 'claim_reward', 'choose_reward_card',
            'skip_reward_cards', 'choose_rest_option', 'buy_card', 'buy_relic',
            'buy_potion', 'remove_card', 'remove_card_at_shop'}:
        if not transaction.telemetry.get('requires_reanchor'):
            result.issue('error', 'unbounded_client_mutation',
                         'Gameplay mutation has no shadow command or verified re-anchor boundary',
                         path, sequence)

    expected_event = (
        "live_action"
        if transaction.mirror_mode == MirrorMode.MIRRORED
        else "client_only_action"
    )
    if row.get("status") == "completed" and row.get("event") != expected_event:
        result.issue(
            "error",
            "completion_event_mismatch",
            f"expected event {expected_event}",
            path,
            sequence,
        )
    nested = (row.get("decision_telemetry") or {}).get("transaction")
    if nested is not None and nested != row.get("transaction"):
        result.issue(
            "error",
            "nested_transaction_mismatch",
            "telemetry transaction differs",
            path,
            sequence,
        )
    if (
        row.get("status") == "completed"
        and transaction.completion == "state_change"
        and row.get("client_digest_before")
        and row.get("client_digest_before") == row.get("client_digest_after")
    ):
        result.issue(
            "error",
            "missing_state_change",
            "client digest did not change",
            path,
            sequence,
        )


def _validate_transaction_timeline(row, transaction, path, sequence, result) -> None:
    timeline = row.get('transaction_timeline') or {}
    if transaction.shadow is None:
        # Client-only interactions have no shadow lifecycle.  They still may
        # persist the client settlement duration, but requiring shadow start,
        # finish, and elapsed fields would turn a valid protocol boundary into
        # a false positive.
        value = timeline.get('client_settled_ms')
        if value is not None and (
                not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0):
            result.issue('error', 'invalid_client_transaction_timeline',
                         'client-only timeline has an invalid settlement duration',
                         path, sequence)
        return
    keys = ('client_settled_ms', 'shadow_started_ms', 'shadow_finished_ms', 'shadow_elapsed_ms')
    values = {key: timeline.get(key) for key in keys}
    if any(not isinstance(value, (int, float)) or not math.isfinite(float(value))
           for value in values.values()):
        result.issue('error', 'invalid_transaction_timeline',
                     'transaction timeline contains a non-finite or missing duration',
                     path, sequence)
        return
    if any(value < 0 for value in values.values()):
        result.issue('error', 'invalid_transaction_timeline',
                     'transaction timeline contains a negative duration', path, sequence)
    if values['shadow_started_ms'] < values['client_settled_ms']:
        result.issue('error', 'transaction_timeline_order',
                     'shadow started before client settlement', path, sequence)
    if values['shadow_finished_ms'] < values['shadow_started_ms']:
        result.issue('error', 'transaction_timeline_order',
                     'shadow finished before it started', path, sequence)
    expected_elapsed = values['shadow_finished_ms'] - values['shadow_started_ms']
    if abs(expected_elapsed - values['shadow_elapsed_ms']) > 5.0:
        result.issue('error', 'transaction_timeline_elapsed_mismatch',
                     'shadow elapsed time differs from timeline endpoints', path, sequence)
    telemetry = transaction.telemetry or {}
    budget = telemetry.get('shadow_timeout_ms')
    if budget is not None:
        try:
            budget = float(budget)
        except (TypeError, ValueError):
            budget = 0.0
        if budget <= 0:
            result.issue('error', 'invalid_shadow_timeout',
                         'shadow_timeout_ms must be positive', path, sequence)
        elif values['shadow_elapsed_ms'] > budget * 1.25 + 250.0:
            result.issue('warning', 'shadow_timeout_overrun',
                         'shadow elapsed time materially exceeded its declared timeout',
                         path, sequence)


def _validate_session_log(path: Path, result: ValidationResult) -> None:
    counts: Counter[str] = Counter()
    failed: list[int] = []
    malformed = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        result.issue("error", "invalid_session_log", str(exc), path)
        return
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            malformed += 1
            result.issue(
                "error",
                "malformed_session_event",
                f"line {line_number}: {exc}",
                path,
            )
            continue
        if not isinstance(event, dict):
            malformed += 1
            result.issue(
                "error",
                "malformed_session_event",
                f"line {line_number} is not an object",
                path,
            )
            continue
        name = str(event.get("event") or "missing")
        counts[name] += 1
        sequence = (
            event.get("sequence")
            if isinstance(event.get("sequence"), int)
            else None
        )
        if (
            name in {"action_failed", "shadow_failed"}
            or event.get("status") == "outcome_unknown"
        ):
            failed.append(sequence if sequence is not None else -1)
            result.issue(
                "error",
                "session_action_failed",
                f"event={name} status={event.get('status')}",
                path,
                sequence,
            )
        if name == "interaction_state" and event.get("stage") == "blocked":
            result.issue(
                "error",
                "blocked_interaction",
                str(event.get("reason") or "blocked"),
                path,
            )
    result.session_log = {
        "path": str(path),
        "event_count": sum(counts.values()),
        "event_types": dict(sorted(counts.items())),
        "failed_sequences": failed,
        "malformed_lines": malformed,
    }


def _longest_run(values: list[str]) -> int:
    longest = current = 0
    previous = None
    for value in values:
        current = current + 1 if value == previous else 1
        longest = max(longest, current)
        previous = value
    return longest


def _print_summary(result: ValidationResult) -> None:
    counts = Counter(issue.severity for issue in result.issues)
    print(f"{result.verdict} live-run transaction validation")
    for name, report in result.reports.items():
        statuses = json.dumps(report["action_statuses"], sort_keys=True)
        print(
            f"  {name}: status={report['status']} "
            f"actions={report['action_count']} statuses={statuses}"
        )
    if result.session_log:
        print(
            f"  session_log: events={result.session_log['event_count']} "
            f"failed={len(result.session_log['failed_sequences'])} "
            f"malformed={result.session_log['malformed_lines']}"
        )
    print(
        "  coverage.families: "
        + json.dumps(result.coverage.get("families", {}), sort_keys=True)
    )
    print(
        "  coverage.mirror_modes: "
        + json.dumps(result.coverage.get("mirror_modes", {}), sort_keys=True)
    )
    print(f"  issues: errors={counts['error']} warnings={counts['warning']}")
    for issue in result.issues:
        location = f" sequence={issue.sequence}" if issue.sequence is not None else ""
        print(
            f"  [{issue.severity.upper()}] "
            f"{issue.code}{location}: {issue.message}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate visible-client action transactions and lifecycle integrity"
        )
    )
    parser.add_argument("--run-report", type=Path, required=True)
    parser.add_argument("--opening-report", type=Path)
    parser.add_argument("--session-log", type=Path)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--fail-on-warnings", action="store_true")
    args = parser.parse_args()
    result = validate_live_run(
        args.run_report,
        opening_report_path=args.opening_report,
        session_log_path=args.session_log,
    )
    _print_summary(result)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return int(
        result.verdict == "FAIL"
        or (args.fail_on_warnings and result.verdict == "WARN")
    )


if __name__ == "__main__":
    raise SystemExit(main())
