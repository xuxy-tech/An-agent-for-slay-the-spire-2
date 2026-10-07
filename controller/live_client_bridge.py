from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from cli.sts2_mod_adapter import ModApiError, Sts2ModAdapter
from controller.action_transaction import (
    ActionTransaction,
    client_boundary_type,
    make_mirrored_transaction,
    make_transaction,
    transaction_from_dict,
    validate_transaction,
)
from controller.engine_parity import is_card_reward_selection, client_living_enemies


class StaleClientStateError(ModApiError):
    """The visible state changed after planning and no action was submitted."""

    def __init__(self, message: str, observed_state: Dict[str, Any]):
        super().__init__(message)
        self.observed_state = observed_state


@dataclass(frozen=True)
class LiveAction:
    name: str
    params: Dict[str, Any]


def _transaction_budget_ms(metadata: Dict[str, Any], default_timeout_s: float,
                           action: str | None = None,
                           params: Dict[str, Any] | None = None) -> float:
    """Return one wall-clock budget for the whole client transaction.

    Older callers do not provide a budget, so their behavior remains bounded by
    the Mod timeout. New callers may provide ``timing_budget_ms`` in telemetry;
    all submit, settlement, and native lifecycle waits then share that deadline.
    """

    telemetry = metadata.get('decision_telemetry') or {}
    candidate = telemetry.get('timing_budget_ms')
    try:
        value = float(candidate) if candidate is not None else float(default_timeout_s) * 1000.0
    except (TypeError, ValueError):
        value = float(default_timeout_s) * 1000.0
    # A multi-card selection is one logical decision but several serialized
    # visible clicks. Each click can spend the normal client action interval;
    # budget the transaction for the declared count instead of timing out
    # halfway through a valid selection. Other actions retain the ordinary
    # per-transaction bound.
    if action == 'select_deck_cards' and not _has_explicit_timing_budget(metadata):
        count = len((params or {}).get('indices') or [])
        if count > 1:
            value += min(count - 1, 8) * float(default_timeout_s) * 1000.0 / 2.0
    # Room transitions may include asset/event initialization after the click
    # is accepted. Give those boundaries a larger adaptive window while
    # keeping combat actions on the normal deadline.
    if (action in {
            'choose_map_node', 'choose_rest_option',
            'choose_shop_action', 'claim_reward', 'choose_reward_card',
            'skip_reward_cards', 'collect_rewards_and_proceed',
        } and not _has_explicit_timing_budget(metadata)):
        value = max(value, 60000.0)
    # Event pages can load their native transition and dynamic variables after
    # the click has been accepted. Keep this longer window limited to event
    # choices so ordinary room actions retain the 60-second bound.
    if action == 'choose_event_option' and not _has_explicit_timing_budget(metadata):
        value = max(value, 120000.0)
    if not math.isfinite(value) or value <= 0:
        raise ModApiError('Transaction timing budget must be positive')
    return value


def _has_explicit_timing_budget(metadata: Dict[str, Any]) -> bool:
    telemetry = metadata.get('decision_telemetry') or {}
    return telemetry.get('timing_budget_ms') is not None


def _remaining_timeout_s(deadline: Optional[float], fallback_s: float) -> float:
    if deadline is None:
        return max(0.01, float(fallback_s))
    return max(0.0, deadline - time.monotonic())


def _read_native_lifecycle(reader: Callable[..., Dict[str, Any]],
                            deadline: Optional[float], fallback_s: float) -> Dict[str, Any]:
    """Read native progress under the transaction deadline."""
    return _read_with_deadline(reader, deadline, fallback_s, 'native lifecycle')


def _read_with_deadline(reader: Callable[..., Dict[str, Any]],
                        deadline: Optional[float], fallback_s: float,
                        description: str) -> Dict[str, Any]:
    remaining = _remaining_timeout_s(deadline, fallback_s)
    if remaining <= 0:
        raise ModApiError(f'Transaction deadline expired before {description}')
    try:
        return reader(timeout_s=remaining)
    except TypeError as exc:
        if 'timeout_s' not in str(exc):
            raise
        return reader()


def client_state_digest(state: Dict[str, Any]) -> str:
    """Hash one complete client observation for trace integrity.

    This is deliberately labelled a *client* digest.  It is not yet a parity
    digest because the headless runtime uses a different JSON schema.
    """

    raw = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _lifecycle_stamp(snapshot: Dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    return (snapshot.get('epoch'), snapshot.get('revision'),
            snapshot.get('next_action_id'), snapshot.get('queue_empty'))


_NATIVE_EVIDENCE_ACTIONS = frozenset({
    'play_card', 'use_potion', 'discard_potion', 'end_turn',
    'select_deck_card', 'select_deck_cards',
})


class JsonlSessionLog:
    def __init__(self, path: Path, *, compact_lifecycle: bool = False):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.on_event: Optional[Callable[[Dict[str, Any]], None]] = None
        self.compact_lifecycle = compact_lifecycle

    def write(self, event: Dict[str, Any]) -> None:
        row = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            **event,
        }
        durable = row
        if self.compact_lifecycle and row.get('event') in {'action_pending', 'action_started'}:
            before = row.get('client_before') or {}
            durable = {
                key: row.get(key) for key in (
                    'timestamp_utc', 'event', 'sequence', 'status', 'client_action',
                    'client_params', 'headless_action', 'headless_args',
                    'screen_before', 'segment_id', 'verification',
                ) if key in row
            }
            durable['floor'] = (before.get('run') or {}).get('floor')
            durable['turn'] = before.get('turn')
        started = time.perf_counter()
        line = json.dumps(durable, ensure_ascii=False, sort_keys=True) + "\n"
        serialize_ms = round((time.perf_counter() - started) * 1000, 3)
        started = time.perf_counter()
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(line)
        timing = event.get('timing_breakdown')
        if isinstance(timing, dict):
            timing['session_log_serialize_ms'] = serialize_ms
            timing['session_log_append_ms'] = round((time.perf_counter() - started) * 1000, 3)
        if self.on_event is not None:
            self.on_event(row)


