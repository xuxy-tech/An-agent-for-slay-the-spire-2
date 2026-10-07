from __future__ import annotations

import argparse
import json
from pathlib import Path

from cli.sts2_mod_adapter import ModClientConfig, Sts2ModAdapter
from controller.human_capture import collect_human_actions


def main() -> None:
    parser = argparse.ArgumentParser(description="Record real human STS2 actions without running the headless agent")
    parser.add_argument("--observer-url", default="http://127.0.0.1:8080")
    parser.add_argument("--capture-url", default="http://127.0.0.1:9878")
    parser.add_argument("--output", type=Path, default=Path("data/human_play/raw"))
    parser.add_argument("--poll-ms", type=float, default=50)
    parser.add_argument("--settle-timeout", type=float, default=20)
    parser.add_argument("--attribution-grace-ms", type=float, default=500)
    parser.add_argument("--max-actions", type=int)
    parser.add_argument("--duration", type=float, help="stop after this many seconds")
    parser.add_argument("--stop-file", type=Path, help="JSON control file used for a graceful stop")
    args = parser.parse_args()
    def stop_requested() -> bool:
        if args.stop_file is None or not args.stop_file.is_file():
            return False
        try:
            value = json.loads(args.stop_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return value.get("desired") == "stop"
    mod = Sts2ModAdapter(ModClientConfig(
        base_url=args.observer_url,
        capture_base_url=args.capture_url,
        timeout_s=args.settle_timeout,
    ))
    directory = collect_human_actions(
        mod, args.output, poll_interval_s=args.poll_ms / 1000.0,
        settle_timeout_s=args.settle_timeout,
        attribution_grace_s=args.attribution_grace_ms / 1000.0,
        max_decisions=args.max_actions,
        duration_s=args.duration,
        stop_requested=stop_requested if args.stop_file else None,
    )
    print(directory.resolve())


if __name__ == "__main__":
    main()
