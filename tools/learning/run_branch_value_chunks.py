#!/usr/bin/env python3
"""Run branch-value collection in per-presave subprocess chunks.

`collect_branch_value.py` is efficient when points are well-behaved, but one
presave can take tens of minutes at K=16/horizon=3. This wrapper isolates each
presave in its own process group, so a point-level timeout is reliable and the
next point can continue without inheriting a poisoned CLI worker.
"""
from __future__ import annotations

import argparse
import glob as globmod
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Set

REPO = Path(__file__).resolve().parents[2]


def _paths_from_args(presaves: List[str], presave_file: str | None) -> List[str]:
    paths: List[str] = []
    if presave_file:
        paths.extend([ln.strip() for ln in Path(presave_file).read_text().splitlines() if ln.strip()])
    for pat in presaves or []:
        paths.extend(sorted(globmod.glob(pat)) if any(c in pat for c in "*?[") else [pat])
    return paths


def _done_presaves(out: Path, *, retry_errors: bool) -> Set[str]:
    done: Set[str] = set()
    if not out.exists():
        return done
    for line in out.open():
        try:
            row = json.loads(line)
        except Exception:
            continue
        presave = row.get("presave")
        if not presave:
            continue
        if retry_errors and row.get("error") and not row.get("card"):
            continue
        done.add(str(presave))
    return done


def _append_error(out: Path, row: Dict[str, Any]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.communicate(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--presaves", nargs="*", default=None)
    ap.add_argument("--presave-file", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit-points", type=int, default=None)
    ap.add_argument("--point-timeout-s", type=float, default=1800.0)
    ap.add_argument("--retry-errors", action="store_true")
    ap.add_argument("--record-point-errors", action="store_true")
    ap.add_argument("--horizon", type=int, default=3)
    ap.add_argument("--rollouts", type=int, default=16)
    ap.add_argument("--shuffle-base", type=int, default=9000)
    ap.add_argument("--depth", type=int, default=10)
    ap.add_argument("--chance-depth", type=int, default=1)
    ap.add_argument("--score-mode", default="balanced")
    args = ap.parse_args()

    paths = _paths_from_args(args.presaves or [], args.presave_file)
    if not paths:
        raise SystemExit("provide --presaves or --presave-file")

    out = Path(args.out)
    done = _done_presaves(out, retry_errors=args.retry_errors)
    attempted = 0
    produced = 0

    for p in paths:
        name = Path(p).name
        if name in done:
            continue
        if args.limit_points is not None and attempted >= int(args.limit_points):
            print(json.dumps({"limit_reached": int(args.limit_points),
                              "attempted": attempted, "produced": produced}, ensure_ascii=False), flush=True)
            break

        attempted += 1
        t0 = time.perf_counter()
        cmd = [
            sys.executable, "-u", "-m", "tools.learning.collect_branch_value",
            "--presaves", p,
            "--horizon", str(args.horizon),
            "--rollouts", str(args.rollouts),
            "--shuffle-base", str(args.shuffle_base),
            "--depth", str(args.depth),
            "--chance-depth", str(args.chance_depth),
            "--score-mode", args.score_mode,
            "--limit-points", "1",
            "--quiet-skips",
            "--out", str(out),
        ]
        proc = subprocess.Popen(
            cmd,
            cwd=REPO,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=float(args.point_timeout_s))
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            secs = round(time.perf_counter() - t0, 1)
            row = {
                "presave": name,
                "error": f"point_timeout after {float(args.point_timeout_s):.1f}s",
                "horizon": args.horizon,
                "K": args.rollouts,
                "depth": args.depth,
                "chance_depth": args.chance_depth,
                "score_mode": args.score_mode,
                "secs": secs,
            }
            _append_error(out, row)
            print(json.dumps({"point_timeout": name, "secs": secs}, ensure_ascii=False), flush=True)
            continue

        if stdout:
            print(stdout, end="" if stdout.endswith("\n") else "\n", flush=True)
            produced += sum(1 for line in stdout.splitlines() if '"card"' in line and '"mean_delta"' in line)
        if stderr:
            print(json.dumps({"point_stderr": name, "stderr": stderr[-2000:]}, ensure_ascii=False), flush=True)
        if proc.returncode != 0:
            secs = round(time.perf_counter() - t0, 1)
            print(json.dumps({"point_returncode": name, "returncode": proc.returncode,
                              "secs": secs}, ensure_ascii=False), flush=True)
            if args.record_point_errors:
                _append_error(out, {
                    "presave": name,
                    "error": f"returncode {proc.returncode}",
                    "horizon": args.horizon,
                    "K": args.rollouts,
                    "depth": args.depth,
                    "chance_depth": args.chance_depth,
                    "score_mode": args.score_mode,
                    "secs": secs,
                })

    print(json.dumps({"DONE": {"attempted": attempted, "produced_stdout_rows": produced,
                              "out": str(out)}}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
