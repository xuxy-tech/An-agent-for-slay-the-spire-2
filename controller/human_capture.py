"""Versioned, model-independent storage for real human play demonstrations."""
from __future__ import annotations

import copy
import hashlib
import json
import queue
import threading
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol

from cli.sts2_mod_adapter import Sts2ModAdapter

RAW_SCHEMA = "sts2.human_capture.raw.v1"
DATASET_SCHEMA = "sts2.human_capture.dataset.v1"
ACTION_EVENT_TYPES = frozenset({"player_action_observed", "player_action_enqueued"})
CAPTURE_PROTOCOL = "2026-09-20-human-capture-v4"
AUTHORITATIVE_SNAPSHOT_SCHEMA = "sts2.combat_snapshot.headless.v3"
MIN_CAPTURE_PATCH_COUNT = 22
MANIFEST_REPLACE_ATTEMPTS = 40
MANIFEST_REPLACE_DELAY_S = 0.025
STALE_CAPTURE_EVENT_TOLERANCE_S = 0.25


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def observation_digest(value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def settlement_digest(value: dict[str, Any]) -> str:
    stable = {
        key: item for key, item in value.items()
        if key not in {"state_version", "agent_view"}
    }
    return observation_digest(stable)


def validate_authoritative_snapshot(
    payload: dict[str, Any], expected: dict[str, Any] | None = None
) -> tuple[dict[str, Any], bytes, str]:
    snapshot_json = payload.get("snapshot_json")
    if not isinstance(snapshot_json, str) or not snapshot_json:
        raise ValueError("authoritative snapshot response has no snapshot_json")
    raw = snapshot_json.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    expected_meta = expected or {}
    for source in ((payload, expected_meta) if expected is not None else (payload,)):
        if source.get("schema") != AUTHORITATIVE_SNAPSHOT_SCHEMA:
            raise ValueError(
                "authoritative snapshot schema mismatch: "
                f"expected={AUTHORITATIVE_SNAPSHOT_SCHEMA!r}, "
                f"actual={source.get('schema')!r}"
            )
        if source.get("sha256") != digest:
            raise ValueError("authoritative snapshot digest mismatch")
        if int(source.get("bytes") or -1) != len(raw):
            raise ValueError("authoritative snapshot byte count mismatch")
    try:
        snapshot = json.loads(snapshot_json)
    except json.JSONDecodeError as exc:
        raise ValueError("authoritative snapshot JSON is malformed") from exc
    if not isinstance(snapshot, dict):
        raise ValueError("authoritative snapshot envelope is not an object")
    required = {
        "Id", "CharacterName", "AscensionLevel", "Seed", "RoomJson", "PlayerJson",
        "NetState", "EnemyCreatureStates", "EnemyAiStates", "RunRngStates",
        "PlayerRngStates", "RoundNumber", "CurrentSide", "RelicStates",
        "HookStates", "PlayerCombatState", "PlayerExtraState",
        "CombatHistoryEntries", "ActivePowerRefs",
    }
    missing = sorted(required.difference(snapshot))
    if missing:
        raise ValueError(f"authoritative snapshot is missing {missing}")
    for key in ("RelicStates", "HookStates", "CombatHistoryEntries", "ActivePowerRefs"):
        if not isinstance(snapshot[key], list):
            raise ValueError(f"authoritative snapshot {key} is not a list")
    for key in ("PlayerCombatState", "PlayerExtraState"):
        if not isinstance(snapshot[key], dict) or not isinstance(snapshot[key].get("Fields"), list):
            raise ValueError(f"authoritative snapshot {key} is incomplete")
    for index, entry in enumerate(snapshot["CombatHistoryEntries"]):
        if not isinstance(entry, dict) or not isinstance(entry.get("Kind"), str):
            raise ValueError(f"authoritative snapshot CombatHistoryEntries[{index}] is invalid")
    for index, entry in enumerate(snapshot["ActivePowerRefs"]):
        if (not isinstance(entry, dict) or not isinstance(entry.get("PowerId"), str)
                or not isinstance(entry.get("CreatureIndex"), int)
                or not isinstance(entry.get("PowerIndex"), int)):
            raise ValueError(f"authoritative snapshot ActivePowerRefs[{index}] is invalid")
    if snapshot.get("Id") != payload.get("snapshot_id") or (
        expected_meta and snapshot.get("Id") != expected_meta.get("snapshot_id")
    ):
        raise ValueError("authoritative snapshot id mismatch")
    for key in ("RoomJson", "PlayerJson"):
        value = snapshot.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"authoritative snapshot {key} is empty")
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"authoritative snapshot {key} is malformed") from exc
        if not isinstance(decoded, dict):
            raise ValueError(f"authoritative snapshot {key} is not an object")
    net_state = snapshot.get("NetState")
    if not isinstance(net_state, dict):
        raise ValueError("authoritative snapshot NetState is not an object")
    if not isinstance(net_state.get("Creatures"), list) or not net_state["Creatures"]:
        raise ValueError("authoritative snapshot has no combat creatures")
    if not isinstance(net_state.get("Players"), list) or not net_state["Players"]:
        raise ValueError("authoritative snapshot has no combat players")
    for player_index, player in enumerate(net_state["Players"]):
        for pile_index, pile in enumerate(player.get("piles") or []):
            for card_index, card in enumerate(pile.get("cards") or []):
                runtime_cost = card.get("runtimeEnergyCost")
                if not isinstance(runtime_cost, dict) or not isinstance(runtime_cost.get("Base"), int) \
                        or not isinstance(runtime_cost.get("LocalModifiers"), list):
                    raise ValueError(
                        "authoritative snapshot is missing runtimeEnergyCost at "
                        f"player={player_index} pile={pile_index} card={card_index}")
    net_rng = net_state.get("Rng")
    if not isinstance(net_rng, dict) or not isinstance(net_rng.get("Counters"), dict):
        raise ValueError("authoritative snapshot NetState RNG is incomplete")
    for key in ("EnemyCreatureStates", "EnemyAiStates"):
        if not isinstance(snapshot.get(key), list):
            raise ValueError(f"authoritative snapshot {key} is not a list")
    for key in ("RunRngStates", "PlayerRngStates"):
        states = snapshot.get(key)
        if not isinstance(states, list) or not states:
            raise ValueError(f"authoritative snapshot {key} is empty")
        for index, state in enumerate(states):
            if not isinstance(state, dict) or not state.get("Name"):
                raise ValueError(f"authoritative snapshot {key}[{index}] is invalid")
            missing_rng = [name for name in ("Counter", "Seed", "S0", "S1", "S2", "S3")
                           if state.get(name) is None]
            if missing_rng:
                raise ValueError(
                    f"authoritative snapshot {key}[{index}] is missing {missing_rng}"
                )
    return snapshot, raw, digest


