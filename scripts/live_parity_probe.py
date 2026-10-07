from __future__ import annotations

import argparse
import json
from pathlib import Path

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from cli.sts2_mod_adapter import ModClientConfig, Sts2ModAdapter
from controller.engine_parity import (
    client_map_checkpoint,
    compare_checkpoints,
    headless_map_checkpoint,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare a visible client map checkpoint with an official current_run.save"
    )
    parser.add_argument("--save", type=Path, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    mod = Sts2ModAdapter(ModClientConfig(base_url=args.url))
    client_state = mod.state()

    cli = Sts2CliAdapter(CliConfig(repo_root=repo_root))
    cli.start()
    try:
        headless_state = cli.load_save(str(args.save.resolve()), lang="en")
        if headless_state.get("type") == "error":
            raise RuntimeError(f"Headless save load failed: {headless_state}")
        headless_map = cli.get_map()
        comparison = compare_checkpoints(
            client_map_checkpoint(client_state),
            headless_map_checkpoint(
                headless_state,
                headless_map,
                run_id=str(client_state.get("run_id") or ""),
            ),
        )
        report = {
            "status": comparison.status,
            "checkpoint": "map_select",
            "client_digest": comparison.client_digest,
            "headless_digest": comparison.headless_digest,
            "difference_count": len(comparison.differences),
            "differences": comparison.differences,
        }
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if comparison.status != "PASS":
            raise SystemExit(2)
    finally:
        cli.stop()


if __name__ == "__main__":
    main()
