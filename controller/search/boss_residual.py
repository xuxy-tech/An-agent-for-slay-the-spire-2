"""Boss-specific residual on top of the hand-tuned leaf evaluator.

    V(s) = baseline_effect_score(s) + Δ_boss(s)

Δ_boss is a small LINEAR model over features the hand-tuned leaf evaluator does
not directly value. The search already scores every line after the enemy turn is
resolved, so these features deliberately avoid immediate damage/block/energy:

  - quality of the visible next draw cycle, not just hand size;
  - future attack/block density in the visible cycle;
  - scaling that has been deployed, plus scaling cards still waiting for a safe
    window;
  - boss phase/scaling pressure that matters beyond the current leaf horizon.

Safety contract: if no weights are loaded for the active boss, Δ_boss returns
EXACTLY 0.0, so the evaluator is bit-identical to the hand-tuned baseline. This
is the fallback the whole design rests on — a bad/untrained residual can only
no-op, never regress below baseline.

Weights live in data/learning/boss_residual/<BOSS>.json:
  {"feature_order": [...], "weights": {...}, "scale": <float>}
Loaded lazily and cached per boss. Set env STS2_BOSS_RESIDUAL=<path-or-dir> to
point at an experiment's weights; unset => no residual (pure baseline).
"""
from __future__ import annotations

from controller.combat_intent import intent_total_damage

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from controller.search.card_strength import expected_visible_draw_pool_strength

# Ironclad power / scaling cards whose value is future-compounding (baseline-blind).
SCALING_POWERS = {
    "DEMON_FORM", "INFLAME", "METALLICIZE", "FEEL_NO_PAIN", "JUGGERNAUT",
    "COMBUST", "EVOLVE", "FIRE_BREATHING", "RUPTURE", "BERSERK", "BARRICADE",
    "DARK_EMBRACE", "BRAND", "STONE_ARMOR", "CORRUPTION", "MAGNETISM",
    "PANACHE", "MENTAL_FORTRESS",
}

ATTACK_CARD_IDS = {
    "STRIKE_IRONCLAD", "BASH", "POMMEL_STRIKE", "CLOTHESLINE", "ANGER",
    "TWIN_STRIKE", "HEAVY_BLADE", "PERFECTED_STRIKE", "WILD_STRIKE",
    "CARNAGE", "HEMOKINESIS", "BLUDGEON", "FIEND_FIRE", "REAPER",
    "ASHEN_STRIKE", "STOMP", "MOLTEN_FIST", "VICIOUS",
}

BLOCK_CARD_IDS = {
    "DEFEND_IRONCLAD", "SHRUG_IT_OFF", "TRUE_GRIT", "ARMAMENTS",
    "IMPERVIOUS", "FLAME_BARRIER", "POWER_THROUGH", "SECOND_WIND",
    "GHOSTLY_ARMOR", "COLOSSUS", "STONE_ARMOR", "TAUNT",
}

ENEMY_SCALING_POWER_IDS = {"RITUAL", "STRENGTH", "STRENGTH_POWER"}

FEATURE_ORDER = [
    "power_stacks",
    "scaling_in_hand",
    "scaling_window",
    "boss_non_attack_turn",
    "draw_pool_strength",
    "future_attack_density",
    "future_block_density",
    "enemy_scaling_pressure",
    "ceremonial_plow_window",
]

_CACHE: Dict[str, Optional[Dict[str, Any]]] = {}


def _weights_for(boss: str) -> Optional[Dict[str, Any]]:
    """Load + cache the residual weights for a boss, or None if unavailable."""
    if boss in _CACHE:
        return _CACHE[boss]
    env = os.environ.get("STS2_BOSS_RESIDUAL")
    path: Optional[Path] = None
    if env:
        p = Path(env)
        path = p if p.is_file() else (p / f"{boss}.json")
    else:
        path = Path(__file__).resolve().parents[2] / "data/learning/boss_residual" / f"{boss}.json"
    data: Optional[Dict[str, Any]] = None
    try:
        if path and path.is_file():
            data = json.loads(path.read_text())
    except Exception:
        data = None
    _CACHE[boss] = data
    return data


def _boss_id(search_state: Mapping[str, Any], root_combat: Optional[Mapping[str, Any]]) -> str:
    """Identify the boss from enemy monster_ids (the residual is per-boss)."""
    src = root_combat or search_state.get("combat") or {}
    ids = {str(e.get("monster_id") or "") for e in (src.get("enemies") or [])}
    if any("VANTOM" in i for i in ids):
        return "VANTOM"
    if any("KIN" in i for i in ids):
        return "THE_KIN"
    if any("CEREMONIAL" in i or "BEAST" in i for i in ids):
        return "CEREMONIAL_BEAST"
    return ""


def _card_id(card: Mapping[str, Any]) -> str:
    return str(card.get("card_id") or card.get("id") or "").strip().upper()


