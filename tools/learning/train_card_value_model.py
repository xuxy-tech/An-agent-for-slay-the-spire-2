#!/usr/bin/env python3
"""Train the offline card-value model on branch-rollout causal ΔReturn samples.

Input: data/training/branch_value_samples.jsonl (from collect_branch_value.py).
Each sample is (deck card-ids, relics, hp, max_hp, gold, floor, candidate card) with
a label mean_delta = CRN-paired causal ΔReturn(take card vs skip), plus stderr.

Model families:
  - card_only: original small model, ignores relics.
  - relic_lite: adds coarse relic-count/category scalar features.
  - relic_embed: adds a bag-of-relics embedding.

Base model (small, matches ~500-sample scale):
  - card embedding table (shared) over the Ironclad vocab.
  - deck representation = mean of its card embeddings (bag-of-embeddings).
  - candidate card = its own embedding.
  - scalar features: hp_frac, floor/17, gold/300, deck_size/40.
  - concat -> MLP -> scalar predicted ΔReturn.

Loss: inverse-variance weighted MSE. The probe data is only ~31% significant; a
sample with large stderr is a noisy label, so weight = 1/(stderr^2 + eps), capped,
so confident samples dominate the fit instead of noise.

Eval: grouped by presave (held-out points, no row leakage). Reports held-out
weighted-MSE and Spearman corr of predicted vs actual ΔReturn, against two
baselines: (a) predict global mean, (b) the observational coef prior
(deck_card_value_coef.json) — to check the causal model beats the correlational one.

Usage:
  python3 -m tools.learning.train_card_value_model \
      --samples data/training/branch_value_samples.jsonl \
      --epochs 300 --emb-dim 32 --out models/branch_value.pt
"""
from __future__ import annotations
import argparse, json, math, sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import numpy as np


def _norm_card(cid: Optional[str]) -> str:
    return str(cid or "").upper().replace("CARD.", "").replace("+", "").rstrip("0123456789").rstrip("_")


def _norm_relic(rid: Optional[str]) -> str:
    s = str(rid or "").upper().replace("RELIC.", "")
    out = []
    for ch in s:
        out.append(ch if ch.isalnum() else "_")
    return "_".join("".join(out).split("_")).strip("_")


_RELIC_LITE_KEYS = [
    "relic_count",
    "energy",
    "sustain",
    "strength",
    "block",
    "draw",
    "exhaust",
    "vulnerable",
    "gold",
    "potion",
]


def relic_lite_features(relics: List[str]) -> List[float]:
    """Small, deliberately coarse relic feature vector.

    This is meant to be useful at low sample counts without memorizing each relic.
    The embedding model can learn id-specific effects once data volume is higher.
    """
    names = [_norm_relic(r) for r in relics or []]
    text = " ".join(names)
    def has(*needles: str) -> float:
        return 1.0 if any(n in text for n in needles) else 0.0
    return [
        min(len(names), 20) / 20.0,
        has("ENERGY", "COFFEE", "SOZU", "LANTERN", "FLOWER"),
        has("BLOOD", "STRAWBERRY", "LEAF", "MEAT", "BREAD", "HEAL"),
        has("STRENGTH", "SWORD", "SKULL", "VAJRA"),
        has("BLOCK", "HELMET", "SCALES", "ANCHOR", "PLATE"),
        has("DRAW", "SCROLL", "CONCH", "GAMBLE", "INK"),
        has("EXHAUST", "ASH", "CHARON", "DEAD_BRANCH"),
        has("VULNERABLE", "VICIOUS", "EYE"),
        has("GOLD", "COFFER", "COIN", "MEMBERSHIP", "MAW"),
        has("POTION", "PHIAL", "HOLSTER", "BELT", "CAPSULE"),
    ]


def load_samples(path: str) -> List[Dict[str, Any]]:
    rows = []
    for line in Path(path).open():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if "card" in d and d.get("mean_delta") is not None:
            rows.append(d)
    return rows


