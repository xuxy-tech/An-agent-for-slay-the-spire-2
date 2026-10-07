from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class ActionFamily(str, Enum):
    SESSION = "session"
    COMBAT = "combat"
    NAVIGATION = "navigation"
    EVENT = "event"
    REST = "rest"
    REWARD = "reward"
    SHOP = "shop"
    SELECTION = "selection"
    TREASURE = "treasure"
    OVERLAY = "overlay"


class MirrorMode(str, Enum):
    MIRRORED = "mirrored"
    CLIENT_ONLY = "client_only"
    SHADOW_ONLY = "shadow_only"


class RecoveryPolicy(str, Enum):
    REPLAY_SHADOW_IF_COMPLETED = "replay_shadow_if_completed"
    REOBSERVE_CLIENT = "reobserve_client"
    MANUAL_RECONCILE = "manual_reconcile"


class SettlementPhase(str, Enum):
    PREPARED = 'PREPARED'
    SUBMITTED = 'SUBMITTED'
    AWAITING_INPUT = 'AWAITING_INPUT'
    RESUMING = 'RESUMING'
    CLIENT_SETTLED = 'CLIENT_SETTLED'
    BOTH_SETTLED = 'BOTH_SETTLED'
    VERIFIED = 'VERIFIED'
    UNKNOWN = 'UNKNOWN'
    DIVERGED = 'DIVERGED'


class BoundaryType(str, Enum):
    COMBAT_PLAYER_DECISION = 'combat_player_decision'
    COMBAT_CHOICE = 'combat_choice'
    REWARD_OVERVIEW = 'reward_overview'
    REWARD_CARD_CHOICE = 'reward_card_choice'
    MAP_DECISION = 'map_decision'
    EVENT_DECISION = 'event_decision'
    CRYSTAL_SPHERE = 'crystal_sphere'
    TERMINAL = 'terminal'


def client_boundary_type(state: Mapping[str, Any]) -> BoundaryType | None:
    screen = state.get('screen')
    if screen == 'CARD_SELECTION' and state.get('in_combat') is True:
        return BoundaryType.COMBAT_CHOICE
    if screen == 'CARD_SELECTION' and (state.get('reward') or {}).get('pending_card_choice') is True:
        return BoundaryType.REWARD_CARD_CHOICE
    if state.get('in_combat') is True and screen == 'COMBAT':
        return BoundaryType.COMBAT_PLAYER_DECISION
    return {'REWARD': BoundaryType.REWARD_OVERVIEW,
            'MAP': BoundaryType.MAP_DECISION,
            'EVENT': BoundaryType.EVENT_DECISION,
            'CRYSTAL_SPHERE': BoundaryType.CRYSTAL_SPHERE,
            'GAME_OVER': BoundaryType.TERMINAL}.get(screen)


def shadow_boundary_type(state: Mapping[str, Any]) -> BoundaryType | None:
    return {'combat_play': BoundaryType.COMBAT_PLAYER_DECISION,
            'card_select': BoundaryType.COMBAT_CHOICE,
            'combat_reward': BoundaryType.REWARD_OVERVIEW,
            'card_reward': BoundaryType.REWARD_CARD_CHOICE,
            'map_select': BoundaryType.MAP_DECISION,
            'event_choice': BoundaryType.EVENT_DECISION,
            'crystal_sphere': BoundaryType.CRYSTAL_SPHERE,
            'game_over': BoundaryType.TERMINAL,
            'defeat': BoundaryType.TERMINAL}.get(state.get('decision'))


CLIENT_AUTHORITATIVE_BOUNDARIES = frozenset({"map"})


