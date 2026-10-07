#!/usr/bin/env python3
"""CRN paired comparison: low-variance Δ between two policies on ONE situation.

The variance-reduction payoff of the reseed primitive. To compare policy A vs B
on a boss situation, run BOTH under the SAME K shuffle seeds (common random
numbers). The shared draw-order noise cancels in the per-seed difference
Δ_k = retained_A(seed_k) - retained_B(seed_k), so Var[mean Δ] is far below what
independent sampling of A and B would give. We report both the paired stdev and
the (hypothetical) unpaired stdev so the CRN win is visible.

Currently the policy axis is `score_mode` (no engine change needed). The same
harness will host deck/card-pick variants once those are wired.

Usage:
  python3 tools/crn_paired.py --snapshot <unit.json> \
      --policy-a balanced --policy-b balanced_future --rollouts 16 --depth 4
"""
from __future__ import annotations
import argparse, json, math, statistics, sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from cli.sts2_cli_adapter import CliConfig
from controller.search.combat_search import CombatWorkerPool
from tools.eval.marginalized_rollout import rollout_once, _dotnet


def _paired_stats(deltas):
    n = len(deltas)
    if n == 0:
        return None
    mean = statistics.mean(deltas)
    sd = statistics.pstdev(deltas) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 0 else 0.0
    # paired t-ish: mean / se (not a real p-value, just an effect/noise ratio)
    ratio = (mean / se) if se > 1e-9 else float("inf") if abs(mean) > 1e-9 else 0.0
    return {"n": n, "mean": mean, "stdev": sd, "stderr": se, "effect_over_se": ratio}


def _merge_counter(target, payload):
    for key, value in (payload or {}).items():
        target[str(key)] += int(value or 0)


def _trace_stats(rollouts):
    action_mix = Counter()
    card_type_mix = Counter()
    power_play_count = 0
    play_count = 0
    n_actions = 0
    for rollout in rollouts:
        trace = rollout.get("trace") or {}
        _merge_counter(action_mix, trace.get("action_mix") or {})
        _merge_counter(card_type_mix, trace.get("card_type_mix") or {})
        power_play_count += int(trace.get("power_play_count") or 0)
        play_count += int(trace.get("play_count") or 0)
        n_actions += int(trace.get("n_actions") or 0)
    return {
        "n_actions": n_actions,
        "action_mix": dict(action_mix),
        "card_type_mix": dict(card_type_mix),
        "power_play_count": power_play_count,
        "play_count": play_count,
        "power_play_rate": (power_play_count / play_count) if play_count else 0.0,
    }


def _first_action_key(rollout):
    actions = rollout.get("actions") or []
    if not actions:
        return ("", "", "", "")
    action = actions[0] or {}
    return (
        str(action.get("action_type") or ""),
        str(action.get("card_id") or ""),
        str(action.get("target_monster_id") or action.get("target_index") or ""),
        str(action.get("potion_id") or ""),
    )


