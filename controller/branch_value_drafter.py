"""Branch-value drafter: pick reward cards by the offline card-value model's
predicted causal ΔReturn, with a rule fallback and softmax (Boltzmann) sampling
in the gray zone. Loaded by run_agent when --draft-policy branchvalue.

Design (per the 2026-06-22 decision):
- The model (`models/branch_value.pt`) predicts ΔReturn =
  causal "take this card vs skip" 3-fight discounted-HP gain. It is trustworthy
  for RELATIVE ranking (within-point spearman +0.379), not absolute values — so
  we use it to rank, then SAMPLE rather than argmax (scores are noisy).
- Rule fallback first: the model only governs the gray zone. Obvious calls
  (deck very bloated -> bias skip) defer to simple rules; everything else is
  softmax over model scores for {each card, skip}.
- Temperature tau controls decisiveness: scores far apart -> near-argmax; close
  -> near-uniform, honestly reflecting "the model isn't sure".

Returns the same shape as choose_card_reward: {"card_index": i} to take, or None
to skip.
"""
from __future__ import annotations
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

REPO = Path(__file__).resolve().parents[1]

_MODELS: Dict[str, Dict[str, Any]] = {}  # lazy-loaded by checkpoint path


def _norm_card(cid: Optional[str]) -> str:
    return str(cid or "").upper().replace("CARD.", "").replace("+", "").rstrip("0123456789").rstrip("_")


def _norm_relic(rid: Optional[str]) -> str:
    s = str(rid or "").upper().replace("RELIC.", "")
    out = []
    for ch in s:
        out.append(ch if ch.isalnum() else "_")
    return "_".join("".join(out).split("_")).strip("_")


def _state_relics(state: Dict[str, Any]) -> List[str]:
    pl = state.get("player") or {}
    out = []
    for rel in pl.get("relics") or []:
        if isinstance(rel, dict):
            out.append(str(rel.get("name") or rel.get("id") or ""))
        else:
            out.append(str(rel))
    return out


def _load_model(path: Optional[str] = None):
    import torch
    from tools.learning.train_card_value_model import make_model
    p = Path(path) if path else (REPO / "models/branch_value.pt")
    key = str(p.resolve())
    if key in _MODELS:
        return _MODELS[key]
    ckpt = torch.load(p, map_location="cpu", weights_only=False)
    model_type = ckpt.get("model_type", "card_only")
    model = make_model(
        len(ckpt["vocab"]),
        ckpt["emb_dim"],
        model_type=model_type,
        scalar_dim=int(ckpt.get("scalar_dim", 4)),
        relic_vocab_size=len(ckpt.get("relic_vocab") or {}),
        max_relics=int(ckpt.get("max_relics", 16)),
        use_card_bias=bool(ckpt.get("card_bias")),
    )
    model.load_state_dict(ckpt["state_dict"], strict=False); model.eval()
    loaded = {"model": model, "vocab": ckpt["vocab"], "emb_dim": ckpt["emb_dim"],
              "relic_vocab": ckpt.get("relic_vocab"), "model_type": model_type,
              "scalar_dim": int(ckpt.get("scalar_dim", 4)),
              "max_relics": int(ckpt.get("max_relics", 16)),
              "y_mean": ckpt.get("y_mean", 0.0), "torch": torch}
    _MODELS[key] = loaded
    return loaded