class LiveClientBridge:
    """Translate headless-agent actions to actions on the visible client."""

    def __init__(
        self,
        mod: Sts2ModAdapter,
        log: Optional[JsonlSessionLog] = None,
        visible_delay_ms: float = 0.0,
        rng_audit_required: bool = False,
    ):
        self.mod = mod
        self.log = log
        self.visible_delay_ms = max(0.0, float(visible_delay_ms))
        self.sequence = 0
        self.before_action: Optional[Callable[[Dict[str, Any]], None]] = None
        self.last_action: Dict[str, Any] = {}
        self.on_wait: Optional[Callable[[Dict[str, Any]], None]] = None
        self.verify_checkpoints = True
        self.rng_audit_required = bool(rng_audit_required)
        self.pending_native_parent: Optional[Dict[str, Any]] = None

    def observe(self) -> Dict[str, Any]:
        state = self.mod.state()
        if self.log is not None:
            self.log.write(
                {
                    "event": "client_observation",
                    "screen": state.get("screen"),
                    "run_id": state.get("run_id"),
                    "client_digest": client_state_digest(state),
                    "transport_ms": round(self.mod.last_call_ms, 3),
                }
            )
        return state

    def execute_headless_action(
        self,
        decision: str,
        action: str,
        args: Optional[Dict[str, Any]] = None,
        expected_client_state: Optional[Dict[str, Any]] = None,
        decision_telemetry: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # The Mod adapter may reuse and mutate its state dictionary after an
        # action is submitted. Keep an immutable transaction snapshot so
        # boundary detection compares the actual before and after states.
        before = (copy.deepcopy(expected_client_state)
                  if expected_client_state is not None
                  else copy.deepcopy(self.mod.state()))
        transaction = self.prepare_headless_transaction(
            decision,
            action,
            args or {},
            before,
            decision_telemetry or {},
        )
        return self.execute_transaction(
            transaction,
            expected_client_state=before,
            decision_telemetry=decision_telemetry,
            decision=decision,
        )

    def prepare_headless_transaction(
        self,
        decision: str,
        action: str,
        args: Dict[str, Any],
        before: Dict[str, Any],
        decision_telemetry: Dict[str, Any],
    ) -> ActionTransaction:
        translation_args = dict(args or {})
        if action in {'use_potion', 'discard_potion'}:
            chosen = decision_telemetry.get('chosen') or {}
            metadata = chosen.get('metadata') or {}
            potion_id = metadata.get('potion_id')
            if potion_id:
                translation_args['potion_id'] = potion_id
            if metadata.get('target_type'):
                translation_args['target_type'] = metadata['target_type']
        translated = translate_headless_action(decision, action, translation_args, before)
        if action == 'play_card':
            chosen = decision_telemetry.get('chosen') or {}
            card_id = str((chosen.get('metadata') or {}).get('card_id') or '').split('.')[-1].upper()
            if card_id:
                cards = (before.get('combat') or {}).get('hand') or []
                expected = translated.params.get('card_index')
                matching = [c for c in cards if str(c.get('card_id') or '').split('.')[-1].upper() == card_id]
                same_index = [c for c in matching if c.get('index') == expected]
                if not same_index:
                    if len(matching) != 1 or type(matching[0].get('index')) is not int:
                        raise ModApiError(f'Chosen card {card_id} has no unique visible counterpart')
                    translated.params['card_index'] = matching[0]['index']
        return make_mirrored_transaction(
            translated.name,
            translated.params,
            shadow_action=action,
            shadow_args=args or {},
            completion='decision_boundary',
            telemetry=decision_telemetry,
        )

    def execute_transaction(
        self,
        transaction: ActionTransaction,
        expected_client_state: Optional[Dict[str, Any]] = None,
        decision_telemetry: Optional[Dict[str, Any]] = None,
        decision: Optional[str] = None,
        segment_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        validate_transaction(transaction)
        if transaction.client is None:
            raise ModApiError('Visible-client execution requires a client command')
        before = (copy.deepcopy(expected_client_state)
                  if expected_client_state is not None
                  else copy.deepcopy(self.mod.state()))
        mirrored = transaction.shadow is not None
        metadata = {
            "decision": decision or transaction.intent,
            "decision_telemetry": decision_telemetry or transaction.telemetry,
            "transaction": transaction.to_dict(),
            "segment_id": segment_id,
            "verification": (
                "PENDING" if mirrored and self.verify_checkpoints
                else "CLIENT_ONLY_UNVERIFIED" if self.verify_checkpoints
                else "NOT_AUDITED"
            ),
        }
        if transaction.shadow is not None:
            metadata.update(
                headless_action=transaction.shadow.action,
                headless_args=dict(transaction.shadow.params),
            )
        return self._execute(
            transaction.client.action,
            dict(transaction.client.params),
            before,
            metadata,
            "live_action" if mirrored else "client_only_action",
        )

    def execute_client_action(
        self,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        expected_client_state: Optional[Dict[str, Any]] = None,
        decision_telemetry: Optional[Dict[str, Any]] = None,
        segment_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Execute an audited client-only action inside a re-anchor segment."""

        transaction_data = (decision_telemetry or {}).get('transaction')
        if transaction_data is None:
            transaction = make_transaction(
                action,
                params or {},
                completion='decision_boundary',
                telemetry=decision_telemetry or {},
            )
        else:
            transaction = transaction_from_dict(transaction_data)
        return self.execute_transaction(
            transaction,
            expected_client_state=expected_client_state,
            decision_telemetry=decision_telemetry,
            segment_id=segment_id,
        )

    def _capture_rng(self, *, deadline: Optional[float] = None) -> Optional[Dict[str, Any]]:
        capture = getattr(self.mod, "rng_snapshot", None)
        if capture is None:
            if self.rng_audit_required:
                raise ModApiError("Visible RNG bridge is unavailable")
            return None
        try:
            return _read_with_deadline(capture, deadline, self.mod.config.timeout_s,
                                       'RNG observation')
        except Exception:
            if self.rng_audit_required:
                raise
            return None

    def _execute(self, action, params, before, metadata, completed_event):
        self.sequence += 1
        transaction_started = time.perf_counter()
        timing_budget_ms = _transaction_budget_ms(
            metadata, self.mod.config.timeout_s, action=action, params=params)
        transaction_deadline = None
        record = {
            **metadata, "sequence": self.sequence, "status": "pending",
            "event": "action_pending", "client_action": action,
            "client_params": params, "screen_before": before.get("screen"),
            "client_before": before,
        }
        from controller.interaction_state import classify_interaction
        record['interaction_before'] = classify_interaction(before).to_dict()
        record['timing_breakdown'] = {}
        record['timing_breakdown']['transaction_budget_ms'] = round(timing_budget_ms, 3)
        record['transaction_budget_explicit'] = _has_explicit_timing_budget(metadata)
        phase_started = time.perf_counter()
        self._record_action(record)
        record['timing_breakdown']['client_pending_record_ms'] = round(
            (time.perf_counter() - phase_started) * 1000, 3)
        try:
            if self.before_action is not None:
                phase_started = time.perf_counter()
                try:
                    self.before_action(record)
                finally:
                    record['timing_breakdown']['client_action_gate_ms'] = round(
                        (time.perf_counter() - phase_started) * 1000, 3)
            transaction_deadline = time.monotonic() + timing_budget_ms / 1000.0
            phase_started = time.perf_counter()
            try:
                lifecycle_reader = getattr(self.mod, 'action_lifecycle', None)
                native_preflight = (_read_native_lifecycle(
                    lifecycle_reader, transaction_deadline, self.mod.config.timeout_s)
                    if action in _NATIVE_EVIDENCE_ACTIONS and callable(lifecycle_reader)
                    else None)
                observed = _read_with_deadline(
                    self.mod.state, transaction_deadline, self.mod.config.timeout_s,
                    'preflight state')
            finally:
                record['timing_breakdown']['client_preflight_state_ms'] = round(
                    (time.perf_counter() - phase_started) * 1000, 3)
            phase_started = time.perf_counter()
            if gameplay_observation(observed) != gameplay_observation(before):
                # A client transition can complete while the preflight state is
                # being read.  Confirm the mismatch once before cancelling: a
                # transient read is safe to discard, while a stable transition
                # remains fail-closed.
                retry_started = time.perf_counter()
                retry_observed = _read_with_deadline(
                    self.mod.state, transaction_deadline, self.mod.config.timeout_s,
                    'stale-state confirmation')
                record['timing_breakdown']['client_stale_retry_state_ms'] = round(
                    (time.perf_counter() - retry_started) * 1000, 3)
                record['stale_observation_retry'] = True
                if gameplay_observation(retry_observed) == gameplay_observation(before):
                    observed = retry_observed
                    record['stale_observation_transient'] = True
                elif _can_rebase_stale_action(action, params, before, retry_observed):
                    observed = retry_observed
                    before = observed
                    record['client_before'] = observed
                    record['screen_before'] = observed.get('screen')
                    record['interaction_before'] = classify_interaction(observed).to_dict()
                    record['stale_observation_rebased'] = True
                    self._record_action(record)
                else:
                    record['stale_observation_stable'] = True
                    raise StaleClientStateError(
                        'Client changed while deciding or paused; stale action cancelled. Re-plan required.',
                        retry_observed,
                    )
            native_action = 'select_deck_card' if action == 'select_deck_cards' else action
            if native_action not in set(observed.get('available_actions') or []):
                raise ModApiError(f'Client action {action!r} is unavailable')
            record['timing_breakdown']['client_preflight_check_ms'] = round(
                (time.perf_counter() - phase_started) * 1000, 3)
            rng_started = time.perf_counter()
            client_rng_before = self._capture_rng(deadline=transaction_deadline)
            record['timing_breakdown']['client_rng_before_ms'] = round(
                (time.perf_counter() - rng_started) * 1000.0, 3
            )
            if client_rng_before is not None:
                record["client_rng_before"] = client_rng_before
            if native_preflight is not None:
                native_after_rng = _read_native_lifecycle(
                    lifecycle_reader, transaction_deadline, self.mod.config.timeout_s)
                if (action == 'play_card'
                        and _lifecycle_stamp(native_preflight) != _lifecycle_stamp(native_after_rng)):
                    raise StaleClientStateError(
                        'Native action changed while sampling pre-action state and RNG',
                        _read_with_deadline(self.mod.state, transaction_deadline,
                                            self.mod.config.timeout_s, 'stale-state evidence'),
                    )
                record['native_preflight_stamp'] = _lifecycle_stamp(native_after_rng)
            record.update(event="action_started", status="executing")
            phase_started = time.perf_counter()
            self._record_action(record)
            record['timing_breakdown']['client_started_record_ms'] = round(
                (time.perf_counter() - phase_started) * 1000, 3)
            after = self._send_and_settle(
                action, params, before, record, deadline=transaction_deadline
            )
            if native_preflight is not None:
                native_postflight = _read_native_lifecycle(
                    lifecycle_reader, transaction_deadline, self.mod.config.timeout_s)
                record['native_postflight_stamp'] = _lifecycle_stamp(native_postflight)
                record['native_lifecycle'] = {
                    'epoch': native_postflight.get('epoch'),
                    'revision_before': native_preflight.get('revision'),
                    'revision_after': native_postflight.get('revision'),
                    'queue_empty': native_postflight.get('queue_empty'),
                    'matching_actions': [
                        {
                            key: item.get(key)
                            for key in ('id', 'semantic_action', 'card_id', 'status', 'pause_type')
                            if item.get(key) is not None
                        }
                        for item in native_postflight.get('actions', [])
                        if isinstance(item, dict)
                    ],
                }
            rng_started = time.perf_counter()
            client_rng_after = record.get('client_rng_after')
            if client_rng_after is None:
                client_rng_after = self._capture_rng(deadline=transaction_deadline)
            record['timing_breakdown']['client_rng_after_ms'] = round(
                (time.perf_counter() - rng_started) * 1000.0, 3
            )
            if client_rng_after is not None:
                record["client_rng_after"] = client_rng_after
                if client_rng_before is not None:
                    from controller.rng_parity import rng_counter_delta
                    record["client_rng_delta"] = rng_counter_delta(client_rng_before, client_rng_after)
                else:
                    # Client-only setup actions can create the RunState itself
                    # (notably CHARACTER_SELECT -> embark). There is no valid
                    # pre-action RNG state to diff; the first complete snapshot
                    # is the authoritative baseline for the new run.
                    record["client_rng_transition"] = "baseline_created"
            awaiting_input = record.get('native_action_phase') == 'awaiting_input'
            if record.get('completion_evidence') in {
                    'native_player_choice_pause', 'native_game_action_and_queue'}:
                boundary = client_boundary_type(after)
                if boundary is None:
                    raise ModApiError('Native action has no recognized client decision boundary')
                record['client_boundary_type'] = boundary.value if boundary else None
            record.update(event='action_awaiting_input' if awaiting_input else completed_event,
                          status='awaiting_input' if awaiting_input else 'completed', client_after=after,
                          screen_after=after.get("screen"), interaction_after=classify_interaction(after).to_dict())
            phase_started = time.perf_counter()
            self._record_action(record)
            record['timing_breakdown']['client_completed_record_ms'] = round(
                (time.perf_counter() - phase_started) * 1000, 3)
            return after
        except Exception as exc:
            submitted = record.get('input_submitted') is True
            record.update(event="action_failed", status="outcome_unknown" if submitted else "cancelled",
                          error=str(exc), verification="UNVERIFIED" if submitted else "NOT_EXECUTED")
            if submitted:
                record['settlement_phase'] = 'UNKNOWN'
            phase_started = time.perf_counter()
            self._record_action(record)
            record['timing_breakdown']['client_failed_record_ms'] = round(
                (time.perf_counter() - phase_started) * 1000, 3)
            raise
        finally:
            record['timing_breakdown']['client_transaction_ms'] = round(
                (time.perf_counter() - transaction_started) * 1000.0, 3
            )

    def _record_action(self, record):
        self.last_action = dict(record)
        if self.log is not None:
            self.log.write(record)

    def _send_and_settle(self, action, params, before, record, *, deadline=None):
        transaction_started = time.perf_counter()
        timing = record.setdefault('timing_breakdown', {})
        available = set(before.get("available_actions") or [])
        native_action = 'select_deck_card' if action == 'select_deck_cards' else action
        if native_action not in available:
            raise ModApiError(
                f"Client action {action!r} is unavailable on screen "
                f"{before.get('screen')!r}; available={sorted(available)!r}"
            )
        lifecycle_reader = getattr(self.mod, 'action_lifecycle', None)
        native_ticket = None
        if action == 'play_card' and callable(lifecycle_reader):
            native_ticket = _read_native_lifecycle(
                lifecycle_reader, deadline, self.mod.config.timeout_s)
            if record.get('native_preflight_stamp') != _lifecycle_stamp(native_ticket):
                raise StaleClientStateError(
                    'Native action changed before play_card submission',
                    _read_with_deadline(self.mod.state, deadline, self.mod.config.timeout_s,
                                        'stale native-action evidence'))
            if native_ticket.get('queue_empty') is not True:
                raise ModApiError('Native action queue was not empty before play_card submission')
            card_index = params.get('card_index')
            cards = [card for card in (before.get('combat') or {}).get('hand') or []
                     if card.get('index') == card_index]
            if len(cards) != 1 or not cards[0].get('card_id'):
                raise ModApiError('Native play_card ticket has no unique visible card')
            native_ticket['expected_card_id'] = cards[0]['card_id']
            record['native_start_revision'] = native_ticket['revision']
            record['settlement_phase'] = 'PREPARED'
        elif action in {'select_deck_card', 'select_deck_cards'} and self.pending_native_parent is not None:
            if not callable(lifecycle_reader):
                raise ModApiError('Native action lifecycle disappeared during a pending choice')
            native_ticket = self.pending_native_parent
            record['settlement_phase'] = 'RESUMING'
        started = time.perf_counter()
        submit = getattr(self.mod, 'submit_action', self.mod.action)
        remaining = _remaining_timeout_s(deadline, self.mod.config.timeout_s)
        if remaining <= 0:
            raise ModApiError(f"Transaction deadline expired before submitting {action!r}")
        if action == 'select_deck_cards':
            response = self._send_selection(params, before, record, deadline=deadline)
        else:
            record['input_submitted'] = True
            if native_ticket is not None and action == 'play_card':
                record['settlement_phase'] = 'SUBMITTED'
            if record.get('transaction_budget_explicit') or isinstance(self.mod, Sts2ModAdapter):
                response = submit(action, timeout_s=remaining, **(params or {}))
            else:
                response = submit(action, **(params or {}))
        action_wall_ms = (time.perf_counter() - started) * 1000.0
        timing['client_submit_ms'] = round(action_wall_ms, 3)
        request_id = self.mod.last_request_id
        record.update(action_wall_ms=round(action_wall_ms, 3), request_id=request_id)
        response_state = response["state"]
        settle_started = time.perf_counter()
        state_started = time.perf_counter()
        settle_initial_state = _read_with_deadline(
            self.mod.state, deadline, self.mod.config.timeout_s, 'initial settlement state')
        timing['client_settle_initial_state_ms'] = round(
            (time.perf_counter() - state_started) * 1000.0, 3
        )
        wait_started = time.perf_counter()
        try:
            if native_ticket is not None:
                after = self._wait_for_native_action(
                    action, before, native_ticket, lifecycle_reader, record,
                    deadline=deadline)
            else:
                after = self._wait_for_decision_boundary(
                    action, before, settle_initial_state, deadline=deadline)
                record['completion_evidence'] = 'legacy_ui_boundary'
                if action == 'choose_map_node' and after.get('screen') in {'EVENT', 'SHOP'}:
                    # Room initialization can finish just after the visible
                    # decision boundary. Take a short stable observation
                    # window before recording client RNG, so mirrored room
                    # generation is compared against the settled frame.
                    stable = None
                    for _ in range(4):
                        time.sleep(0.05)
                        candidate = _read_with_deadline(
                            self.mod.state, deadline, self.mod.config.timeout_s,
                            'room initialization state')
                        observation = gameplay_observation(candidate)
                        if observation == stable:
                            after = candidate
                            break
                        stable = observation
                        after = candidate
                    settled_rng = self._capture_rng(deadline=deadline)
                    if settled_rng is not None:
                        record['client_rng_after'] = settled_rng
        finally:
            timing['client_settle_wait_ms'] = round(
                (time.perf_counter() - wait_started) * 1000.0, 3
            )
        settle_ms = (time.perf_counter() - settle_started) * 1000.0
        timing['client_settle_total_ms'] = round(settle_ms, 3)
        timing['client_action_transaction_ms'] = round(
            (time.perf_counter() - transaction_started) * 1000.0, 3
        )
        record.update(action_wall_ms=round(action_wall_ms, 3), settle_ms=round(settle_ms, 3),
                      request_id=request_id,
                      client_digest_before=client_state_digest(before),
                      client_digest_after=client_state_digest(after))
        return after

    def _wait_for_native_action(self, action, before, ticket, lifecycle_reader, record, *, deadline=None):
        """Wait for the same native GameAction across its choice suspension."""
        deadline = deadline if deadline is not None else time.monotonic() + self.mod.config.timeout_s
        expected_epoch = ticket['epoch']
        parent_id = ticket.get('id')
        first_id = ticket.get('next_action_id')
        progress = None
        while True:
            try:
                progress = _read_native_lifecycle(
                    lifecycle_reader, deadline, self.mod.config.timeout_s)
            except ModApiError as exc:
                if time.monotonic() < deadline:
                    raise
                record['latest_native_progress'] = progress
                raise ModApiError(f'Native play_card completion is unknown after {action!r}; '
                                  f'action_id={parent_id!r}, progress={progress!r}') from exc
            if progress['epoch'] != expected_epoch:
                raise ModApiError('Native action queue changed while an action was pending')
            matches = [item for item in progress['actions']
                       if item.get('semantic_action') == 'play_card'
                       and type(item.get('id')) is int
                       and (item['id'] == parent_id if parent_id is not None
                            else item['id'] >= first_id)]
            if len(matches) > 1:
                raise ModApiError('More than one native play_card matches this input')
            if matches:
                native = matches[0]
                expected_card_id = ticket.get('expected_card_id')
                if expected_card_id is not None and native.get('card_id') != expected_card_id:
                    raise ModApiError('Native play_card identity differs from selected visible card')
                parent_id = native['id']
                status = native.get('status')
                if status not in {'running', 'awaiting_input', 'completed', 'failed'}:
                    raise ModApiError(f'Invalid native action lifecycle status: {native!r}')
                phase = ('completed' if status == 'completed'
                         and progress['queue_empty'] is True else status
                         if status != 'completed' else 'running')
                if phase == 'failed':
                    raise ModApiError(f'Native play_card {parent_id} failed: {native!r}')
                if phase == 'awaiting_input' and native.get('pause_type') != 'player_choice':
                    raise ModApiError('Native play_card paused for an unsupported input type')
                if phase in {'awaiting_input', 'completed'} and not (
                        action in {'select_deck_card', 'select_deck_cards'}
                        and phase == 'awaiting_input'):
                    state = _read_with_deadline(
                        self.mod.state, deadline, self.mod.config.timeout_s,
                        'native settlement state')
                    rng = self._capture_rng(deadline=deadline)
                    fence = _read_native_lifecycle(
                        lifecycle_reader, deadline, self.mod.config.timeout_s)
                    if (fence['epoch'] == progress['epoch']
                            and fence['revision'] == progress['revision']
                            and fence['queue_empty'] == progress['queue_empty']):
                        if phase == 'awaiting_input':
                            ready = (state.get('screen') == 'CARD_SELECTION'
                                     and 'select_deck_card' in (state.get('available_actions') or []))
                        else:
                            ready = _is_decision_boundary(action, before, state)
                        if ready:
                            record.update(native_action_id=parent_id,
                                          native_action_epoch=expected_epoch,
                                          native_action_phase=phase,
                                          completion_evidence='native_game_action_and_queue'
                                          if phase == 'completed' else 'native_player_choice_pause',
                                          sample_revision=progress['revision'])
                            record['native_end_revision'] = progress['revision']
                            if ticket.get('parent_sequence') is not None:
                                record['parent_sequence'] = ticket['parent_sequence']
                            if rng is not None:
                                record['client_rng_after'] = rng
                            if phase == 'awaiting_input':
                                self.pending_native_parent = {
                                    'epoch': expected_epoch, 'id': parent_id,
                                    'parent_sequence': record['sequence'],
                                    'expected_card_id': expected_card_id,
                                }
                            else:
                                self.pending_native_parent = None
                            return state
            # A completed selection can remove the parent action from the
            # native queue before the next lifecycle poll.  Queue exhaustion
            # plus a verified visible boundary is sufficient evidence; the
            # action id's disappearance is not itself a protocol failure.
            if (not matches and progress.get('queue_empty') is True
                    and parent_id is not None
                    and progress.get('next_action_id', 0) > parent_id):
                state = _read_with_deadline(
                    self.mod.state, deadline, self.mod.config.timeout_s,
                    'native settlement state')
                if _is_decision_boundary(action, before, state):
                    record.update(native_action_id=parent_id,
                                  native_action_epoch=expected_epoch,
                                  native_action_phase='completed',
                                  completion_evidence='native_queue_exhausted_boundary',
                                  sample_revision=progress['revision'])
                    record['native_end_revision'] = progress['revision']
                    self.pending_native_parent = None
                    return state
            if time.monotonic() >= deadline:
                record['latest_native_progress'] = progress
                raise ModApiError(f'Native play_card completion is unknown after {action!r}; '
                                  f'action_id={parent_id!r}, progress={progress!r}')
            if self.on_wait is not None:
                self.on_wait({'phase': 'waiting_native_action', 'action_id': parent_id})
            time.sleep(0.05)

    def pace_after_verification(
        self,
        *,
        verification: str,
        record: Optional[Dict[str, Any]] = None,
    ) -> float:
        """Apply the operator-controlled viewing interval after a verified transaction.

        This method deliberately has no state, engine, settlement, or parity work.
        Callers invoke it only after all required client/shadow checks succeed.
        """

        if not verification or not str(verification).strip():
            raise ValueError('Visible pacing requires a completed verification label')
        target_ms = self.visible_delay_ms
        started = time.perf_counter()
        if target_ms:
            time.sleep(target_ms / 1000.0)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        target = record if record is not None else self.last_action
        if isinstance(target, dict):
            timing = target.setdefault('timing_breakdown', {})
            timing['visible_pacing_target_ms'] = round(target_ms, 3)
            timing['visible_pacing_ms'] = round(elapsed_ms, 3)
            target['visible_delay_ms'] = round(elapsed_ms, 3)
            target['pacing_status'] = 'completed'
            target['pacing_after_verification'] = str(verification)
        if self.last_action:
            last_timing = self.last_action.setdefault('timing_breakdown', {})
            last_timing['visible_pacing_target_ms'] = round(target_ms, 3)
            last_timing['visible_pacing_ms'] = round(elapsed_ms, 3)
            self.last_action['visible_delay_ms'] = round(elapsed_ms, 3)
            self.last_action['pacing_status'] = 'completed'
            self.last_action['pacing_after_verification'] = str(verification)
        if self.log is not None:
            self.log.write({
                'event': 'visible_pacing',
                'sequence': (target or {}).get('sequence'),
                'verification': str(verification),
                'target_ms': round(target_ms, 3),
                'elapsed_ms': round(elapsed_ms, 3),
            })
        return elapsed_ms

    def _send_selection(self, params, before, record, *, deadline=None):
        indices = params.get('indices') or []
        if len(indices) < 2 or len(set(indices)) != len(indices):
            raise ModApiError('Multi-card selection requires distinct indices')
        selection = before.get('selection') or {}
        from controller.interaction_state import classify_interaction
        spec = classify_interaction(before).selection
        if spec and spec.minimum is not None and len(indices) < spec.minimum:
            raise ModApiError('Multi-card plan is shorter than the mandatory selection count')
        if spec and spec.maximum is not None and len(indices) > spec.maximum:
            raise ModApiError('Multi-card plan exceeds the selection maximum')
        signature = [(card.get('index'), card.get('card_id')) for card in selection.get('cards') or []]
        visible = {index for index, _ in signature}
        if not set(indices).issubset(visible):
            raise ModApiError('Multi-card indices are not visible')
        for offset, index in enumerate(indices):
            final = offset == len(indices) - 1
            remaining = _remaining_timeout_s(deadline, self.mod.config.timeout_s)
            if remaining <= 0:
                raise ModApiError('Transaction deadline expired during multi-card selection')
            kwargs = {
                'option_index': index,
                'allow_pending_selection': not final,
            }
            if record.get('transaction_budget_explicit') or isinstance(self.mod, Sts2ModAdapter):
                kwargs['timeout_s'] = remaining
            record['input_submitted'] = True
            response = self.mod.action('select_deck_card', **kwargs)
            state = response['state']
            if self.log:
                self.log.write({'event': 'selection_subaction', 'sequence': self.sequence,
                                'option_index': index, 'ordinal': offset + 1,
                                'status': response.get('status'), 'state': state})
            if not final:
                current = state.get('selection') or {}
                if (state.get('run_id') != before.get('run_id') or current.get('kind') != selection.get('kind')
                    or [(c.get('index'), c.get('card_id')) for c in current.get('cards') or []] != signature):
                    raise ModApiError('Selection changed before all planned cards were clicked')
        return response

    def wait_for_state(
        self,
        predicate: Callable[[Dict[str, Any]], bool],
        description: str,
        initial_state: Optional[Dict[str, Any]] = None,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Wait for an explicitly described, observable client boundary."""

        deadline = time.monotonic() + (
            self.mod.config.timeout_s if timeout_s is None else float(timeout_s)
        )
        state = initial_state or _read_with_deadline(
            self.mod.state, deadline, self.mod.config.timeout_s, 'initial decision state')
        while True:
            if predicate(state):
                if self.log is not None:
                    self.log.write(
                        {
                            "event": "client_settled",
                            "description": description,
                            "screen": state.get("screen"),
                            "turn": state.get("turn"),
                            "client_digest": client_state_digest(state),
                        }
                    )
                return state
            if time.monotonic() >= deadline:
                raise ModApiError(
                    f"Client did not reach {description}; "
                    f"screen={state.get('screen')!r}, turn={state.get('turn')!r}, "
                    f"actions={state.get('available_actions')!r}"
                )
            if self.on_wait is not None:
                self.on_wait({'phase': 'waiting', 'waiting_for': description})
            time.sleep(0.05)
            state = _read_with_deadline(
                self.mod.state, deadline, self.mod.config.timeout_s,
                'decision-boundary state')

    def reveal_card_reward(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Open the card reward without selecting a card or advancing the shadow."""
        reward = state.get('reward') or {}
        if state.get('screen') not in {'REWARD', 'CARD_SELECTION'}:
            raise ModApiError('Card reward can only be opened from REWARD')
        if reward.get('pending_card_choice') is not True:
            entries = [row for row in reward.get('rewards') or []
                       if row.get('reward_type') == 'Card' and row.get('claimable') is True]
            if len(entries) != 1 or type(entries[0].get('index')) is not int:
                raise ModApiError('Cannot uniquely identify a claimable card reward')
            state = self.execute_client_action(
                'claim_reward', {'option_index': entries[0]['index']},
                expected_client_state=state,
                decision_telemetry={'policy': 'open_card_reward'})
        return self.wait_for_state(
            is_card_reward_selection,
            'visible card reward options', initial_state=state)

    def _wait_for_decision_boundary(
        self,
        action: str,
        before: Dict[str, Any],
        response_state: Dict[str, Any],
        *,
        deadline=None,
    ) -> Dict[str, Any]:
        """Do not trust an action acknowledgement as a completed game frame.

        The recovered Mod reports end_turn stable as soon as the enemy side
        starts, before enemy actions and the next draw have necessarily
        resolved.  Poll the authoritative state endpoint until a new player
        decision boundary is visible.
        """

        deadline = deadline if deadline is not None else time.monotonic() + self.mod.config.timeout_s
        state = response_state
        stable_decision_state = None
        while True:
            if _is_decision_boundary(action, before, state):
                # Room transitions can publish the MAP projection before the
                # underlying native synchronizer has finished unwinding the
                # prior room.  A following map click submitted against that
                # first frame may be consumed by the stale room (observed as
                # CHEST after leaving a treasure room).  Require two stable
                # gameplay observations whenever a client action exposes MAP.
                # This is a settlement rule, independent of visible pacing.
                if state.get('screen') == 'MAP':
                    observation = gameplay_observation(state)
                    if observation == stable_decision_state:
                        return state
                    stable_decision_state = observation
                    if time.monotonic() >= deadline:
                        raise ModApiError(
                            f"Client did not settle after {action!r}; "
                            f"screen={state.get('screen')!r}, turn={state.get('turn')!r}, "
                            f"actions={state.get('available_actions')!r}"
                        )
                    if self.on_wait is not None:
                        self.on_wait({'phase': 'waiting', 'waiting_for': action})
                    time.sleep(0.05)
                    state = _read_with_deadline(
                        self.mod.state, deadline, self.mod.config.timeout_s,
                        'map settlement state')
                    continue
                # A map click can expose COMBAT before enemies and intents have
                # finished populating. Require two identical ready snapshots;
                # this is settlement, independent from the configurable pacing.
                if action == 'choose_map_node' and state.get('in_combat'):
                    observation = gameplay_observation(state)
                    if observation == stable_decision_state:
                        return state
                    stable_decision_state = observation
                # Event option descriptions can initially contain unresolved
                # native dynamic variables such as {RipHpLoss}.  The event
                # policy reads the resolved values, so an EVENT boundary is not
                # settled until those inputs are complete and stable twice.
                elif state.get('screen') == 'EVENT':
                    # An option can legitimately advance an event to its next
                    # page while the native exporter still exposes unresolved
                    # template text.  Once the event identity/options have
                    # progressed, the action boundary is settled by two stable
                    # observations; unresolved text on the new page must not
                    # consume the whole transaction budget.  For an unchanged
                    # page we retain the strict dynamic-value readiness check.
                    event_progressed = _event_page_progressed(before, state)
                    if not event_progressed and not _event_decision_ready(state):
                        stable_decision_state = None
                    else:
                        observation = gameplay_observation(state)
                        if observation == stable_decision_state:
                            return state
                        stable_decision_state = observation
                else:
                    return state
            else:
                stable_decision_state = None
            if time.monotonic() >= deadline:
                raise ModApiError(
                    f"Client did not settle after {action!r}; "
                    f"screen={state.get('screen')!r}, turn={state.get('turn')!r}, "
                    f"actions={state.get('available_actions')!r}"
                )
            if self.on_wait is not None:
                self.on_wait({'phase': 'waiting', 'waiting_for': action})
            time.sleep(0.05)
            state = _read_with_deadline(
                self.mod.state, deadline, self.mod.config.timeout_s,
                'decision-boundary state')


def gameplay_observation(state: Dict[str, Any]) -> Dict[str, Any]:
    # Exclude transport counters and redundant presentation projections only.
    return {key: value for key, value in state.items()
            if key not in {"state_version", "agent_view", "session"}}


_UNRESOLVED_EVENT_VALUE = re.compile(r"\{[^{}]+\}")


def _event_decision_ready(state: Dict[str, Any]) -> bool:
    """Require every visible event choice to expose resolved policy inputs."""

    event = state.get('event') or {}
    options = event.get('options') or []
    if not options:
        return False
    for field in ('title', 'description'):
        value = event.get(field)
        if isinstance(value, str) and _UNRESOLVED_EVENT_VALUE.search(value):
            return False
    for option in options:
        for field in ('title', 'description'):
            value = option.get(field)
            if isinstance(value, str) and _UNRESOLVED_EVENT_VALUE.search(value):
                return False
    return True


def _event_page_progressed(before: Dict[str, Any], state: Dict[str, Any]) -> bool:
    """Return whether an event choice moved to a different native page."""

    if before.get('screen') != 'EVENT' or state.get('screen') != 'EVENT':
        return False
    old = before.get('event') or {}
    new = state.get('event') or {}
    if old.get('event_id') != new.get('event_id'):
        return True
    identity = ('is_finished', 'page', 'page_id', 'current_page', 'choice_id')
    if any(old.get(key) != new.get(key) for key in identity
           if key in old or key in new):
        return True
    old_options = old.get('options') or []
    new_options = new.get('options') or []
    old_keys = [(row.get('index'), row.get('text_key'), row.get('is_proceed'))
                for row in old_options]
    new_keys = [(row.get('index'), row.get('text_key'), row.get('is_proceed'))
                for row in new_options]
    return old_keys != new_keys


def event_resolution_equivalent(
    before: Dict[str, Any],
    observed: Dict[str, Any],
) -> bool:
    """Accept only native event-template resolution in a legacy snapshot."""

    expected = copy.deepcopy(gameplay_observation(before))
    actual = copy.deepcopy(gameplay_observation(observed))
    if expected.get('screen') != 'EVENT' or actual.get('screen') != 'EVENT':
        return False
    if expected.get('run_id') != actual.get('run_id'):
        return False
    expected_event = expected.get('event') or {}
    actual_event = actual.get('event') or {}
    if expected_event.get('event_id') != actual_event.get('event_id'):
        return False

    expected_options = expected_event.get('options') or []
    actual_options = actual_event.get('options') or []
    if len(expected_options) != len(actual_options):
        return False
    identity_fields = (
        'index', 'text_key', 'is_locked', 'is_proceed', 'will_kill_player',
    )
    for old_option, new_option in zip(expected_options, actual_options):
        if any(old_option.get(field) != new_option.get(field) for field in identity_fields):
            return False
        for field in ('title', 'description'):
            old_value = old_option.get(field)
            new_value = new_option.get(field)
            if (isinstance(old_value, str)
                    and _UNRESOLVED_EVENT_VALUE.search(old_value)):
                if (not isinstance(new_value, str)
                        or _UNRESOLVED_EVENT_VALUE.search(new_value)):
                    return False
                old_option[field] = new_value

    for field in ('title', 'description'):
        old_value = expected_event.get(field)
        new_value = actual_event.get(field)
        if isinstance(old_value, str) and _UNRESOLVED_EVENT_VALUE.search(old_value):
            if (not isinstance(new_value, str)
                    or _UNRESOLVED_EVENT_VALUE.search(new_value)):
                return False
            expected_event[field] = new_value
    return expected == actual


def _can_rebase_stale_action(
    action: str,
    params: Dict[str, Any],
    before: Dict[str, Any],
    observed: Dict[str, Any],
) -> bool:
    """Allow a narrow re-observation for stable treasure targets.

    Chest animations can mutate exported presentation/state fields between the
    policy observation and submission. Rebase only when the same run, scene,
    action, and concrete relic identity remain available; combat and all other
    decisions keep the strict stale-action cancellation behavior.
    """
    if action != 'choose_treasure_relic':
        return False
    if before.get('run_id') != observed.get('run_id'):
        return False
    if before.get('screen') != 'CHEST' or observed.get('screen') != 'CHEST':
        return False
    if action not in set(observed.get('available_actions') or []):
        return False
    option_index = params.get('option_index')

    def relic_id(state: Dict[str, Any]) -> Optional[str]:
        rows = ((state.get('chest') or {}).get('relic_options') or [])
        matches = [row for row in rows if row.get('index') == option_index]
        return str(matches[0].get('relic_id') or '') if len(matches) == 1 else None

    before_relic = relic_id(before)
    return bool(before_relic and before_relic == relic_id(observed))


def translate_headless_action(
    decision: str,
    action: str,
    args: Dict[str, Any],
    client_state: Dict[str, Any],
) -> LiveAction:
    """Map the headless protocol to the visible-client Mod protocol.

    Ambiguous or unsupported transitions fail closed.  In particular, map
    selection resolves coordinates against the client's currently visible
    choices rather than assuming the two protocols use the same list index.
    """

    if action == "play_card":
        params = _copy_required(args, 'card_index', optional=('target_index',))
        if params.get('target_index') is not None:
            params['target_index'] = _client_enemy_index(client_state, params['target_index'])
        return LiveAction(
            "play_card",
            params,
        )
    if action == "end_turn":
        return LiveAction("end_turn", {})
    if action == "use_potion":
        potion_index = args.get("potion_index", args.get("option_index"))
        if potion_index is None:
            raise ModApiError("use_potion is missing potion_index")
        visible_index = _client_potion_index(
            client_state, int(potion_index), args.get('potion_id'), require_usable=True
        )
        params = {
            "option_index": visible_index
        }
        visible_potion = next(
            (row for row in (client_state.get('run') or {}).get('potions') or []
             if isinstance(row, dict) and row.get('index') == visible_index), None
        )
        if visible_potion and visible_potion.get('requires_target') is True:
            target_space = visible_potion.get('target_index_space')
            valid_targets = visible_potion.get('valid_target_indices') or []
            if target_space == 'enemies':
                if args.get('target_index') is None:
                    raise ModApiError('Targeted enemy potion has no planned target')
                target_index = _client_enemy_index(client_state, int(args['target_index']))
            elif target_space == 'players' and len(valid_targets) == 1:
                target_index = valid_targets[0]
            else:
                raise ModApiError(f'Unsupported potion target space: {target_space!r}')
            if target_index not in valid_targets:
                raise ModApiError(f'Potion target {target_index} is not in the visible valid targets')
            params['target_index'] = target_index
        elif args.get('target_index') is not None:
            raise ModApiError('Planned potion target conflicts with the visible targetless potion')
        return LiveAction("use_potion", params)
    if action == "discard_potion":
        potion_index = args.get("potion_index", args.get("option_index"))
        if potion_index is None:
            raise ModApiError("discard_potion is missing potion_index")
        return LiveAction(
            "discard_potion",
            {"option_index": _client_potion_index(
                client_state, int(potion_index), args.get('potion_id'), require_usable=False
            )},
        )
    if action == "select_map_node":
        coord = (args.get("row"), args.get("col"))
        nodes = ((client_state.get("map") or {}).get("available_nodes") or [])
        matches = []
        for node in nodes:
            node_coord = node.get("coord") or node
            if node_coord.get("row") == coord[0] and node_coord.get("col") == coord[1]:
                matches.append(node)
        if len(matches) != 1:
            raise ModApiError(
                f"Cannot resolve map coordinate row={coord[0]!r}, col={coord[1]!r}; "
                f"matching visible nodes={len(matches)}"
            )
        option_index = matches[0].get("index")
        if option_index is None:
            raise ModApiError("Matched client map node has no index")
        return LiveAction("choose_map_node", {"option_index": int(option_index)})
    if action == "choose_option" and decision == "event_choice":
        return LiveAction("choose_event_option", _copy_required(args, "option_index"))
    if action == "select_card_reward":
        if "resolve_rewards" in (client_state.get("available_actions") or []):
            return LiveAction(
                "resolve_rewards",
                {"card_index": int(_required(args, "card_index"))},
            )
        return LiveAction(
            "choose_reward_card",
            {"option_index": int(_required(args, "card_index"))},
        )
    if action in {"skip_card_reward", "skip_reward"}:
        if "resolve_rewards" in (client_state.get("available_actions") or []):
            return LiveAction("resolve_rewards", {"option_index": -1})
        return LiveAction("skip_reward_cards", {})
    if action == "proceed":
        return LiveAction("proceed", {})
    if action == "leave_room" and "proceed" in (client_state.get("available_actions") or []):
        return LiveAction("proceed", {})
    raise ModApiError(
        f"No visible-client translation for decision={decision!r}, action={action!r}"
    )


def _normalized_game_id(value: Any) -> str:
    return str(value or '').split('.')[-1].strip().upper()


def _client_potion_index(
    state: Dict[str, Any],
    requested_index: int,
    potion_id: Any,
    *,
    require_usable: bool,
) -> int:
    potions = (state.get('run') or {}).get('potions') or []
    if not potions:
        return requested_index
    requested_id = _normalized_game_id(potion_id)

    def eligible(row: Dict[str, Any]) -> bool:
        if not row.get('occupied'):
            return False
        capability = 'can_use' if require_usable else 'can_discard'
        return row.get(capability) is not False

    occupied = [row for row in potions if isinstance(row, dict) and row.get('occupied')]
    matching = [
        row for row in potions
        if isinstance(row, dict)
        and eligible(row)
        and (not requested_id or _normalized_game_id(row.get('potion_id')) == requested_id)
    ]
    # The headless inventory is compact, while the visible client keeps empty
    # physical slots. Resolve its ordinal against occupied slots before using
    # the native slot index; this also distinguishes two identical potions.
    if 0 <= requested_index < len(occupied):
        ordinal_row = occupied[requested_index]
        if (eligible(ordinal_row)
                and (not requested_id or _normalized_game_id(ordinal_row.get('potion_id')) == requested_id)
                and type(ordinal_row.get('index')) is int):
            return int(ordinal_row['index'])
    if requested_id and len(matching) == 1 and type(matching[0].get('index')) is int:
        return int(matching[0]['index'])
    if not requested_id:
        raise ModApiError(
            f'Potion slot {requested_index} is not occupied or available in the visible client'
        )
    raise ModApiError(
        f'Chosen potion {requested_id} has no unique visible counterpart; matches={len(matching)}'
    )


def _client_enemy_index(state: Dict[str, Any], index: int) -> int:
    enemies = client_living_enemies(state)
    if not 0 <= index < len(enemies):
        raise ModApiError(f'Shadow enemy index {index} has no living client target')
    target = enemies[index]
    native = target.get('index')
    if type(native) is not int or target.get('is_hittable') is False:
        raise ModApiError('Mapped client enemy is not targetable')
    return native


def _required(args: Dict[str, Any], key: str) -> Any:
    if args.get(key) is None:
        raise ModApiError(f"Action is missing required field {key!r}")
    return args[key]


def _copy_required(
    args: Dict[str, Any],
    required: str,
    optional: Tuple[str, ...] = (),
) -> Dict[str, Any]:
    result = {required: int(_required(args, required))}
    for key in optional:
        if args.get(key) is not None:
            result[key] = int(args[key])
    return result


def _is_decision_boundary(action: str, before: Dict[str, Any], state: Dict[str, Any]) -> bool:
    from controller.interaction_flow import transition_completed
    return transition_completed(action, before, state)
