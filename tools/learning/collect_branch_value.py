#!/usr/bin/env python3
"""Collect (state, candidate-card) -> counterfactual ΔReturn training data via
model-based branch rollout, for the offline card-value model.

For each presave (a real pre-room global state), this:
  1. reads the global state features (deck card-ids, relics, hp, max_hp, gold, floor),
  2. scouts the cards offered at the first card_reward,
  3. computes the CRN-paired discounted return G for SKIP once (the shared baseline),
  4. for each offered card, computes G for taking it under the SAME K shuffle seeds,
  5. emits one sample per (state, card): ΔReturn = mean_k[ G_take(k) - G_skip(k) ].

The skip baseline is computed ONCE and reused for all candidates at a point, so a
3-card reward costs 4K rollouts (skip + 3 cards) instead of 6K.

Output: one JSONL row per (state, card):
  {presave, floor, hp, max_hp, gold, deck:[ids], relics:[ids],
   card, n, mean_delta, stderr, effect_over_se, G_take_mean, G_skip_mean,
   per_seed_delta:[...], horizon, K}

This is the training corpus for tools/learning/train_card_value_model.py. The label
(mean_delta) is a CAUSAL effect (CRN-paired engine rollout), not an observational
correlation — the whole point of the model-based route.

Usage:
  python3 -m tools.learning.collect_branch_value \
      --presaves data/learning/combat_eval_set_v4/presaves/*_card_reward.save \
      --horizon 3 --rollouts 16 --shuffle-base 9000 \
      --out data/learning/branch_value/samples.jsonl
"""
from __future__ import annotations
import argparse, glob as globmod, json, math, signal, statistics, sys, time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.search.combat_search import CombatWorkerPool
from tools.learning.branch_rollout_probe import (
    play_one_branch, scout_first_reward, _dotnet, _norm_card,
)


def _read_state_features(cfg: CliConfig, presave: str) -> Optional[Dict[str, Any]]:
    """Read deck/relics/hp/gold/floor from a presave's load frame."""
    cli = Sts2CliAdapter(cfg); cli.start()
    try:
        r = cli.load_save(str(Path(presave).resolve()))
        if r.get("type") == "error":
            return None
        pl = r.get("player") or {}
        deck = [_norm_card((c.get("id") or c.get("card_id") or "")) for c in (pl.get("deck") or [])]
        relics = [str(x.get("name") or x.get("id") or "") for x in (pl.get("relics") or [])]
        return {"deck": deck, "relics": relics, "hp": pl.get("hp"),
                "max_hp": pl.get("max_hp"), "gold": pl.get("gold"),
                "floor": r.get("floor"), "act": r.get("act")}
    finally:
        cli.stop()


def _paired(deltas: List[float]) -> Dict[str, Any]:
    n = len(deltas)
    if n == 0:
        return {"n": 0, "mean": None, "stderr": None, "effect_over_se": None}
    mean = statistics.mean(deltas)
    sd = statistics.pstdev(deltas) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n else 0.0
    ratio = (mean / se) if se > 1e-9 else (float("inf") if abs(mean) > 1e-9 else 0.0)
    return {"n": n, "mean": round(mean, 5), "stderr": round(se, 5),
            "effect_over_se": round(ratio, 2)}


@contextmanager
def _point_timeout(seconds: Optional[float]):
    if seconds is None or seconds <= 0:
        yield
        return

    def _raise_timeout(signum, frame):
        raise TimeoutError(f"point timed out after {seconds:.1f}s")

    old_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, float(seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, old_handler)