def derive_combat_id(observation: dict[str, Any], run_id: str) -> str | None:
    if observation.get("in_combat") is not True:
        return None
    run = observation.get("run") if isinstance(observation.get("run"), dict) else {}
    map_state = observation.get("map") if isinstance(observation.get("map"), dict) else {}
    parts = {
        "act": run.get("act_id"),
        "floor": run.get("floor"),
        "map": map_state.get("current"),
    }
    identity = ":".join(f"{key}={value}" for key, value in parts.items() if value is not None)
    return f"{run_id}:{identity or 'combat_unknown'}"


def normalize_action(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data")
    if event.get("type") not in ACTION_EVENT_TYPES or not isinstance(data, dict):
        raise ValueError("Expected one exact player-action event")
    semantic = str(data.get("semantic_action") or "").strip()
    if not semantic:
        raise ValueError("Capture event has no semantic_action")
    result = {
        "type": semantic,
        "source": data.get("source") or "legacy_combat_action_queue",
        "phase": data.get("phase") or "committed",
        "action_role": data.get("action_role") or "decision",
        "owner_id": data.get("owner_id"),
        "queue_action_id": data.get("queue_action_id"),
        "native_action_type": data.get("action_type"),
        "native_action_class": data.get("action_class"),
    }
    reserved = {
        "semantic_action", "source", "phase", "action_role", "owner_id", "queue_action_id",
        "action_type", "action_class",
    }
    for key, value in data.items():
        if key not in reserved and value is not None:
            result[key] = copy.deepcopy(value)
    return result


class JsonlCaptureSession:
    def __init__(self, directory: Path, metadata: dict[str, Any]):
        self.session_id = str(metadata.get("session_id") or f"human_{uuid.uuid4().hex}")
        self.directory = directory / self.session_id
        self.directory.mkdir(parents=True, exist_ok=False)
        self.events_path = self.directory / "events.jsonl"
        self.snapshots_directory = self.directory / "snapshots"
        self.manifest_path = self.directory / "manifest.json"
        self._lock = threading.Lock()
        self._sequence = 0
        self.manifest = {
            "schema": RAW_SCHEMA,
            "session_id": self.session_id,
            "created_at_utc": utc_now(),
            "status": "recording",
            "records": 0,
            **metadata,
        }
        self._write_manifest()
        self.append("session_started", {"metadata": metadata})

    def append(self, record_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._sequence += 1
            row = {
                "schema": RAW_SCHEMA,
                "session_id": self.session_id,
                "sequence": self._sequence,
                "record_type": record_type,
                "recorded_at_utc": utc_now(),
                **payload,
            }
            with self.events_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            self.manifest["records"] = self._sequence
            self._write_manifest()
            return row

    def append_decision(
        self,
        event: dict[str, Any],
        before: dict[str, Any],
        after: dict[str, Any] | None,
        settlement: str,
    ) -> dict[str, Any]:
        action = normalize_action(event)
        run_id = str(before.get("run_id") or "run_unknown")
        turn = before.get("turn")
        event_id = event.get("event_id")
        snapshot = copy.deepcopy(event.get("authoritative_snapshot"))
        if isinstance(snapshot, dict) and snapshot.get("status") == "complete":
            snapshot["root_observation_sha256"] = observation_digest(before)
        return self.append("decision", {
            "decision_id": f"{self.session_id}:{event_id}",
            "run_id": run_id,
            "combat_id": derive_combat_id(before, run_id),
            "turn": turn,
            "screen": before.get("screen"),
            "observation_before": copy.deepcopy(before),
            "observation_before_sha256": observation_digest(before),
            "legal_actions_before": copy.deepcopy(before.get("available_actions") or []),
            "action": action,
            "native_event": copy.deepcopy(event),
            "authoritative_snapshot": snapshot,
            "observation_after": copy.deepcopy(after),
            "observation_after_sha256": observation_digest(after) if after else None,
            "settlement": settlement,
            "label_semantics": "chosen_action_is_positive; unchosen_actions_are_unlabeled",
            "provenance": "independent_human_play",
            "extensions": {},
        })

    def persist_authoritative_snapshot(
        self, event: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any]:
        metadata = ((event.get("data") or {}).get("authoritative_snapshot") or {})
        if not isinstance(metadata, dict) or metadata.get("status") != "complete":
            raise ValueError("combat action has no complete authoritative snapshot metadata")
        snapshot, raw, digest = validate_authoritative_snapshot(payload, metadata)
        snapshot_id = str(payload["snapshot_id"])
        if not snapshot_id.replace("_", "").isalnum():
            raise ValueError("authoritative snapshot id is not path-safe")
        self.snapshots_directory.mkdir(parents=True, exist_ok=True)
        destination = self.snapshots_directory / f"{snapshot_id}.json"
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_bytes(raw)
        temporary.replace(destination)
        return {
            "status": "complete",
            "schema": AUTHORITATIVE_SNAPSHOT_SCHEMA,
            "snapshot_id": snapshot_id,
            "path": destination.relative_to(self.directory).as_posix(),
            "sha256": digest,
            "bytes": len(raw),
            "captured_at_utc": metadata.get("captured_at_utc") or payload.get("captured_at_utc"),
            "event_id": event.get("event_id"),
            "round_number": snapshot.get("RoundNumber"),
            "current_side": snapshot.get("CurrentSide"),
        }

    def close(self, status: str = "completed") -> None:
        self.append("session_ended", {"status": status})
        self.manifest["status"] = status
        self.manifest["ended_at_utc"] = utc_now()
        self._write_manifest()

    def _write_manifest(self) -> None:
        temporary = self.manifest_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        for attempt in range(MANIFEST_REPLACE_ATTEMPTS):
            try:
                temporary.replace(self.manifest_path)
                return
            except PermissionError:
                if attempt + 1 >= MANIFEST_REPLACE_ATTEMPTS:
                    raise
                time.sleep(MANIFEST_REPLACE_DELAY_S)


class ObservationSampler:
    def __init__(self, mod: Sts2ModAdapter, interval_s: float = 0.05, capacity: int = 512):
        self.mod = mod
        self.interval_s = max(0.02, float(interval_s))
        self.samples: deque[tuple[float, dict[str, Any]]] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="human-capture-state", daemon=True)
        self.last_error: str | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def latest(self) -> dict[str, Any]:
        with self._lock:
            if not self.samples:
                raise RuntimeError(self.last_error or "No observer state has been sampled")
            return copy.deepcopy(self.samples[-1][1])

    def before_sample(self, timestamp_utc: str | None) -> tuple[float, dict[str, Any]]:
        event_time = parse_event_timestamp(timestamp_utc, float("inf"))
        with self._lock:
            if not self.samples:
                raise RuntimeError(self.last_error or "No observer state has been sampled")
            candidates = [sample for sample in self.samples if sample[0] <= event_time]
            sampled_at, state = candidates[-1] if candidates else self.samples[0]
            return sampled_at, copy.deepcopy(state)

    def before(self, timestamp_utc: str | None) -> dict[str, Any]:
        return self.before_sample(timestamp_utc)[1]

    def settled_after(
        self,
        event_time: float,
        before: dict[str, Any],
        stable_s: float | None = None,
    ) -> tuple[float, dict[str, Any]] | None:
        required_stability = max(0.1, self.interval_s * 2) if stable_s is None else stable_s
        before_digest = settlement_digest(before)
        changed_digest: str | None = None
        changed_since = 0.0
        changed_state: dict[str, Any] | None = None
        with self._lock:
            samples = list(self.samples)
        for sampled_at, state in samples:
            if sampled_at <= event_time:
                continue
            digest = settlement_digest(state)
            if digest == before_digest:
                changed_digest = None
                changed_state = None
                continue
            if digest != changed_digest:
                changed_digest = digest
                changed_since = sampled_at
                changed_state = state
                continue
            if sampled_at - changed_since >= required_stability:
                return sampled_at, copy.deepcopy(changed_state or state)
        return None

    def wait_for_change(self, before: dict[str, Any], timeout_s: float) -> dict[str, Any] | None:
        before_digest = settlement_digest(before)
        deadline = time.monotonic() + timeout_s
        changed: dict[str, Any] | None = None
        stable_since = 0.0
        while time.monotonic() < deadline:
            current = self.latest()
            if settlement_digest(current) != before_digest:
                if changed is not None and settlement_digest(current) == settlement_digest(changed):
                    if time.monotonic() - stable_since >= max(0.1, self.interval_s * 2):
                        return current
                else:
                    changed = current
                    stable_since = time.monotonic()
            time.sleep(self.interval_s)
        return changed

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                state = self.mod.state()
                with self._lock:
                    self.samples.append((time.time(), state))
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
            self._stop.wait(self.interval_s)


def parse_event_timestamp(timestamp_utc: str | None, fallback: float | None = None) -> float:
    if timestamp_utc:
        try:
            return datetime.fromisoformat(timestamp_utc.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time() if fallback is None else fallback


@dataclass
class PendingDecision:
    event: dict[str, Any]
    event_time: float
    before_sampled_at: float
    before: dict[str, Any]
    deadline: float

    @property
    def before_digest(self) -> str:
        return settlement_digest(self.before)


class ActionAttributor:
    def __init__(
        self,
        sampler: ObservationSampler,
        settle_timeout_s: float,
        attribution_grace_s: float | None = None,
    ):
        self.sampler = sampler
        self.settle_timeout_s = settle_timeout_s
        self.attribution_grace_s = (
            max(0.5, sampler.interval_s * 4)
            if attribution_grace_s is None else attribution_grace_s
        )
        self.pending: list[PendingDecision] = []

    def add(self, event: dict[str, Any]) -> None:
        event_time = parse_event_timestamp(event.get("timestamp_utc"))
        sampled_at, before = self.sampler.before_sample(event.get("timestamp_utc"))
        self.pending.append(PendingDecision(
            event=event,
            event_time=event_time,
            before_sampled_at=sampled_at,
            before=before,
            deadline=event_time + self.settle_timeout_s,
        ))
        self.pending.sort(key=lambda item: (item.event_time, int(item.event.get("event_id") or 0)))

    def ready(
        self, now: float | None = None
    ) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None, str]]:
        current_time = time.time() if now is None else now
        results: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None, str]] = []
        while self.pending:
            pending = self.pending[0]
            candidate = self.sampler.settled_after(pending.event_time, pending.before)
            if candidate is None:
                if current_time < pending.deadline:
                    break
                self.pending.pop(0)
                results.append((pending.event, pending.before, None, "timeout"))
                continue
            settled_at, after = candidate
            overlapping = [
                item for item in self.pending
                if item.event_time <= settled_at and item.before_digest == pending.before_digest
            ]
            if len(overlapping) > 1:
                overlap_ids = {id(item) for item in overlapping}
                self.pending = [item for item in self.pending if id(item) not in overlap_ids]
                results.extend(
                    (item.event, item.before, after, "ambiguous_overlap")
                    for item in overlapping
                )
                continue
            if current_time < settled_at + self.attribution_grace_s:
                break
            self.pending.pop(0)
            results.append((pending.event, pending.before, after, "settled"))
        return results

    def flush(
        self, settlement: str
    ) -> list[tuple[dict[str, Any], dict[str, Any], None, str]]:
        results = [(item.event, item.before, None, settlement) for item in self.pending]
        self.pending.clear()
        return results


