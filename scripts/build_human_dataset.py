from __future__ import annotations

import argparse
import json
from pathlib import Path

from controller.human_capture import build_dataset, current_capture_event_paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Build model-independent training examples from human capture JSONL")
    parser.add_argument("--input", type=Path, default=Path("data/human_play/raw"))
    parser.add_argument("--output", type=Path, default=Path("data/human_play/datasets/latest"))
    parser.add_argument(
        "--allow-unaudited", action="store_true",
        help="allow synthetic or legacy sessions that fail the current live-capture integrity gate",
    )
    args = parser.parse_args()
    paths = (
        sorted(args.input.glob("*/events.jsonl"))
        if args.allow_unaudited else current_capture_event_paths(args.input)
    )
    if not paths:
        qualifier = "current-protocol " if not args.allow_unaudited else ""
        raise SystemExit(f"No {qualifier}events.jsonl files found below {args.input}")
    print(json.dumps(build_dataset(
        paths, args.output, require_integrity=not args.allow_unaudited
    ), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