# A mirrored transaction is one logical action with two transports.  Keep the
# mapping in one place so a client command cannot accidentally be paired with
# an unrelated shadow command (the old code only validated each side alone).
CLIENT_SHADOW_ACTIONS: dict[str, frozenset[str]] = {
    "choose_map_node": frozenset({"select_map_node"}),
    "play_card": frozenset({"play_card"}),
    "end_turn": frozenset({"end_turn"}),
    "use_potion": frozenset({"use_potion"}),
    "discard_potion": frozenset({"discard_potion"}),
    "open_chest": frozenset({"open_chest"}),
    "choose_treasure_relic": frozenset({"choose_treasure_relic"}),
    "choose_event_option": frozenset({"choose_option", "reconcile_relics", "finish_combat_rewards"}),
    "crystal_sphere_divine": frozenset({"crystal_sphere_divine"}),
    "choose_rest_option": frozenset({"choose_option"}),
    "claim_reward": frozenset({"claim_combat_reward", "ack_event_reward"}),
    "choose_reward_card": frozenset({"select_card_reward"}),
    "skip_reward_cards": frozenset({"skip_card_reward"}),
    # Older client builds expose a single resolve command for both choices.
    # The bridge selects the concrete shadow action from the requested decision.
    "resolve_rewards": frozenset({"select_card_reward", "skip_card_reward"}),
    "collect_rewards_and_proceed": frozenset({"finish_combat_rewards"}),
    "buy_card": frozenset({"buy_card"}),
    "buy_relic": frozenset({"buy_relic"}),
    "buy_potion": frozenset({"buy_potion"}),
    "remove_card": frozenset({"remove_card"}),
    "remove_card_at_shop": frozenset({"remove_card"}),
    "select_deck_card": frozenset({"select_cards"}),
    "select_deck_cards": frozenset({"select_cards"}),
    "confirm_selection": frozenset({"select_cards"}),
    "cancel_selection": frozenset({"skip_select"}),
    # reconcile_relics is a boundary repair carried by the same visible
    # proceed click; it is deliberately explicit rather than an arbitrary
    # client/shadow pairing.
    "proceed": frozenset({"proceed", "leave_room", "reconcile_relics", "finish_combat_rewards"}),
    "choose_bundle": frozenset({"select_bundle"}),
}


def validate_action_mapping(client: "ActionCommand | None", shadow: "ActionCommand | None") -> None:
    """Validate the cross-side protocol without doing any state inspection."""
    if client is None or shadow is None:
        return
    allowed = CLIENT_SHADOW_ACTIONS.get(client.action)
    if allowed is None or shadow.action not in allowed:
        raise ValueError(
            f"Unmapped mirrored transaction: client={client.action!r}, "
            f"shadow={shadow.action!r}"
        )


@dataclass(frozen=True)
class ActionDefinition:
    intent: str
    family: ActionFamily
    atomic: bool = True


@dataclass(frozen=True)
class ActionCommand:
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    operation: str | None = None
    telemetry: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "params": dict(self.params)}


@dataclass(frozen=True)
class ActionTransaction:
    """One player decision across the visible client and headless shadow.

    Player intent is separate from transport commands. A multi-click native
    selection is still one atomic player transaction.
    """

    intent: str
    family: ActionFamily
    client: ActionCommand | None
    shadow: ActionCommand | None
    completion: str = "state_change"
    operation: str | None = None
    telemetry: dict[str, Any] = field(default_factory=dict)
    atomic: bool = True
    recovery: RecoveryPolicy = RecoveryPolicy.REOBSERVE_CLIENT

    @property
    def mirror_mode(self) -> MirrorMode:
        if self.client is not None and self.shadow is not None:
            return MirrorMode.MIRRORED
        if self.client is not None:
            return MirrorMode.CLIENT_ONLY
        return MirrorMode.SHADOW_ONLY

    @property
    def shadow_action(self) -> str | None:
        return self.shadow.action if self.shadow else None

    @property
    def shadow_args(self) -> dict[str, Any] | None:
        return dict(self.shadow.params) if self.shadow else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "family": self.family.value,
            "client": self.client.to_dict() if self.client else None,
            "shadow": self.shadow.to_dict() if self.shadow else None,
            "completion": self.completion,
            "operation": self.operation,
            "telemetry": dict(self.telemetry),
            "atomic": self.atomic,
            "recovery": self.recovery.value,
            "mirror_mode": self.mirror_mode.value,
        }


