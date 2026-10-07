from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


class ActionValidationError(ValueError):
    pass


@dataclass(frozen=True)
class SearchAction:
    action_type: str
    card_index: Optional[int] = None
    target_index: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    return int(value)


def action_from_available(raw: Dict[str, Any]) -> SearchAction:
    return SearchAction(
        action_type=str(raw["action_type"]),
        card_index=_as_int(raw.get("card_index")),
        target_index=_as_int(raw.get("target_index")),
        metadata=dict(raw.get("metadata") or {}),
    )


def available_actions_from_search_state(search_state: Dict[str, Any]) -> List[SearchAction]:
    combat = search_state.get("combat") or {}
    raw_actions = combat.get("available_actions") or []
    return [action_from_available(raw) for raw in raw_actions if isinstance(raw, dict)]


def action_signature(action: SearchAction) -> Tuple[Any, ...]:
    potion_index = None
    if action.metadata:
        potion_index = action.metadata.get("potion_index")

    if action.action_type == "play_card":
        return (action.action_type, action.card_index, action.target_index)
    if action.action_type == "use_potion":
        return (action.action_type, potion_index, action.target_index)
    if action.action_type == "discard_potion":
        return (action.action_type, potion_index)
    return (action.action_type,)


def validate_action(action: SearchAction, legal_actions: Iterable[SearchAction]) -> None:
    legal_signatures = {action_signature(a) for a in legal_actions}
    candidate = action_signature(action)
    if candidate not in legal_signatures:
        raise ActionValidationError(
            f"Illegal action {candidate}; legal={sorted(legal_signatures, key=str)}"
        )


def cli_payload_for_action(action: SearchAction) -> Tuple[str, Dict[str, Any]]:
    payload: Dict[str, Any] = {}

    if action.action_type == "play_card":
        if action.card_index is None:
            raise ActionValidationError("play_card requires card_index")
        payload["card_index"] = action.card_index
        if action.target_index is not None:
            payload["target_index"] = action.target_index
        return "play_card", payload

    if action.action_type == "end_turn":
        return "end_turn", payload

    if action.action_type == "use_potion":
        potion_index = (action.metadata or {}).get("potion_index")
        if potion_index is None:
            raise ActionValidationError("use_potion requires metadata.potion_index")
        payload["potion_index"] = int(potion_index)
        if action.target_index is not None:
            payload["target_index"] = action.target_index
        return "use_potion", payload

    if action.action_type == "discard_potion":
        potion_index = (action.metadata or {}).get("potion_index")
        if potion_index is None:
            raise ActionValidationError("discard_potion requires metadata.potion_index")
        payload["potion_index"] = int(potion_index)
        return "discard_potion", payload

    raise ActionValidationError(f"Unsupported action_type={action.action_type}")


CARD_INSTANCE_FIELDS = (
    "upgrade",
    "current_cost",
    "display_cost",
    "display_costs_x",
    "keywords",
    "affliction",
    "affliction_count",
)


def _normalize_card_id(card_id: str) -> str:
    normalized = str(card_id or '').strip().upper()
    if normalized.startswith('CARD.'):
        normalized = normalized[5:]
    return normalized.replace('-', '_').replace(' ', '_')


def _normalized_card_metadata_value(field: str, value: Any) -> Any:
    if field == "keywords":
        return tuple(sorted(str(item) for item in (value or [])))
    return value


def _same_card_instance(chosen: Dict[str, Any], candidate: Dict[str, Any]) -> bool:
    chosen_card_id = _normalize_card_id(str(chosen.get("card_id") or ""))
    candidate_card_id = _normalize_card_id(str(candidate.get("card_id") or ""))
    if chosen_card_id and candidate_card_id != chosen_card_id:
        return False
    for field in CARD_INSTANCE_FIELDS:
        if field not in chosen:
            continue
        if (
            _normalized_card_metadata_value(field, candidate.get(field))
            != _normalized_card_metadata_value(field, chosen.get(field))
        ):
            return False
    return True


