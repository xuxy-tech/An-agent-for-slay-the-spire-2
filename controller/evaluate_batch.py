from __future__ import annotations

import argparse
import collections
from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def summarize_events(lines: List[Dict[str, Any]]) -> Dict[str, Any]:
    last = lines[-1] if lines else {}
    last_context = last.get("context") or {}
    reward_cards = [
        (event.get("reward_choice") or {}).get("card_id")
        for event in lines
        if event.get("reward_choice")
        and (event.get("reward_choice") or {}).get("action") == "select_card_reward"
    ]
    shop_buys = [
        {
            "item_id": (event.get("shop_choice") or {}).get("item_id"),
            "floor": event.get("floor"),
        }
        for event in lines
        if event.get("shop_choice")
        and (event.get("shop_choice") or {}).get("action", "").startswith("buy_")
    ]
    rest_choices = [
        {
            "floor": event.get("floor"),
            "payload": event.get("rest_choice"),
        }
        for event in lines
        if event.get("rest_choice") is not None
    ]
    combats = [
        {
            "floor": event.get("floor"),
            "encounter_id": (event.get("combat_start") or {}).get("encounter_id"),
            "summary": (event.get("combat_start") or {}).get("summary"),
        }
        for event in lines
        if event.get("combat_start")
    ]
    combat_actions = [
        {
            "step_id": event.get("step_id"),
            "floor": event.get("floor"),
            "encounter_id": (event.get("combat_action") or {}).get("encounter_id"),
            "search_wall_ms": (event.get("combat_action") or {}).get("search_wall_ms"),
            "replay_calls": (event.get("combat_action") or {}).get("replay_calls"),
            "replay_total_ms": (event.get("combat_action") or {}).get("replay_total_ms"),
            "nodes": (event.get("combat_action") or {}).get("nodes"),
            "parallel_top_level": (event.get("combat_action") or {}).get("parallel_top_level"),
            "chosen": (event.get("combat_action") or {}).get("chosen"),
        }
        for event in lines
        if event.get("combat_action")
    ]
    combat_actions_sorted = sorted(
        combat_actions,
        key=lambda item: float(item.get("search_wall_ms") or 0.0),
        reverse=True,
    )
    terminal = next((event for event in reversed(lines) if event.get("terminal")), None)
    last_combat_action = next((event for event in reversed(lines) if event.get("combat_action")), None)
    final_decision = last.get("decision")
    terminal_payload = terminal.get("terminal") if terminal else None
    terminal_player = (terminal_payload or {}).get("player") or {}
    return {
        "steps": len(lines),
        "last_floor": last.get("floor") or last_context.get("floor"),
        "last_decision": final_decision,
        "terminal": terminal_payload,
        "terminal_player": {
            "hp": terminal_player.get("hp"),
            "max_hp": terminal_player.get("max_hp"),
            "gold": terminal_player.get("gold"),
            "deck_size": terminal_player.get("deck_size"),
        } if terminal_player else None,
        "reward_cards": [card for card in reward_cards if card],
        "shop_buys": shop_buys,
        "rest_choices": rest_choices,
        "combats_seen": combats,
        "slowest_combat_actions": combat_actions_sorted[:8],
        "last_combat_action": {
            "floor": last_combat_action.get("floor"),
            "encounter_id": (last_combat_action.get("combat_action") or {}).get("encounter_id"),
            "chosen": (last_combat_action.get("combat_action") or {}).get("chosen"),
            "score": (last_combat_action.get("combat_action") or {}).get("score"),
        } if last_combat_action else None,
    }


ACT_AWARE_SUMMARY_FIELDS = [
    "max_act",
    "acts_cleared",
    "last_act",
    "last_act_floor",
    "last_position",
    "combat_policy",
    "global_policy",
    "policy_seed",
]


