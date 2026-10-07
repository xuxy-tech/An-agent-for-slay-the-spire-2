from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def search_state(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("type") == "search_state_result":
        return result.get("combat_state_for_search") or {}
    return result


def run() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Fast Windows smoke test for the STS2 agent")
    parser.add_argument("--skip-engine", action="store_true", help="check Python and model only")
    parser.add_argument('--score-mode', choices=('preference', 'balanced'), default='preference')
    args = parser.parse_args()

    started = time.perf_counter()
    import numpy
    import scipy
    import sklearn
    import torch

    from cli.runtime_paths import resolve_dotnet
    from controller.branch_value_drafter import _predict_delta

    report: dict[str, Any] = {
        "platform": os.name,
        "python": sys.version.split()[0],
        "dependencies": {
            "numpy": numpy.__version__,
            "scipy": scipy.__version__,
            "scikit_learn": sklearn.__version__,
            "torch": torch.__version__,
        },
    }

    model_path = REPO / "models/branch_value.pt"
    check(model_path.is_file(), f"BranchValue checkpoint is missing: {model_path}")
    model_scores = _predict_delta(
        {
            "player": {"hp": 70, "max_hp": 80, "gold": 50},
            "context": {"floor": 5},
        },
        ["STRIKE_IRONCLAD"] * 5 + ["DEFEND_IRONCLAD"] * 4 + ["BASH"],
        ["ANGER", "SHRUG_IT_OFF", "INFLAME"],
        str(model_path),
    )
    check(model_scores is not None and len(model_scores) == 3, "BranchValue inference failed")
    report["branch_value_scores"] = [round(float(value), 6) for value in model_scores]

    if args.skip_engine:
        report["engine"] = "skipped"
        report["elapsed_s"] = round(time.perf_counter() - started, 3)
        return report

    dotnet = resolve_dotnet()
    runtimes = subprocess.run(
        [str(dotnet), "--list-runtimes"],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    ).stdout
    check("Microsoft.NETCore.App 9." in runtimes, ".NET 9 runtime is not installed")
    report["dotnet"] = str(dotnet)

    from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
    from controller.search.actions import action_signature
    from controller.search.combat_search import CombatSearcher, CombatSpec, CombatWorkerPool
    from controller.search.state_cache import hash_search_state

    cli = Sts2CliAdapter(CliConfig(repo_root=REPO))
    try:
        boot = cli.start()
        check(boot.get("type") != "error", f"CLI boot failed: {boot}")
        combat = cli.start_test_combat(encounter="CORPSE_SLUGS_WEAK", seed="42")
        check(combat.get("type") != "error", f"Test combat failed: {combat}")
        before_result = cli.get_search_state(timeout_s=20)
        before = search_state(before_result)
        check(bool((before.get("combat") or {}).get("available_actions")), "No legal combat actions")

        snapshot_id = "windows_smoke_root"
        captured = cli.capture_combat_snapshot(snapshot_id)
        check(captured.get("type") != "error", f"Snapshot capture failed: {captured}")
        restored = cli.restore_combat_snapshot(snapshot_id)
        check(restored.get("type") != "error", f"Snapshot restore failed: {restored}")
        after = search_state(cli.get_search_state(timeout_s=20))
        check(hash_search_state(before) == hash_search_state(after), "Snapshot restore changed search state")
    finally:
        cli.stop()

    serial = CombatSearcher(
        CliConfig(repo_root=REPO),
        CombatSpec(character="Ironclad", encounter="CORPSE_SLUGS_WEAK", seed="42"),
        reuse_cli_processes=True,
        score_mode=args.score_mode,
    )
    try:
        serial_result = serial.search(depth=1, chance_depth=0)
        check(bool(serial_result.sequence), "Serial depth-1 search returned no action")
    finally:
        serial.close()

    pool = CombatWorkerPool(CliConfig(repo_root=REPO))
    pool.prewarm(2)
    parallel = CombatSearcher(
        CliConfig(repo_root=REPO),
        CombatSpec(character="Ironclad", encounter="CORPSE_SLUGS_WEAK", seed="42"),
        parallel_top_level=True,
        max_workers=2,
        reuse_cli_processes=True,
        worker_pool=pool,
        score_mode=args.score_mode,
    )
    try:
        parallel_result = parallel.search(depth=1, chance_depth=0)
        check(bool(parallel_result.sequence), "Parallel depth-1 search returned no action")
        check(
            action_signature(serial_result.sequence[0]) == action_signature(parallel_result.sequence[0]),
            "Serial and parallel search selected different root actions",
        )
        check(
            hash_search_state(serial_result.leaf_state) == hash_search_state(parallel_result.leaf_state),
            "Serial and parallel search produced different leaf states",
        )
        report["search"] = {
            'score_mode': args.score_mode,
            "action": parallel_result.sequence[0].action_type,
            "score": round(float(parallel_result.score), 4),
            "serial_nodes": serial_result.stats.get("nodes"),
            "parallel_nodes": parallel_result.stats.get("nodes"),
            "parallel_root_coverage": (parallel_result.stats.get("root_coverage") or {}).get(
                "candidate_coverage_ratio"
            ),
        }
    finally:
        parallel.close()
        pool.close()

    report["engine"] = "ok"
    report["elapsed_s"] = round(time.perf_counter() - started, 3)
    return report


if __name__ == "__main__":
    try:
        print(json.dumps({"status": "ok", **run()}, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False, indent=2))
        raise
