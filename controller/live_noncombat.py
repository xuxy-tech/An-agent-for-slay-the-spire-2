from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from controller.action_transaction import ActionCommand
from controller.interaction_state import explicit_selection_count
from controller.run_agent import (
    _card_descriptions,
    choose_card_selection,
    choose_event_option,
    choose_rest_option,
    infer_card_selection_mode,
)


class UnsupportedClientDecision(RuntimeError):
    """The visible client exposed a decision we cannot identify safely."""


ClientDecision = ActionCommand


def client_event_policy_state(state: Dict[str, Any]) -> Dict[str, Any]:
    event = state.get("event") or {}
    run = state.get("run") or {}
    return {
        "event_id": event.get("event_id"),
        "event_name": event.get("event_id"),
        "options": [
            {
                **option,
                "option_id": option.get("text_key"),
                "label": option.get("title"),
                "is_enabled": not bool(option.get("is_locked")),
            }
            for option in event.get("options") or []
        ],
        "player": {
            "hp": run.get("current_hp"),
            "max_hp": run.get("max_hp"),
            "gold": run.get("gold"),
            "deck": run.get("deck") or [],
        },
        "context": {"floor": run.get("floor")},
    }


def choose_client_event(state: Dict[str, Any]) -> ClientDecision:
    policy_state = client_event_policy_state(state)
    enabled = [
        option
        for option in policy_state["options"]
        if not option.get("is_locked") and option.get("index") is not None
    ]
    if not enabled:
        raise UnsupportedClientDecision("事件没有完整、可用的选项")

    # Once an event has resolved, select its explicit continuation rather than
    # scoring the button as if it were another reward.
    proceed = next((option for option in enabled if option.get("is_proceed")), None)
    if proceed is not None:
        chosen = proceed
        policy = "explicit_proceed"
    else:
        payload = choose_event_option(policy_state)
        wanted = int(payload["option_index"])
        chosen = next(
            (option for option in enabled if int(option["index"]) == wanted), None
        )
        if chosen is None:
            raise UnsupportedClientDecision(
                f"事件策略选择了不可见选项 {wanted}; 可见选项={[o['index'] for o in enabled]}"
            )
        policy = "existing_event_heuristic"

    operation = infer_card_selection_mode(chosen)
    return ClientDecision(
        action="choose_event_option",
        params={"option_index": int(chosen["index"])},
        operation=operation,
        telemetry={
            "policy": policy,
            "event_id": policy_state.get("event_id"),
            "option_index": int(chosen["index"]),
            "option_id": chosen.get("option_id"),
            "selection_operation": operation,
        },
    )


def client_selection_policy_state(state: Dict[str, Any]) -> Dict[str, Any]:
    selection = state.get("selection") or {}
    run = state.get("run") or {}
    return {
        "cards": selection.get("cards") or [],
        "min_select": selection.get("min_select"),
        "max_select": selection.get("max_select"),
        "can_skip": selection.get("can_skip") if selection.get("can_skip") is not None else
                    bool(set(state.get('available_actions') or []).intersection({'cancel_selection', 'skip_reward', 'proceed'})),
        "player": {
            "hp": run.get("current_hp"),
            "max_hp": run.get("max_hp"),
            "gold": run.get("gold"),
            "deck": run.get("deck") or [],
        },
        "context": {"floor": run.get("floor")},
    }


