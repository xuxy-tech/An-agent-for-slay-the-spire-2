from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from controller.human_capture import audit_capture_session, build_dataset
from controller.human_training import load_training_backend, run_training

CORE_CASES = (
    "play_card",
    "end_turn",
    "use_potion:no_manual_target",
    "use_potion:manual_target",
    "source:player_choice",
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the real-session capture, dataset, and training acceptance chain"
    )
    parser.add_argument("--input", type=Path, required=True, help="session directory or events.jsonl")
    parser.add_argument("--output", type=Path, default=Path("data/human_play/acceptance"))
    parser.add_argument(
        "--require", action="append", dest="required_cases",
        help="override the default core case list; repeat for multiple cases",
    )
    parser.add_argument("--backend", default="frequency")
    parser.add_argument("--backend-config", type=Path)
    args = parser.parse_args()

    events_path = args.input / "events.jsonl" if args.input.is_dir() else args.input
    required = args.required_cases if args.required_cases is not None else list(CORE_CASES)
    audit = audit_capture_session(events_path, required)
    if not audit["ok"]:
        print(json.dumps({"audit": audit}, ensure_ascii=False, indent=2))
        raise SystemExit(1)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = args.output / f"acceptance_{timestamp}"
    dataset_dir = run_dir / "dataset"
    training_dir = run_dir / "training"
    dataset_manifest = build_dataset(
        [events_path], dataset_dir, require_integrity=True
    )
    config = (
        json.loads(args.backend_config.read_text(encoding="utf-8"))
        if args.backend_config else {}
    )
    if not isinstance(config, dict):
        raise SystemExit("--backend-config must contain a JSON object")
    training = run_training(
        dataset_dir, training_dir, load_training_backend(args.backend, config)
    )
    report = {
        "schema": "sts2.human_pipeline.acceptance.v1",
        "audit": audit,
        "dataset": dataset_manifest,
        "training": training,
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "acceptance_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