def _win_stats(paired):
    wins_a = sum(1 for p in paired if (p.get("a") or {}).get("outcome") == "won")
    wins_b = sum(1 for p in paired if (p.get("b") or {}).get("outcome") == "won")
    n = len(paired)
    return {
        "n": n,
        "wins_a": wins_a,
        "wins_b": wins_b,
        "win_rate_a": (wins_a / n) if n else 0.0,
        "win_rate_b": (wins_b / n) if n else 0.0,
        "win_rate_delta_a_minus_b": ((wins_a - wins_b) / n) if n else 0.0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True, help="eval-set unit JSON")
    ap.add_argument("--policy-a", default="balanced")
    ap.add_argument("--policy-b", default="balanced_future")
    ap.add_argument("--rollouts", type=int, default=16)
    ap.add_argument("--depth", type=int, default=4)
    ap.add_argument("--chance-depth", type=int, default=1)
    ap.add_argument("--max-steps", type=int, default=80)
    ap.add_argument("--shuffle-base", type=int, default=1000)
    ap.add_argument("--json-out", default=None)
    a = ap.parse_args()

    unit = json.loads(Path(a.snapshot).read_text())
    sj = unit["snapshot_json"]
    boss = unit.get("encounter_id")
    run_seed = str(unit.get("seed"))
    es = unit.get("entry_summary") or {}
    cfg = CliConfig(repo_root=REPO, dotnet_path=_dotnet())
    pool = CombatWorkerPool(cfg)

    print(f"snapshot={Path(a.snapshot).name} enc={boss} entry_hp={es.get('hp')} "
          f"enemy_hp={es.get('enemy_hp')}")
    print(f"A={a.policy_a}  B={a.policy_b}  rollouts={a.rollouts} depth={a.depth}\n")

    a_rets, b_rets, deltas = [], [], []
    paired = []
    first_action_changed = 0
    try:
        for i in range(a.rollouts):
            sh = a.shuffle_base + i  # SAME seed for both policies = CRN
            ra = rollout_once(cfg, boss, run_seed, sh, depth=a.depth,
                              chance_depth=a.chance_depth, score_mode=a.policy_a,
                              max_steps=a.max_steps, pool=pool, snapshot_json=sj,
                              trace_actions=True)
            rb = rollout_once(cfg, boss, run_seed, sh, depth=a.depth,
                              chance_depth=a.chance_depth, score_mode=a.policy_b,
                              max_steps=a.max_steps, pool=pool, snapshot_json=sj,
                              trace_actions=True)
            va, vb = ra.get("retained"), rb.get("retained")
            tag = ""
            if va is not None and vb is not None:
                d = va - vb
                deltas.append(d); a_rets.append(va); b_rets.append(vb)
                tag = f"Δ={d:+.3f}"
            changed = _first_action_key(ra) != _first_action_key(rb)
            if changed:
                first_action_changed += 1
            paired.append({
                "shuffle": sh,
                "a": ra,
                "b": rb,
                "delta": (va - vb) if va is not None and vb is not None else None,
                "first_action_changed": changed,
            })
            print(f"  shuffle={sh}: A={ra['outcome']:11s} {('%.3f'%va) if va is not None else '  -  '}"
                  f"  B={rb['outcome']:11s} {('%.3f'%vb) if vb is not None else '  -  '}  {tag}")
    finally:
        pool.close()

    print("\n=== CRN PAIRED COMPARISON ===")
    st = _paired_stats(deltas)
    win_st = _win_stats(paired)
    a_trace = _trace_stats([p["a"] for p in paired])
    b_trace = _trace_stats([p["b"] for p in paired])
    change_rate = first_action_changed / len(paired) if paired else 0.0
    if st is None:
        print("  no paired terminal samples")
        print(f"  wins A/B          : {win_st['wins_a']}/{win_st['n']} vs {win_st['wins_b']}/{win_st['n']}")
        print("\n=== ACTION TRACE ===")
        print(f"  first-action changed: {first_action_changed}/{len(paired)} ({100.0 * change_rate:.1f}%)")
        print(f"  A action mix         : {a_trace['action_mix']}")
        print(f"  B action mix         : {b_trace['action_mix']}")
        print(f"  A card type mix      : {a_trace['card_type_mix']}  power_rate={a_trace['power_play_rate']:.3f}")
        print(f"  B card type mix      : {b_trace['card_type_mix']}  power_rate={b_trace['power_play_rate']:.3f}")
        if a.json_out:
            Path(a.json_out).write_text(json.dumps({
                "snapshot": Path(a.snapshot).name,
                "encounter_id": boss,
                "policy_a": a.policy_a,
                "policy_b": a.policy_b,
                "paired": paired,
                "stats": None,
                "win_stats": win_st,
                "first_action_changed": first_action_changed,
                "first_action_change_rate": change_rate,
                "trace_a": a_trace,
                "trace_b": b_trace,
            }, indent=2), encoding="utf-8")
        return 0
    print(f"  wins A/B           : {win_st['wins_a']}/{win_st['n']} vs {win_st['wins_b']}/{win_st['n']} "
          f"(Δ={win_st['win_rate_delta_a_minus_b']:+.3f})")
    print(f"  paired n          : {st['n']}")
    print(f"  mean retained A    : {statistics.mean(a_rets):.4f}")
    print(f"  mean retained B    : {statistics.mean(b_rets):.4f}")
    print(f"  mean Δ (A-B)       : {st['mean']:+.4f}")
    print(f"  paired stdev(Δ)    : {st['stdev']:.4f}")
    print(f"  paired stderr(Δ)   : {st['stderr']:.4f}")
    print(f"  effect/stderr      : {st['effect_over_se']:.2f}   (|.|>~2 = signal beats noise)")
    # Contrast: unpaired stderr if A and B were sampled independently.
    if len(a_rets) > 1:
        var_a = statistics.pvariance(a_rets)
        var_b = statistics.pvariance(b_rets)
        unpaired_se = math.sqrt((var_a + var_b) / len(a_rets))
        print(f"  unpaired stderr    : {unpaired_se:.4f}   <-- CRN beats this "
              f"(ratio {unpaired_se / st['stderr']:.2f}x)" if st['stderr'] > 1e-9 else "")
    print("\n=== ACTION TRACE ===")
    print(f"  first-action changed: {first_action_changed}/{len(paired)} ({100.0 * change_rate:.1f}%)")
    print(f"  A action mix         : {a_trace['action_mix']}")
    print(f"  B action mix         : {b_trace['action_mix']}")
    print(f"  A card type mix      : {a_trace['card_type_mix']}  power_rate={a_trace['power_play_rate']:.3f}")
    print(f"  B card type mix      : {b_trace['card_type_mix']}  power_rate={b_trace['power_play_rate']:.3f}")
    if a.json_out:
        payload = {
            "snapshot": Path(a.snapshot).name,
            "encounter_id": boss,
            "policy_a": a.policy_a,
            "policy_b": a.policy_b,
            "rollouts": a.rollouts,
            "depth": a.depth,
            "chance_depth": a.chance_depth,
            "max_steps": a.max_steps,
            "paired": paired,
            "stats": st,
            "win_stats": win_st,
            "mean_retained_a": statistics.mean(a_rets),
            "mean_retained_b": statistics.mean(b_rets),
            "first_action_changed": first_action_changed,
            "first_action_change_rate": change_rate,
            "trace_a": a_trace,
            "trace_b": b_trace,
        }
        Path(a.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json_out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote JSON -> {a.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
