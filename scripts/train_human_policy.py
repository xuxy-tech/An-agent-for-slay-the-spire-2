from __future__ import annotations

import argparse
import json
from pathlib import Path

from controller.human_training import load_training_backend, run_training


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a replaceable trainer over a human-play dataset")
    parser.add_argument("--dataset", type=Path, default=Path("data/human_play/datasets/latest"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--backend", default="frequency",
        help="built-in frequency or an external module:factory",
    )
    parser.add_argument("--backend-config", type=Path, help="JSON object passed to the backend factory")
    args = parser.parse_args()
    config = (
        json.loads(args.backend_config.read_text(encoding="utf-8"))
        if args.backend_config else {}
    )
    if not isinstance(config, dict):
        raise SystemExit("--backend-config must contain a JSON object")
    backend = load_training_backend(args.backend, config)
    print(json.dumps(run_training(args.dataset, args.output, backend), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