def _predict_delta(state: Dict[str, Any], running_deck: List[str],
                   cand_ids: List[str], model_path: Optional[str]) -> Optional[List[float]]:
    """Return predicted ΔReturn for each candidate card id, or None if model
    unavailable. Deck = running_deck (Ironclad starter + taken cards so far)."""
    try:
        M = _load_model(model_path)
    except Exception:
        return None
    torch = M["torch"]; vocab = M["vocab"]; unk = vocab.get("<unk>", 1)
    pl = state.get("player") or {}
    hp = pl.get("hp") or 0; mhp = pl.get("max_hp") or 80
    floor = (state.get("context") or {}).get("floor") or state.get("floor") or 0
    gold = pl.get("gold") or 0
    deck_idx = [vocab.get(_norm_card(c), unk) for c in (running_deck or [])][:40]
    deck_idx = deck_idx + [0] * (40 - len(deck_idx))
    scal = [(hp / mhp) if mhp else 0.0, floor / 17.0, gold / 300.0, len(running_deck) / 40.0]
    if M.get("model_type") == "relic_lite":
        from tools.learning.train_card_value_model import relic_lite_features
        scal.extend(relic_lite_features(_state_relics(state)))
    deck_t = torch.tensor([deck_idx] * len(cand_ids), dtype=torch.long)
    scal_t = torch.tensor([scal] * len(cand_ids), dtype=torch.float32)
    cand_t = torch.tensor([vocab.get(_norm_card(c), unk) for c in cand_ids], dtype=torch.long)
    relic_t = None
    if M.get("model_type") == "relic_embed":
        relic_vocab = M.get("relic_vocab") or {}
        rel_unk = relic_vocab.get("<unk>", 1)
        max_relics = int(M.get("max_relics", 16))
        rel_idx = [relic_vocab.get(_norm_relic(r), rel_unk) for r in _state_relics(state)][:max_relics]
        rel_idx = rel_idx + [0] * (max_relics - len(rel_idx))
        relic_t = torch.tensor([rel_idx] * len(cand_ids), dtype=torch.long)
    with torch.no_grad():
        out = M["model"](cand_t, deck_t, scal_t, relic_t).numpy().tolist()
    return out


def choose_card_reward_branchvalue(state: Dict[str, Any], running_deck: List[str], *,
                                   rng=None, tau: float = 0.05,
                                   model_path: Optional[str] = None,
                                   bloat_size: int = 18) -> Optional[Dict[str, Any]]:
    """Model-scored softmax card pick with a rule fallback.

    tau: Boltzmann temperature on ΔReturn scores (ΔReturn spread ~0.05-0.1, so
         tau~0.05 gives meaningful but not razor-sharp probabilities).
    bloat_size: above this deck size, bias toward skip (rule).
    """
    import random as _random
    rng = rng or _random.Random()
    cards = state.get("cards") or state.get("rewards") or []
    if not cards:
        return None
    cand_ids = [str((c or {}).get("card_id") or (c or {}).get("id") or (c or {}).get("name") or "") for c in cards]
    deltas = _predict_delta(state, running_deck, cand_ids, model_path)
    if deltas is None:
        # model unavailable -> no opinion; caller should fall back to heuristic
        return {"_fallback": True}

    # skip is the ΔReturn baseline (=0 by construction: ΔReturn is "vs skip")
    options: List[tuple] = [(i, float(deltas[i])) for i in range(len(cards))]
    skip_score = 0.0

    # Rule: a very bloated deck makes marginal takes harmful -> require a clearly
    # positive predicted gain to take at all (otherwise skip).
    take_margin = 0.0
    if len(running_deck) >= bloat_size:
        take_margin = 0.03  # must beat skip by this to bother adding to a bloated deck

    # Boltzmann over {cards, skip}. Cards below skip+margin are dropped from the
    # take set; if none qualify, skip.
    cand_opts = [(i, s) for (i, s) in options if s > skip_score + take_margin]
    if not cand_opts:
        return None  # skip
    scores = np.array([s for _, s in cand_opts], dtype=np.float64)
    # include skip itself as an option so the policy can still skip stochastically
    all_scores = np.append(scores, skip_score)
    z = (all_scores - all_scores.max()) / max(tau, 1e-6)
    p = np.exp(z); p = p / p.sum()
    choice = rng.choices(range(len(all_scores)), weights=p.tolist(), k=1)[0]
    if choice == len(all_scores) - 1:
        return None  # sampled skip
    idx = cand_opts[choice][0]
    return {"card_index": int((cards[idx] or {}).get("index", idx))}
