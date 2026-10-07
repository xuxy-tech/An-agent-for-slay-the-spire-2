"""Learned deck-value drafter: score candidate cards by how much they lower the
deck-value model's predicted (encounter-normalized) HP loss over the Act-1
elites/bosses ahead. Loaded by run_agent when --draft-policy learned.

The model (data/learning/deck_value_model.json) predicts per-encounter HP loss
from deck features + encounter one-hot (see tools/fit_deck_value.py). For
drafting we use it in a relative way: for each offered card, form the deck that
would result from taking it, recompute deck features, and average the model's
predicted HP loss across the standard Act-1 elite/boss encounters. Pick the card
with the LOWEST average predicted loss (best survival). "Skip" is scored with the
unchanged deck. A take only happens if it beats skip by a margin.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import numpy as np

# coarse Ironclad card -> capability tags (mirror tools/gate1_deck_value.deck_feats)
ATTACK_KEYS = ("STRIKE","BASH","THUNDERCLAP","POMMEL","ANGER","CLEAVE","BLUDGEON",
               "ASHEN","HEADBUTT","MOLTEN","WHIRLWIND","CLOTHESLINE","TWIN_STRIKE",
               "PUMMEL","HEAVY_BLADE","SEVER","CARNAGE","REAPER","IMMOLATE","FIEND_FIRE")
BLOCK_KEYS = ("DEFEND","SHRUG","IRON_WAVE","TRUE_GRIT","FLAME_BARRIER","IMPERVIOUS",
              "COLOSSUS","ENTRENCH","BARRICADE","BLOOD_WALL")
POWER_KEYS = ("INFLAME","METALLICIZE","DEMON_FORM","FEEL_NO_PAIN","JUGGERNAUT",
              "COMBUST","EVOLVE","FIRE_BREATHING","RUPTURE","BERSERK","BARRICADE")
FEATS = ["deck_size","atk_frac","blk_frac","pwr_ct","basic_frac","nonbasic","upgrades"]


def _deck_feats(deck_ids: List[str]) -> Optional[Dict[str, float]]:
    n = len(deck_ids)
    if n == 0:
        return None
    up = sum(1 for c in deck_ids if c.endswith("+") or "_UP" in c)
    def has(keys, c): return any(k in c for k in keys)
    atk = sum(1 for c in deck_ids if has(ATTACK_KEYS, c))
    blk = sum(1 for c in deck_ids if has(BLOCK_KEYS, c))
    pwr = sum(1 for c in deck_ids if has(POWER_KEYS, c))
    basic = sum(1 for c in deck_ids if c in ("STRIKE_IRONCLAD", "DEFEND_IRONCLAD"))
    return {"deck_size": float(n), "atk_frac": atk/n, "blk_frac": blk/n,
            "pwr_ct": float(pwr), "basic_frac": basic/n, "nonbasic": float(n-basic),
            "upgrades": float(up)}


class DeckValueModel:
    def __init__(self, path: str):
        m = json.load(open(path))
        self.feats = m["feats"]
        self.encs = m["encounters"]
        self.b = float(m["intercept"])
        self.w = np.array(m["w_std"], float)
        self.mu = np.array(m["mean"], float)
        self.sd = np.array(m["sd"] if "sd" in m else m["std"], float)

    def _pred(self, feats: Dict[str, float], enc: str) -> float:
        f = [feats[k] for k in self.feats] + [1.0 if e == enc else 0.0 for e in self.encs]
        x = (np.array(f, float) - self.mu) / self.sd
        return float(self.b + x @ self.w)

    def deck_badness(self, deck_ids: List[str]) -> float:
        """Avg predicted HP loss across all known encounters (lower = better)."""
        f = _deck_feats(deck_ids)
        if f is None:
            return 0.0
        return float(np.mean([self._pred(f, e) for e in self.encs]))


_MODEL: Optional[DeckValueModel] = None


def _model() -> Optional[DeckValueModel]:
    global _MODEL
    if _MODEL is None:
        p = Path("data/learning/deck_value_model.json")
        if p.exists():
            try:
                _MODEL = DeckValueModel(str(p))
            except Exception:
                _MODEL = None
    return _MODEL


def choose_card_reward_learned(state: Dict[str, Any], current_deck_ids: List[str],
                               take_margin: float = 0.02) -> Optional[Dict[str, Any]]:
    """Pick the offered card that most lowers deck-badness vs skipping. Returns
    {"card_index": i} to take, or None to skip. take_margin: only take if the
    best card beats skip by at least this much (normalized-HP-loss units)."""
    model = _model()
    cards = state.get("cards") or state.get("rewards") or []
    if model is None or not cards:
        return None
    skip_badness = model.deck_badness(current_deck_ids)
    best_idx, best_badness = None, skip_badness - take_margin
    for i, card in enumerate(cards):
        cid = str(card.get("card_id") or card.get("id") or card.get("label") or card.get("name") or "").upper()
        if not cid:
            continue
        b = model.deck_badness(current_deck_ids + [cid])
        if b < best_badness:
            best_badness = b
            best_idx = i
    if best_idx is None:
        return None
    return {"card_index": int((cards[best_idx] or {}).get("index", best_idx))}
