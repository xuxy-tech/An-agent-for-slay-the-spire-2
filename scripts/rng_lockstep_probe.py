from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from cli.sts2_mod_adapter import ModClientConfig, Sts2ModAdapter
from controller.rng_parity import compare_rng_snapshots


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare visible-client and headless RNG state without taking an action")
    parser.add_argument("--save", type=Path, required=True)
    parser.add_argument("--resume-room", action="store_true")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--rng-url", default="http://127.0.0.1:9877")
    args = parser.parse_args()
    mod = Sts2ModAdapter(ModClientConfig(base_url=args.url, rng_base_url=args.rng_url))
    cli = Sts2CliAdapter(CliConfig(ROOT))
    try:
        mod_health = mod.health()
        bridge_health = mod.rng_health()
        identity = mod.rng_identity()
        client = mod.rng_snapshot()
        cli.start()
        loaded = cli.load_save(str(args.save.resolve()), resume_room=args.resume_room)
        if loaded.get("type") == "error":
            raise RuntimeError(f"Headless load failed: {loaded}")
        shadow = cli.get_rng_snapshot()
        result = compare_rng_snapshots(client, shadow)
        print(json.dumps({
            "status": result.status,
            "differences": result.differences,
            "client_digest": client.get("digest_sha256"),
            "shadow_digest": shadow.get("digest_sha256"),
            "mod_health": mod_health,
            "rng_bridge_health": bridge_health,
            "identity": identity,
            "headless_decision": loaded.get("decision"),
        }, ensure_ascii=False, indent=2))
        return 0 if result.passed else 2
    finally:
        cli.stop()


if __name__ == "__main__":
    raise SystemExit(main())