def map_object_indices(client_rows, shadow_rows, selected_indices, identity):
    """Map exported objects by identity and, when equal, complete offer order.

    Duplicate model IDs are legal. Ordered equal offers identify occurrences;
    reordered offers require a unique identity. No room/card whitelist is used.
    Callers supply domain-specific identity, including relevant modifiers.
    """
    left, right = list(client_rows), list(shadow_rows)
    left_keys, right_keys = [identity(r) for r in left], [identity(r) for r in right]
    ordered = left_keys == right_keys
    result = []
    for selected in selected_indices:
        matches = [i for i, r in enumerate(left) if r.get("index") == selected]
        if len(matches) != 1:
            raise ValueError("Selected object has no unique exported client index")
        position = matches[0]
        candidates = ([position] if ordered else
                      [i for i, key in enumerate(right_keys) if key == left_keys[position]])
        if len(candidates) != 1:
            raise ValueError("Selected object has no unambiguous shadow identity")
        index = right[candidates[0]].get("index")
        if type(index) is not int or index in result:
            raise ValueError("Shadow object index is missing or selected twice")
        result.append(index)
    return result


def normalize_contract_telemetry(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize legacy boundary flags once, without inferring game legality."""
    result = dict(value)
    if result.pop("reanchor_after", False):
        result["requires_reanchor"] = True
    if result.get("requires_reanchor"):
        result.setdefault("reanchor_boundary", "map")
    return result


def register_action_pair(client_action: str, shadow_action: str) -> None:
    """Extend registered transports for exported game capabilities, before use.

    A pair does not assert object identity or legality in any specific room.
    Handlers still map the actual exported objects and selection constraints.
    """
    if client_action not in CLIENT_ACTIONS or shadow_action not in SHADOW_ACTIONS:
        raise ValueError("Register both endpoint action definitions before their mapping")
    CLIENT_SHADOW_ACTIONS[client_action] = CLIENT_SHADOW_ACTIONS.get(
        client_action, frozenset()
    ) | {shadow_action}


def validate_transaction(transaction: ActionTransaction) -> None:
    """One contract validator for construction, persistence, execution and replay."""
    if transaction.client is None and transaction.shadow is None:
        raise ValueError("Transaction has neither a client nor a shadow command")
    for command, registry in ((transaction.client, CLIENT_ACTIONS),
                              (transaction.shadow, SHADOW_ACTIONS)):
        if command is not None:
            if command.action not in registry or not isinstance(command.params, dict):
                raise ValueError("Unregistered transaction action or invalid params")
            validate_command_params(command, side='client' if registry is CLIENT_ACTIONS else 'shadow')
    validate_action_mapping(transaction.client, transaction.shadow)
    definition = (CLIENT_ACTIONS[transaction.client.action] if transaction.client
                  else SHADOW_ACTIONS[transaction.shadow.action])
    if transaction.family != definition.family:
        raise ValueError("Transaction family mismatch")
    if not transaction.intent or not transaction.completion:
        raise ValueError("Transaction intent and completion rule are required")
    telemetry = normalize_contract_telemetry(transaction.telemetry)
    if telemetry.get("requires_reanchor"):
        if telemetry["reanchor_boundary"] not in CLIENT_AUTHORITATIVE_BOUNDARIES:
            raise ValueError("Unsupported client-authoritative boundary")
        if transaction.shadow is None and not telemetry.get("client_authoritative"):
            raise ValueError("A client-only transaction requiring reanchor must be explicitly client-authoritative")
    if transaction.shadow is None and transaction.recovery == RecoveryPolicy.REPLAY_SHADOW_IF_COMPLETED:
        raise ValueError("Client-only transaction cannot replay a shadow command")


def validate_command_params(command: ActionCommand, *, side: str) -> None:
    """Structural constraints only; instance/target legality uses endpoint states."""
    action, params = command.action, command.params
    required = {}
    if side == 'client':
        indexed = {'choose_map_node', 'choose_event_option', 'choose_rest_option',
                   'claim_reward', 'choose_reward_card', 'select_deck_card',
                   'choose_bundle', 'choose_treasure_relic', 'choose_capstone_option',
                   'choose_timeline_epoch', 'use_potion', 'discard_potion',
                   'buy_card', 'buy_relic', 'buy_potion', 'choose_potion_slot', 'replace_potion'}
        if action in indexed:
            required['option_index'] = 0
        if action == 'play_card':
            required['card_index'] = 0
    else:
        keys = {'play_card': 'card_index', 'use_potion': 'potion_index',
                'discard_potion': 'potion_index', 'choose_option': 'option_index',
                'select_card_reward': 'card_index', 'claim_combat_reward': 'reward_index',
                'select_bundle': 'bundle_index', 'choose_treasure_relic': 'relic_index',
                'buy_card': 'card_index', 'buy_relic': 'relic_index', 'buy_potion': 'potion_index'}
        if action in keys:
            required[keys[action]] = 0
        if action == 'ack_event_reward':
            required.update(reward_index=0, reward_set_id=0)
        if action == 'select_map_node':
            required.update(row=0, col=0)
    for key, minimum in required.items():
        if type(params.get(key)) is not int or params[key] < minimum:
            raise ValueError(f'{side} {action} requires nonnegative integer {key}')
    if action == 'crystal_sphere_divine':
        for key in ('x', 'y'):
            if type(params.get(key)) is not int or params[key] < 0:
                raise ValueError(f'{side} {action} requires nonnegative integer {key}')
        if params.get('tool') not in {'small', 'big'}:
            raise ValueError(f'{side} {action} requires small or big tool')
        if side == 'client' and (type(params.get('expected_remaining')) is not int
                                 or params['expected_remaining'] <= 0):
            raise ValueError('Crystal Sphere client action requires expected_remaining')
    for key in {'target_index', 'reward_set_id'} & params.keys():
        if params[key] is not None and (type(params[key]) is not int or params[key] < 0):
            raise ValueError(f'{side} {action} has invalid {key}')
    if action == 'select_map_node' and any(params[k] > 255 for k in ('row', 'col')):
        raise ValueError('Map coordinates exceed the native byte range')
    if action in {'select_deck_cards', 'select_cards'}:
        value = params.get('indices')
        if side == 'shadow' and isinstance(value, str):
            try:
                value = [int(part) for part in value.split(',')] if value else []
            except ValueError:
                value = None
        if (not isinstance(value, list) or (not value and side == 'client')
                or any(type(index) is not int or index < 0 for index in value)
                or len(set(value)) != len(value)):
            raise ValueError(f'{side} {action} requires distinct nonnegative indices')
        if 'option_index' in params:
            raise ValueError('Selection cannot carry both indices and option_index')


def _command_from_dict(value: Any, registry: Mapping[str, ActionDefinition], side: str) -> ActionCommand | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"Transaction {side} command must be an object")
    action = value.get("action")
    params = value.get("params", {})
    if not isinstance(action, str) or action not in registry:
        raise ValueError(f"Unregistered transaction {side} action: {action!r}")
    if not isinstance(params, dict):
        raise ValueError(f"Transaction {side} params must be an object")
    return ActionCommand(action, dict(params))


def transaction_from_dict(value: Any) -> ActionTransaction:
    """Load and validate a persisted transaction before execution or replay."""

    if not isinstance(value, dict):
        raise ValueError("Transaction must be an object")
    client = _command_from_dict(value.get("client"), CLIENT_ACTIONS, "client")
    shadow = _command_from_dict(value.get("shadow"), SHADOW_ACTIONS, "shadow")
    if client is None and shadow is None:
        raise ValueError("Transaction has neither a client nor a shadow command")
    try:
        family = ActionFamily(value.get("family"))
        recovery = RecoveryPolicy(value.get("recovery"))
    except ValueError as exc:
        raise ValueError(f"Invalid transaction enum: {exc}") from exc
    intent = value.get("intent")
    completion = value.get("completion")
    telemetry = value.get("telemetry", {})
    operation = value.get("operation")
    atomic = value.get("atomic", True)
    if not isinstance(intent, str) or not intent:
        raise ValueError("Transaction intent is missing")
    if not isinstance(completion, str) or not completion:
        raise ValueError("Transaction completion rule is missing")
    if not isinstance(telemetry, dict):
        raise ValueError("Transaction telemetry must be an object")
    if telemetry.get("requires_reanchor"):
        boundary = telemetry.get("reanchor_boundary", "map")
        if boundary not in CLIENT_AUTHORITATIVE_BOUNDARIES:
            raise ValueError(f"Unsupported client-authoritative boundary: {boundary!r}")
    if operation is not None and not isinstance(operation, str):
        raise ValueError("Transaction operation must be a string or null")
    if not isinstance(atomic, bool):
        raise ValueError("Transaction atomic flag must be boolean")
    expected_definition = (
        CLIENT_ACTIONS[client.action] if client is not None
        else SHADOW_ACTIONS[shadow.action]
    )
    validate_action_mapping(client, shadow)
    if family != expected_definition.family:
        raise ValueError(
            f"Transaction family mismatch: recorded={family.value!r}, "
            f"expected={expected_definition.family.value!r}"
        )
    if shadow is None and recovery == RecoveryPolicy.REPLAY_SHADOW_IF_COMPLETED:
        raise ValueError("Client-only transaction cannot replay a shadow command")
    transaction = ActionTransaction(
        intent=intent,
        family=family,
        client=client,
        shadow=shadow,
        completion=completion,
        operation=operation,
        telemetry=normalize_contract_telemetry(telemetry),
        atomic=atomic,
        recovery=recovery,
    )
    recorded_mode = value.get("mirror_mode")
    if recorded_mode is not None and recorded_mode != transaction.mirror_mode.value:
        raise ValueError(
            f"Transaction mirror mode mismatch: recorded={recorded_mode!r}, "
            f"actual={transaction.mirror_mode.value!r}"
        )
    validate_transaction(transaction)
    return transaction


# Protocol actions observed in the current visible-client Mod. This is not a
# claim that every future STS2 screen has already been exported.
CLIENT_ACTIONS: dict[str, ActionDefinition] = {
    "open_character_select": ActionDefinition("session.open_character_select", ActionFamily.SESSION),
    "select_character": ActionDefinition("session.select_character", ActionFamily.SESSION),
    "increase_ascension": ActionDefinition("session.adjust_ascension", ActionFamily.SESSION),
    "decrease_ascension": ActionDefinition("session.adjust_ascension", ActionFamily.SESSION),
    "embark": ActionDefinition("session.embark", ActionFamily.SESSION),
    "abandon_run": ActionDefinition("session.abandon_run", ActionFamily.SESSION),
    "return_to_main_menu": ActionDefinition("session.return_to_main_menu", ActionFamily.SESSION),
    "play_card": ActionDefinition("combat.play_card", ActionFamily.COMBAT),
    "end_turn": ActionDefinition("combat.end_turn", ActionFamily.COMBAT),
    "use_potion": ActionDefinition("combat.use_potion", ActionFamily.COMBAT),
    "discard_potion": ActionDefinition("combat.discard_potion", ActionFamily.COMBAT),
    "choose_map_node": ActionDefinition("navigation.choose_map_node", ActionFamily.NAVIGATION),
    "proceed": ActionDefinition("navigation.proceed", ActionFamily.NAVIGATION),
    "choose_event_option": ActionDefinition("event.choose_option", ActionFamily.EVENT),
    "crystal_sphere_divine": ActionDefinition("event.crystal_sphere_divine", ActionFamily.EVENT),
    "choose_rest_option": ActionDefinition("rest.choose_option", ActionFamily.REST),
    "claim_reward": ActionDefinition("reward.claim_item", ActionFamily.REWARD),
    "choose_reward_card": ActionDefinition("reward.choose_card", ActionFamily.REWARD),
    "skip_reward_cards": ActionDefinition("reward.skip_cards", ActionFamily.REWARD),
    "resolve_rewards": ActionDefinition("reward.resolve", ActionFamily.REWARD),
    "collect_rewards_and_proceed": ActionDefinition("reward.finish", ActionFamily.REWARD),
    "open_chest": ActionDefinition("treasure.open_chest", ActionFamily.TREASURE),
    "choose_treasure_relic": ActionDefinition("treasure.choose_relic", ActionFamily.TREASURE),
    "choose_bundle": ActionDefinition("reward.choose_bundle", ActionFamily.REWARD),
    "confirm_bundle": ActionDefinition("reward.confirm_bundle", ActionFamily.REWARD),
    "choose_capstone_option": ActionDefinition("reward.choose_capstone", ActionFamily.REWARD),
    "open_shop_inventory": ActionDefinition("shop.open", ActionFamily.SHOP),
    "close_shop_inventory": ActionDefinition("shop.close", ActionFamily.SHOP),
    "buy_card": ActionDefinition("shop.buy_card", ActionFamily.SHOP),
    "buy_relic": ActionDefinition("shop.buy_relic", ActionFamily.SHOP),
    "buy_potion": ActionDefinition("shop.buy_potion", ActionFamily.SHOP),
    "remove_card": ActionDefinition("shop.remove_card", ActionFamily.SHOP),
    "remove_card_at_shop": ActionDefinition("shop.remove_card", ActionFamily.SHOP),
    "select_deck_card": ActionDefinition("selection.select_cards", ActionFamily.SELECTION),
    "select_deck_cards": ActionDefinition("selection.select_cards", ActionFamily.SELECTION),
    "confirm_selection": ActionDefinition("selection.confirm", ActionFamily.SELECTION),
    "cancel_selection": ActionDefinition("selection.cancel", ActionFamily.SELECTION),
    "choose_potion_slot": ActionDefinition("selection.replace_potion", ActionFamily.SELECTION),
    "replace_potion": ActionDefinition("selection.replace_potion", ActionFamily.SELECTION),
    "cancel_potion": ActionDefinition("selection.keep_potions", ActionFamily.SELECTION),
    "skip_reward": ActionDefinition("selection.skip_reward", ActionFamily.SELECTION),
    "confirm_modal": ActionDefinition("overlay.confirm_modal", ActionFamily.OVERLAY),
    "dismiss_modal": ActionDefinition("overlay.dismiss_modal", ActionFamily.OVERLAY),
    "close_cards_view": ActionDefinition("overlay.close_cards_view", ActionFamily.OVERLAY),
    "close_main_menu_submenu": ActionDefinition("overlay.close_menu", ActionFamily.OVERLAY),
    "choose_timeline_epoch": ActionDefinition("overlay.choose_timeline_epoch", ActionFamily.OVERLAY),
    "confirm_timeline_overlay": ActionDefinition("overlay.confirm_timeline", ActionFamily.OVERLAY),
}


SHADOW_ACTIONS: dict[str, ActionDefinition] = {
    "select_map_node": ActionDefinition("navigation.choose_map_node", ActionFamily.NAVIGATION),
    "play_card": ActionDefinition("combat.play_card", ActionFamily.COMBAT),
    "end_turn": ActionDefinition("combat.end_turn", ActionFamily.COMBAT),
    "use_potion": ActionDefinition("combat.use_potion", ActionFamily.COMBAT),
    "discard_potion": ActionDefinition("combat.discard_potion", ActionFamily.COMBAT),
    "open_chest": ActionDefinition("treasure.open_chest", ActionFamily.TREASURE),
    "choose_treasure_relic": ActionDefinition("treasure.choose_relic", ActionFamily.TREASURE),
    "choose_option": ActionDefinition("run.choose_option", ActionFamily.EVENT),
    "crystal_sphere_divine": ActionDefinition("event.crystal_sphere_divine", ActionFamily.EVENT),
    "reconcile_relics": ActionDefinition("event.reconcile_relics", ActionFamily.EVENT),
    "select_card_reward": ActionDefinition("reward.choose_card", ActionFamily.REWARD),
    "skip_card_reward": ActionDefinition("reward.skip_cards", ActionFamily.REWARD),
    "claim_combat_reward": ActionDefinition("reward.claim_item", ActionFamily.REWARD),
    "ack_event_reward": ActionDefinition("reward.ack_event_item", ActionFamily.REWARD),
    "finish_combat_rewards": ActionDefinition("reward.finish", ActionFamily.REWARD),
    "buy_card": ActionDefinition("shop.buy_card", ActionFamily.SHOP),
    "buy_relic": ActionDefinition("shop.buy_relic", ActionFamily.SHOP),
    "buy_potion": ActionDefinition("shop.buy_potion", ActionFamily.SHOP),
    "remove_card": ActionDefinition("shop.remove_card", ActionFamily.SHOP),
    "select_bundle": ActionDefinition("reward.choose_bundle", ActionFamily.REWARD),
    "select_cards": ActionDefinition("selection.select_cards", ActionFamily.SELECTION),
    "skip_select": ActionDefinition("selection.cancel", ActionFamily.SELECTION),
    "leave_room": ActionDefinition("navigation.leave_room", ActionFamily.NAVIGATION),
    "proceed": ActionDefinition("navigation.proceed", ActionFamily.NAVIGATION),
}


def make_transaction(
    client_action: str | None,
    client_params: Mapping[str, Any] | None = None,
    *,
    shadow_action: str | None = None,
    shadow_args: Mapping[str, Any] | None = None,
    intent: str | None = None,
    family: ActionFamily | None = None,
    completion: str = "state_change",
    operation: str | None = None,
    telemetry: Mapping[str, Any] | None = None,
    atomic: bool | None = None,
) -> ActionTransaction:
    client_definition = CLIENT_ACTIONS.get(client_action or "")
    shadow_definition = SHADOW_ACTIONS.get(shadow_action or "")
    definition = client_definition or shadow_definition
    if definition is None:
        raise ValueError(
            f"Unregistered transaction action: client={client_action!r}, shadow={shadow_action!r}"
        )
    recovery = (
        RecoveryPolicy.REPLAY_SHADOW_IF_COMPLETED
        if shadow_action is not None
        else RecoveryPolicy.REOBSERVE_CLIENT
    )
    client_command = (
        ActionCommand(
            client_action,
            dict(client_params or {}),
            operation=operation,
            telemetry=dict(telemetry or {}),
        )
        if client_action
        else None
    )
    shadow_command = ActionCommand(shadow_action, dict(shadow_args or {})) if shadow_action else None
    validate_action_mapping(client_command, shadow_command)
    normalized_telemetry = normalize_contract_telemetry(telemetry or {})
    selected_family = family or definition.family
    if selected_family != definition.family:
        raise ValueError(
            f"Transaction family mismatch: requested={selected_family.value!r}, "
            f"expected={definition.family.value!r}"
        )
    transaction = ActionTransaction(
        intent=intent or definition.intent,
        family=selected_family,
        client=client_command,
        shadow=shadow_command,
        completion=completion,
        operation=operation,
        telemetry=normalized_telemetry,
        atomic=definition.atomic if atomic is None else atomic,
        recovery=recovery,
    )
    validate_transaction(transaction)
    return transaction


def make_mirrored_transaction(
    client_action: str,
    client_params: Mapping[str, Any] | None = None,
    *,
    shadow_action: str,
    shadow_args: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> ActionTransaction:
    """Canonical constructor for a client/shadow pair.

    Keeping this small wrapper separate makes mirrored intent visible at call
    sites while retaining one implementation and one validation path.
    """
    return make_transaction(
        client_action,
        client_params,
        shadow_action=shadow_action,
        shadow_args=shadow_args,
        **kwargs,
    )
