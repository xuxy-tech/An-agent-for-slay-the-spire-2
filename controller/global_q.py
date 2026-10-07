from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


FeatureMap = Dict[str, float]


def normalize_card_id(card_id: str) -> str:
    cid = str(card_id or "").strip().upper()
    if cid.startswith("CARD."):
        cid = cid[5:]
    return cid.replace("-", "_").replace(" ", "_")


def run_floor(state: Dict[str, Any]) -> int:
    context = state.get("context") or {}
    try:
        return int(context.get("floor") or state.get("floor") or 0)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _player(state: Dict[str, Any]) -> Dict[str, Any]:
    return state.get("player") or {}


def player_deck_cards(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    player = _player(state)
    deck = player.get("deck") or state.get("deck") or []
    if isinstance(deck, list):
        return [c for c in deck if isinstance(c, dict)]
    return []


def deck_counts(state: Dict[str, Any]) -> Dict[str, float]:
    deck = player_deck_cards(state)
    counts = {
        "deck_size": float(len(deck)),
        "deck_attack": 0.0,
        "deck_skill": 0.0,
        "deck_power": 0.0,
        "deck_basic": 0.0,
        "deck_upgraded": 0.0,
        "deck_cost_sum": 0.0,
        "deck_cost_seen": 0.0,
    }
    for card in deck:
        ctype = str(card.get("type") or "").upper()
        cid = normalize_card_id(str(card.get("id") or card.get("card_id") or card.get("name") or ""))
        if ctype == "ATTACK":
            counts["deck_attack"] += 1.0
        elif ctype == "SKILL":
            counts["deck_skill"] += 1.0
        elif ctype == "POWER":
            counts["deck_power"] += 1.0
        if cid in {"STRIKE_IRONCLAD", "DEFEND_IRONCLAD"} and not bool(card.get("upgraded")):
            counts["deck_basic"] += 1.0
        if bool(card.get("upgraded")):
            counts["deck_upgraded"] += 1.0
        cost = card.get("cost")
        if cost is not None:
            counts["deck_cost_sum"] += max(0.0, _safe_float(cost))
            counts["deck_cost_seen"] += 1.0
    return counts


def base_features(state: Dict[str, Any]) -> FeatureMap:
    player = _player(state)
    max_hp = max(1.0, _safe_float(player.get("max_hp"), 1.0))
    hp = max(0.0, _safe_float(player.get("hp"), max_hp))
    counts = deck_counts(state)
    deck_size = max(1.0, counts["deck_size"])
    context = state.get("context") or {}
    act = _safe_float(context.get("act"), 1.0)
    relics = player.get("relics") or []
    potions = player.get("potions") or []
    feats: FeatureMap = {
        "bias": 1.0,
        "act": act / 4.0,
        "floor": run_floor(state) / 16.0,
        "hp_ratio": hp / max_hp,
        "missing_hp_ratio": max(0.0, (max_hp - hp) / max_hp),
        "gold_100": _safe_float(player.get("gold")) / 100.0,
        "deck_size_20": counts["deck_size"] / 20.0,
        "deck_attack_frac": counts["deck_attack"] / deck_size,
        "deck_skill_frac": counts["deck_skill"] / deck_size,
        "deck_power_frac": counts["deck_power"] / deck_size,
        "deck_basic_frac": counts["deck_basic"] / deck_size,
        "deck_upgraded_frac": counts["deck_upgraded"] / deck_size,
        "relic_count_10": (float(len(relics)) if isinstance(relics, list) else 0.0) / 10.0,
        "potion_count_3": (float(len(potions)) if isinstance(potions, list) else 0.0) / 3.0,
    }
    if counts["deck_cost_seen"] > 0:
        feats["deck_avg_cost"] = counts["deck_cost_sum"] / counts["deck_cost_seen"]
    return feats


def _add_prefixed(dst: FeatureMap, prefix: str, src: FeatureMap) -> None:
    for key, value in src.items():
        if value:
            dst[f"{prefix}:{key}"] = float(value)


def _add_rest_interactions(feats: FeatureMap) -> None:
    hp_ratio = _safe_float(feats.get("s:hp_ratio"))
    missing_hp_ratio = _safe_float(feats.get("s:missing_hp_ratio"))
    deck_upgraded_frac = _safe_float(feats.get("s:deck_upgraded_frac"))
    deck_basic_frac = _safe_float(feats.get("s:deck_basic_frac"))
    if feats.get("rest:heal"):
        feats["rest:heal_missing_hp"] = missing_hp_ratio
        feats["rest:heal_low_hp"] = 1.0 if hp_ratio < 0.50 else 0.0
        feats["rest:heal_mid_hp"] = 1.0 if hp_ratio < 0.75 else 0.0
    if feats.get("rest:smith"):
        feats["rest:smith_hp_ratio"] = hp_ratio
        feats["rest:smith_safe_hp"] = 1.0 if hp_ratio >= 0.65 else 0.0
        feats["rest:smith_upgrade_need"] = max(0.0, 1.0 - deck_upgraded_frac)
        feats["rest:smith_basic_frac"] = deck_basic_frac


def _normalize_rarity(rarity: Any) -> str:
    text = str(rarity or "").strip().upper()
    if text in {"COMMON", "UNCOMMON", "RARE"}:
        return text.lower()
    return "unknown"


def _card_id(card: Dict[str, Any]) -> str:
    return normalize_card_id(str(card.get("card_id") or card.get("id") or card.get("cardEnName") or card.get("name") or ""))


def _card_index(card: Dict[str, Any], fallback: int) -> int:
    try:
        return int(card.get("index"))
    except (TypeError, ValueError):
        return int(fallback)


def card_reward_candidates(
    state: Dict[str, Any],
    reward_scores: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    cards = state.get("cards") or state.get("rewards") or []
    score_by_index: Dict[int, Dict[str, Any]] = {}
    for row in reward_scores or []:
        for key in ("card_index", "index"):
            try:
                score_by_index[int(row.get(key))] = row
                break
            except (TypeError, ValueError):
                continue
    candidates: List[Dict[str, Any]] = []
    base = base_features(state)
    for idx, card in enumerate(cards):
        if not isinstance(card, dict):
            continue
        card_index = _card_index(card, idx)
        ctype = str(card.get("type") or "").strip().upper()
        rarity = _normalize_rarity(card.get("rarity"))
        score_row = score_by_index.get(card_index) or score_by_index.get(idx) or {}
        score = _safe_float(score_row.get("total_score"))
        cost = max(-1.0, _safe_float(card.get("cost"), -1.0))
        feats: FeatureMap = {}
        _add_prefixed(feats, "s", base)
        feats.update({
            "decision:card_reward": 1.0,
            "action:take_card": 1.0,
            f"card_type:{ctype.lower() if ctype else 'unknown'}": 1.0,
            f"card_rarity:{rarity}": 1.0,
            "card_cost": max(0.0, cost) / 3.0 if cost >= 0 else 0.0,
            "card_score_50": score / 50.0,
        })
        candidates.append({
            "action": "select_card_reward",
            "payload": {"card_index": card_index},
            "label": _card_id(card),
            "features": feats,
        })
    skip_feats: FeatureMap = {}
    _add_prefixed(skip_feats, "s", base)
    skip_feats.update({"decision:card_reward": 1.0, "action:skip_card": 1.0})
    candidates.append({
        "action": "skip_card_reward",
        "payload": {},
        "label": "SKIP",
        "features": skip_feats,
    })
    return candidates


def rest_site_candidates(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    options = [o for o in (state.get("options") or []) if isinstance(o, dict) and o.get("is_enabled")]
    if not options:
        options = [{"index": 0, "option_id": "UNKNOWN"}]
    base = base_features(state)
    candidates: List[Dict[str, Any]] = []
    for opt in options:
        option_id = str(opt.get("option_id") or opt.get("id") or opt.get("label") or "UNKNOWN").upper()
        try:
            option_index = int(opt.get("index"))
        except (TypeError, ValueError):
            option_index = len(candidates)
        feats: FeatureMap = {}
        _add_prefixed(feats, "s", base)
        feats["decision:rest_site"] = 1.0
        if option_id in {"HEAL", "REST"}:
            feats["rest:heal"] = 1.0
        elif option_id == "SMITH":
            feats["rest:smith"] = 1.0
        else:
            feats["rest:other"] = 1.0
        _add_rest_interactions(feats)
        candidates.append({
            "action": "choose_option",
            "payload": {"option_index": option_index},
            "label": option_id,
            "features": feats,
        })
    return candidates


def _node_map(map_data: Optional[Dict[str, Any]]) -> Dict[Tuple[int, int], Dict[str, Any]]:
    nodes: Dict[Tuple[int, int], Dict[str, Any]] = {}
    if not isinstance(map_data, dict):
        return nodes
    for row_nodes in map_data.get("rows") or []:
        for node in row_nodes or []:
            try:
                nodes[(int(node.get("col")), int(node.get("row")))] = dict(node)
            except Exception:
                continue
    boss = map_data.get("boss") or {}
    try:
        nodes[(int(boss.get("col")), int(boss.get("row")))] = dict(boss)
    except Exception:
        pass
    return nodes


def _lookahead_counts(
    key: Tuple[int, int],
    nodes: Dict[Tuple[int, int], Dict[str, Any]],
    depth: int = 4,
    memo: Optional[Dict[Tuple[Tuple[int, int], int], Dict[str, float]]] = None,
) -> Dict[str, float]:
    if memo is None:
        memo = {}
    memo_key = (key, depth)
    if memo_key in memo:
        return dict(memo[memo_key])
    node = nodes.get(key) or {}
    node_type = str(node.get("type") or "Unknown")
    counts = {node_type: 1.0}
    if depth > 1:
        best_child: Dict[str, float] = {}
        best_total = -1.0
        for child in node.get("children") or []:
            try:
                child_key = (int(child.get("col")), int(child.get("row")))
            except Exception:
                continue
            child_counts = _lookahead_counts(child_key, nodes, depth - 1, memo)
            total = sum(child_counts.values())
            if total > best_total:
                best_total = total
                best_child = child_counts
        for typ, val in best_child.items():
            counts[typ] = counts.get(typ, 0.0) + val
    memo[memo_key] = dict(counts)
    return counts


def map_candidates(state: Dict[str, Any], map_data: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    choices = state.get("choices") or []
    base = base_features(state)
    nodes = _node_map(map_data)
    memo: Dict[Tuple[Tuple[int, int], int], Dict[str, float]] = {}
    candidates: List[Dict[str, Any]] = []
    for choice in choices:
        if not isinstance(choice, dict) or choice.get("col") is None or choice.get("row") is None:
            continue
        key = (int(choice.get("col")), int(choice.get("row")))
        node = nodes.get(key) or choice
        typ = str(node.get("type") or choice.get("type") or "Unknown")
        look = _lookahead_counts(key, nodes, depth=4, memo=memo) if nodes else {typ: 1.0}
        feats: FeatureMap = {}
        _add_prefixed(feats, "s", base)
        feats.update({
            "decision:map_select": 1.0,
            f"node:{typ}": 1.0,
            "look:Monster": look.get("Monster", 0.0) / 4.0,
            "look:Elite": look.get("Elite", 0.0) / 4.0,
            "look:RestSite": look.get("RestSite", 0.0) / 4.0,
            "look:Shop": look.get("Shop", 0.0) / 4.0,
            "look:Unknown": look.get("Unknown", 0.0) / 4.0,
            "look:Treasure": look.get("Treasure", 0.0) / 4.0,
        })
        candidates.append({
            "action": "select_map_node",
            "payload": {"col": key[0], "row": key[1]},
            "label": f"{typ}@{key[0]},{key[1]}",
            "features": feats,
        })
    return candidates


@dataclass
class QChoice:
    action: str
    payload: Dict[str, Any]
    trace: Dict[str, Any]


class GlobalQPolicy:
    def __init__(self, weights: Optional[Dict[str, float]] = None, metadata: Optional[Dict[str, Any]] = None):
        self.weights = {str(k): float(v) for k, v in (weights or {}).items()}
        self.metadata = dict(metadata or {})

    @classmethod
    def load(cls, path: Optional[str | Path]) -> "GlobalQPolicy":
        if not path:
            return cls()
        p = Path(path)
        if not p.exists():
            return cls(metadata={"missing_model": str(p)})
        data = json.loads(p.read_text())
        return cls(weights=data.get("weights") or {}, metadata=data.get("metadata") or {})

    def score(self, features: FeatureMap) -> float:
        return float(sum(self.weights.get(k, 0.0) * float(v) for k, v in features.items()))

    def choose(
        self,
        decision: str,
        candidates: List[Dict[str, Any]],
        rng: random.Random,
        epsilon: float = 0.0,
    ) -> Optional[QChoice]:
        if not candidates:
            return None
        rows: List[Dict[str, Any]] = []
        for idx, cand in enumerate(candidates):
            q = self.score(cand.get("features") or {})
            rows.append({
                "index": idx,
                "action": cand.get("action"),
                "payload": cand.get("payload") or {},
                "label": cand.get("label"),
                "q": q,
                "features": cand.get("features") or {},
            })
        explored = rng.random() < max(0.0, min(1.0, epsilon))
        if explored:
            chosen_idx = rng.randrange(len(rows))
        else:
            best_q = max(row["q"] for row in rows)
            best = [row["index"] for row in rows if math.isclose(row["q"], best_q, rel_tol=0.0, abs_tol=1e-12)]
            chosen_idx = rng.choice(best)
        row = rows[chosen_idx]
        trace = {
            "policy": "r1_q",
            "decision": decision,
            "explored": explored,
            "epsilon": epsilon,
            "selected_index": chosen_idx,
            "selected_action": row["action"],
            "selected_label": row.get("label"),
            "selected_q": row["q"],
            "candidates": rows,
        }
        return QChoice(action=str(row["action"]), payload=dict(row["payload"] or {}), trace=trace)

    def trace_for_selection(
        self,
        decision: str,
        candidates: List[Dict[str, Any]],
        *,
        selected_action: str,
        selected_payload: Dict[str, Any],
        explored: bool = False,
        epsilon: float = 0.0,
        behavior: str = "forced",
    ) -> Optional[QChoice]:
        if not candidates:
            return None
        rows: List[Dict[str, Any]] = []
        selected_idx: Optional[int] = None
        selected_payload = dict(selected_payload or {})
        for idx, cand in enumerate(candidates):
            payload = dict(cand.get("payload") or {})
            q = self.score(cand.get("features") or {})
            rows.append({
                "index": idx,
                "action": cand.get("action"),
                "payload": payload,
                "label": cand.get("label"),
                "q": q,
                "features": cand.get("features") or {},
            })
            if cand.get("action") == selected_action and payload == selected_payload:
                selected_idx = idx
        if selected_idx is None:
            return None
        row = rows[selected_idx]
        trace = {
            "policy": "r1_q",
            "behavior": behavior,
            "decision": decision,
            "explored": explored,
            "epsilon": epsilon,
            "selected_index": selected_idx,
            "selected_action": row["action"],
            "selected_label": row.get("label"),
            "selected_q": row["q"],
            "candidates": rows,
        }
        return QChoice(action=str(row["action"]), payload=dict(row["payload"] or {}), trace=trace)

    def choose_card_reward(
        self,
        state: Dict[str, Any],
        rng: random.Random,
        epsilon: float = 0.0,
        reward_scores: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[QChoice]:
        return self.choose("card_reward", card_reward_candidates(state, reward_scores), rng, epsilon)

    def choose_rest_site(self, state: Dict[str, Any], rng: random.Random, epsilon: float = 0.0) -> Optional[QChoice]:
        return self.choose("rest_site", rest_site_candidates(state), rng, epsilon)

    def choose_map_select(
        self,
        state: Dict[str, Any],
        map_data: Optional[Dict[str, Any]],
        rng: random.Random,
        epsilon: float = 0.0,
    ) -> Optional[QChoice]:
        return self.choose("map_select", map_candidates(state, map_data), rng, epsilon)


def stable_feature_names(rows: Iterable[Dict[str, Any]]) -> List[str]:
    names = set()
    for row in rows:
        for key in (row.get("features") or {}).keys():
            names.add(str(key))
    return sorted(names)
