"""Shared enemy-intent damage semantics.

Current headless snapshots export ``total_damage`` as the authoritative damage
for the whole intent. Older recorded states only have per-hit
``display_damage`` plus ``hits``; keep that fallback so historical regression
fixtures remain readable without reintroducing the multi-hit double count.
"""
from __future__ import annotations

from typing import Any, Mapping


DAMAGE_INTENT_TYPES = frozenset({"attack", "deathblow"})


def intent_total_damage(intent: Mapping[str, Any] | None) -> float:
    if not intent:
        return 0.0
    total = intent.get("total_damage")
    if isinstance(total, (int, float)):
        return float(total)
    damage = intent.get("display_damage")
    if not isinstance(damage, (int, float)):
        return 0.0
    hits = intent.get("hits")
    return float(damage) * float(hits if isinstance(hits, (int, float)) and hits > 0 else 1)


def intent_deals_damage(intent: Mapping[str, Any] | None) -> bool:
    """Return whether an intent represents incoming direct damage.

    STS2 has damage-bearing intent subclasses whose visible type is not
    literally Attack. DeathBlow is one example. Prefer explicit intent types,
    then retain a numeric fallback for old captures and future engine types.
    """
    if not intent:
        return False
    raw_types = intent.get("intent_types")
    if raw_types is None and intent.get("type") is not None:
        raw_types = [intent.get("type")]
    if raw_types:
        if any(str(value).replace("_", "").casefold() in DAMAGE_INTENT_TYPES
               for value in raw_types):
            return True
    return intent_total_damage(intent) > 0
