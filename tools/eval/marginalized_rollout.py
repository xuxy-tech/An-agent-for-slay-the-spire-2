#!/usr/bin/env python3
"""Marginalized rollout: E[retained-HP] over draw-shuffle noise for ONE situation.

The denoising instrument behind the whole "golden-finger" direction. Fix a boss
situation (encounter + deck + seed) and play the SAME combat K times, each with a
freshly reseeded Shuffle stream (reseed_rng_stream), leaving every other stream
(MonsterAi, card-gen, ...) frozen. The spread of retained-HP across the K draws
is the draw-order noise floor — it tells us how much of "we can't learn anything"
was just shuffle variance drowning the signal.

Caveat (verified): start_test_combat deals the opening hand + first draw BEFORE
we can reseed, so turn 1 is shared across rollouts; divergence begins at the first
reshuffle (~turn 2). That shares one turn of noise but captures the bulk of it.

Usage:
  python3 tools/marginalized_rollout.py --boss CEREMONIAL_BEAST_BOSS --seed 100 \
      --rollouts 16 --depth 2
"""
from __future__ import annotations
import argparse, json, statistics, sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from cli.runtime_paths import resolve_dotnet
from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.search.combat_search import CombatSearcher, CombatSpec, CombatWorkerPool
from controller.search.actions import cli_payload_for_action

_card_meta_path = REPO / "data/card_metadata/ironclad.json"
CARD_META = json.loads(_card_meta_path.read_text()) if _card_meta_path.exists() else {}


def _dotnet():
    return resolve_dotnet()


def _cof(s):
    return (s.get("combat_state_for_search") or {}).get("combat") or {}


def _terminal_from_frame(res):
    """Interpret an action result frame for combat termination.

    Returns a dict {outcome, retained, hp, max_hp} if the frame is terminal,
    else None. Real combat END only:
      - loss: next decision game_over/defeat.
      - win : a genuine POST-combat decision (left combat alive), or all enemies
        dead. Post-combat player.hp (after Burning Blood) is the retained label.

    NOT terminal (returns None so the caller keeps playing):
      - decision == combat_play (still fighting).
      - card_select / bundle_select: a MID-combat modal (e.g. a card that opens a
        selection). run_agent resolves these with select_cards/skip and continues
        the fight — they are NOT a win. (Bug fixed 2026-06-17: the old code did
        `if decision: return WON`, mislabeling a mid-combat card_select as a win
        with all enemies still alive. seed42 THE_KIN "wins" were this artifact;
        played to a real terminal the line DIES at end_turn->game_over.)
    """
    if not isinstance(res, dict):
        return None
    decision = str(res.get("decision") or "")
    player = res.get("player") or {}
    hp = float(player.get("hp") or 0.0)
    mhp = float(player.get("max_hp") or 1.0)

    # Explicit loss.
    if decision in ("game_over", "defeat") or res.get("type") == "game_over":
        return {"outcome": "dead", "retained": 0.0, "hp": 0.0, "max_hp": mhp}

    # Still mid-combat, or a mid-combat selection modal: not terminal.
    if decision in ("combat_play", "card_select", "bundle_select"):
        return None

    # A genuine post-combat decision means the fight resolved as a win (we left
    # combat into card_reward/map_select/treasure/victory/...). Trust this
    # frame's post-combat hp (Burning Blood heal already settled).
    _POST_COMBAT = {"card_reward", "map_select", "combat_reward", "victory",
                    "treasure", "shop", "rest_site", "event_choice"}
    if decision in _POST_COMBAT:
        return {"outcome": "won", "retained": hp / mhp if mhp > 0 else 0.0,
                "hp": hp, "max_hp": mhp}

    # No/unknown decision but enemies are all gone -> also a win.
    enemies = res.get("enemies")
    if enemies is not None and not any(
            float((e or {}).get("hp") or 0) > 0 for e in enemies):
        return {"outcome": "won", "retained": hp / mhp if mhp > 0 else 0.0,
                "hp": hp, "max_hp": mhp}
    return None


