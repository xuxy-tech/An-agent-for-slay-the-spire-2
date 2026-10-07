from __future__ import annotations

import argparse
import json
from pathlib import Path

from controller.human_capture import audit_capture_session


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit one real-client human capture session")
    parser.add_argument("--input", type=Path, required=True, help="session directory or events.jsonl")
    parser.add_argument(
        "--require", action="append", default=[], dest="required_cases",
        help=("required settled action, source:<name>, use_potion:manual_target, "
              "or use_potion:no_manual_target"),
    )
    parser.add_argument("--output", type=Path, help="optional JSON report path")
    args = parser.parse_args()
    events_path = args.input / "events.jsonl" if args.input.is_dir() else args.input
    report = audit_capture_session(events_path, args.required_cases)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8", newline="\n")
    print(text, end="")
    if not report["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