def collect_human_actions(
    mod: Sts2ModAdapter,
    output_root: Path,
    *,
    poll_interval_s: float = 0.05,
    settle_timeout_s: float = 20.0,
    attribution_grace_s: float | None = None,
    max_decisions: int | None = None,
    duration_s: float | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> Path:
    if max_decisions is not None and max_decisions <= 0:
        raise ValueError("max_decisions must be positive")
    if duration_s is not None and duration_s <= 0:
        raise ValueError("duration_s must be positive")
    observer_identity = mod.health()
    capture_health = mod.capture_health()
    capture_identity = mod.capture_identity()
    session = JsonlCaptureSession(output_root, {
        "observer_identity": observer_identity,
        "capture_health": capture_health,
        "capture_identity": capture_identity,
        "collection_mode": "real_client_human; no_headless_actions",
    })
    collection_started_at = time.time()
    discarded_capture_events = 0
    sampler = ObservationSampler(mod, poll_interval_s)
    stream_items: queue.Queue[tuple[str, Any]] = queue.Queue()

    def close_session(status: str) -> None:
        session.manifest["discarded_capture_events"] = discarded_capture_events
        session.close(status)

    def read_stream() -> None:
        try:
            for captured_event in mod.capture_events():
                stream_items.put(("event", captured_event))
        except BaseException as exc:
            stream_items.put(("error", exc))
        else:
            stream_items.put(("eof", None))

    sampler.start()
    try:
        deadline = time.monotonic() + max(2.0, settle_timeout_s)
        while True:
            try:
                sampler.latest()
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(poll_interval_s)
        attributor = ActionAttributor(sampler, settle_timeout_s, attribution_grace_s)
        stream_thread = threading.Thread(target=read_stream, name="human-capture-events", daemon=True)
        stream_thread.start()
        collection_deadline = (
            time.monotonic() + duration_s if duration_s is not None else None
        )
        finalized = 0
        while True:
            try:
                kind, value = stream_items.get(timeout=poll_interval_s)
            except queue.Empty:
                kind, value = "idle", None
            if kind == "error":
                for event, before, after, settlement in attributor.flush("stream_error"):
                    session.append_decision(event, before, after, settlement)
                raise value
            if kind == "eof":
                for event, before, after, settlement in attributor.flush("stream_ended"):
                    session.append_decision(event, before, after, settlement)
                raise RuntimeError("Human-capture event stream ended unexpectedly")
            if kind == "event":
                event = value
                event_time = parse_event_timestamp(event.get("timestamp_utc"), None)
                if event_time is not None and event_time < collection_started_at - STALE_CAPTURE_EVENT_TOLERANCE_S:
                    discarded_capture_events += 1
                    session.append("capture_event_discarded", {
                        "native_event": event,
                        "reason": "before_collection_started",
                    })
                    continue
                session.append("capture_event", {"native_event": event})
                if event.get("type") in ACTION_EVENT_TYPES:
                    event = copy.deepcopy(event)
                    snapshot_metadata = ((event.get("data") or {}).get("authoritative_snapshot") or {})
                    if not isinstance(snapshot_metadata, dict):
                        snapshot_metadata = {}
                    snapshot_status = snapshot_metadata.get("status")
                    if snapshot_status == "complete":
                        try:
                            payload = mod.capture_snapshot(str(snapshot_metadata.get("snapshot_id") or ""))
                            event["authoritative_snapshot"] = session.persist_authoritative_snapshot(
                                event, payload
                            )
                        except Exception as exc:
                            session.append("capture_snapshot_failure", {
                                "event_id": event.get("event_id"),
                                "snapshot_metadata": snapshot_metadata,
                                "error": str(exc),
                            })
                            raise RuntimeError(
                                f"Authoritative combat snapshot failed for event {event.get('event_id')}: {exc}"
                            ) from exc
                    elif snapshot_status == "not_combat":
                        event["authoritative_snapshot"] = {
                            "status": "not_combat",
                            "schema": AUTHORITATIVE_SNAPSHOT_SCHEMA,
                            "event_id": event.get("event_id"),
                        }
                    else:
                        session.append("capture_snapshot_failure", {
                            "event_id": event.get("event_id"),
                            "snapshot_metadata": snapshot_metadata,
                            "error": "missing_or_failed_snapshot_metadata",
                        })
                        raise RuntimeError(
                            f"Capture Mod did not provide valid snapshot metadata for event {event.get('event_id')}"
                        )
                    attributor.add(event)
            for event, before, after, settlement in attributor.ready():
                session.append_decision(event, before, after, settlement)
                finalized += 1
            limit_reached = max_decisions is not None and finalized >= max_decisions
            duration_reached = (
                collection_deadline is not None and time.monotonic() >= collection_deadline
            )
            requested_stop = stop_requested is not None and stop_requested()
            if limit_reached or duration_reached or requested_stop:
                reason = "stopped" if requested_stop else "collection_limit"
                for event, before, after, settlement in attributor.flush(reason):
                    session.append_decision(event, before, after, settlement)
                close_session("stopped" if requested_stop else "completed")
                break
    except KeyboardInterrupt:
        if "attributor" in locals():
            for event, before, after, settlement in attributor.flush("interrupted"):
                session.append_decision(event, before, after, settlement)
        close_session("stopped")
    except Exception:
        close_session("failed")
        raise
    finally:
        sampler.stop()
    return session.directory


class ExampleTransform(Protocol):
    def __call__(self, example: dict[str, Any]) -> dict[str, Any]: ...


def iter_decisions(paths: Iterable[Path]) -> Iterator[dict[str, Any]]:
    for path in paths:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                if row.get("schema") == RAW_SCHEMA and row.get("record_type") == "decision":
                    yield row


def dataset_split(row: dict[str, Any]) -> str:
    key = dataset_group_key(row)
    return split_for_group_key(key)


def split_for_group_key(key: str) -> str:
    bucket = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % 100
    return "train" if bucket < 80 else "validation" if bucket < 90 else "test"


def dataset_group_key(row: dict[str, Any]) -> str:
    run_id = str(row.get("run_id") or "")
    return (
        run_id if run_id and run_id != "run_unknown"
        else f"session:{row.get('session_id') or row['decision_id']}"
    )


def assign_dataset_splits(rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    keys = sorted({dataset_group_key(row) for row in rows})
    assignments = {key: split_for_group_key(key) for key in keys}
    if keys and "train" not in assignments.values():
        fallback = min(keys, key=lambda key: hashlib.sha256(key.encode("utf-8")).hexdigest())
        assignments[fallback] = "train"
    return assignments


def action_specificity(row: dict[str, Any]) -> int:
    source = str((row.get("action") or {}).get("source") or "")
    return {
        "combat_action_queue": 100,
        "player_choice": 90,
        "reward_item": 80,
        "merchant_purchase": 70,
        "rest_site_selection": 60,
        "event_selection": 60,
        "treasure_selection": 60,
        "map_selection": 60,
        "reward_selection": 50,
        "shop_navigation": 20,
        "ui_navigation": 10,
        "legacy_combat_action_queue": 100,
    }.get(source, 0)


def event_time_bucket(row: dict[str, Any]) -> int:
    timestamp = str((row.get("native_event") or {}).get("timestamp_utc") or "")
    try:
        return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return int(row.get("sequence") or 0)


def _matches_acceptance_case(action: dict[str, Any], case: str) -> bool:
    if case.startswith("source:"):
        return action.get("source") == case.partition(":")[2]
    if case in {"use_potion:manual_target", "use_potion:targeted"}:
        return action.get("type") == "use_potion" and action.get("requires_target") is True
    if case in {"use_potion:no_manual_target", "use_potion:targetless"}:
        return action.get("type") == "use_potion" and action.get("requires_target") is False
    return action.get("type") == case


def audit_capture_session(events_path: Path, required_cases: Iterable[str] = ()) -> dict[str, Any]:
    rows = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    integrity_errors: list[str] = []
    starts = [row for row in rows if row.get("record_type") == "session_started"]
    metadata = ((starts[0].get("metadata") or {}) if starts else {})
    protocol = str(((metadata.get("capture_health") or {}).get("protocol_version") or ""))
    if protocol != CAPTURE_PROTOCOL:
        integrity_errors.append(
            f"capture protocol is {protocol or 'missing'}, expected {CAPTURE_PROTOCOL}"
        )
    patch_count = int(((metadata.get("capture_health") or {}).get("patch_count") or 0))
    if patch_count < MIN_CAPTURE_PATCH_COUNT:
        integrity_errors.append(
            f"capture patch count is {patch_count}, expected at least {MIN_CAPTURE_PATCH_COUNT}"
        )
    if ((metadata.get("capture_health") or {}).get("authoritative_combat_snapshot") is not True):
        integrity_errors.append("capture Mod did not advertise authoritative combat snapshots")

    capture_rows = [row for row in rows if row.get("record_type") == "capture_event"]
    discarded_capture_events = [
        row for row in rows if row.get("record_type") == "capture_event_discarded"
    ]
    native_events: list[tuple[int, int]] = []
    for row in capture_rows:
        native = row.get("native_event")
        if not isinstance(native, dict):
            continue
        try:
            native_events.append((int(row.get("sequence") or 0), int(native["event_id"])))
        except (KeyError, TypeError, ValueError):
            integrity_errors.append(f"sequence {row.get('sequence')}: invalid native event_id")
    event_ids = [event_id for _, event_id in sorted(native_events)]
    for previous, current in zip(event_ids, event_ids[1:]):
        if current != previous + 1:
            integrity_errors.append(
                f"capture event sequence discontinuity: {previous} followed by {current}"
            )

    decisions = [row for row in rows if row.get("record_type") == "decision"]
    captured_ids = {event_id for _, event_id in native_events}
    for row in decisions:
        decision_event_id = (row.get("native_event") or {}).get("event_id")
        if decision_event_id not in captured_ids:
            integrity_errors.append(
                f"sequence {row.get('sequence')}: decision has no matching raw capture event"
            )
    settlements: Counter[str] = Counter()
    actions: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    potion_target_semantics: Counter[str] = Counter()
    settled_actions: list[dict[str, Any]] = []
    authoritative_snapshots = 0
    for row in decisions:
        sequence = row.get("sequence")
        if row.get("schema") != RAW_SCHEMA:
            integrity_errors.append(f"sequence {sequence}: unsupported raw schema")
        action = row.get("action")
        before = row.get("observation_before")
        after = row.get("observation_after")
        settlement = str(row.get("settlement") or "missing")
        settlements[settlement] += 1
        if not isinstance(action, dict) or not action.get("type") or not action.get("source"):
            integrity_errors.append(f"sequence {sequence}: incomplete normalized action")
            continue
        actions[str(action["type"])] += 1
        sources[str(action["source"])] += 1
        if action.get("type") == "use_potion":
            requires_target = action.get("requires_target")
            if requires_target is True:
                potion_target_semantics["manual_target"] += 1
            elif requires_target is False:
                potion_target_semantics["no_manual_target"] += 1
            else:
                potion_target_semantics["unknown"] += 1
                integrity_errors.append(
                    f"sequence {sequence}: potion action has no explicit requires_target semantics"
                )
        if not isinstance(before, dict) or observation_digest(before) != row.get("observation_before_sha256"):
            integrity_errors.append(f"sequence {sequence}: invalid before observation or digest")
        snapshot_ref = row.get("authoritative_snapshot")
        if isinstance(before, dict) and before.get("in_combat") is True:
            if not isinstance(snapshot_ref, dict) or snapshot_ref.get("status") != "complete":
                integrity_errors.append(
                    f"sequence {sequence}: combat decision has no complete authoritative snapshot"
                )
            else:
                try:
                    relative = Path(str(snapshot_ref.get("path") or ""))
                    snapshot_path = (events_path.parent / relative).resolve()
                    snapshot_root = (events_path.parent / "snapshots").resolve()
                    if not snapshot_path.is_relative_to(snapshot_root):
                        raise ValueError("snapshot path escapes the session snapshot directory")
                    snapshot_json = snapshot_path.read_text(encoding="utf-8")
                    validate_authoritative_snapshot(
                        {**snapshot_ref, "snapshot_json": snapshot_json}, snapshot_ref
                    )
                    if snapshot_ref.get("event_id") != (row.get("native_event") or {}).get("event_id"):
                        raise ValueError("snapshot event id mismatch")
                    if snapshot_ref.get("root_observation_sha256") != row.get("observation_before_sha256"):
                        raise ValueError("snapshot root observation digest mismatch")
                    authoritative_snapshots += 1
                except (OSError, ValueError, TypeError) as exc:
                    integrity_errors.append(
                        f"sequence {sequence}: invalid authoritative snapshot: {exc}"
                    )
        elif isinstance(snapshot_ref, dict) and snapshot_ref.get("status") not in {
            "not_combat", "complete"
        }:
            integrity_errors.append(f"sequence {sequence}: invalid noncombat snapshot status")
        if settlement == "settled":
            if not isinstance(after, dict) or observation_digest(after) != row.get("observation_after_sha256"):
                integrity_errors.append(f"sequence {sequence}: invalid settled after observation or digest")
            elif settlement_digest(before) == settlement_digest(after):
                integrity_errors.append(f"sequence {sequence}: settled transition did not change state")
            else:
                settled_actions.append(action)

    required = sorted(set(str(case) for case in required_cases))
    missing = [
        case for case in required
        if not any(_matches_acceptance_case(action, case) for action in settled_actions)
    ]
    return {
        "schema": "sts2.human_capture.audit.v1",
        "events_path": str(events_path),
        "ok": not integrity_errors and not missing,
        "protocol_version": protocol or None,
        "patch_count": patch_count,
        "records": len(rows),
        "discarded_capture_events": len(discarded_capture_events),
        "decisions": len(decisions),
        "settlements": dict(sorted(settlements.items())),
        "actions": dict(sorted(actions.items())),
        "sources": dict(sorted(sources.items())),
        "potion_target_semantics": dict(sorted(potion_target_semantics.items())),
        "authoritative_combat_snapshots": authoritative_snapshots,
        "required_cases": required,
        "missing_required_cases": missing,
        "integrity_errors": integrity_errors,
    }


def build_dataset(
    event_paths: Iterable[Path],
    output_dir: Path,
    transform: ExampleTransform | None = None,
    *,
    require_integrity: bool = False,
) -> dict[str, Any]:
    paths = [Path(path) for path in event_paths]
    source_sessions: list[dict[str, Any]] = []
    for path in paths:
        raw_manifest_path = path.with_name("manifest.json")
        raw_manifest = (
            json.loads(raw_manifest_path.read_text(encoding="utf-8"))
            if raw_manifest_path.is_file() else {}
        )
        audit = audit_capture_session(path) if require_integrity else None
        if audit is not None and not audit["ok"]:
            raise ValueError(
                f"Capture integrity audit failed for {path}: {audit['integrity_errors']}"
            )
        capture_health = raw_manifest.get("capture_health") or {}
        capture_identity = raw_manifest.get("capture_identity") or {}
        observer_identity = raw_manifest.get("observer_identity") or {}
        source_sessions.append({
            "session_id": raw_manifest.get("session_id"),
            "events_sha256": observation_file_sha256(path),
            "capture_protocol": capture_health.get("protocol_version"),
            "capture_patch_count": capture_health.get("patch_count"),
            "capture_assembly_sha256": (
                (capture_identity.get("capture_assembly") or {}).get("sha256")
            ),
            "game_assembly_sha256": (
                (capture_identity.get("game_assembly") or {}).get("sha256")
            ),
            "observer_protocol": observer_identity.get("protocol_version"),
            "integrity_audit": audit,
        })
    output_dir.mkdir(parents=True, exist_ok=True)
    streams = {
        split: (output_dir / f"{split}.jsonl").open("w", encoding="utf-8", newline="\n")
        for split in ("train", "validation", "test")
    }
    counts = {split: 0 for split in streams}
    skipped = 0
    skipped_duplicate = 0
    settlement_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    screen_counts: Counter[str] = Counter()
    try:
        candidates: dict[tuple[str, str, str, int], dict[str, Any]] = {}
        for row in iter_decisions(paths):
            settlement_counts[str(row.get("settlement") or "missing")] += 1
            if row.get("settlement") != "settled" or not isinstance(row.get("observation_after"), dict):
                skipped += 1
                continue
            transition = (
                str(row.get("session_id")),
                str(row.get("observation_before_sha256")),
                str(row.get("observation_after_sha256")),
                event_time_bucket(row),
            )
            current = candidates.get(transition)
            if current is not None:
                skipped_duplicate += 1
                if action_specificity(row) <= action_specificity(current):
                    continue
            candidates[transition] = row
        selected_rows = sorted(candidates.values(), key=lambda value: (
            str(value.get("session_id")), int(value.get("sequence") or 0)
        ))
        split_assignments = assign_dataset_splits(selected_rows)
        for row in selected_rows:
            action = row.get("action") or {}
            action_counts[str(action.get("type") or "unknown")] += 1
            source_counts[str(action.get("source") or "unknown")] += 1
            screen_counts[str(row.get("screen") or "UNKNOWN")] += 1
            example = {
                "schema": DATASET_SCHEMA,
                "example_id": row["decision_id"],
                "session_id": row["session_id"],
                "run_id": row["run_id"],
                "combat_id": row["combat_id"],
                "turn": row.get("turn"),
                "observation": row["observation_before"],
                "legal_actions": row["legal_actions_before"],
                "chosen_action": row["action"],
                "resulting_observation": row["observation_after"],
                "authoritative_snapshot": copy.deepcopy(row.get("authoritative_snapshot")),
                "label_semantics": row["label_semantics"],
                "provenance": row["provenance"],
                "extensions": copy.deepcopy(row.get("extensions") or {}),
            }
            if transform is not None:
                example = transform(example)
            split = split_assignments[dataset_group_key(row)]
            streams[split].write(json.dumps(example, ensure_ascii=False, sort_keys=True) + "\n")
            counts[split] += 1
    finally:
        for stream in streams.values():
            stream.close()
    manifest = {
        "schema": DATASET_SCHEMA,
        "created_at_utc": utc_now(),
        "counts": counts,
        "skipped_unsettled": skipped,
        "skipped_duplicate_transitions": skipped_duplicate,
        "settlement_counts": dict(sorted(settlement_counts.items())),
        "action_counts": dict(sorted(action_counts.items())),
        "source_counts": dict(sorted(source_counts.items())),
        "screen_counts": dict(sorted(screen_counts.items())),
        "split_unit": "run_id; session_id fallback when run_id is unavailable",
        "split_policy": "stable_sha256_80_10_10; force one run group to train only when train would be empty",
        "integrity_gate": "required" if require_integrity else "not_required",
        "source_sessions": source_sessions,
        "raw_observation_preserved": True,
        "authoritative_combat_snapshot_preserved_by_reference": True,
        "feature_extraction": "downstream adapter; not frozen into collection schema",
        "contract": {
            "observation": "full observer payload; model-independent",
            "chosen_action": "extensible normalized native action",
            "resulting_observation": "full settled observer payload",
            "authoritative_snapshot": (
                "content-addressed session sidecar compatible with headless snapshot import; "
                "required for combat decisions"
            ),
            "unchosen_actions": "unlabeled",
        },
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return manifest


def observation_file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture_protocol_for_events(path: Path) -> str | None:
    manifest_path = Path(path).with_name("manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    protocol = ((manifest.get("capture_health") or {}).get("protocol_version"))
    return str(protocol) if protocol else None


def current_capture_event_paths(root: Path) -> list[Path]:
    return [
        path for path in sorted(Path(root).glob("*/events.jsonl"))
        if capture_protocol_for_events(path) == CAPTURE_PROTOCOL
    ]
