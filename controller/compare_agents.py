"""Paired ablation comparison for the ACT1 agent.

Runs the agent over the SAME seed set under two configs that differ on exactly
ONE axis (combat policy OR global policy OR score mode), then reports a PAIRED
comparison. Pairing on common seeds removes per-seed luck variance, so a modest
floor gain becomes statistically legible.

Reads the `run_summary` JSON line emitted by run_agent (last_floor,
total_hp_loss, per_floor_hp_loss, final_deck, outcome).

Usage (compare current search vs the random combat lower bound, same global):
  python3 -m controller.compare_agents \
    --seeds 1 4 7 13 21 42 52 99 \
    --axis combat-policy --a search --b random \
    --timeout-s 240 --max-workers 6

The non-varied axes stay fixed (use --global-policy / --combat-policy / --score-mode
to pin the shared baseline), so exactly one thing changes between A and B.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _run_one(repo_root: Path, seed: str, overrides: Dict[str, str],
             base: Dict[str, str], max_steps: int, timeout_s: int,
             depth: int = 10, chance_depth: int = 1) -> Dict[str, Any]:
    cmd = [
        sys.executable, "-u", "-m", "controller.run_agent",
        "--character", "Ironclad",
        "--seed", str(seed),
        "--depth", str(depth), "--chance-depth", str(chance_depth),
        "--reuse-cli-processes",
        "--max-steps", str(max_steps),
    ]
    merged = {**base, **overrides}
    for flag, val in merged.items():
        cmd += [f"--{flag}", str(val)]
    start = time.time()
    summary: Optional[Dict[str, Any]] = None
    timed_out = False
    stderr_tail: List[str] = []
    try:
        proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, timeout=timeout_s)
        stdout_text, stderr_text = proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout_text = exc.stdout or ""
        stderr_text = exc.stderr or ""
        if isinstance(stdout_text, bytes):
            stdout_text = stdout_text.decode("utf-8", "replace")
        if isinstance(stderr_text, bytes):
            stderr_text = stderr_text.decode("utf-8", "replace")
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("run_summary"):
            summary = obj
    stderr_tail = (stderr_text or "").splitlines()[-8:]
    return {
        "seed": str(seed),
        "timed_out": timed_out,
        "elapsed_s": round(time.time() - start, 2),
        "summary": summary,
        "stderr_tail": stderr_tail,
    }


def _floor(res: Dict[str, Any]) -> Optional[int]:
    s = res.get("summary") or {}
    f = s.get("last_floor")
    return int(f) if isinstance(f, (int, float)) else None


def _paired_stats(label_a: str, label_b: str,
                  a_by_seed: Dict[str, Dict[str, Any]],
                  b_by_seed: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Paired comparison on common seeds where BOTH runs produced a floor."""
    diffs: List[Tuple[str, int, int, int]] = []  # (seed, a_floor, b_floor, a-b)
    for seed in sorted(set(a_by_seed) & set(b_by_seed), key=lambda s: int(s) if s.isdigit() else s):
        fa, fb = _floor(a_by_seed[seed]), _floor(b_by_seed[seed])
        if fa is None or fb is None:
            continue
        diffs.append((seed, fa, fb, fa - fb))
    if not diffs:
        return {"paired_seeds": 0, "note": "no comparable seeds (missing floors)"}
    deltas = [d[3] for d in diffs]
    a_floors = [d[1] for d in diffs]
    b_floors = [d[2] for d in diffs]
    a_wins = sum(1 for d in deltas if d > 0)
    b_wins = sum(1 for d in deltas if d < 0)
    ties = sum(1 for d in deltas if d == 0)
    mean_delta = statistics.mean(deltas)
    # paired t-like signal: mean / standard error. Reported as a rough effect
    # size, not a p-value — sample sizes here are small and floors are discrete.
    if len(deltas) > 1 and statistics.pstdev(deltas) > 0:
        se = statistics.stdev(deltas) / (len(deltas) ** 0.5)
        t_stat = mean_delta / se if se > 0 else None
    else:
        t_stat = None
    return {
        "label_a": label_a,
        "label_b": label_b,
        "paired_seeds": len(diffs),
        "avg_floor_a": round(statistics.mean(a_floors), 3),
        "avg_floor_b": round(statistics.mean(b_floors), 3),
        "mean_floor_delta_a_minus_b": round(mean_delta, 3),
        "a_wins": a_wins,
        "b_wins": b_wins,
        "ties": ties,
        "paired_t_stat": round(t_stat, 3) if t_stat is not None else None,
        "per_seed": [
            {"seed": s, "floor_a": fa, "floor_b": fb, "delta": d} for (s, fa, fb, d) in diffs
        ],
    }


_AXES = {"combat-policy": "combat-policy", "global-policy": "global-policy", "score-mode": "score-mode"}


def _run_config(repo_root: Path, seeds: List[str], overrides: Dict[str, str],
                base: Dict[str, str], max_steps: int, timeout_s: int,
                max_workers: int, depth: int = 10, chance_depth: int = 1) -> Dict[str, Dict[str, Any]]:
    workers = max(1, min(max_workers, len(seeds)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            seed: pool.submit(_run_one, repo_root, seed, overrides, base, max_steps, timeout_s, depth, chance_depth)
            for seed in seeds
        }
        return {seed: fut.result() for seed, fut in futures.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description="Paired ablation comparison for the ACT1 agent")
    parser.add_argument("--seeds", nargs="+", required=True)
    parser.add_argument("--axis", required=True, choices=sorted(_AXES),
                        help="which single axis differs between A and B")
    parser.add_argument("--a", required=True, help="value of --axis for config A")
    parser.add_argument("--b", required=True, help="value of --axis for config B")
    # shared baseline for the non-varied axes
    parser.add_argument("--combat-policy", default="search")
    parser.add_argument("--global-policy", default="full")
    parser.add_argument("--score-mode", default="preference")
    parser.add_argument("--max-steps", type=int, default=160)
    parser.add_argument("--timeout-s", type=int, default=240)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--depth", type=int, default=10,
                        help="combat search depth for both A and B (default 10 = "
                        "production full-strength; lower for cheap fast iteration)")
    parser.add_argument("--chance-depth", type=int, default=1)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    axis = _AXES[args.axis]
    base = {
        "combat-policy": args.combat_policy,
        "global-policy": args.global_policy,
        "score-mode": args.score_mode,
    }
    # A and B differ ONLY on the chosen axis; everything else stays at `base`.
    label_a, label_b = f"{args.axis}={args.a}", f"{args.axis}={args.b}"
    res_a = _run_config(repo_root, args.seeds, {axis: args.a}, base, args.max_steps, args.timeout_s, args.max_workers, args.depth, args.chance_depth)
    res_b = _run_config(repo_root, args.seeds, {axis: args.b}, base, args.max_steps, args.timeout_s, args.max_workers, args.depth, args.chance_depth)

    stats = _paired_stats(label_a, label_b, res_a, res_b)
    print(json.dumps({
        "axis": args.axis,
        "depth": args.depth,
        "shared_baseline": {k: v for k, v in base.items() if k != axis},
        "paired_stats": stats,
        "raw_a": res_a,
        "raw_b": res_b,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
