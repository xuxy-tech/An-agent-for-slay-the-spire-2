"""Summarize client-authoritative reconciliation segments from live reports.

Read-only diagnostic tool.  It consumes run_report.json and session.jsonl
files and never modifies the recorded run.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def session_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def action_label(event: dict[str, Any]) -> str:
    action = event.get("action") or event.get("client_action") or event.get("name")
    tx = event.get("transaction") or {}
    client = tx.get("client") if isinstance(tx, dict) else None
    if not action and isinstance(client, dict):
        action = client.get("action")
    return str(action or event.get("event") or "?")


def summarize(report_path: Path) -> list[dict[str, Any]]:
    report = load_json(report_path)
    session_path = report_path.with_name("session.jsonl")
    events = session_events(session_path)
    actions = [e for e in events if e.get("event") == "action" or e.get("action")]
    output: list[dict[str, Any]] = []
    for segment in report.get("client_only_segments") or []:
        sid = segment.get("segment_id")
        start = segment.get("start_sequence")
        end = segment.get("end_sequence")
        rows = []
        for event in actions:
            seq = event.get("sequence")
            if isinstance(seq, int) and isinstance(start, int) and seq >= start:
                if isinstance(end, int) and seq > end:
                    continue
                rows.append(event)
        reanchors = [e for e in events if e.get("event") in {
            "pre_action_map_reanchor_started",
            "client_only_segment_started",
            "client_only_segment_verified",
        } and e.get("segment_id") == sid]
        mirrored_after = []
        if isinstance(end, int):
            for event in actions:
                seq = event.get("sequence")
                if isinstance(seq, int) and seq > end:
                    tx = event.get("transaction") or {}
                    if isinstance(tx, dict) and tx.get("shadow"):
                        mirrored_after.append(event)
                        break
        output.append({
            "segment_id": sid,
            "status": segment.get("status"),
            "reason": segment.get("reason"),
            "boundary": segment.get("reanchor_boundary", "map"),
            "start_sequence": start,
            "end_sequence": end,
            "segment_actions": [action_label(e) for e in rows],
            "reanchor_events": len(reanchors),
            "first_mirrored_after": action_label(mirrored_after[0]) if mirrored_after else None,
        })
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default="logs/live_dashboard",
                        help="log root or one run directory")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="emit machine-readable JSON")
    args = parser.parse_args()
    root = Path(args.path)
    reports = [root / "run_report.json"] if (root / "run_report.json").exists() else sorted(root.glob("*/run_report.json"))
    all_rows: list[dict[str, Any]] = []
    for report in reports:
        for row in summarize(report):
            row["run"] = report.parent.name
            all_rows.append(row)
    if args.as_json:
        print(json.dumps(all_rows, ensure_ascii=False, indent=2))
        return 0
    print(f"runs={len(reports)} segments={len(all_rows)}")
    counts = Counter((r["status"], r["reason"]) for r in all_rows)
    for (status, reason), count in counts.most_common():
        print(f"{count:3d}  {status or '?':18s}  {reason or '?'}")
    for row in all_rows:
        print(
            f"{row['run']} seg={row['segment_id']} status={row['status']} "
            f"boundary={row['boundary']} reason={row['reason']} "
            f"reanchors={row['reanchor_events']} actions={','.join(row['segment_actions']) or '-'} "
            f"first_mirrored_after={row['first_mirrored_after'] or '-'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
