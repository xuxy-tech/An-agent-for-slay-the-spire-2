"""Action-aware root residual for R0 combat learning.

This is intentionally separate from boss_residual.py. The old residual scores
leaf states and can cancel across sibling actions. R0 scores the candidate root
action together with its immediate child/leaf context:

    score(root_action) = balanced_leaf_score + w . phi(root, action, child, leaf)

Safety contract: unless a weights file exists, or when all weights are zero, this
module returns exactly 0.0. Production balanced mode stays unchanged.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from controller.combat_intent import intent_deals_damage, intent_total_damage
from controller.search.actions import SearchAction

REPO_ROOT = Path(__file__).resolve().parents[2]
CARD_META_PATH = REPO_ROOT / "data" / "card_metadata" / "ironclad.json"

FEATURES_A = [
    "plays_power",
    "plays_power_when_survivable",
    "affordable_power_count_in_hand",
    "turn_number",
    "enemy_total_hp",
    "incoming_survivable_after_action",
]

FEATURES_B = FEATURES_A + [
    "positive_power_stock",
    "visible_power_count",
    "power_stock_gain_action",
]

FEATURES_C = FEATURES_B + [
    "power_class_scaling",
    "power_class_damage_engine",
    "power_class_block_engine",
    "power_class_other",
]

FEATURES_D = FEATURES_C + [
    "card_INFLAME",
    "card_INFERNO",
    "card_STAMPEDE",
    "card_RUPTURE",
    "card_CRUELTY",
    "card_FEEL_NO_PAIN",
    "card_DARK_EMBRACE",
    "card_DRUM_OF_BATTLE",
    "card_PYRE",
    "card_STONE_ARMOR",
]

FEATURES_BY_MODE = {
    "balanced_r0a": FEATURES_A,
    "balanced_r0b": FEATURES_B,
    "balanced_r0c": FEATURES_C,
    "balanced_r0d": FEATURES_D,
}

SCALING_POWERS = {
    "AGGRESSION",
    "BARRICADE",
    "BERSERK",
    "BRUTALITY",
    "COMBUST",
    "CORRUPTION",
    "CRUELTY",
    "DEMON_FORM",
    "EVOLVE",
    "INFLAME",
    "LIMIT_BREAK",
    "RUPTURE",
    "STAMPEDE",
    "VICIOUS",
}

DAMAGE_ENGINE_POWERS = {
    "FIRE_BREATHING",
    "INFERNO",
    "JUGGERNAUT",
    "PYRE",
    "DRUM_OF_BATTLE",
}

BLOCK_ENGINE_POWERS = {
    "BARRICADE",
    "FEEL_NO_PAIN",
    "METALLICIZE",
    "STONE_ARMOR",
}

POSITIVE_POWER_IDS = {
    "STRENGTH",
    "STRENGTH_POWER",
    "DEXTERITY",
    "DEXTERITY_POWER",
    "RITUAL",
    "METALLICIZE",
    "FEEL_NO_PAIN",
    "INFERNO",
    "INFLAME",
    "RUPTURE",
    "DARK_EMBRACE",
    "BARRICADE",
    "JUGGERNAUT",
    "COMBUST",
    "FIRE_BREATHING",
    "EVOLVE",
    "BRUTALITY",
    "CORRUPTION",
    "DRUM_OF_BATTLE",
    "STAMPEDE",
    "PYRE",
    "STONE_ARMOR",
    "CRUELTY",
    "VICIOUS",
}

_CARD_META: Optional[Dict[str, Dict[str, Any]]] = None
_WEIGHT_CACHE: Dict[str, Optional[Dict[str, Any]]] = {}


def is_r0_mode(mode: str) -> bool:
    return mode in FEATURES_BY_MODE


def leaf_mode_for(mode: str) -> str:
    return "balanced" if is_r0_mode(mode) else mode


def feature_order_for(mode: str) -> Sequence[str]:
    return FEATURES_BY_MODE.get(mode, ())


def _card_meta() -> Dict[str, Dict[str, Any]]:
    global _CARD_META
    if _CARD_META is not None:
        return _CARD_META
    try:
        payload = json.loads(CARD_META_PATH.read_text())
        _CARD_META = payload if isinstance(payload, dict) else {}
    except Exception:
        _CARD_META = {}
    return _CARD_META


def _card_id(card: Optional[Mapping[str, Any]], fallback: Optional[str] = None) -> str:
    if not isinstance(card, Mapping):
        card = {}
    return str(card.get("card_id") or card.get("id") or fallback or "").strip().upper()


def _card_type(card_id: str) -> str:
    return str((_card_meta().get(card_id) or {}).get("type") or "").strip().upper()


def _card_cost(card: Optional[Mapping[str, Any]], fallback_card_id: str = "") -> float:
    if not isinstance(card, Mapping):
        card = {}
    for key in ("current_cost", "cost"):
        try:
            if card.get(key) is not None:
                return float(card.get(key))
        except Exception:
            pass
    meta = _card_meta().get(_card_id(card, fallback_card_id)) or {}
    try:
        return float(meta.get("cost") or 0.0)
    except Exception:
        return 0.0


def _power_stock(entity: Mapping[str, Any]) -> float:
    total = 0.0
    for power in entity.get("powers") or []:
        if not isinstance(power, Mapping):
            continue
        pid = str(power.get("id") or power.get("power_id") or "").strip().upper()
        if pid not in POSITIVE_POWER_IDS:
            continue
        try:
            total += max(1.0, float(power.get("amount") or 0.0))
        except Exception:
            total += 1.0
    return total


def _incoming_damage(combat: Mapping[str, Any]) -> float:
    total = 0.0
    for enemy in combat.get("enemies") or []:
        if not isinstance(enemy, Mapping):
            continue
        intent = enemy.get("intent") or {}
        if not intent_deals_damage(intent):
            continue
        try:
            total += intent_total_damage(intent)
        except Exception:
            continue
    return total


def _visible_cards(combat: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    out = []
    for key in ("hand", "draw_pile", "discard_pile"):
        for card in combat.get(key) or []:
            if isinstance(card, Mapping):
                out.append(card)
    return out


def _power_class(card_id: str) -> str:
    if card_id in BLOCK_ENGINE_POWERS:
        return "block_engine"
    if card_id in DAMAGE_ENGINE_POWERS:
        return "damage_engine"
    if card_id in SCALING_POWERS:
        return "scaling"
    return "other"


def root_action_features(
    root_state: Mapping[str, Any],
    action: SearchAction,
    child_state: Optional[Mapping[str, Any]],
    leaf_state: Optional[Mapping[str, Any]],
    mode: str,
) -> Dict[str, float]:
    root_combat = root_state.get("combat") or {}
    child_combat = (child_state or {}).get("combat") or {}
    leaf_combat = (leaf_state or {}).get("combat") or child_combat
    hand = root_combat.get("hand") or []
    root_player = root_combat.get("player") or {}
    child_player = child_combat.get("player") or {}
    leaf_player = leaf_combat.get("player") or child_player
    enemies = root_combat.get("enemies") or []

    card = {}
    if action.card_index is not None and 0 <= action.card_index < len(hand):
        maybe = hand[action.card_index]
        card = maybe if isinstance(maybe, Mapping) else {}
    metadata = action.metadata or {}
    card_id = _card_id(card, metadata.get("card_id"))
    is_play_card = action.action_type == "play_card"
    plays_power = 1.0 if is_play_card and _card_type(card_id) == "POWER" else 0.0

    energy = float(root_player.get("energy") or 0.0)
    affordable_power_count = 0
    for c in hand:
        if not isinstance(c, Mapping):
            continue
        cid = _card_id(c)
        if _card_type(cid) != "POWER":
            continue
        if _card_cost(c, cid) <= energy:
            affordable_power_count += 1

    enemy_total_hp = 0.0
    for enemy in enemies:
        if isinstance(enemy, Mapping):
            enemy_total_hp += float(enemy.get("hp") or 0.0)

    try:
        turn_number = float(root_combat.get("turn_number") or root_combat.get("round_number") or 1.0)
    except Exception:
        turn_number = 1.0

    incoming = _incoming_damage(root_combat)
    try:
        child_hp = float(child_player.get("hp") if child_player.get("hp") is not None else root_player.get("hp") or 0.0)
        child_block = float(child_player.get("block") if child_player.get("block") is not None else root_player.get("block") or 0.0)
    except Exception:
        child_hp, child_block = 0.0, 0.0
    incoming_survivable = 1.0 if max(0.0, incoming - child_block) < child_hp else 0.0

    root_stock = _power_stock(root_player)
    leaf_stock = _power_stock(leaf_player)
    visible_power_count = sum(
        1 for c in _visible_cards(root_combat)
        if _card_type(_card_id(c)) == "POWER"
    )

    feats: Dict[str, float] = {
        "plays_power": plays_power,
        "plays_power_when_survivable": plays_power * incoming_survivable,
        "affordable_power_count_in_hand": float(affordable_power_count),
        "turn_number": turn_number,
        "enemy_total_hp": enemy_total_hp / 100.0,
        "incoming_survivable_after_action": incoming_survivable,
        "positive_power_stock": root_stock,
        "visible_power_count": float(visible_power_count),
        "power_stock_gain_action": max(0.0, leaf_stock - root_stock),
        "power_class_scaling": 0.0,
        "power_class_damage_engine": 0.0,
        "power_class_block_engine": 0.0,
        "power_class_other": 0.0,
    }
    if plays_power:
        cls = _power_class(card_id)
        feats[f"power_class_{cls}"] = 1.0
        feats[f"card_{card_id}"] = 1.0
    for key in FEATURES_D:
        feats.setdefault(key, 0.0)
    return {key: float(feats.get(key, 0.0)) for key in feature_order_for(mode)}


def _weights_for(mode: str) -> Optional[Dict[str, Any]]:
    if mode in _WEIGHT_CACHE:
        return _WEIGHT_CACHE[mode]
    env = os.environ.get("STS2_ROOT_ACTION_RESIDUAL")
    if env:
        p = Path(env)
        path = p if p.is_file() else (p / f"{mode}.json")
    else:
        path = REPO_ROOT / "data" / "learning" / "root_action_residual" / f"{mode}.json"
    data: Optional[Dict[str, Any]] = None
    try:
        if path.is_file():
            data = json.loads(path.read_text())
    except Exception:
        data = None
    _WEIGHT_CACHE[mode] = data
    return data


def root_action_residual(
    mode: str,
    root_state: Mapping[str, Any],
    action: SearchAction,
    child_state: Optional[Mapping[str, Any]],
    leaf_state: Optional[Mapping[str, Any]],
) -> tuple[float, Dict[str, float]]:
    if not is_r0_mode(mode):
        return 0.0, {}
    feats = root_action_features(root_state, action, child_state, leaf_state, mode)
    payload = _weights_for(mode)
    if not payload:
        return 0.0, feats
    order = list(payload.get("feature_order") or feature_order_for(mode))
    allowed = list(feature_order_for(mode))
    if order != allowed:
        return 0.0, feats
    weights = payload.get("weights") or {}
    intercept = float(payload.get("intercept", 0.0))
    scale = float(payload.get("scale", 1.0))
    value = intercept
    for key in order:
        value += float(weights.get(key, 0.0)) * float(feats.get(key, 0.0))
    return value * scale, feats
