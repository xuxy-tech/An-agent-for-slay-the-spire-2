#!/usr/bin/env python3
"""Branch-rollout probe: does a counterfactual card-pick produce a learnable,
CRN-denoisable value signal over a short (N-fight) horizon?

This is the minimal experiment behind the "model-based approximate Q with sparse
branching" idea. It does NOT build a learning loop — it just measures whether the
signal exists and how strong it is, so we know if the full loop is worth building.

What it does, for ONE branch point (a pre-room presave):
  1. load_save -> reseed Shuffle(k) -> walk into the room -> play the combat
     (reusing decide_combat_action, the SAME primitive run_agent uses).
  2. At the FIRST card_reward, FORCE a chosen pick (or skip) — this is the branch.
  3. Continue playing under the baseline global policy for up to N total fights.
  4. Return the discounted return over those fights:
        G = sum_i gamma^i * retained_hp_i        (retained_hp = post-fight hp/maxhp)
  Run this for branch A and branch B under the SAME K shuffle seeds (CRN). The
  per-seed paired delta G_A(k) - G_B(k) cancels shared draw-order noise, so its
  effect/stderr ratio tells us if "pick A vs pick B" is separable past noise.

Honest caveats this probe is designed to expose:
  - CRN across MULTIPLE fights is weaker than single-fight: only the Shuffle
    stream is reseeded per fight; the 2nd/3rd fights are different encounters so
    card-reward/event RNG alignment degrades. We measure 1-fight AND 3-fight
    horizons side by side to see how much CRN pairing survives.
  - retained-HP is a biased policy-ranking metric in general; here it is only the
    per-fight reward feeding the discounted return, gated on a fixed horizon.

Usage:
  python3 -m tools.learning.branch_rollout_probe \
      --presave data/learning/combat_eval_set_v4/presaves/52_11_card_reward.save \
      --pick-a-card BASH --pick-b skip --horizon 3 --rollouts 6 --shuffle-base 9000
"""
from __future__ import annotations
import argparse, json, math, statistics, sys, time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from cli.runtime_paths import resolve_dotnet
from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.search.combat_search import CombatSpec, CombatWorkerPool
from controller.combat_step import CombatStepConfig, PlanState, decide_combat_action
from controller.run_agent import (
    choose_map_node_global, choose_card_reward, choose_rest_option,
    choose_shop_action, reward_card_label, choose_event_option,
    choose_card_select_pick,
)


def _dotnet() -> Path:
    return resolve_dotnet()


def _norm_card(cid: Optional[str]) -> str:
    return str(cid or "").upper().replace("CARD.", "").replace("+", "").rstrip("0123456789").rstrip("_")


def _retained(state: Dict[str, Any]) -> float:
    p = state.get("player") or {}
    hp, mhp = p.get("hp"), p.get("max_hp")
    try:
        return float(hp) / float(mhp) if mhp else 0.0
    except (TypeError, ValueError):
        return 0.0