def _visible_cards(combat: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    for key in ("hand", "draw_pile", "discard_pile"):
        for card in combat.get(key) or []:
            if isinstance(card, dict):
                yield card


def _visible_density(combat: Mapping[str, Any], card_ids: set[str]) -> float:
    total = 0
    matched = 0
    for card in _visible_cards(combat):
        total += 1
        if _card_id(card) in card_ids:
            matched += 1
    if total <= 0:
        return 0.0
    # Scale to a five-card hand expectation so weights live near the evaluator's
    # normal score range and are comparable to draw_pool_strength.
    return 5.0 * float(matched) / float(total)


def _power_amount(entity: Mapping[str, Any], power_ids: set[str]) -> float:
    total = 0.0
    wanted = {p.upper() for p in power_ids}
    for power in entity.get("powers") or []:
        pid = str(power.get("id") or power.get("power_id") or "").upper()
        if pid not in wanted:
            continue
        try:
            total += float(power.get("amount") or 0.0)
        except Exception:
            continue
    return total


def _enemy_scaling_pressure(enemies: Iterable[Mapping[str, Any]]) -> float:
    total = 0.0
    for enemy in enemies:
        total += _power_amount(enemy, ENEMY_SCALING_POWER_IDS)
    return total


def _ceremonial_plow_window(enemies: Iterable[Mapping[str, Any]]) -> float:
    """How close the Beast is to the once-per-fight 150HP stun threshold.

    This is state-only boss phase information, not a forced action rule. It can
    only matter if rollout labels learn that being near the threshold changes the
    value of preserving burst/draw for the next turn.
    """
    for enemy in enemies:
        mid = str(enemy.get("monster_id") or "")
        if "CEREMONIAL" not in mid and "BEAST" not in mid:
            continue
        hp = float(enemy.get("hp") or 0.0)
        has_plow = any(
            str(p.get("id") or p.get("power_id") or "").upper() in {"PLOW", "PLOW_POWER"}
            for p in enemy.get("powers") or []
        )
        if not has_plow or hp <= 150.0:
            return 0.0
        distance = hp - 150.0
        return max(0.0, 1.0 - distance / 35.0)
    return 0.0


def residual_features(search_state: Mapping[str, Any], root_combat: Optional[Mapping[str, Any]]) -> Dict[str, float]:
    """Small future-info feature set for post-enemy-turn leaf residuals."""
    combat = search_state.get("combat") or {}
    player = combat.get("player") or {}
    enemies = combat.get("enemies") or []

    # scaling power stacks the player has accumulated on board (future value)
    power_stacks = 0.0
    for p in (player.get("powers") or []):
        amt = p.get("amount")
        power_stacks += float(amt) if isinstance(amt, (int, float)) else 0.0

    # scaling/power cards sitting in hand (development opportunity not yet taken)
    hand_ids = [str(c.get("card_id") or "") for c in (combat.get("hand") or [])]
    scaling_in_hand = float(sum(1 for c in hand_ids if c in SCALING_POWERS))

    # Boss timing at the leaf: after the enemy turn is resolved, this is the next
    # visible intent. Baseline sees incoming damage, but not how that combines
    # with the opportunity cost of deploying scaling.
    non_attack_turn = 1.0
    for e in enemies:
        it = (e.get("intent") or {})
        dmg = intent_total_damage(it)
        if dmg > 0:
            non_attack_turn = 0.0
        types = it.get("intent_types") or []
        if any(t in ("Attack",) for t in types) and not isinstance(dmg, (int, float)):
            non_attack_turn = 0.0

    return {
        "power_stacks": power_stacks,
        "scaling_in_hand": scaling_in_hand,
        "scaling_window": scaling_in_hand * non_attack_turn,
        "boss_non_attack_turn": non_attack_turn,
        "draw_pool_strength": expected_visible_draw_pool_strength(dict(search_state)),
        "future_attack_density": _visible_density(combat, ATTACK_CARD_IDS),
        "future_block_density": _visible_density(combat, BLOCK_CARD_IDS),
        "enemy_scaling_pressure": _enemy_scaling_pressure(enemies),
        "ceremonial_plow_window": _ceremonial_plow_window(enemies),
    }


def boss_residual(search_state: Mapping[str, Any], root_combat: Optional[Mapping[str, Any]]) -> float:
    """Δ_boss(s). Returns 0.0 unless trained weights exist for the active boss."""
    boss = _boss_id(search_state, root_combat)
    if not boss:
        return 0.0
    w = _weights_for(boss)
    if not w:
        return 0.0
    feats = residual_features(search_state, root_combat)
    order = w.get("feature_order") or FEATURE_ORDER
    if list(order) != FEATURE_ORDER:
        return 0.0
    weights = w.get("weights") or {}
    means = w.get("feature_mean") or {}
    val = float(w.get("intercept", 0.0))
    for k in order:
        val += float(weights.get(k, 0.0)) * (float(feats.get(k, 0.0)) - float(means.get(k, 0.0)))
    return val * float(w.get("scale", 1.0))
