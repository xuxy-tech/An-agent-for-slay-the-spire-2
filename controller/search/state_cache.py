from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

from controller.combat_intent import intent_total_damage


UNMODELED_SEARCH_POTION_IDS = frozenset({
    "COLORLESS_POTION",
    "STABLE_SERUM",
})

AUTOMATIC_POTION_IDS = frozenset({"FAIRY_IN_A_BOTTLE"})


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _normalize(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def canonicalize_search_state(search_state: Dict[str, Any]) -> Dict[str, Any]:
    normalized = _normalize(search_state)
    # Engine snapshot handles and fingerprints describe transport/storage, not
    # visible combat semantics. Strict DAG reuse reads the fingerprint directly;
    # legacy visible-state hashes must remain stable across snapshot ids.
    normalized.pop("engine_snapshot_id", None)
    normalized.pop("engine_state_fingerprint", None)
    normalized.pop("engine_state_fingerprint_schema", None)
    normalized.pop("engine_semantic_state_fingerprint", None)
    normalized.pop("engine_semantic_state_fingerprint_schema", None)
    return normalized


def hash_search_state(search_state: Dict[str, Any]) -> str:
    normalized = canonicalize_search_state(search_state)
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _card_sort_key(card: Dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(card.get("card_id") or ""),
        int(card.get("upgrade") or 0),
        card.get("current_cost"),
        str(card.get("affliction") or ""),
        int(card.get("affliction_count") or 0),
        tuple(card.get("keywords") or []),
    )


def canonicalize_search_state_for_dedup(search_state: Dict[str, Any]) -> Dict[str, Any]:
    normalized = canonicalize_search_state(search_state)
    combat = normalized.get("combat")
    if not isinstance(combat, dict):
        return normalized

    # Card index order within hand is a UI/runtime detail, so we may normalize
    # piles whose *internal sequence* cannot affect any future draw within the
    # search horizon. draw_pile is deliberately NOT sorted here: its order
    # determines which cards are drawn next (by in-turn draw effects, or by the
    # start-of-next-turn draw once an enemy-turn chance node is expanded), so two
    # states that differ only in draw_pile order are NOT equivalent and must not
    # be merged. Sorting it would silently drop the better draw-order branch.
    for key in ("discard_pile", "exhaust_pile", "play_pile"):
        cards = combat.get(key)
        if isinstance(cards, list):
            combat[key] = sorted(cards, key=_card_sort_key)
    combat.pop("available_actions", None)
    return normalized


def hash_search_state_for_dedup(search_state: Dict[str, Any]) -> str:
    normalized = canonicalize_search_state_for_dedup(search_state)
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _semantic_card(card: Any) -> Any:
    if not isinstance(card, dict):
        return card
    return _normalize({
        "card_id": card.get("card_id", card.get("id")),
        "upgrade": card.get("upgrade", card.get("upgrade_count")),
        "current_cost": card.get("current_cost", card.get("cost")),
        "costs_x": card.get("costs_x"),
        "keywords": card.get("keywords") or [],
        "affliction": card.get("affliction"),
        "affliction_count": card.get("affliction_count"),
        "dynamic_values": card.get("dynamic_values") or [],
        "mods": card.get("mods") or [],
    })


def _semantic_power(power: Any) -> Any:
    if not isinstance(power, dict):
        return power
    return _normalize({
        "id": power.get("id", power.get("power_id")),
        "amount": power.get("amount", power.get("stacks")),
        "extra": power.get("extra"),
        "counter": power.get("counter"),
    })


def _semantic_inventory_item(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    return _normalize({
        "id": item.get("potion_id", item.get("relic_id", item.get("id"))),
        "slot": item.get("slot_index", item.get("index")),
        "amount": item.get("amount"),
        "counter": item.get("counter"),
        "charges": item.get("charges"),
    })


def _inventory_id(item: Any) -> str:
    if isinstance(item, dict):
        raw = item.get("potion_id") or item.get("id") or ""
    else:
        raw = item or ""
    value = str(raw).strip().upper().replace("-", "_").replace(" ", "_")
    return value.removeprefix("POTION.")


def _semantic_cards(cards: Any, *, preserve_order: bool) -> List[Any]:
    result = [_semantic_card(card) for card in (cards or [])]
    if preserve_order:
        return result
    return sorted(result, key=lambda value: json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ))


def canonicalize_search_state_for_plan_reuse(search_state: Dict[str, Any]) -> Dict[str, Any]:
    """Project a state onto fields that can invalidate a same-turn plan.

    Search workers reconstruct snapshots and intentionally remove unsupported
    potions. Runtime metadata and those unmodeled potions must not invalidate a
    card sequence when the visible combat state is otherwise identical.
    """
    combat = search_state.get("combat") or {}
    if not isinstance(combat, dict):
        return {"terminal_decision": search_state.get("terminal_decision"), "combat": None}
    player = combat.get("player") or {}
    potions = [
        _semantic_inventory_item(item)
        for item in (player.get("potions") or combat.get("potions") or [])
        if _inventory_id(item) not in UNMODELED_SEARCH_POTION_IDS
    ]
    enemies = []
    for enemy in combat.get("enemies") or []:
        if not isinstance(enemy, dict):
            continue
        intent = enemy.get("intent") or {}
        enemies.append({
            "monster_id": enemy.get("monster_id", enemy.get("enemy_id")),
            "hp": enemy.get("hp"),
            "max_hp": enemy.get("max_hp"),
            "block": enemy.get("block"),
            "powers": sorted(
                (_semantic_power(power) for power in (enemy.get("powers") or [])),
                key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
            ),
            "intent": {
                "intent_types": list(intent.get("intent_types") or []),
                "total_damage": (
                    intent_total_damage(intent)
                    if intent.get("total_damage") is not None or intent.get("display_damage") is not None
                    else None
                ),
                "hits": intent.get("hits"),
            },
        })
    return _normalize({
        "terminal_decision": search_state.get("terminal_decision"),
        "combat": {
            "round_number": combat.get("round_number"),
            "turn_number": combat.get("turn_number"),
            "is_player_turn": combat.get("is_player_turn"),
            "player": {
                "hp": player.get("hp"),
                "max_hp": player.get("max_hp"),
                "block": player.get("block"),
                "energy": player.get("energy"),
                "powers": sorted(
                    (_semantic_power(power) for power in (player.get("powers") or [])),
                    key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
                ),
                "relics": sorted(
                    (_semantic_inventory_item(item) for item in (player.get("relics") or [])),
                    key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
                ),
                "potions": sorted(
                    potions,
                    key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
                ),
            },
            "enemies": enemies,
            "hand": _semantic_cards(combat.get("hand"), preserve_order=False),
            "draw_pile": _semantic_cards(combat.get("draw_pile"), preserve_order=True),
            "discard_pile": _semantic_cards(combat.get("discard_pile"), preserve_order=False),
            "exhaust_pile": _semantic_cards(combat.get("exhaust_pile"), preserve_order=False),
            "play_pile": _semantic_cards(combat.get("play_pile"), preserve_order=False),
        },
    })


def hash_search_state_for_plan_reuse(search_state: Dict[str, Any]) -> str:
    normalized = canonicalize_search_state_for_plan_reuse(search_state)
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def diff_plan_reuse_states(expected: Dict[str, Any], actual: Dict[str, Any], limit: int = 8) -> List[Dict[str, Any]]:
    differences: List[Dict[str, Any]] = []

    def visit(path: str, left: Any, right: Any) -> None:
        if len(differences) >= limit or left == right:
            return
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                visit(f"{path}.{key}" if path else key, left.get(key), right.get(key))
                if len(differences) >= limit:
                    return
            return
        if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
            for index, (left_item, right_item) in enumerate(zip(left, right)):
                visit(f"{path}[{index}]", left_item, right_item)
                if len(differences) >= limit:
                    return
            return
        differences.append({"path": path, "expected": left, "actual": right})

    visit("", expected, actual)
    return differences


def canonicalize_search_state_for_subtree_cache(search_state: Dict[str, Any]) -> Dict[str, Any]:
    normalized = canonicalize_search_state(search_state)
    combat = normalized.get("combat")
    if not isinstance(combat, dict):
        return normalized

    # For subtree reuse we keep hand/draw ordering exact, because same-turn
    # draw effects depend on them. We only erase ordering from piles whose
    # sequence is strategically irrelevant for this shallow search horizon.
    for key in ("discard_pile", "exhaust_pile", "play_pile"):
        cards = combat.get(key)
        if isinstance(cards, list):
            combat[key] = sorted(cards, key=_card_sort_key)
    combat.pop("available_actions", None)
    return normalized


def hash_search_state_for_subtree_cache(search_state: Dict[str, Any]) -> str:
    normalized = canonicalize_search_state_for_subtree_cache(search_state)
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
