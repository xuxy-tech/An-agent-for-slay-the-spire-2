from __future__ import annotations

import argparse
import json
from pathlib import Path

from cli.sts2_mod_adapter import ModClientConfig, Sts2ModAdapter
from controller.live_client_bridge import JsonlSessionLog, LiveClientBridge, client_state_digest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only connectivity and completeness check for the visible STS2 client"
    )
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--log", type=Path)
    args = parser.parse_args()

    mod = Sts2ModAdapter(ModClientConfig(base_url=args.url, timeout_s=args.timeout))
    bridge = LiveClientBridge(mod, JsonlSessionLog(args.log) if args.log else None)
    health = mod.health()
    state = bridge.observe()
    actions = mod.available_actions()
    print(
        json.dumps(
            {
                "status": "ok",
                "health": health,
                "screen": state["screen"],
                "run_id": state["run_id"],
                "available_actions": [item.get("name") for item in actions["actions"]],
                "client_digest": client_state_digest(state),
                "transport_ms": round(mod.last_call_ms, 3),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