def build_vocab(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    cards = set()
    for r in rows:
        cards.add(_norm_card(r["card"]))
        for c in (r.get("deck") or []):
            cards.add(_norm_card(c))
    vocab = {"<pad>": 0, "<unk>": 1}
    for c in sorted(cards):
        if c not in vocab:
            vocab[c] = len(vocab)
    return vocab


def build_relic_vocab(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    relics = set()
    for r in rows:
        for rel in (r.get("relics") or []):
            nr = _norm_relic(rel)
            if nr:
                relics.add(nr)
    vocab = {"<pad>": 0, "<unk>": 1}
    for rel in sorted(relics):
        if rel not in vocab:
            vocab[rel] = len(vocab)
    return vocab


def featurize(rows: List[Dict[str, Any]], vocab: Dict[str, int], *,
              model_type: str = "card_only", relic_vocab: Optional[Dict[str, int]] = None,
              max_deck: int = 40, max_relics: int = 16):
    """Return arrays: cand_idx, deck_idx, relic_idx, scalars, y, w, groups."""
    unk = vocab["<unk>"]
    rel_unk = (relic_vocab or {}).get("<unk>", 1)
    cand, deck, relic_arr, scal, y, w, groups = [], [], [], [], [], [], []
    for r in rows:
        cand.append(vocab.get(_norm_card(r["card"]), unk))
        ids = [vocab.get(_norm_card(c), unk) for c in (r.get("deck") or [])][:max_deck]
        ids = ids + [0] * (max_deck - len(ids))
        deck.append(ids)
        hp = r.get("hp") or 0; mhp = r.get("max_hp") or 80
        base_scal = [
            (hp / mhp) if mhp else 0.0,
            (r.get("floor") or 0) / 17.0,
            (r.get("gold") or 0) / 300.0,
            len(r.get("deck") or []) / 40.0,
        ]
        if model_type == "relic_lite":
            base_scal.extend(relic_lite_features(r.get("relics") or []))
        scal.append(base_scal)
        rel_ids = []
        if relic_vocab is not None:
            rel_ids = [relic_vocab.get(_norm_relic(rel), rel_unk) for rel in (r.get("relics") or [])][:max_relics]
        rel_ids = rel_ids + [0] * (max_relics - len(rel_ids))
        relic_arr.append(rel_ids)
        y.append(float(r["mean_delta"]))
        se = r.get("stderr")
        se = float(se) if se is not None else 0.1
        w.append(1.0 / (se * se + 1e-3))
        groups.append(r["presave"])
    return (np.array(cand, dtype=np.int64), np.array(deck, dtype=np.int64),
            np.array(relic_arr, dtype=np.int64),
            np.array(scal, dtype=np.float32), np.array(y, dtype=np.float32),
            np.array(w, dtype=np.float32), np.array(groups))


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 3:
        return float("nan")
    ra = np.argsort(np.argsort(a)); rb = np.argsort(np.argsort(b))
    ra = ra - ra.mean(); rb = rb - rb.mean()
    denom = math.sqrt((ra * ra).sum() * (rb * rb).sum())
    return float((ra * rb).sum() / denom) if denom > 0 else float("nan")


def _finite_or_none(x: float) -> Optional[float]:
    return float(x) if math.isfinite(float(x)) else None


def point_ranking_metrics(pred: np.ndarray, y: np.ndarray, groups: np.ndarray) -> Dict[str, Any]:
    """Decision-facing metrics inside each card reward point.

    OOF MSE answers "did we fit ΔReturn"; these answer "would the model pick the
    best offered card more often, and how much value does it leave on the table".
    """
    import collections

    by_pt = collections.defaultdict(list)
    for i, g in enumerate(groups):
        by_pt[g].append(i)

    rank_corr, top1_hits, regrets = [], [], []
    for idxs in by_pt.values():
        if len(idxs) < 2:
            continue
        idxs_arr = np.array(idxs, dtype=np.int64)
        sr = spearman(pred[idxs_arr], y[idxs_arr])
        if not math.isnan(sr):
            rank_corr.append(sr)
        pred_pick = idxs_arr[int(np.argmax(pred[idxs_arr]))]
        best_pick = idxs_arr[int(np.argmax(y[idxs_arr]))]
        top1_hits.append(1.0 if int(pred_pick) == int(best_pick) else 0.0)
        regrets.append(float(y[best_pick] - y[pred_pick]))

    return {
        "n_points": len(top1_hits),
        "within_point_spearman_mean": _finite_or_none(float(np.mean(rank_corr))) if rank_corr else None,
        "top1_match_rate": _finite_or_none(float(np.mean(top1_hits))) if top1_hits else None,
        "mean_regret": _finite_or_none(float(np.mean(regrets))) if regrets else None,
        "median_regret": _finite_or_none(float(np.median(regrets))) if regrets else None,
    }


def make_model(vocab_size: int, emb_dim: int, *,
               model_type: str = "card_only", scalar_dim: int = 4,
               relic_vocab_size: int = 0, max_relics: int = 16,
               use_card_bias: bool = False):
    import torch.nn as nn

    class CardValueModel(nn.Module):
        def __init__(self):
            super().__init__()
            self.model_type = model_type
            self.use_card_bias = use_card_bias
            self.emb = nn.Embedding(vocab_size, emb_dim, padding_idx=0)
            if use_card_bias:
                self.card_bias = nn.Embedding(vocab_size, 1, padding_idx=0)
            else:
                self.card_bias = None
            if model_type == "relic_embed":
                if relic_vocab_size <= 0:
                    raise ValueError("relic_embed requires relic_vocab_size > 0")
                self.relic_emb = nn.Embedding(relic_vocab_size, emb_dim, padding_idx=0)
                input_dim = emb_dim * 3 + scalar_dim
            else:
                self.relic_emb = None
                input_dim = emb_dim * 2 + scalar_dim
            self.mlp = nn.Sequential(
                nn.Linear(input_dim, 128), nn.ReLU(), nn.Dropout(0.1),
                nn.Linear(128, 64), nn.ReLU(), nn.Dropout(0.1),
                nn.Linear(64, 32), nn.ReLU(),
                nn.Linear(32, 1),
            )

        def forward(self, cand, deck, scal, relics=None):
            cand_e = self.emb(cand)                       # [B, D]
            deck_e = self.emb(deck)                       # [B, L, D]
            mask = (deck != 0).float().unsqueeze(-1)      # [B, L, 1]
            deck_mean = (deck_e * mask).sum(1) / mask.sum(1).clamp(min=1.0)
            parts = [cand_e, deck_mean]
            if self.model_type == "relic_embed":
                if relics is None:
                    raise ValueError("relic_embed forward requires relics")
                rel_e = self.relic_emb(relics)
                rel_mask = (relics != 0).float().unsqueeze(-1)
                rel_mean = (rel_e * rel_mask).sum(1) / rel_mask.sum(1).clamp(min=1.0)
                parts.append(rel_mean)
            parts.append(scal)
            x = self.mlp(__import__("torch").cat(parts, dim=-1))
            if self.card_bias is not None:
                x = x + self.card_bias(cand)
            return x.squeeze(-1)

    return CardValueModel()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", default="data/training/branch_value_samples.jsonl")
    ap.add_argument("--epochs", type=int, default=300)
    ap.add_argument("--emb-dim", type=int, default=32)
    ap.add_argument("--lr", type=float, default=3e-3)
    ap.add_argument("--folds", type=int, default=5, help="grouped CV folds (by presave)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--model-type", default="card_only",
                    choices=["card_only", "relic_lite", "relic_embed"])
    ap.add_argument("--max-relics", type=int, default=16)
    ap.add_argument("--out", default="models/branch_value.pt")
    ap.add_argument("--metrics-out", default=None,
                    help="optional JSON metrics path for model/version comparison")
    ap.add_argument("--split-out", default=None,
                    help="optional JSON path recording grouped fold assignment by presave")
    a = ap.parse_args()

    import torch
    torch.manual_seed(a.seed); np.random.seed(a.seed)

    rows = load_samples(a.samples)
    vocab = build_vocab(rows)
    relic_vocab = build_relic_vocab(rows) if a.model_type == "relic_embed" else None
    cand, deck, relics, scal, y, w, groups = featurize(
        rows, vocab, model_type=a.model_type, relic_vocab=relic_vocab, max_relics=a.max_relics)
    print(f"samples={len(rows)} model_type={a.model_type} vocab={len(vocab)} "
          f"relic_vocab={len(relic_vocab or {})} scalar_dim={scal.shape[1]} "
          f"uniq_presaves={len(set(groups))} y[mean={y.mean():.4f} std={y.std():.4f}]", flush=True)

    # observational coef prior baseline
    coef = {}
    try:
        coef = json.loads((REPO / "data/learning/deck_card_value_coef.json").read_text())["coef"]
    except Exception:
        pass
    coef_pred = np.array([coef.get(_norm_card(r["card"]), 0.0) for r in rows], dtype=np.float32)

    uniq = sorted(set(groups.tolist()))
    rng = np.random.RandomState(a.seed); rng.shuffle(uniq)
    fold_of = {g: i % a.folds for i, g in enumerate(uniq)}
    fold_idx = np.array([fold_of[g] for g in groups])
    if a.split_out:
        split_path = Path(a.split_out)
        split_path.parent.mkdir(parents=True, exist_ok=True)
        split_path.write_text(json.dumps({
            "samples": a.samples,
            "seed": a.seed,
            "folds": a.folds,
            "groups": {str(g): int(fold_of[g]) for g in sorted(fold_of)},
        }, indent=2, sort_keys=True) + "\n")

    oof = np.zeros(len(y), dtype=np.float32)   # out-of-fold predictions
    for f in range(a.folds):
        tr = fold_idx != f; te = fold_idx == f
        if te.sum() == 0:
            continue
        model = make_model(len(vocab), a.emb_dim, model_type=a.model_type,
                           scalar_dim=int(scal.shape[1]),
                           relic_vocab_size=len(relic_vocab or {}),
                           max_relics=a.max_relics)
        opt = torch.optim.Adam(model.parameters(), lr=a.lr, weight_decay=1e-4)
        Ct, Dt = torch.tensor(cand[tr]), torch.tensor(deck[tr])
        Rt, St = torch.tensor(relics[tr]), torch.tensor(scal[tr])
        Yt, Wt = torch.tensor(y[tr]), torch.tensor(w[tr])
        Wt = Wt / Wt.mean()
        Ce, De = torch.tensor(cand[te]), torch.tensor(deck[te])
        Re, Se = torch.tensor(relics[te]), torch.tensor(scal[te])
        model.train()
        for ep in range(a.epochs):
            opt.zero_grad()
            pred = model(Ct, Dt, St, Rt)
            loss = (Wt * (pred - Yt) ** 2).mean()
            loss.backward(); opt.step()
        model.eval()
        with torch.no_grad():
            oof[te] = model(Ce, De, Se, Re).numpy()

    # metrics (out-of-fold = honest held-out)
    def wmse(p):
        return float((w * (p - y) ** 2).sum() / w.sum())
    base_mean = np.full_like(y, y.mean())
    print(f"\n=== grouped {a.folds}-fold OOF (held-out by presave) ===", flush=True)
    print(f"  model        wMSE={wmse(oof):.5f}  spearman={spearman(oof, y):+.3f}", flush=True)
    print(f"  predict-mean wMSE={wmse(base_mean):.5f}", flush=True)
    print(f"  coef-prior   wMSE={wmse(coef_pred):.5f}  spearman={spearman(coef_pred, y):+.3f}", flush=True)
    model_point = point_ranking_metrics(oof, y, groups)
    coef_point = point_ranking_metrics(coef_pred, y, groups)
    if model_point["n_points"]:
        print(f"  within-point ranking spearman: mean={model_point['within_point_spearman_mean']:+.3f} "
              f"(n_points={model_point['n_points']})", flush=True)
        print(f"  model top1_match={model_point['top1_match_rate']:.3f} "
              f"mean_regret={model_point['mean_regret']:.5f}", flush=True)

    metrics = {
        "samples": a.samples,
        "n_samples": int(len(rows)),
        "n_presaves": int(len(set(groups))),
        "model_type": a.model_type,
        "emb_dim": int(a.emb_dim),
        "epochs": int(a.epochs),
        "lr": float(a.lr),
        "folds": int(a.folds),
        "seed": int(a.seed),
        "vocab_size": int(len(vocab)),
        "relic_vocab_size": int(len(relic_vocab or {})),
        "scalar_dim": int(scal.shape[1]),
        "target_mean": float(y.mean()),
        "target_std": float(y.std()),
        "oof": {
            "model_wmse": wmse(oof),
            "model_spearman": _finite_or_none(spearman(oof, y)),
            "predict_mean_wmse": wmse(base_mean),
            "coef_prior_wmse": wmse(coef_pred),
            "coef_prior_spearman": _finite_or_none(spearman(coef_pred, y)),
            "model_point": model_point,
            "coef_prior_point": coef_point,
        },
    }

    # train final model on ALL data, save with vocab
    model = make_model(len(vocab), a.emb_dim, model_type=a.model_type,
                       scalar_dim=int(scal.shape[1]),
                       relic_vocab_size=len(relic_vocab or {}),
                       max_relics=a.max_relics)
    opt = torch.optim.Adam(model.parameters(), lr=a.lr, weight_decay=1e-4)
    C, D, R, S = torch.tensor(cand), torch.tensor(deck), torch.tensor(relics), torch.tensor(scal)
    Y, W = torch.tensor(y), torch.tensor(w); W = W / W.mean()
    model.train()
    for ep in range(a.epochs):
        opt.zero_grad()
        loss = (W * (model(C, D, S, R) - Y) ** 2).mean()
        loss.backward(); opt.step()
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    torch.save({"state_dict": model.state_dict(), "vocab": vocab,
                "relic_vocab": relic_vocab, "emb_dim": a.emb_dim,
                "model_type": a.model_type, "scalar_dim": int(scal.shape[1]),
                "max_relics": a.max_relics, "y_mean": float(y.mean()),
                "metrics": metrics}, out)
    print(f"\nsaved final model -> {out}", flush=True)
    if a.metrics_out:
        metrics_path = Path(a.metrics_out)
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n")
        print(f"saved metrics -> {metrics_path}", flush=True)


if __name__ == "__main__":
    main()