def _action_observation(action):
    metadata = action.metadata or {}
    card_id = metadata.get("card_id")
    card_type = (CARD_META.get(str(card_id or "")) or {}).get("type")
    return {
        "action_type": action.action_type,
        "card_id": card_id,
        "card_type": card_type,
        "target_index": action.target_index,
        "target_monster_id": metadata.get("target_monster_id"),
        "potion_id": metadata.get("potion_id"),
    }


def _trace_summary(actions):
    action_mix = Counter()
    card_type_mix = Counter()
    power_play_count = 0
    play_count = 0
    for action in actions:
        action_type = action.get("action_type")
        if action_type:
            action_mix[action_type] += 1
        if action_type == "play_card":
            play_count += 1
            card_type = action.get("card_type") or "UNKNOWN"
            card_type_mix[card_type] += 1
            if card_type == "POWER":
                power_play_count += 1
    return {
        "n_actions": len(actions),
        "action_mix": dict(action_mix),
        "card_type_mix": dict(card_type_mix),
        "power_play_count": power_play_count,
        "play_count": play_count,
        "power_play_rate": (power_play_count / play_count) if play_count else 0.0,
    }


def rollout_once(cfg, boss, run_seed, shuffle_seed, *, depth, chance_depth,
                 score_mode, max_steps, pool, snapshot_json=None,
                 presave_path=None, map_node=None, inject=None,
                 trace_actions=False):
    """Play one full combat with Shuffle reseeded to `shuffle_seed`.

    Entry modes (checked in order):
      - inject given: start_run -> set_player(inject) -> reseed Shuffle ->
        enter_room(combat, boss). Coarse counterfactual deck contrast; UPGRADE
        LEVELS LOST (set_player rebuilds by card_id). inject = dict with keys
        deck (required, list of card_id), hp, max_hp, relics, potions.
      - snapshot_json given: import + restore a real eval-set situation (real
        deck, relics, boss HP) — the correct denoising substrate.
      - presave_path given: native pre-room save + reseed + walk in.
      - else: start_test_combat with the starter deck (toy / smoke only).

    Returns dict: outcome, retained (fraction), step, hp, max_hp.
    The driver cli is resynced from the exported snapshot before each apply
    (avoids the in-place use_potion-noop drift class)."""
    cli = Sts2CliAdapter(cfg)
    traced_actions = []

    def _finish(payload):
        if trace_actions:
            payload = dict(payload)
            payload["actions"] = list(traced_actions)
            payload["trace"] = _trace_summary(traced_actions)
        return payload

    cli.start()
    try:
        if inject is not None:
            cli.start_run(character="Ironclad", seed=str(run_seed), lang="en")
            fields = {"deck": list(inject["deck"])}
            for k in ("hp", "max_hp", "relics", "potions"):
                if inject.get(k) is not None:
                    fields[k] = inject[k]
            sp = cli.set_player(**fields)
            if sp.get("type") == "error":
                return {"outcome": "set_player_failed", "retained": None, "step": 0}
            # Reseed BEFORE enter_room so the opening-hand deal uses the new seed.
            if shuffle_seed is not None:
                rr = cli.reseed_rng_stream({"Shuffle": int(shuffle_seed)})
                if not rr.get("success"):
                    return {"outcome": "reseed_failed", "retained": None, "step": 0}
            er = cli.enter_room("combat", encounter=boss)
            if er.get("type") == "error":
                return {"outcome": "enter_room_failed", "retained": None, "step": 0}
        elif presave_path is not None:
            ld = cli.load_save(str(presave_path))
            if ld.get("type") == "error":
                return {"outcome": "load_failed", "retained": None, "step": 0}
            # Reseed BEFORE walking into the room so the shuffle+deal that
            # enter_room performs uses the new seed (re-randomizes opening hand).
            if shuffle_seed is not None:
                rr = cli.reseed_rng_stream({"Shuffle": int(shuffle_seed)})
                if not rr.get("success"):
                    return {"outcome": "reseed_failed", "retained": None, "step": 0}
            # Walk into the hard room via the saved map node; the engine routes
            # naturally (frozen non-Shuffle streams pick the same encounter).
            if map_node is not None:
                cli.action("select_map_node",
                           {"col": int(map_node["col"]), "row": int(map_node["row"])})
            # Advance through any intermediate non-combat frames until combat.
            for _ in range(8):
                if _cof(cli.get_search_state()):
                    break
                dec = str((cli.get_search_state() or {}).get("decision") or "")
                if dec in ("game_over", "defeat", "victory", ""):
                    break
                cli.action("proceed", {})
        elif snapshot_json is not None:
            cli.import_combat_snapshot(snapshot_json, "evalroot")
            cli.restore_combat_snapshot("evalroot")
            # Reseed ONLY the draw-shuffle stream; freeze everything else.
            if shuffle_seed is not None:
                rr = cli.reseed_rng_stream({"Shuffle": int(shuffle_seed)})
                if not rr.get("success"):
                    return {"outcome": "reseed_failed", "retained": None, "step": 0}
        else:
            cli.send({"cmd": "start_test_combat", "character": "Ironclad",
                      "encounter": boss, "seed": str(run_seed), "lang": "en"})
            if shuffle_seed is not None:
                rr = cli.reseed_rng_stream({"Shuffle": int(shuffle_seed)})
                if not rr.get("success"):
                    return {"outcome": "reseed_failed", "retained": None, "step": 0}

        for step in range(max_steps):
            ss = cli.get_search_state()
            decision = str((ss or {}).get("decision") or "")
            # Real combat end while sitting on a decision frame.
            if decision in ("game_over", "defeat"):
                return _finish({"outcome": "dead", "retained": 0.0, "step": step})
            if decision in ("card_reward", "map_select", "combat_reward",
                            "victory", "treasure", "shop", "rest_site",
                            "event_choice"):
                p = (ss or {}).get("player") or {}
                hp = float(p.get("hp") or 0); mhp = float(p.get("max_hp") or 1)
                return _finish({"outcome": "won", "retained": hp / mhp if mhp > 0 else 0.0,
                        "step": step, "hp": hp, "max_hp": mhp, "via": decision})
            # Mid-combat modal: resolve it (select first / skip / proceed) and keep
            # fighting. NOT a win — mislabeling this was the fake-win bug.
            if decision in ("card_select", "bundle_select"):
                if decision == "bundle_select":
                    cli.action("proceed", {})
                else:
                    rsel = cli.action("select_cards", {"indices": "[0]"})
                    if isinstance(rsel, dict) and rsel.get("type") == "error":
                        cli.action("skip_select", {})
                continue
            c = _cof(ss)
            if not c:
                # Test-combat has no post-combat decision point; a vanished combat
                # means it already ended. Trust the last applied action's terminal
                # frame (handled below); reaching here without one is a stall.
                return _finish({"outcome": "no_combat", "retained": None, "step": step})
            p = c.get("player") or {}
            hp = float(p.get("hp") or 0)
            mhp = float(p.get("max_hp") or 1)
            alive = [e for e in (c.get("enemies") or []) if float(e.get("hp") or 0) > 0]
            if hp <= 0:
                return _finish({"outcome": "dead", "retained": 0.0, "step": step, "hp": 0, "max_hp": mhp})
            if not alive:
                return _finish({"outcome": "won", "retained": hp / mhp, "step": step, "hp": hp, "max_hp": mhp})

            cli.capture_combat_snapshot(f"s{step}")
            exp = cli.export_combat_snapshot(f"s{step}")
            s = CombatSearcher(
                cfg, CombatSpec(character="Ironclad", encounter=boss, seed=str(run_seed)),
                reuse_cli_processes=True, parallel_top_level=True, max_workers=4,
                score_mode=score_mode, root_snapshot_json=exp["snapshot_json"],
                worker_pool=pool, symmetry_dedup=True)
            try:
                r = s.search(depth=depth, chance_depth=chance_depth)
            finally:
                s.close()
            if not r.sequence:
                return _finish({"outcome": "search_empty", "retained": hp / mhp, "step": step,
                        "hp": hp, "max_hp": mhp})
            if trace_actions:
                traced_actions.append(_action_observation(r.sequence[0]))
            an, pl = cli_payload_for_action(r.sequence[0])
            # Resync driver from the exact exported snapshot before applying.
            cli.import_combat_snapshot(exp["snapshot_json"], f"rs{step}")
            cli.restore_combat_snapshot(f"rs{step}")
            res = cli.action(an, pl)

            # Detect combat termination from the action's result frame (the same
            # signal run_agent uses): a victory/defeat context, or a frame whose
            # enemies are all dead. Post-combat player.hp is the retained-HP label
            # (Burning Blood heal already settled here).
            term = _terminal_from_frame(res)
            if term is not None:
                return _finish({**term, "step": step})
        return _finish({"outcome": "max_steps", "retained": None, "step": max_steps})
    finally:
        cli.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boss", default="CEREMONIAL_BEAST_BOSS")
    ap.add_argument("--seed", default="100", help="run seed (fixes the situation)")
    ap.add_argument("--snapshot", default=None,
                    help="path to an eval-set unit JSON (real deck/relics/boss). "
                         "Overrides start_test_combat; this is the correct substrate.")
    ap.add_argument("--rollouts", type=int, default=16, help="K draw-order samples")
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--chance-depth", type=int, default=1)
    ap.add_argument("--score-mode", default="balanced")
    ap.add_argument("--max-steps", type=int, default=80)
    ap.add_argument("--shuffle-base", type=int, default=1000,
                    help="shuffle seeds = base + i for i in range(rollouts)")
    a = ap.parse_args()

    cfg = CliConfig(repo_root=REPO, dotnet_path=_dotnet())

    snapshot_json = None
    boss = a.boss
    run_seed = a.seed
    if a.snapshot:
        import json as _json
        unit = _json.loads(Path(a.snapshot).read_text())
        snapshot_json = unit["snapshot_json"]
        boss = unit.get("encounter_id", a.boss)
        run_seed = str(unit.get("seed", a.seed))
        es = unit.get("entry_summary") or {}
        print(f"snapshot={Path(a.snapshot).name} enc={boss} seed={run_seed} "
              f"entry_hp={es.get('hp')} enemy_hp={es.get('enemy_hp')}")

    pool = CombatWorkerPool(cfg)
    print(f"boss={boss} run_seed={run_seed} rollouts={a.rollouts} "
          f"depth={a.depth} score_mode={a.score_mode}\n")

    rows = []
    try:
        for i in range(a.rollouts):
            sh = a.shuffle_base + i
            r = rollout_once(cfg, boss, run_seed, sh, depth=a.depth,
                             chance_depth=a.chance_depth, score_mode=a.score_mode,
                             max_steps=a.max_steps, pool=pool, snapshot_json=snapshot_json)
            rows.append(r)
            ret = r.get("retained")
            ret_s = f"{ret:.3f}" if ret is not None else "  -  "
            print(f"  shuffle={sh}: {r['outcome']:12s} retained={ret_s} step={r.get('step')}")
    finally:
        pool.close()

    rets = [r["retained"] for r in rows if r.get("retained") is not None]
    wins = sum(1 for r in rows if r["outcome"] == "won")
    print("\n=== MARGINALIZED OVER DRAW-SHUFFLE NOISE ===")
    print(f"  rollouts        : {len(rows)}")
    print(f"  wins            : {wins}/{len(rows)}")
    if rets:
        mean = statistics.mean(rets)
        sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
        print(f"  E[retained-HP]  : {mean:.4f}")
        print(f"  stdev (noise)   : {sd:.4f}   <-- the draw-order noise floor")
        print(f"  min / max       : {min(rets):.3f} / {max(rets):.3f}")
        print(f"  spread (max-min): {max(rets)-min(rets):.4f}")
    else:
        print("  no terminal retained-HP samples (all stuck/empty)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
