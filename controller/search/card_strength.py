from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List


@lru_cache(maxsize=1)
def load_card_types() -> Dict[str, str]:
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / 'data' / 'card_stats' / 'sts2_linear_metadata_dataset.csv'
    types: Dict[str, str] = {}
    with path.open('r', encoding='utf-8-sig', newline='') as stream:
        for row in csv.DictReader(stream):
            card_id = str(row.get('card_id') or '').strip().upper()
            if card_id:
                types[card_id] = str(row.get('type') or '').strip().upper()
    return types


@lru_cache(maxsize=1)
def load_card_strengths() -> Dict[str, float]:
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / 'data' / 'card_stats' / 'sts2_linear_metadata_dataset.csv'
    strengths: Dict[str, float] = {}
    with path.open('r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            card_id = str(row.get('card_id') or '').strip().upper()
            if not card_id:
                continue
            try:
                strength = float(row.get('strength_score') or 0.0)
            except ValueError:
                strength = 0.0
            strengths[card_id] = strength
    strengths.update({k: v for k, v in load_gamersky_fallback_strengths().items() if k not in strengths})
    return strengths


@lru_cache(maxsize=1)
def load_gamersky_fallback_strengths() -> Dict[str, float]:
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / 'data' / 'card_stats' / 'gamersky_sts2_card_stats.csv'
    strengths: Dict[str, float] = {}
    with path.open('r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            card_id = str(row.get('cardEnName') or '').strip().upper()
            if not card_id:
                continue
            try:
                pick = float(row.get('avgPickRate') or 0.0)
            except ValueError:
                pick = 0.0
            try:
                win = float(row.get('avgWinRate') or 0.0)
            except ValueError:
                win = 0.0
            build_type = str(row.get('buildType') or '')
            mechanism = str(row.get('mechanism') or '')
            # Normalize weak/basic cards into a small band around zero, but treat
            # generic status junk as clearly negative future draw quality.
            score = (win - 30.0) / 5.0 + pick / 100.0
            if '状态' in build_type or '状态' in mechanism:
                score = min(score, -2.5)
            strengths[card_id] = score
    return strengths


def expected_visible_draw_pool_strength(search_state: Dict[str, Any], hand_size: int = 5) -> float:
    combat = search_state.get('combat') or {}
    pool: List[Dict[str, Any]] = []
    pool.extend(combat.get('draw_pile') or [])
    pool.extend(combat.get('discard_pile') or [])
    pool.extend(combat.get('hand') or [])
    if not pool:
        return 0.0
    strengths = load_card_strengths()
    total = 0.0
    count = 0
    for card in pool:
        if not isinstance(card, dict):
            continue
        card_id = str(card.get('card_id') or '').strip().upper()
        total += strengths.get(card_id, 0.0)
        count += 1
    if count <= 0:
        return 0.0
    draws = min(hand_size, count)
    return (total / count) * draws


def expected_future_attack_plays(search_state: Dict[str, Any], turns: float) -> float:
    combat = search_state.get('combat') or {}
    pool: List[Dict[str, Any]] = []
    for key in ('draw_pile', 'discard_pile', 'hand'):
        pool.extend(combat.get(key) or [])
    if not pool:
        return 0.0
    card_types = load_card_types()
    attacks = 0
    cards = 0
    for card in pool:
        if not isinstance(card, dict):
            continue
        cards += 1
        card_id = str(card.get('card_id') or card.get('id') or '').split('.')[-1].strip().upper()
        card_type = str(card.get('type') or card.get('card_type') or card_types.get(card_id) or '').upper()
        if card_type == 'ATTACK':
            attacks += 1
    if cards <= 0:
        return 0.0
    return attacks / cards * 5.0 * max(0.0, turns)
