"""Generate headless counterfactual candidates from v4 human snapshots."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from controller.human_capture import AUTHORITATIVE_SNAPSHOT_SCHEMA, CAPTURE_PROTOCOL, validate_authoritative_snapshot


def _jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _sessions(root: Path, excluded: list | None = None) -> list[Path]:
    result = []
    candidates = [root] if (root / "manifest.json").is_file() else sorted(root.glob("human_*"))
    for session in candidates:
        manifest_path = session / "manifest.json"
        events_path = session / "events.jsonl"
        if not manifest_path.is_file() or not events_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("status") == "failed":
            if excluded is not None:
                failures = [row for row in _jsonl(events_path) if row.get("record_type") == "capture_snapshot_failure"]
                excluded.append({"session": session.name, "reason": "capture_failed",
                                 "last_failure": failures[-1] if failures else None})
            continue
        if (manifest.get("capture_health") or {}).get("protocol_version") != CAPTURE_PROTOCOL:
            continue
        if (manifest.get("capture_health") or {}).get("snapshot_schema") != AUTHORITATIVE_SNAPSHOT_SCHEMA:
            continue
        result.append(session)
    return result


def _decision_rows(session: Path) -> list[dict[str, Any]]:
    result = []
    seen_snapshots: set[str] = set()
    for row in _jsonl(session / "events.jsonl"):
        action = row.get("action") or {}
        observation = row.get("observation_before") or {}
        snapshot = row.get("authoritative_snapshot") or {}
        if row.get("record_type") != "decision" or row.get("settlement") != "settled":
            continue
        if observation.get("in_combat") is not True:
            continue
        if action.get("type") not in {"play_card", "end_turn"}:
            continue
        if snapshot.get("status") != "complete" or not snapshot.get("snapshot_id"):
            continue
        snapshot_id = str(snapshot["snapshot_id"])
        if snapshot_id in seen_snapshots:
            continue
        seen_snapshots.add(snapshot_id)
        result.append(row)
    return result


def _load_snapshot(session: Path, row: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    metadata = row["authoritative_snapshot"]
    snapshot_id = str(metadata["snapshot_id"])
    path = session / "snapshots" / f"{snapshot_id}.json"
    raw = path.read_text(encoding="utf-8")
    payload = {
        "snapshot_json": raw,
        "schema": metadata.get("schema"),
        "sha256": metadata.get("sha256"),
        "bytes": metadata.get("bytes"),
        "snapshot_id": snapshot_id,
    }
    snapshot, _raw, digest = validate_authoritative_snapshot(payload, metadata)
    return snapshot_id, snapshot, digest


def generate(args: argparse.Namespace) -> dict[str, Any]:
    from scripts.generate_turn_preferences import generate as generate_turns
    return generate_turns(args)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--human-input", type=Path, default=Path("data/human_play/raw"))
    parser.add_argument("--output", type=Path, default=Path("data/human_play/leaf/current_turns.jsonl"))
    parser.add_argument("--dll", type=Path, default=Path("third_party/sts2-cli/src/Sts2Headless/bin/Release/net9.0/Sts2Headless.dll"))
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="limit the number of combat roots for a smoke test")
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--max-nodes", type=int, default=2500)
    parser.add_argument("--max-seconds", type=float, default=60)
    parser.add_argument("--max-actions", type=int, default=64)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    result = generate(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("success"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
