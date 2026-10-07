from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from cli.sts2_mod_adapter import ModClientConfig, Sts2ModAdapter
from controller.engine_parity import (
    client_combat_checkpoint,
    client_map_checkpoint,
    compare_checkpoints,
    headless_combat_checkpoint,
    headless_map_checkpoint,
)
from controller.live_client_bridge import JsonlSessionLog, LiveClientBridge
from controller.run_agent import choose_map_route_global


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Choose one map node visibly and compare the first combat checkpoint"
    )
    parser.add_argument("--save", type=Path, required=True)
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--visible-delay-ms", type=float, default=700.0)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    args.session_dir.mkdir(parents=True, exist_ok=True)
    immutable_save = args.session_dir / "checkpoint_map.save"
    shutil.copy2(args.save, immutable_save)

    mod = Sts2ModAdapter(ModClientConfig(base_url=args.url))
    log = JsonlSessionLog(args.session_dir / "session.jsonl")
    live = LiveClientBridge(mod, log, args.visible_delay_ms)
    client_before = live.observe()

    cli = Sts2CliAdapter(CliConfig(repo_root=repo_root))
    cli.start()
    try:
        headless_before = cli.load_save(str(immutable_save.resolve()), lang="en")
        headless_map = cli.get_map()
        decision_started = time.perf_counter()
        choice, route = choose_map_route_global(headless_before, headless_map)
        decision_ms = (time.perf_counter() - decision_started) * 1000.0
        telemetry = {
            "policy": "existing_global_route_heuristic",
            "decision_ms": round(decision_ms, 3),
            "route": route,
        }
        if client_before.get("screen") == "MAP":
            map_comparison = compare_checkpoints(
                client_map_checkpoint(client_before),
                headless_map_checkpoint(
                    headless_before,
                    headless_map,
                    run_id=str(client_before["run_id"]),
                ),
            )
            _record_comparison(log, 0, map_comparison)
            if map_comparison.status != "PASS":
                raise RuntimeError(f"Map parity failed: {map_comparison.differences[:5]}")
            client_after = live.execute_headless_action(
                "map_select",
                "select_map_node",
                choice,
                expected_client_state=client_before,
                decision_telemetry=telemetry,
            )
        elif client_before.get("screen") == "COMBAT":
            # Recovery path for a probe interrupted after the visible client
            # entered combat but before its first combat comparison was written.
            map_comparison = compare_checkpoints(
                headless_map_checkpoint(
                    headless_before,
                    headless_map,
                    run_id=str(client_before["run_id"]),
                ),
                headless_map_checkpoint(
                    headless_before,
                    headless_map,
                    run_id=str(client_before["run_id"]),
                ),
            )
            client_after = client_before
        else:
            raise RuntimeError(
                f"Expected visible client at MAP or COMBAT, got {client_before.get('screen')!r}"
            )
        headless_after = cli.action("select_map_node", choice)
        if headless_after.get("type") == "error":
            raise RuntimeError(f"Headless map action failed: {headless_after}")
        search_result = cli.get_search_state(timeout_s=10.0)
        search_state = search_result.get("combat_state_for_search") or {}
        if not search_state:
            raise RuntimeError(f"Headless combat state is incomplete: {search_result}")

        combat_comparison = compare_checkpoints(
            client_combat_checkpoint(client_after),
            headless_combat_checkpoint(
                headless_after,
                search_state,
                run_id=str(client_after["run_id"]),
            ),
        )
        _record_comparison(log, 1, combat_comparison)
        if combat_comparison.status == "PASS":
            live.pace_after_verification(verification='combat_entry_checkpoint')
        report = {
            "status": combat_comparison.status,
            "map": {
                "status": map_comparison.status,
                "digest": map_comparison.client_digest,
            },
            "selected_node": choice,
            "decision_ms": round(decision_ms, 3),
            "combat": {
                "status": combat_comparison.status,
                "client_digest": combat_comparison.client_digest,
                "headless_digest": combat_comparison.headless_digest,
                "difference_count": len(combat_comparison.differences),
                "differences": combat_comparison.differences,
            },
        }
        (args.session_dir / "enter_combat_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if combat_comparison.status != "PASS":
            raise SystemExit(2)
    finally:
        cli.stop()


def _record_comparison(log: JsonlSessionLog, sequence: int, result: object) -> None:
    log.write(
        {
            "event": "parity_checkpoint",
            "sequence": sequence,
            "status": result.status,
            "client_digest": result.client_digest,
            "headless_digest": result.headless_digest,
            "difference_count": len(result.differences),
            "differences": result.differences,
        }
    )


if __name__ == "__main__":
    main()