def choose_client_selection(
    state: Dict[str, Any],
    repo_root: Path,
    pending_operation: Optional[str] = None,
    deck_profile: Optional[Dict[str, Any]] = None,
) -> ClientDecision:
    selection = state.get("selection") or {}
    kind = str(selection.get("kind") or "")
    operation = infer_card_selection_mode(selection_kind=kind) or pending_operation
    if operation is None:
        raise UnsupportedClientDecision(
            f"无法识别选牌目的；selection.kind={kind!r}。已暂停，未进行猜测点击。"
        )
    policy_state = client_selection_policy_state(state)
    count_source = 'mod'
    if not policy_state.get('min_select') and not policy_state.get('max_select'):
        # Native deck grids may report 0/0; only an explicit prompt quantity
        # can supply the missing bound.
        count = explicit_selection_count(selection)
        if count is None:
            raise UnsupportedClientDecision('Selection prompt has no valid explicit card count')
        policy_state['min_select'] = policy_state['max_select'] = count
        count_source = 'explicit_prompt'
    choice = choose_card_selection(policy_state, repo_root, operation, deck_profile)
    if choice is None:
        raise UnsupportedClientDecision(
            f"{operation} 选牌界面没有可选择的完整卡牌数据"
        )
    indices = [int(value) for value in str(choice["indices"]).split(",") if value]
    minimum = int(policy_state.get('min_select') or 0)
    maximum = int(policy_state.get('max_select') or len(selection.get('cards') or []))
    if not minimum <= len(indices) <= maximum:
        raise UnsupportedClientDecision(
            f'Selection policy returned {len(indices)} cards; required {minimum}..{maximum}')
    if not indices:
        raise UnsupportedClientDecision(f"{operation} 策略没有返回卡牌索引")

    visible_cards = selection.get("cards") or []
    if len(indices) > 1:
        if selection.get('selected_count', 0) != 0:
            raise UnsupportedClientDecision('Cannot begin a multi-card selection with unknown prior selections')
        chosen = [visible_cards[index] for index in indices]
        native_indices = [card.get('index') for card in chosen]
        if any(type(index) is not int for index in native_indices) or len(set(native_indices)) != len(indices):
            raise UnsupportedClientDecision('Multi-card selection has invalid native indices')
        return ClientDecision(
            action='select_deck_cards', params={'indices': native_indices}, operation=operation,
            telemetry={'policy': f'card_selection_{operation}', 'selection_operation': operation,
                       'selection_kind': kind, 'selection_count': len(indices), 'count_source': count_source,
                       'card_ids': [card.get('card_id') for card in chosen]})
    index = indices[0]
    if not (0 <= index < len(visible_cards)):
        raise UnsupportedClientDecision(
            f"选牌索引 {index} 超出客户端卡牌列表范围 {len(visible_cards)}"
        )
    card = visible_cards[index] or {}
    native_index = card.get("index")
    if native_index is None:
        raise UnsupportedClientDecision("客户端选牌项缺少稳定 index")
    return ClientDecision(
        action="select_deck_card",
        params={"option_index": int(native_index)},
        operation=operation,
        telemetry={
            "policy": f"card_selection_{operation}",
            "selection_kind": kind,
            "selection_operation": operation,
            "card_index": int(native_index),
            "card_id": card.get("card_id"),
        },
    )


def choose_client_rest(state: Dict[str, Any], repo_root: Path) -> ClientDecision:
    rest = state.get("rest") or {}
    run = state.get("run") or {}
    policy_state = {
        "options": rest.get("options") or [],
        "player": {
            "hp": run.get("current_hp"),
            "max_hp": run.get("max_hp"),
            "gold": run.get("gold"),
            "deck": run.get("deck") or [],
        },
        "context": {"floor": run.get("floor")},
    }
    payload = choose_rest_option(policy_state, _card_descriptions(repo_root))
    wanted = int(payload["option_index"])
    option = next(
        (
            row
            for row in policy_state["options"]
            if row.get("index") is not None and int(row["index"]) == wanted
        ),
        None,
    )
    if option is None or not option.get("is_enabled"):
        raise UnsupportedClientDecision(f"营火策略选择了不可用选项 {wanted}")
    operation = infer_card_selection_mode(option)
    return ClientDecision(
        action="choose_rest_option",
        params={"option_index": wanted},
        operation=operation,
        telemetry={
            "policy": "existing_rest_heuristic",
            "option_index": wanted,
            "option_id": option.get("option_id"),
            "selection_operation": operation,
        },
    )
