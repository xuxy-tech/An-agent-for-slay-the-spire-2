"""Action-aware boss ranker for root combat decisions.

This is intentionally narrower than the old leaf-state residual:

    score(root_action) = baseline_search_score + learned_bonus(root_state, action)

The bonus is only applied at the top-level root action choice. If no weights are
available for the active boss, the function returns exactly 0.0, preserving the
baseline bit-for-bit.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from controller.combat_intent import intent_deals_damage, intent_total_damage
from controller.search.actions import SearchAction


_CACHE: Dict[str, Optional[Dict[str, Any]]] = {}

SCALING_CARD_IDS = {
    "DEMON_FORM",
    "INFLAME",
    "METALLICIZE",
    "FEEL_NO_PAIN",
    "JUGGERNAUT",
    "DARK_EMBRACE",
    "BRAND",
    "STONE_ARMOR",
    "INFERNO",
    "CRUELTY",
    "STAMPede".upper(),
    "VICIOUS",
}

DRAW_CARD_IDS = {
    "POMMEL_STRIKE",
    "SHRUG_IT_OFF",
    "BATTLE_TRANCE",
    "OFFERING",
    "BURNING_PACT",
    "DARK_EMBRACE",
    "UNRELENTING",
}

BLOCK_CARD_IDS = {
    "DEFEND_IRONCLAD",
    "SHRUG_IT_OFF",
    "TRUE_GRIT",
    "IMPERVIOUS",
    "COLOSSUS",
    "STONE_ARMOR",
    "TAUNT",
    "ARMAMENTS",
}

DEBUFF_CARD_IDS = {
    "BASH",
    "THUNDERCLAP",
    "TAUNT",
    "DOMINATE",
    "MOLTEN_FIST",
    "VICIOUS",
}


def boss_id_from_combat(combat: Mapping[str, Any]) -> str:
    ids = {str(e.get("monster_id") or "") for e in (combat.get("enemies") or [])}
    if any("THE_KIN" in i or "KIN_" in i for i in ids):
        return "THE_KIN"
    if any("CEREMONIAL" in i or "BEAST" in i for i in ids):
        return "CEREMONIAL_BEAST"
    if any("VANTOM" in i for i in ids):
        return "VANTOM"
    return ""


def boss_id_from_state(search_state: Mapping[str, Any]) -> str:
    return boss_id_from_combat(search_state.get("combat") or {})


def _weights_for(boss: str) -> Optional[Dict[str, Any]]:
    if not boss:
        return None
    if boss in _CACHE:
        return _CACHE[boss]

    env = os.environ.get("STS2_BOSS_ACTION_RANKER")
    path: Optional[Path]
    if env:
        p = Path(env)
        path = p if p.is_file() else (p / f"{boss}.json")
    else:
        path = Path(__file__).resolve().parents[2] / "data" / "learning" / "boss_action_ranker" / f"{boss}.json"

    data: Optional[Dict[str, Any]] = None
    try:
        if path and path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = None
    _CACHE[boss] = data
    return data


def action_margin_gate(parent_state: Mapping[str, Any]) -> Optional[float]:
    """Maximum baseline-score gap where the ranker may reorder actions.

    None means no trained ranker. The default is deliberately conservative:
    learning breaks ties / low-margin cases instead of overriding a clearly
    superior baseline line.
    """
    weights = _weights_for(boss_id_from_state(parent_state))
    if not weights:
        return None
    try:
        return float(weights.get("margin_gate", 80.0))
    except Exception:
        return 80.0


def _card_for_action(combat: Mapping[str, Any], action: SearchAction) -> Dict[str, Any]:
    hand = combat.get("hand") or []
    if action.card_index is not None and 0 <= action.card_index < len(hand):
        card = hand[action.card_index]
        if isinstance(card, dict):
            return card
    return {}


def _target_for_action(combat: Mapping[str, Any], action: SearchAction) -> Dict[str, Any]:
    enemies = combat.get("enemies") or []
    for enemy in enemies:
        try:
            if action.target_index is not None and int(enemy.get("index")) == int(action.target_index):
                return enemy
        except Exception:
            continue
    return {}


def _incoming_damage(combat: Mapping[str, Any]) -> float:
    total = 0.0
    for enemy in combat.get("enemies") or []:
        intent = enemy.get("intent") or {}
        if intent_deals_damage(intent):
            total += intent_total_damage(intent)
    return total


def _card_stat(card: Mapping[str, Any], name: str) -> float:
    stats = card.get("stats") or {}
    try:
        if stats.get(name) is not None:
            return float(stats.get(name) or 0.0)
    except Exception:
        pass
    # Search-state cards often expose only dynamic/basic fields; missing stats
    # are treated as unknown, not as proof the card has no effect.
    return 0.0


def _power_amount(entity: Mapping[str, Any], power_id: str) -> float:
    wanted = power_id.upper()
    for power in entity.get("powers") or []:
        pid = str(power.get("id") or power.get("power_id") or "").upper()
        if pid == wanted:
            try:
                return float(power.get("amount") or 0.0)
            except Exception:
                return 0.0
    return 0.0


def _enemy_hp_by_id(combat: Mapping[str, Any]) -> Dict[tuple[str, int], float]:
    out: Dict[tuple[str, int], float] = {}
    for pos, enemy in enumerate(combat.get("enemies") or []):
        out[(str(enemy.get("monster_id") or ""), int(enemy.get("index") if enemy.get("index") is not None else pos))] = float(enemy.get("hp") or 0.0)
    return out


def action_features(
    parent_state: Mapping[str, Any],
    action: SearchAction,
    child_state: Optional[Mapping[str, Any]] = None,
    effect_score: float = 0.0,
) -> Dict[str, float]:
    """Feature vector for a root action.

    The vector is cheap and available both during search and during offline
    rollout labeling. Most features are action-relative; state-only quantities
    are included mainly for interaction features.
    """
    combat = parent_state.get("combat") or {}
    child_combat = (child_state or {}).get("combat") or {}
    player = combat.get("player") or {}
    target = _target_for_action(combat, action)
    card = _card_for_action(combat, action)
    metadata = action.metadata or {}
    boss = boss_id_from_combat(combat)

    card_id = str(card.get("card_id") or metadata.get("card_id") or "")
    card_type = str(card.get("type") or card.get("card_type") or "").lower()
    target_id = str(target.get("monster_id") or metadata.get("target_monster_id") or "")
    intent = target.get("intent") or {}
    intent_types = intent.get("intent_types") or []
    incoming = _incoming_damage(combat)
    hp = float(player.get("hp") or 0.0)
    max_hp = max(1.0, float(player.get("max_hp") or 1.0))
    block = float(player.get("block") or 0.0)
    energy = float(player.get("energy") or 0.0)
    target_hp = float(target.get("hp") or 0.0)
    target_max_hp = max(1.0, float(target.get("max_hp") or 1.0))

    parent_hp_by_id = _enemy_hp_by_id(combat)
    child_hp_by_id = _enemy_hp_by_id(child_combat)
    target_damage = 0.0
    kin_follower_damage = 0.0
    kin_priest_damage = 0.0
    total_enemy_damage = 0.0
    for key, before_hp in parent_hp_by_id.items():
        after_hp = child_hp_by_id.get(key, before_hp)
        delta = max(0.0, before_hp - after_hp)
        total_enemy_damage += delta
        monster_id = key[0]
        if monster_id == target_id:
            target_damage += delta
        if "KIN_FOLLOWER" in monster_id:
            kin_follower_damage += delta
        if "KIN_PRIEST" in monster_id:
            kin_priest_damage += delta

    child_player = child_combat.get("player") or {}
    child_block = float(child_player.get("block") or block)
    child_hp = float(child_player.get("hp") or hp)

    is_attack = 1.0 if (card_type == "attack" or action.action_type == "play_card" and target_id) else 0.0
    is_skill = 1.0 if card_type == "skill" else 0.0
    is_power = 1.0 if card_type == "power" or card_id in SCALING_CARD_IDS else 0.0
    is_draw = 1.0 if card_id in DRAW_CARD_IDS else 0.0
    is_block = 1.0 if card_id in BLOCK_CARD_IDS or _card_stat(card, "block") > 0 else 0.0
    is_debuff = 1.0 if card_id in DEBUFF_CARD_IDS else 0.0
    is_kin = 1.0 if boss == "THE_KIN" else 0.0
    target_is_follower = 1.0 if "KIN_FOLLOWER" in target_id else 0.0
    target_is_priest = 1.0 if "KIN_PRIEST" in target_id else 0.0
    target_is_attacking = 1.0 if intent_deals_damage(target.get("intent") or {}) else 0.0
    target_is_buffing = 1.0 if "Buff" in intent_types else 0.0

    kin_buffing_followers = 0.0
    kin_alive_followers = 0.0
    for enemy in combat.get("enemies") or []:
        mid = str(enemy.get("monster_id") or "")
        if "KIN_FOLLOWER" not in mid or float(enemy.get("hp") or 0.0) <= 0:
            continue
        kin_alive_followers += 1.0
        e_intent = enemy.get("intent") or {}
        if "Buff" in (e_intent.get("intent_types") or []):
            kin_buffing_followers += 1.0

    hp_ratio = hp / max_hp
    net_incoming_ratio = max(0.0, incoming - block) / max_hp
    target_hp_frac = target_hp / target_max_hp
    block_gain = max(0.0, child_block - block)
    self_hp_loss = max(0.0, hp - child_hp)

    return {
        "bias": 1.0,
        "effect_score": float(effect_score),
        "total_enemy_damage": total_enemy_damage,
        "target_damage": target_damage,
        "kin_follower_damage": kin_follower_damage,
        "kin_priest_damage": kin_priest_damage,
        "block_gain": block_gain,
        "self_hp_loss": self_hp_loss,
        "is_play_card": 1.0 if action.action_type == "play_card" else 0.0,
        "is_use_potion": 1.0 if action.action_type == "use_potion" else 0.0,
        "is_end_turn": 1.0 if action.action_type == "end_turn" else 0.0,
        "is_attack": is_attack,
        "is_skill": is_skill,
        "is_power_or_scaling": is_power,
        "is_draw": is_draw,
        "is_block": is_block,
        "is_debuff": is_debuff,
        "hp_ratio": hp_ratio,
        "energy": energy,
        "incoming": incoming,
        "net_incoming_ratio": net_incoming_ratio,
        "target_hp_frac": target_hp_frac,
        "target_is_attacking": target_is_attacking,
        "target_is_buffing": target_is_buffing,
        "target_is_kin_follower": target_is_follower,
        "target_is_kin_priest": target_is_priest,
        "kin_alive_followers": kin_alive_followers,
        "kin_buffing_followers": kin_buffing_followers,
        "kin_attack_follower": is_kin * is_attack * target_is_follower,
        "kin_attack_priest": is_kin * is_attack * target_is_priest,
        "kin_debuff_follower": is_kin * is_debuff * target_is_follower,
        "kin_debuff_priest": is_kin * is_debuff * target_is_priest,
        "kin_damage_follower": is_kin * kin_follower_damage,
        "kin_damage_priest": is_kin * kin_priest_damage,
        "safe_scaling": is_power * (1.0 if incoming <= block + 2.0 else 0.0),
        "urgent_block": is_block * net_incoming_ratio,
        "draw_when_energy": is_draw * max(0.0, energy),
    }


def action_bonus(
    parent_state: Mapping[str, Any],
    action: SearchAction,
    child_state: Optional[Mapping[str, Any]] = None,
    effect_score: float = 0.0,
) -> float:
    # The current rollout datasets label normal combat root actions only.
    # Do not extrapolate learned card/end-turn weights onto emergency potion use.
    if action.action_type not in {"play_card", "end_turn"}:
        return 0.0
    boss = boss_id_from_state(parent_state)
    weights = _weights_for(boss)
    if not weights:
        return 0.0
    feats = action_features(parent_state, action, child_state, effect_score)
    order = list(weights.get("feature_order") or feats.keys())
    means = weights.get("feature_mean") or {}
    w = weights.get("weights") or {}
    value = float(weights.get("intercept") or 0.0)
    for key in order:
        value += float(w.get(key, 0.0)) * (float(feats.get(key, 0.0)) - float(means.get(key, 0.0)))
    return value * float(weights.get("scale", 1.0))