def _copy_run_summary_fields(summary: Dict[str, Any], run_summary: Dict[str, Any]) -> None:
    """Copy authoritative run-level fields emitted by controller.run_agent.

    Older logs may not contain these fields, so callers must tolerate missing
    values. Keep the copy explicit so downstream report code can depend on a
    stable summary shape without treating legacy runs as malformed.
    """
    for field in ACT_AWARE_SUMMARY_FIELDS:
        summary[field] = run_summary.get(field, "unknown")


def _as_int_or_none(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def run_seed(repo_root: Path, seed: str, score_mode: str, max_steps: int, timeout_s: int,
             depth: int = 10, chance_depth: int = 1, worker_pool: bool = False,
             log_dir: Path | None = None, combat_policy: str = "search",
             global_policy: str = "full", draft_policy: str = "heuristic",
             draft_tau: float = 0.05, draft_model: str | None = None,
             capture_presave: str | None = None,
             draft_heuristic_reward_count: int = 0) -> Dict[str, Any]:
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "controller.run_agent",
        "--character",
        "Ironclad",
        "--seed",
        str(seed),
        "--depth",
        str(depth),
        "--chance-depth",
        str(chance_depth),
        "--reuse-cli-processes",
        "--score-mode",
        score_mode,
        "--max-steps",
        str(max_steps),
        "--combat-policy",
        combat_policy,
        "--global-policy",
        global_policy,
        "--draft-policy",
        draft_policy,
    ]
    if draft_policy in ("branchvalue", "branchvalue_act2"):
        cmd += ["--draft-tau", str(draft_tau)]
        if draft_model:
            cmd += ["--draft-model", draft_model]
        if draft_heuristic_reward_count > 0:
            cmd += ["--draft-heuristic-reward-count", str(draft_heuristic_reward_count)]
    if capture_presave:
        cmd += ["--capture-presave", capture_presave]
    if worker_pool:
        cmd.append("--experimental-worker-pool")
    start = time.time()
    timed_out = False
    stdout_text = ""
    stderr_text = ""
    returncode = 0
    proc: subprocess.Popen[str] | None = None

    def _as_text(v: Any) -> str:
        if v is None:
            return ""
        return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else v

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        stdout_text, stderr_text = proc.communicate(timeout=timeout_s)
        returncode = proc.returncode or 0
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        # subprocess.run(timeout=...) can leave grandchildren alive. Use a
        # dedicated process group and kill it explicitly so worker threads do
        # not wedge on stale Sts2Headless / dotnet children.
        stdout_text = _as_text(exc.stdout)
        stderr_text = _as_text(exc.stderr)
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                more_stdout, more_stderr = proc.communicate(timeout=30)
                if more_stdout:
                    stdout_text = more_stdout
                if more_stderr:
                    stderr_text = more_stderr
            except Exception:
                pass
            returncode = proc.returncode if proc.returncode is not None else -999
        else:
            returncode = -999
    elapsed = time.time() - start
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / f"seed_{seed}.jsonl").write_text(stdout_text, encoding="utf-8")
        except Exception:
            pass
    events: List[Dict[str, Any]] = []
    run_summary: Dict[str, Any] = {}
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("run_summary"):
            run_summary = obj
            continue
        events.append(obj)
    summary = summarize_events(events)
    # The run_summary line is the authoritative outcome (won / defeat /
    # game_over / max_steps / error). max_steps was the false outcome the
    # use_potion no-op bug produced — now fixed, so it should be rare.
    summary["outcome"] = run_summary.get("outcome")
    summary["run_summary_last_floor"] = run_summary.get("last_floor")
    summary["total_hp_loss"] = run_summary.get("total_hp_loss")
    _copy_run_summary_fields(summary, run_summary)
    return {
        "seed": str(seed),
        "returncode": returncode,
        "timed_out": timed_out,
        "elapsed_s": round(elapsed, 3),
        "summary": summary,
        "stderr_tail": stderr_text.splitlines()[-10:],
    }