def collect_point(cfg, pool, presave, horizon, rollouts, shuffle_base,
                  *, depth, chance_depth, score_mode) -> List[Dict[str, Any]]:
    feats = _read_state_features(cfg, presave)
    if feats is None:
        return [{"presave": Path(presave).name, "error": "load_failed"}]
    offered = scout_first_reward(cfg, pool, presave, shuffle_base,
                                 depth=depth, chance_depth=chance_depth, score_mode=score_mode)
    if len(offered) < 1:
        return [{"presave": Path(presave).name, "error": "no_offer", "offered": offered, **feats}]

    # shared SKIP baseline: G_skip(k) for each shuffle seed
    g_skip: Dict[int, float] = {}
    for i in range(rollouts):
        sh = shuffle_base + i
        rb = play_one_branch(cfg, presave, sh, "skip", horizon,
                             depth=depth, chance_depth=chance_depth,
                             score_mode=score_mode, pool=pool)
        g = rb.get("discounted_return")
        if g is not None:
            g_skip[sh] = g

    rows: List[Dict[str, Any]] = []
    for card in offered:
        deltas, g_takes = [], []
        for i in range(rollouts):
            sh = shuffle_base + i
            if sh not in g_skip:
                continue
            ra = play_one_branch(cfg, presave, sh, card, horizon,
                                 depth=depth, chance_depth=chance_depth,
                                 score_mode=score_mode, pool=pool)
            ga = ra.get("discounted_return")
            if ga is None:
                continue
            g_takes.append(ga)
            deltas.append(ga - g_skip[sh])
        st = _paired(deltas)
        rows.append({
            "presave": Path(presave).name, "card": card,
            "floor": feats["floor"], "act": feats["act"],
            "hp": feats["hp"], "max_hp": feats["max_hp"], "gold": feats["gold"],
            "deck": feats["deck"], "relics": feats["relics"],
            "horizon": horizon, "K": rollouts,
            "mean_delta": st["mean"], "stderr": st["stderr"],
            "effect_over_se": st["effect_over_se"], "n": st["n"],
            "G_take_mean": round(statistics.mean(g_takes), 5) if g_takes else None,
            "G_skip_mean": round(statistics.mean(list(g_skip.values())), 5) if g_skip else None,
            "per_seed_delta": [round(d, 5) for d in deltas],
        })
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--presaves", nargs="*", default=None, help="presave paths (globs ok)")
    ap.add_argument("--presave-file", default=None,
                    help="text file with one presave path per line (use to control order, e.g. shallow floors first)")
    ap.add_argument("--horizon", type=int, default=3)
    ap.add_argument("--rollouts", type=int, default=16)
    ap.add_argument("--shuffle-base", type=int, default=9000)
    ap.add_argument("--depth", type=int, default=10)
    ap.add_argument("--chance-depth", type=int, default=1)
    ap.add_argument("--score-mode", default="balanced")
    ap.add_argument("--limit-points", type=int, default=None,
                    help="process at most this many not-yet-done presaves, for resumable chunks")
    ap.add_argument("--point-timeout-s", type=float, default=None,
                    help="optional wall-time cap per presave; use for large resumable batches")
    ap.add_argument("--record-point-errors", action="store_true",
                    help="append exception/timeout rows so future resume skips known bad/slow points")
    ap.add_argument("--retry-errors", action="store_true",
                    help="when resuming, ignore prior rows that only contain an error and no card sample")
    ap.add_argument("--quiet-skips", action="store_true",
                    help="suppress per-presave skip_done output")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    paths: List[str] = []
    if a.presave_file:
        paths = [ln.strip() for ln in Path(a.presave_file).read_text().splitlines() if ln.strip()]
    for pat in (a.presaves or []):
        paths.extend(sorted(globmod.glob(pat)) if any(c in pat for c in "*?[") else [pat])
    if not paths:
        raise SystemExit("provide --presaves or --presave-file")
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)

    # resume: skip presaves already in the output
    done = set()
    if out.exists():
        for line in out.open():
            try:
                row = json.loads(line)
            except Exception:
                pass
            else:
                presave = row.get("presave")
                if not presave:
                    continue
                if a.retry_errors and row.get("error") and not row.get("card"):
                    continue
                done.add(presave)

    cfg = CliConfig(repo_root=REPO, dotnet_path=_dotnet())
    pool = CombatWorkerPool(cfg)
    n_samples = 0
    n_points = 0
    try:
        with out.open("a") as fh:
            for p in paths:
                if Path(p).name in done:
                    if not a.quiet_skips:
                        print(json.dumps({"skip_done": Path(p).name}, ensure_ascii=False), flush=True)
                    continue
                if a.limit_points is not None and n_points >= int(a.limit_points):
                    print(json.dumps({"limit_reached": int(a.limit_points), "samples_total": n_samples},
                                     ensure_ascii=False), flush=True)
                    break
                t0 = time.perf_counter()
                try:
                    with _point_timeout(a.point_timeout_s):
                        rows = collect_point(cfg, pool, p, a.horizon, a.rollouts, a.shuffle_base,
                                             depth=a.depth, chance_depth=a.chance_depth, score_mode=a.score_mode)
                except Exception as exc:
                    # A CLI subprocess can die mid-rollout (engine crash / slow-point
                    # timeout). Don't let one bad point abort the whole run: log it,
                    # rebuild the pool (the dead worker poisoned it), and move on.
                    # The run is resumable, so a skipped point can be re-collected later.
                    n_points += 1
                    print(json.dumps({"point_error": Path(p).name,
                                      "error": f"{type(exc).__name__}: {exc}"[:300]}, ensure_ascii=False), flush=True)
                    if a.record_point_errors:
                        fh.write(json.dumps({
                            "presave": Path(p).name,
                            "error": f"{type(exc).__name__}: {exc}"[:300],
                            "horizon": a.horizon,
                            "K": a.rollouts,
                            "depth": a.depth,
                            "chance_depth": a.chance_depth,
                            "score_mode": a.score_mode,
                            "point_timeout_s": a.point_timeout_s,
                            "secs": round(time.perf_counter() - t0, 1),
                        }, ensure_ascii=False) + "\n")
                        fh.flush()
                    try:
                        pool.close()
                    except Exception:
                        pass
                    pool = CombatWorkerPool(cfg)
                    continue
                n_points += 1
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                fh.flush()
                n_samples += sum(1 for r in rows if "card" in r)
                for r in rows:
                    if "card" in r:
                        print(json.dumps({"presave": r["presave"], "card": r["card"],
                                          "floor": r["floor"], "mean_delta": r["mean_delta"],
                                          "eff/se": r["effect_over_se"]}, ensure_ascii=False), flush=True)
                    else:
                        print(json.dumps(r, ensure_ascii=False), flush=True)
                print(json.dumps({"point_done": Path(p).name, "secs": round(time.perf_counter() - t0, 1),
                                  "samples_total": n_samples}, ensure_ascii=False), flush=True)
    finally:
        pool.close()
    print(json.dumps({"DONE": {"out": str(out), "samples": n_samples, "points": n_points}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
