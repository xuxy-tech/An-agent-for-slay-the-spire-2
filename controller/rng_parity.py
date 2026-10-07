from __future__ import annotations

from dataclasses import dataclass
from typing import Any


RNG_FIELDS = ("counter", "seed", "s0", "s1", "s2", "s3")


@dataclass(frozen=True)
class RngParityResult:
    status: str
    differences: list[dict[str, Any]]

    @property
    def passed(self) -> bool:
        return self.status == "PASS"


def flatten_rng_streams(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise ValueError("RNG snapshot schema_version must be 1")
    if snapshot.get("complete") is not True:
        raise ValueError("RNG snapshot is incomplete")
    result: dict[str, dict[str, Any]] = {}
    run_streams = snapshot.get("run_streams")
    if not isinstance(run_streams, dict) or not run_streams:
        raise ValueError("RNG snapshot has no run streams")
    for name, state in run_streams.items():
        result[f"run.{name}"] = _validated_state(state, f"run.{name}")
    players = snapshot.get("players")
    if not isinstance(players, list):
        raise ValueError("RNG snapshot players must be a list")
    for index, player in enumerate(players):
        if not isinstance(player, dict) or not isinstance(player.get("streams"), dict):
            raise ValueError(f"RNG snapshot player {index} is incomplete")
        player_id = str(player.get("net_id") or index)
        for name, state in player["streams"].items():
            result[f"player[{player_id}].{name}"] = _validated_state(
                state, f"player[{player_id}].{name}"
            )
    return result


def rng_counter_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, Any]]:
    left, right = flatten_rng_streams(before), flatten_rng_streams(after)
    if set(left) != set(right):
        raise ValueError(f"RNG stream set changed: before={sorted(left)}, after={sorted(right)}")
    return {
        name: {
            "before": left[name]["counter"],
            "after": right[name]["counter"],
            "delta": right[name]["counter"] - left[name]["counter"],
            "state_changed": any(left[name][field] != right[name][field] for field in RNG_FIELDS[1:]),
        }
        for name in sorted(left)
    }


def compare_rng_snapshots(client: dict[str, Any], shadow: dict[str, Any]) -> RngParityResult:
    try:
        left, right = flatten_rng_streams(client), flatten_rng_streams(shadow)
    except ValueError as exc:
        return RngParityResult("INCOMPLETE", [{"kind": "invalid_snapshot", "message": str(exc)}])
    differences: list[dict[str, Any]] = []
    if client.get("run_seed") != shadow.get("run_seed"):
        differences.append({
            "kind": "run_seed",
            "client": client.get("run_seed"),
            "shadow": shadow.get("run_seed"),
        })
    for name in sorted(set(left) | set(right)):
        if name not in left or name not in right:
            differences.append({"kind": "stream_set", "stream": name,
                                "client_present": name in left, "shadow_present": name in right})
            continue
        for field in RNG_FIELDS:
            if left[name][field] != right[name][field]:
                differences.append({"kind": "stream_field", "stream": name, "field": field,
                                    "client": left[name][field], "shadow": right[name][field]})
    return RngParityResult("PASS" if not differences else "FAIL", differences)


def compare_rng_transitions(
    client_before: dict[str, Any], client_after: dict[str, Any],
    shadow_before: dict[str, Any], shadow_after: dict[str, Any],
) -> dict[str, Any]:
    before = compare_rng_snapshots(client_before, shadow_before)
    after = compare_rng_snapshots(client_after, shadow_after)
    try:
        client_delta = rng_counter_delta(client_before, client_after)
        shadow_delta = rng_counter_delta(shadow_before, shadow_after)
    except ValueError as exc:
        return {"status": "INCOMPLETE", "before": before.status, "after": after.status,
                "differences": [{"kind": "delta", "message": str(exc)}]}
    delta_differences = []
    for name in sorted(set(client_delta) | set(shadow_delta)):
        c, h = client_delta.get(name), shadow_delta.get(name)
        if c != h:
            delta_differences.append({"stream": name, "client": c, "shadow": h})
    status = "PASS" if before.passed and after.passed and not delta_differences else "FAIL"
    return {
        "status": status,
        "before": before.status,
        "after": after.status,
        "client_delta": client_delta,
        "shadow_delta": shadow_delta,
        "differences": [*before.differences, *delta_differences, *after.differences],
    }


def _validated_state(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"RNG stream {label} is not an object")
    missing = [field for field in RNG_FIELDS if value.get(field) is None]
    if missing:
        raise ValueError(f"RNG stream {label} is missing {missing}")
    if type(value["counter"]) is not int:
        raise ValueError(f"RNG stream {label} counter is not an integer")
    return {field: value[field] for field in RNG_FIELDS}