def aggregate_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    floors: List[int] = []
    acts_cleared_values: List[int] = []
    timed_out = 0
    outcome_counts: collections.Counter[str] = collections.Counter()
    acts_cleared_counts: collections.Counter[str] = collections.Counter()
    max_act_counts: collections.Counter[str] = collections.Counter()
    last_position_counts: collections.Counter[str] = collections.Counter()
    terminal_counts: collections.Counter[str] = collections.Counter()
    last_decision_counts: collections.Counter[str] = collections.Counter()
    last_combat_counts: collections.Counter[str] = collections.Counter()
    reward_counts: collections.Counter[str] = collections.Counter()
    shop_buy_counts: collections.Counter[str] = collections.Counter()
    slowest_actions: List[Dict[str, Any]] = []

    for result in results:
        if result.get("timed_out"):
            timed_out += 1
        summary = result.get("summary") or {}
        outcome_counts[str(summary.get("outcome") or "unknown")] += 1
        floor = summary.get("last_floor")
        if isinstance(floor, int):
            floors.append(floor)
        acts_cleared = _as_int_or_none(summary.get("acts_cleared"))
        if acts_cleared is None:
            acts_cleared_counts["unknown"] += 1
        else:
            acts_cleared_values.append(acts_cleared)
            acts_cleared_counts[str(acts_cleared)] += 1
        max_act = _as_int_or_none(summary.get("max_act"))
        max_act_counts[str(max_act) if max_act is not None else "unknown"] += 1
        last_position_counts[str(summary.get("last_position") or "unknown")] += 1
        terminal = summary.get("terminal") or {}
        terminal_counts[str(terminal.get("decision") or summary.get("last_decision") or "unknown")] += 1
        last_decision_counts[str(summary.get("last_decision") or "unknown")] += 1
        last_combat = summary.get("last_combat_action") or {}
        if last_combat.get("encounter_id"):
            last_combat_counts[str(last_combat["encounter_id"])] += 1
        for card_id in summary.get("reward_cards") or []:
            reward_counts[str(card_id)] += 1
        for buy in summary.get("shop_buys") or []:
            item_id = buy.get("item_id")
            if item_id:
                shop_buy_counts[str(item_id)] += 1
        for action in summary.get("slowest_combat_actions") or []:
            slowest_actions.append(
                {
                    "seed": result.get("seed"),
                    "floor": action.get("floor"),
                    "encounter_id": action.get("encounter_id"),
                    "search_wall_ms": action.get("search_wall_ms"),
                    "nodes": action.get("nodes"),
                    "parallel_top_level": action.get("parallel_top_level"),
                    "chosen": action.get("chosen"),
                }
            )

    slowest_actions_sorted = sorted(
        slowest_actions,
        key=lambda item: float(item.get("search_wall_ms") or 0.0),
        reverse=True,
    )
    act_known = len(acts_cleared_values)
    act1_clears = sum(1 for value in acts_cleared_values if value >= 1)
    return {
        "num_seeds": len(results),
        "timed_out_count": timed_out,
        "outcome_counts": dict(outcome_counts.most_common()),
        "win_count": int(outcome_counts.get("won", 0)),
        "win_rate": round(outcome_counts.get("won", 0) / len(results), 3) if results else None,
        "acts_cleared_counts": dict(acts_cleared_counts.most_common()),
        "max_act_counts": dict(max_act_counts.most_common()),
        "act1_clear_count": act1_clears,
        "act1_clear_rate": round(act1_clears / act_known, 3) if act_known else None,
        "act1_clear_rate_denominator": act_known,
        "last_position_counts": dict(last_position_counts.most_common()),
        "avg_last_floor": round(sum(floors) / len(floors), 3) if floors else None,
        "min_last_floor": min(floors) if floors else None,
        "max_last_floor": max(floors) if floors else None,
        "last_decision_counts": dict(last_decision_counts),
        "last_combat_counts": dict(last_combat_counts.most_common()),
        "reward_card_counts": dict(reward_counts.most_common(12)),
        "shop_buy_counts": dict(shop_buy_counts.most_common(12)),
        "slowest_actions_global": slowest_actions_sorted[:12],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--score-mode", default="preference")
    parser.add_argument("--max-steps", type=int, default=400)
    parser.add_argument("--timeout-s", type=int, default=3600)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--depth", type=int, default=10,
                        help="combat search depth (default 10 = production full strength)")
    parser.add_argument("--chance-depth", type=int, default=1)
    parser.add_argument("--out", default=None, help="write full JSON results to this path")
    parser.add_argument("--log-dir", default=None,
                        help="write each run's full per-step JSONL stdout to <dir>/seed_<seed>.jsonl")
    parser.add_argument("--experimental-worker-pool", action="store_true",
                        help="keep CLI workers warm across steps (~6x combat search; now accuracy-validated)")
    parser.add_argument("--combat-policy", default="search",
                        help="search | fallback | naive | random (baseline control groups)")
    parser.add_argument("--global-policy", default="full",
                        help="full | random")
    parser.add_argument("--seeds", nargs="+", default=["1", "4", "7", "13", "42"])
    parser.add_argument("--draft-policy", default="heuristic",
                        help="heuristic | learned | branchvalue (card-reward policy)")
    parser.add_argument("--draft-tau", type=float, default=0.05,
                        help="branchvalue: Boltzmann temperature")
    parser.add_argument("--draft-model", default=None,
                        help="branchvalue: path to card_value_model.pt")
    parser.add_argument("--capture-presave", default=None,
                        help="directory to write pre-room .save files at decision points")
    parser.add_argument("--draft-heuristic-reward-count", type=int, default=0,
                        help="branchvalue: use heuristic for first N non-starter cards in Act 1")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    log_dir = Path(args.log_dir) if args.log_dir else None
    max_workers = max(1, min(args.max_workers, len(args.seeds)))
    if max_workers <= 1:
        results = [
            run_seed(repo_root=repo_root, seed=seed, score_mode=args.score_mode, max_steps=args.max_steps,
                     timeout_s=args.timeout_s, depth=args.depth, chance_depth=args.chance_depth,
                     worker_pool=args.experimental_worker_pool, log_dir=log_dir,
                     combat_policy=args.combat_policy, global_policy=args.global_policy,
                     draft_policy=args.draft_policy, draft_tau=args.draft_tau, draft_model=args.draft_model,
                capture_presave=args.capture_presave,
                draft_heuristic_reward_count=args.draft_heuristic_reward_count)
            for seed in args.seeds
        ]
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(
                    run_seed,
                    repo_root=repo_root,
                    seed=seed,
                    score_mode=args.score_mode,
                    max_steps=args.max_steps,
                    timeout_s=args.timeout_s,
                    depth=args.depth,
                    chance_depth=args.chance_depth,
                    worker_pool=args.experimental_worker_pool,
                    log_dir=log_dir,
                    combat_policy=args.combat_policy,
                    global_policy=args.global_policy,
                    draft_policy=args.draft_policy,
                    draft_tau=args.draft_tau,
                    draft_model=args.draft_model,
                    capture_presave=args.capture_presave,
                    draft_heuristic_reward_count=args.draft_heuristic_reward_count,
                )
                for seed in args.seeds
            ]
            results = [future.result() for future in futures]
    aggregate = aggregate_results(results)
    payload = {
        "score_mode": args.score_mode,
        "depth": args.depth,
        "chance_depth": args.chance_depth,
        "max_steps": args.max_steps,
        "timeout_s": args.timeout_s,
        "seeds": args.seeds,
        "combat_policy": args.combat_policy,
        "global_policy": args.global_policy,
        "draft_policy": args.draft_policy,
        "aggregate": aggregate,
        "results": results,
    }
    output = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
