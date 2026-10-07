from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from controller.action_transaction import (
    ActionTransaction, SettlementPhase, client_boundary_type,
    shadow_boundary_type, validate_transaction,
)


@dataclass(frozen=True)
class TransactionResult:
    client_state: dict[str, Any]
    shadow_state: dict[str, Any] | None


def execute_transaction(
    transaction: ActionTransaction,
    *,
    live: Any,
    client_state: dict[str, Any],
    log: Any,
    report: dict[str, Any],
    advance_shadow: Callable[[str, dict[str, Any], Any, dict[str, Any]], dict[str, Any]],
    operation_label: str,
    telemetry: dict[str, Any] | None = None,
) -> TransactionResult:
    """Execute one validated player transaction in client-then-shadow order."""

    if transaction.client is None:
        raise RuntimeError("A live transaction requires a visible-client command")
    # Cheap protocol guard: catch a drifted/malformed pair before touching
    # either endpoint.  State parity remains the responsibility of the normal
    # boundary checks, so this adds no extra polling or restore round-trip.
    validate_transaction(transaction)
    recorded = transaction.to_dict()
    decision_telemetry = dict(transaction.telemetry)
    decision_telemetry.update(telemetry or {})
    decision_telemetry["transaction"] = recorded
    runtime_started = time.perf_counter()
    after = live.execute_transaction(
        transaction,
        expected_client_state=client_state,
        decision_telemetry=decision_telemetry,
    )
    row = report["actions"][-1]
    timeline = row.setdefault('transaction_timeline', {})
    timeline['client_settled_ms'] = round(
        (time.perf_counter() - runtime_started) * 1000.0, 3
    )
    row["transaction"] = recorded
    row["flow_managed"] = True
    row['logical_action_id'] = row.get('parent_sequence') or row.get('sequence')
    native_evidence = row.get('completion_evidence') in {
        'native_game_action_and_queue', 'native_player_choice_pause'}
    if native_evidence:
        row['settlement_phase'] = (SettlementPhase.AWAITING_INPUT.value
                                   if row.get('native_action_phase') == 'awaiting_input'
                                   else SettlementPhase.CLIENT_SETTLED.value)
    row["shadow_action_applied"] = False if transaction.shadow is not None else None
    awaiting_input = row.get('native_action_phase') == 'awaiting_input'
    row["client_completed"] = not awaiting_input
    row["shadow_completed"] = False if transaction.shadow is not None else None
    row["boundary_verified"] = False
    row["transaction_status"] = (
        'awaiting_input' if awaiting_input else
        "shadow_pending" if transaction.shadow is not None else "client_only_completed"
    )
    shadow_state = None
    if transaction.shadow is not None:
        row.update(
            headless_action=transaction.shadow.action,
            headless_args=dict(transaction.shadow.params),
        )
        shadow_started = time.perf_counter()
        timeline['shadow_started_ms'] = round(
            (shadow_started - runtime_started) * 1000.0, 3
        )
        try:
            shadow_state = advance_shadow(
                transaction.shadow.action,
                dict(transaction.shadow.params),
                log,
                report,
            )
        except Exception as exc:
            timeline['shadow_finished_ms'] = round(
                (time.perf_counter() - runtime_started) * 1000.0, 3
            )
            timeline['shadow_elapsed_ms'] = round(
                (time.perf_counter() - shadow_started) * 1000.0, 3
            )
            row["transaction_status"] = "shadow_failed"
            row["shadow_error"] = str(exc)
            if log is not None:
                log.write(
                    {
                        "event": "shadow_failed",
                        "sequence": row.get("sequence"),
                        "action": transaction.shadow.action,
                        "error": str(exc),
                    }
                )
            raise
        timeline['shadow_finished_ms'] = round(
            (time.perf_counter() - runtime_started) * 1000.0, 3
        )
        timeline['shadow_elapsed_ms'] = round(
            (time.perf_counter() - shadow_started) * 1000.0, 3
        )
        if shadow_state.get("type") == "error":
            error = repr(shadow_state)
            row["transaction_status"] = "shadow_failed"
            row["shadow_error"] = error
            if log is not None:
                log.write(
                    {
                        "event": "shadow_failed",
                        "sequence": row.get("sequence"),
                        "action": transaction.shadow.action,
                        "error": error,
                    }
                )
            raise RuntimeError(f"Headless failed to {operation_label}: {shadow_state}")
        row["shadow_action_applied"] = True
        shadow_awaiting = shadow_state.get('decision') == 'card_select' and awaiting_input
        if awaiting_input and not shadow_awaiting:
            raise RuntimeError('Client awaits card input but Headless did not reach card_select')
        row["shadow_completed"] = not shadow_awaiting
        if native_evidence:
            client_boundary = client_boundary_type(after)
            shadow_boundary = shadow_boundary_type(shadow_state)
            row['client_boundary_type'] = client_boundary.value if client_boundary else None
            row['shadow_boundary_type'] = shadow_boundary.value if shadow_boundary else None
            if client_boundary is None or client_boundary != shadow_boundary:
                row['settlement_phase'] = SettlementPhase.DIVERGED.value
                raise RuntimeError('Native action client and shadow boundaries differ')
            if not shadow_awaiting:
                row['settlement_phase'] = SettlementPhase.BOTH_SETTLED.value
        row["transaction_status"] = ('both_awaiting_input' if shadow_awaiting
                                     else 'shadow_completed_awaiting_checkpoint')
        if log is not None:
            event = {
                "event": "shadow_applied",
                "sequence": row.get("sequence"),
                "action": transaction.shadow.action,
                "transaction_status": row['transaction_status'],
                "shadow_completed": row['shadow_completed'],
            }
            if shadow_awaiting:
                event['transaction_record'] = dict(row)
            log.write(event)
    return TransactionResult(after, shadow_state)
