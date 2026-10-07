from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from controller.deck_profile import (
    choose_profile_reward,
    load_deck_profile,
    score_profile_card,
)


# Compatibility entry points for callers and historical tests. The named-card
# policy itself lives in the editable draft profile, never in combat ordering.
def score_self_damage_card(card_id: str, deck_ids: Iterable[str]) -> Dict[str, Any]:
    return score_profile_card(load_deck_profile(), card_id, deck_ids)


def choose_self_damage_reward(
    cards: Iterable[Dict[str, Any]], deck: Iterable[Dict[str, Any]]
) -> tuple[Optional[Dict[str, Any]], list[Dict[str, Any]]]:
    return choose_profile_reward(load_deck_profile(), cards, deck)