def _offered_cards(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    return state.get("cards") or state.get("rewards") or []


def play_one_branch(cfg: CliConfig, presave: str, shuffle_seed: int,
                    pick_card: Optional[str], horizon: int, *,
                    depth: int, chance_depth: int, score_mode: str,
                    pool: CombatWorkerPool, max_steps: int = 300) -> Dict[str, Any]:
    """Load presave, reseed, play; force the FIRST card_reward to `pick_card`
    (None or 'skip' = skip the reward), then baseline-play up to `horizon`
    fights. Returns per-fight retained list + discounted return + outcome."""
    cli = Sts2CliAdapter(cfg)
    cli.start()
    fights: List[float] = []
    forced_done = False
    forced_pick_label: Optional[str] = None
    outcome = "ok"
    presave = str(Path(presave).resolve())
    try:
        r = cli.load_save(presave)
        if r.get("type") == "error":
            return {"outcome": "load_failed", "detail": r}
        cli.reseed_rng_stream({"Shuffle": int(shuffle_seed)})
        state = r
        in_combat_played = 0
        for _ in range(max_steps):
            dec = str(state.get("decision") or "")
            if dec in ("game_over", "defeat"):
                outcome = "dead"; break
            if dec == "map_select":
                ch = state.get("choices") or []
                if not ch:
                    outcome = "no_map_choice"; break
                pick = choose_map_node_global(state) if False else {"col": int(ch[0]["col"]), "row": int(ch[0]["row"])}
                state = cli.action("select_map_node", pick)
                continue
            if dec == "combat_play":
                spec = CombatSpec(character="Ironclad",
                                  encounter=str((state.get("context") or {}).get("encounter_id") or ""),
                                  seed=str(shuffle_seed))
                rt = str((state.get("context") or {}).get("room_type") or "")
                step_cfg = CombatStepConfig(
                    cli_cfg=cfg, spec=spec, depth=depth, chance_depth=chance_depth,
                    score_mode=score_mode, max_workers=4, reuse_cli_processes=False,
                    floor=(state.get("context") or {}).get("floor"), room_type=rt,
                    worker_pool=pool, max_search_ms=30000.0,
                )
                plan = PlanState()
                # Play the whole combat. decide_combat_action consumes the SEARCH
                # state (combat_state_for_search) — its hand has real card ids; the
                # display frame from cli.action has hand=[None,...]. Control flow is
                # driven by the OUTER decision frame (cli.action return): keep
                # stepping while it stays combat_play, stop when it advances to the
                # reward/next decision.
                steps = 0
                while str(state.get("decision") or "") == "combat_play" and steps < max_steps:
                    ss = cli.get_search_state().get("combat_state_for_search") or {}
                    if not ss.get("success"):
                        break
                    sr = decide_combat_action(cli, ss, step_cfg, plan)
                    state = cli.action(sr.action, args=sr.payload, with_snapshot=False)
                    steps += 1
                # combat ended; the post-combat frame (state) carries hp + the next
                # decision (card_reward / game_over / map_select ...)
                fights.append(_retained(state))
                in_combat_played += 1
                if in_combat_played >= horizon:
                    outcome = "horizon_reached"; break
                continue
            if dec == "card_reward":
                cards = _offered_cards(state)
                if not forced_done:
                    # THE BRANCH: force the chosen pick
                    forced_done = True
                    if pick_card in (None, "skip", "SKIP"):
                        forced_pick_label = "skip"
                        state = cli.action("skip_card_reward", {})
                    else:
                        idx = None
                        for c in cards:
                            if _norm_card((c or {}).get("card_id") or (c or {}).get("id")) == _norm_card(pick_card):
                                idx = int((c or {}).get("index", 0)); break
                        if idx is None:
                            forced_pick_label = f"card_not_offered:{pick_card}"
                            state = cli.action("skip_card_reward", {})
                        else:
                            forced_pick_label = _norm_card(pick_card)
                            state = cli.action("select_card_reward", {"card_index": idx})
                else:
                    # subsequent rewards: baseline heuristic
                    pick = choose_card_reward(state, REPO)
                    if pick is None:
                        state = cli.action("skip_card_reward", {})
                    else:
                        state = cli.action("select_card_reward", pick)
                continue
            if dec == "rest_site":
                state = cli.action("choose_option", choose_rest_option(state))
                continue
            if dec == "event_choice":
                state = cli.action("choose_option", choose_event_option(state))
                continue
            if dec == "card_select":
                pick = choose_card_select_pick(state, REPO)
                if pick is None:
                    state = cli.action("skip_select", {})
                else:
                    state = cli.action("select_cards", {"indices": str(pick["indices"])})
                continue
            if dec == "bundle_select":
                state = cli.action("proceed", {})
                continue
            if dec == "shop":
                # Leave immediately: shop purchasing is not the branch under study,
                # and the exit action is leave_room (not leave_shop). Buying would
                # also perturb the deck off the branch we are measuring.
                state = cli.action("leave_room", {})
                continue
            if dec in ("treasure", "unknown"):
                state = cli.action("proceed", {})
                continue
            # any other decision: try to proceed; if it returns a non-advancing
            # empty frame (no decision, just a message), bail rather than spin.
            nxt = cli.action("proceed", {})
            if nxt.get("type") == "error" or not nxt.get("decision"):
                outcome = f"stuck_on:{dec}"; break
            state = nxt
        gamma = 0.9
        disc_return = sum((gamma ** i) * v for i, v in enumerate(fights))
        return {"outcome": outcome, "fights": fights, "n_fights": len(fights),
                "discounted_return": disc_return, "sum_retained": sum(fights),
                "forced_pick": forced_pick_label}
    finally:
        cli.stop()


def _paired_stats(deltas: List[float]) -> Dict[str, Any]:
    n = len(deltas)
    if n == 0:
        return {"n": 0}
    mean = statistics.mean(deltas)
    sd = statistics.pstdev(deltas) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n else 0.0
    ratio = (mean / se) if se > 1e-9 else (float("inf") if abs(mean) > 1e-9 else 0.0)
    return {"n": n, "mean": round(mean, 4), "stdev": round(sd, 4),
            "stderr": round(se, 4), "effect_over_se": round(ratio, 2)}


def run_crn_comparison(cfg, pool, presave, pick_a, pick_b, horizon, rollouts,
                       shuffle_base, *, depth, chance_depth, score_mode, verbose=True):
    """One CRN-paired A-vs-B comparison. Returns (summary, rows_a, rows_b, deltas)."""
    rows_a, rows_b, deltas = [], [], []
    for i in range(rollouts):
        sh = shuffle_base + i
        t0 = time.perf_counter()
        ra = play_one_branch(cfg, presave, sh, pick_a, horizon,
                             depth=depth, chance_depth=chance_depth,
                             score_mode=score_mode, pool=pool)
        rb = play_one_branch(cfg, presave, sh, pick_b, horizon,
                             depth=depth, chance_depth=chance_depth,
                             score_mode=score_mode, pool=pool)
        ga = ra.get("discounted_return"); gb = rb.get("discounted_return")
        d = (ga - gb) if (ga is not None and gb is not None) else None
        if d is not None:
            deltas.append(d)
        rows_a.append(ra); rows_b.append(rb)
        if verbose:
            print(json.dumps({"pair": f"{pick_a}|{pick_b}", "h": horizon, "shuffle": sh,
                              "A_G": round(ga, 4) if ga is not None else None,
                              "B_G": round(gb, 4) if gb is not None else None,
                              "delta": round(d, 4) if d is not None else None,
                              "secs": round(time.perf_counter() - t0, 1)}, ensure_ascii=False), flush=True)
    stats = _paired_stats(deltas)
    summary = {
        "presave": Path(presave).name, "pick_a": pick_a, "pick_b": pick_b,
        "horizon": horizon, "rollouts": rollouts,
        "paired_delta_stats": stats,
        "mean_G_a": round(statistics.mean([r["discounted_return"] for r in rows_a if r.get("discounted_return") is not None]), 4) if any(r.get("discounted_return") is not None for r in rows_a) else None,
        "mean_G_b": round(statistics.mean([r["discounted_return"] for r in rows_b if r.get("discounted_return") is not None]), 4) if any(r.get("discounted_return") is not None for r in rows_b) else None,
        "verdict": ("SIGNAL" if stats.get("n", 0) >= 3 and abs(stats.get("effect_over_se", 0)) >= 2.0
                    else "NOISE/INCONCLUSIVE"),
    }
    return summary, rows_a, rows_b, deltas


def scout_first_reward(cfg, pool, presave, shuffle_seed, *, depth, chance_depth, score_mode):
    """Play in from the presave and report the cards offered at the FIRST
    card_reward (no forced pick — we stop and read the offer). Returns the list
    of offered card_ids, or [] if none reached."""
    cli = Sts2CliAdapter(cfg); cli.start()
    presave = str(Path(presave).resolve())
    try:
        r = cli.load_save(presave)
        if r.get("type") == "error":
            return []
        cli.reseed_rng_stream({"Shuffle": int(shuffle_seed)})
        state = r
        for _ in range(200):
            dec = str(state.get("decision") or "")
            if dec in ("game_over", "defeat"):
                return []
            if dec == "card_reward":
                return [_norm_card((c or {}).get("card_id") or (c or {}).get("id"))
                        for c in _offered_cards(state)]
            if dec == "map_select":
                ch = state.get("choices") or []
                if not ch:
                    return []
                state = cli.action("select_map_node", {"col": int(ch[0]["col"]), "row": int(ch[0]["row"])})
                continue
            if dec == "combat_play":
                spec = CombatSpec(character="Ironclad", encounter="", seed=str(shuffle_seed))
                sc = CombatStepConfig(cli_cfg=cfg, spec=spec, depth=depth, chance_depth=chance_depth,
                                      score_mode=score_mode, max_workers=4,
                                      room_type=str((state.get("context") or {}).get("room_type") or ""),
                                      worker_pool=pool, max_search_ms=30000.0)
                plan = PlanState(); steps = 0
                while str(state.get("decision") or "") == "combat_play" and steps < 200:
                    ss = cli.get_search_state().get("combat_state_for_search") or {}
                    if not ss.get("success"):
                        break
                    sr = decide_combat_action(cli, ss, sc, plan)
                    state = cli.action(sr.action, args=sr.payload, with_snapshot=False)
                    steps += 1
                continue
            if dec == "event_choice":
                state = cli.action("choose_option", choose_event_option(state)); continue
            if dec == "rest_site":
                state = cli.action("choose_option", choose_rest_option(state)); continue
            if dec == "card_select":
                pick = choose_card_select_pick(state, REPO)
                state = cli.action("skip_select", {}) if pick is None else cli.action("select_cards", {"indices": str(pick["indices"])})
                continue
            if dec == "shop":
                state = cli.action("leave_room", {}); continue
            if dec in ("treasure", "unknown"):
                state = cli.action("proceed", {}); continue
            nxt = cli.action("proceed", {})
            if nxt.get("type") == "error" or not nxt.get("decision"):
                return []
            state = nxt
        return []
    finally:
        cli.stop()


def _load_coef() -> Dict[str, float]:
    try:
        return json.loads((REPO / "data/learning/deck_card_value_coef.json").read_text())["coef"]
    except Exception:
        return {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--presave", help="single-comparison mode: one presave")
    ap.add_argument("--pick-a-card", help="single mode: card to force (branch A)")
    ap.add_argument("--pick-b", default="skip", help="single mode: card_id or 'skip' (branch B)")
    # sweep mode
    ap.add_argument("--sweep-presaves", nargs="*", help="decisive sweep: list of presave paths")
    ap.add_argument("--horizons", nargs="*", type=int, default=None,
                    help="sweep: horizons to test (e.g. 1 3)")
    ap.add_argument("--horizon", type=int, default=3, help="number of fights to roll out")
    ap.add_argument("--rollouts", type=int, default=6, help="CRN shuffle seeds")
    ap.add_argument("--shuffle-base", type=int, default=9000)
    ap.add_argument("--depth", type=int, default=10)
    ap.add_argument("--chance-depth", type=int, default=1)
    ap.add_argument("--score-mode", default="balanced")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    cfg = CliConfig(repo_root=REPO, dotnet_path=_dotnet())
    pool = CombatWorkerPool(cfg)

    # ---- decisive sweep mode -------------------------------------------------
    if a.sweep_presaves:
        coef = _load_coef()
        horizons = a.horizons or [1, 3]
        results = []
        try:
            for ps in a.sweep_presaves:
                offered = scout_first_reward(cfg, pool, ps, a.shuffle_base,
                                             depth=a.depth, chance_depth=a.chance_depth,
                                             score_mode=a.score_mode)
                if len(offered) < 2:
                    print(json.dumps({"presave": Path(ps).name, "skip": "fewer than 2 offered cards", "offered": offered}, ensure_ascii=False), flush=True)
                    continue
                ranked = sorted(offered, key=lambda c: coef.get(c, 0.0), reverse=True)
                strong, weak = ranked[0], ranked[-1]
                pairs = [(strong, weak), (strong, "skip"), (weak, "skip")]
                print(json.dumps({"presave": Path(ps).name, "offered": offered,
                                  "strong": strong, "weak": weak,
                                  "coef": {c: round(coef.get(c, 0.0), 3) for c in offered}}, ensure_ascii=False), flush=True)
                for h in horizons:
                    for (pa, pb) in pairs:
                        summ, ra, rb, dl = run_crn_comparison(
                            cfg, pool, ps, pa, pb, h, a.rollouts, a.shuffle_base,
                            depth=a.depth, chance_depth=a.chance_depth, score_mode=a.score_mode)
                        results.append({"presave": Path(ps).name, "strong": strong, "weak": weak,
                                        "summary": summ})
                        st = summ["paired_delta_stats"]
                        print(json.dumps({"RESULT": {"presave": Path(ps).name, "pair": f"{pa} vs {pb}",
                                          "h": h, "mean_delta": st.get("mean"),
                                          "effect_over_se": st.get("effect_over_se"),
                                          "verdict": summ["verdict"]}}, ensure_ascii=False), flush=True)
        finally:
            pool.close()
        if a.out:
            Path(a.out).write_text(json.dumps({"results": results}, ensure_ascii=False, indent=1))
        # rollup
        sig = sum(1 for r in results if r["summary"]["verdict"] == "SIGNAL")
        print(json.dumps({"SWEEP_ROLLUP": {"comparisons": len(results), "SIGNAL": sig,
                          "by_pair_and_h": [{"presave": r["presave"],
                                             "pair": f'{r["summary"]["pick_a"]} vs {r["summary"]["pick_b"]}',
                                             "h": r["summary"]["horizon"],
                                             "mean": r["summary"]["paired_delta_stats"].get("mean"),
                                             "eff/se": r["summary"]["paired_delta_stats"].get("effect_over_se"),
                                             "verdict": r["summary"]["verdict"]} for r in results]}},
                         ensure_ascii=False, indent=2))
        return

    # ---- single-comparison mode ---------------------------------------------
    try:
        summary, rows_a, rows_b, deltas = run_crn_comparison(
            cfg, pool, a.presave, a.pick_a_card, a.pick_b, a.horizon, a.rollouts,
            a.shuffle_base, depth=a.depth, chance_depth=a.chance_depth, score_mode=a.score_mode)
    finally:
        pool.close()
    print(json.dumps({"SUMMARY": summary}, ensure_ascii=False, indent=2))
    if a.out:
        Path(a.out).write_text(json.dumps({"summary": summary, "rows_a": rows_a, "rows_b": rows_b,
                                           "deltas": deltas}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