def resolve_combat_payload(
    current_search_state: Dict[str, Any],
    action: str,
    payload: Dict[str, Any],
    chosen_metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve a semantic action against the current backend search state."""
    combat = current_search_state.get("combat") or {}
    available = combat.get("available_actions") or []
    chosen_metadata = chosen_metadata or {}

    if action in {"use_potion", "discard_potion"}:
        chosen_potion_id = str(chosen_metadata.get("potion_id") or "")
        chosen_target_monster_id = str(chosen_metadata.get("target_monster_id") or "")
        chosen_target_type = str(chosen_metadata.get("target_type") or "")

        def matches_potion(av: Dict[str, Any]) -> bool:
            if str(av.get("action_type") or "") != action:
                return False
            metadata = av.get("metadata") or {}
            if chosen_potion_id and str(metadata.get("potion_id") or "") != chosen_potion_id:
                return False
            if chosen_target_monster_id:
                return str(metadata.get("target_monster_id") or "") == chosen_target_monster_id
            if chosen_target_type in {"AnyEnemy", "SingleEnemy"}:
                return av.get("target_index") is not None
            return True

        exact = [av for av in available if matches_potion(av)]
        if exact:
            metadata = exact[0].get("metadata") or {}
            resolved = {
                "potion_index": int(metadata.get("potion_index", exact[0].get("potion_index", 0)))
            }
            if exact[0].get("target_index") is not None:
                resolved["target_index"] = int(exact[0]["target_index"])
            return resolved

        same_potion = [
            av for av in available
            if str(av.get("action_type") or "") == action
            and str((av.get("metadata") or {}).get("potion_id") or "") == chosen_potion_id
        ]
        if same_potion:
            metadata = same_potion[0].get("metadata") or {}
            resolved = {
                "potion_index": int(
                    metadata.get("potion_index", same_potion[0].get("potion_index", 0))
                )
            }
            if same_potion[0].get("target_index") is not None:
                resolved["target_index"] = int(same_potion[0]["target_index"])
            return resolved
        return dict(payload)

    if action != "play_card":
        return dict(payload)

    target_index = payload.get("target_index")
    chosen_card_id = str(chosen_metadata.get("card_id") or "")
    chosen_target_type = str(chosen_metadata.get("target_type") or "")
    chosen_target_monster_id = str(chosen_metadata.get("target_monster_id") or "")
    has_instance_fingerprint = any(field in chosen_metadata for field in CARD_INSTANCE_FIELDS)

    def matches(av: Dict[str, Any]) -> bool:
        if str(av.get("action_type") or "") != "play_card":
            return False
        metadata = av.get("metadata") or {}
        if not _same_card_instance(chosen_metadata, metadata):
            return False
        av_target = av.get("target_index")
        # target_index identifies an occurrence in the current combat.  It
        # must take precedence over target_monster_id because several enemies
        # can share the same model id (for example PHANTASMAL_GARDENER).
        if target_index is not None and av_target != target_index:
            return False
        if chosen_target_monster_id:
            return str(metadata.get("target_monster_id") or "") == chosen_target_monster_id
        if target_index is None and chosen_target_type in {"AnyEnemy", "SingleEnemy"}:
            return False
        return True

    exact = [av for av in available if matches(av)]
    if exact:
        resolved = {"card_index": int(exact[0]["card_index"])}
        if exact[0].get("target_index") is not None:
            resolved["target_index"] = int(exact[0]["target_index"])
        return resolved

    if has_instance_fingerprint:
        return None

    # Backward compatibility for old recorded states that only identify the
    # card model. Current engine exports use the strict fingerprint above.
    same_card = [
        av for av in available
        if str(av.get("action_type") or "") == "play_card"
        and _normalize_card_id(str((av.get("metadata") or {}).get("card_id") or ""))
        == _normalize_card_id(chosen_card_id)
    ]
    if same_card:
        resolved = {"card_index": int(same_card[0]["card_index"])}
        if same_card[0].get("target_index") is not None:
            resolved["target_index"] = int(same_card[0]["target_index"])
        return resolved
    return None


def resolve_planned_action(
    current_search_state: Dict[str, Any],
    chosen: SearchAction,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    action, raw_payload = cli_payload_for_action(chosen)
    payload = resolve_combat_payload(
        current_search_state,
        action,
        raw_payload,
        chosen.metadata,
    )
    if payload is None:
        return None
    for legal in available_actions_from_search_state(current_search_state):
        if legal.action_type != chosen.action_type:
            continue
        legal_action, legal_payload = cli_payload_for_action(legal)
        resolved_legal = resolve_combat_payload(
            current_search_state,
            legal_action,
            legal_payload,
            legal.metadata,
        )
        if action == legal_action and payload == resolved_legal:
            return action, payload
    return None


def summarize_combat_action(
    current_search_state: Dict[str, Any],
    action: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    for legal in available_actions_from_search_state(current_search_state):
        legal_action, legal_payload = cli_payload_for_action(legal)
        resolved_legal = resolve_combat_payload(
            current_search_state,
            legal_action,
            legal_payload,
            legal.metadata,
        )
        if action == legal_action and payload == resolved_legal:
            return {
                "action_type": legal.action_type,
                "card_index": legal.card_index,
                "target_index": legal.target_index,
                "metadata": legal.metadata,
            }
    return {
        "action_type": action,
        "card_index": payload.get("card_index"),
        "target_index": payload.get("target_index"),
        "metadata": {},
    }
