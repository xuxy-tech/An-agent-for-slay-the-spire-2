from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import re
from typing import Any


class InteractionKind(str, Enum):
    MAP = 'map_choice'
    COMBAT = 'combat_action'
    SELECT = 'card_selection'
    CONFIRM = 'selection_confirm'
    REWARD = 'reward_overview'
    REWARD_CARD = 'reward_card'
    POTION = 'potion_replacement'
    SHOP_ROOM = 'shop_room'
    SHOP_INVENTORY = 'shop_inventory'
    REST = 'rest_choice'
    EVENT = 'event_choice'
    CRYSTAL_SPHERE = 'crystal_sphere'
    CHEST = 'chest_open'
    RELIC = 'treasure_relic'
    BUNDLE = 'bundle_choice'
    BUNDLE_CONFIRM = 'bundle_confirm'
    CAPSTONE = 'capstone_choice'
    PROCEED = 'proceed'
    MODAL = 'modal'
    INSPECT = 'cards_view'
    MENU = 'main_menu'
    CHARACTER = 'character_select'
    TIMELINE = 'timeline'
    TERMINAL = 'game_over'
    WAIT = 'transition'
    UNSUPPORTED = 'unsupported'


def interaction_at_boundary(kind: InteractionKind, boundary: str) -> bool:
    """Resolve contract boundaries explicitly; UI kind names are not boundaries."""
    boundaries = {'map': InteractionKind.MAP}
    if boundary not in boundaries:
        raise ValueError(f'Unsupported recovery boundary: {boundary!r}')
    return kind == boundaries[boundary]


@dataclass(frozen=True)
class SelectionSpec:
    operation: str | None
    source: str
    minimum: int | None
    maximum: int | None
    selected: tuple[int, ...] | None
    count_source: str
    requires_confirmation: bool
    can_confirm: bool
    scope: str


@dataclass(frozen=True)
class InteractionState:
    scene: str
    kind: InteractionKind
    stage: str
    actions: tuple[str, ...]
    reason: str = ''
    selection: SelectionSpec | None = None

    def to_dict(self):
        value = asdict(self)
        value['kind'] = self.kind.value
        return value


@dataclass
class FlowContext:
    scene: str = ''
    location: tuple = ()
    operation: str | None = None
    selection_source: str = ''
    shop_stage: str = 'arrived'
    shop_reanchor_required: bool = False
    reanchor_required: bool = False
    selected_bundle: int | None = None
    reward_card_resolved: bool = False
    resolved_reward_items: list[str] = field(default_factory=list)
    active_reward_item: str | None = None
    reward_reanchor_generation: int = 0
    reward_occurrence: int = 0
    reward_set_mapping: dict | None = None
    pending_reward_finish: dict | None = None
    pending_event_option_id: str | None = None
    pending_event_shadow_index: int | None = None
    authorized_modal: bool = False
    stack: list[str] = field(default_factory=list)

    def observe(self, state):
        run = state.get('run') or {}
        location = (state.get('run_id'), run.get('act_id'), run.get('floor'))
        screen = state.get('screen') or 'UNKNOWN'
        if not self.scene:
            self.scene = ('COMBAT' if state.get('in_combat') else 'REWARD') if screen == 'CARD_SELECTION' else screen
        if location != self.location:
            self.reward_set_mapping = None
            self.pending_reward_finish = None
            self.resolved_reward_items.clear()
            self.active_reward_item = None
            self.shop_stage = 'arrived'
            self.reward_card_resolved = False
            self.pending_event_option_id = None
            self.pending_event_shadow_index = None
            self.location = location
        overlays = {'CARD_SELECTION', 'MODAL', 'CARDS_VIEW', 'BUNDLE_SELECTION'}
        if screen not in overlays:
            if screen != self.scene:
                if self.scene == 'REWARD' and screen != 'REWARD':
                    self.reward_set_mapping = None
                    self.resolved_reward_items.clear()
                    self.active_reward_item = None
                self.operation = None
                self.selection_source = ''
                self.stack.clear()
                if screen == 'REWARD':
                    self.reward_card_resolved = False
            self.scene = screen
        underlying = (state.get('modal') or {}).get('underlying_screen')
        self.stack = [self.scene]
        if underlying and underlying not in {self.scene, screen}:
            self.stack.append(underlying)
        if screen != self.scene:
            self.stack.append(screen)

    def completed(self, decision, before, after):
        command = getattr(decision, 'client', None) or decision
        action = command.action
        telemetry = getattr(decision, 'telemetry', {}) or {}
        if telemetry.get('requires_reanchor'):
            self.reanchor_required = True
        if action == 'open_shop_inventory':
            self.shop_stage = 'opened'
        elif action == 'close_shop_inventory':
            self.shop_stage = 'leaving'
        elif action in {'buy_card', 'remove_card', 'remove_card_at_shop'}:
            if telemetry.get('requires_reanchor'):
                self.shop_reanchor_required = True
        elif action in {'choose_reward_card', 'skip_reward_cards', 'resolve_rewards'}:
            self.reward_card_resolved = True
            if self.active_reward_item is not None:
                self.resolved_reward_items.append(self.active_reward_item)
                self.active_reward_item = None
        elif action == 'claim_reward':
            self.active_reward_item = telemetry.get('reward_item_key')
        elif action == 'choose_event_option':
            if telemetry.get('policy') == 'explicit_proceed':
                if telemetry.get('reward_finalization'):
                    self.pending_reward_finish = None
                self.pending_event_option_id = None
                self.pending_event_shadow_index = None
            else:
                self.pending_event_option_id = telemetry.get('option_id')
                shadow = getattr(decision, 'shadow', None)
                shadow_index = (shadow.params or {}).get('option_index') if shadow else None
                self.pending_event_shadow_index = shadow_index if type(shadow_index) is int else None
        elif action == 'choose_bundle':
            self.selected_bundle = command.params.get('option_index')
        if getattr(decision, 'operation', None):
            self.operation = decision.operation
            self.selection_source = action


_SELECTION_OPERATIONS = {
    'deck_upgrade_select': 'upgrade', 'combat_hand_upgrade_select': 'upgrade',
    'deck_remove_select': 'remove', 'deck_transform_select': 'transform',
    'deck_copy_select': 'copy', 'deck_enchant_select': 'enchant',
    'choose_card_select': 'pick', 'reward_card_select': 'pick',
}


def explicit_selection_count(selection):
    prompt = re.sub(r'\[[^\]]*\]', '', str(selection.get('prompt') or ''))
    match = re.search(r'选择\s*(\d+|一)\s*张[\u4e00-\u9fff]{0,8}牌', prompt)
    if match is None:
        match = re.search(r'(?:select|choose|transform|remove|upgrade)\s+(\d+|a|one)\s+cards?\b', prompt, re.I)
    if match is None:
        return None
    token = match.group(1).lower()
    count = 1 if token in {'一', 'a', 'one'} else int(token)
    return count if 1 <= count <= len(selection.get('cards') or []) else None


def _selection_spec(state, context, shadow):
    selection = state.get('selection') or {}
    metadata = state.get('interaction') or (state.get('agent_view') or {}).get('interaction') or {}
    native = metadata.get('selection') or {}
    kind = str(selection.get('kind') or '')
    operation = native.get('operation') or _SELECTION_OPERATIONS.get(kind) or context.operation
    if operation is None and kind and selection.get('cards'):
        # Unknown selectors still expose a concrete visible card list. Treat the
        # operation as a generic pick instead of blocking on an incomplete Mod
        # taxonomy; legality continues to come from the visible client.
        operation = 'pick'
    low, high = native.get('minimum', selection.get('min_select')), native.get('maximum', selection.get('max_select'))
    source = 'mod'
    if not high or high < 1:
        count = explicit_selection_count(selection)
        if count is not None:
            low = high = count
            source = 'explicit_prompt'
    if (not high or high < 1) and shadow.get('decision') == 'card_select':
        low, high = shadow.get('min_select'), shadow.get('max_select')
        source = 'headless_selection'
    selected = native.get('selected_indices')
    if selected is None and selection.get('selected_count') == 0:
        selected = []
    return SelectionSpec(operation, kind, low if type(low) is int else None,
                         high if type(high) is int and high > 0 else None,
                         tuple(selected) if isinstance(selected, list) else None, source,
                         bool(native.get('requires_confirmation', selection.get('requires_confirmation'))),
                         bool(native.get('can_confirm', selection.get('can_confirm'))),
                         'combat' if state.get('in_combat') else 'run')


def classify_interaction(state: dict[str, Any], context: FlowContext | None = None,
                         shadow: dict[str, Any] | None = None) -> InteractionState:
    context = context or FlowContext()
    shadow = shadow or {}
    screen = str(state.get('screen') or 'UNKNOWN')
    actions = tuple(state.get('available_actions') or [])
    action_set = set(actions)
    scene = context.scene or screen

    def result(kind, stage='ready', reason='', selection=None):
        return InteractionState(scene if screen in {'MODAL', 'CARDS_VIEW', 'CARD_SELECTION'} else screen,
                                kind, stage, actions, reason, selection)

    # Modal decisions take precedence over their underlying room.
    if screen == 'MODAL':
        return result(InteractionKind.MODAL)
    if screen == 'CARDS_VIEW':
        if 'close_cards_view' in action_set:
            return result(InteractionKind.INSPECT)
        return result(InteractionKind.WAIT, 'waiting',
                      'Waiting for cards view controls to settle')
    if screen == 'GAME_OVER':
        return result(InteractionKind.TERMINAL, 'terminal')
    if screen == 'CRYSTAL_SPHERE':
        sphere = state.get('crystal_sphere') or {}
        if sphere.get('phase') == 'divining' and 'crystal_sphere_divine' in action_set:
            return result(InteractionKind.CRYSTAL_SPHERE)
        if sphere.get('phase') == 'proceed' and 'proceed' in action_set:
            return result(InteractionKind.PROCEED)
        return result(InteractionKind.WAIT, 'waiting', 'Waiting for Crystal Sphere controls')
    if screen == 'MULTIPLAYER_LOBBY' or (state.get('session') or {}).get('mode') not in {None, 'singleplayer'}:
        return result(InteractionKind.UNSUPPORTED, 'blocked', 'Only singleplayer automation is enabled')
    reward = state.get('reward') or {}
    if reward.get('pending_card_choice') is True:
        card_options = reward.get('card_options') or []
        can_skip_empty = (
            'skip_reward_cards' in action_set
            and reward.get('can_skip') is not False
        )
        ready = bool(card_options) or can_skip_empty
        return result(InteractionKind.REWARD_CARD, 'ready' if ready else 'waiting',
                      '' if ready else 'Waiting for reward cards')
    if screen == 'CARD_SELECTION':
        spec = _selection_spec(state, context, shadow)
        if 'confirm_selection' in action_set and spec.can_confirm:
            return result(InteractionKind.CONFIRM, selection=spec)
        if 'replace_potion' in action_set or 'choose_potion_slot' in action_set:
            return result(InteractionKind.POTION, selection=spec)
        if not (state.get('selection') or {}).get('cards'):
            if action_set.intersection({'cancel_selection', 'skip_reward'}):
                return result(InteractionKind.SELECT, selection=spec)
            if 'proceed' in action_set:
                return result(InteractionKind.PROCEED)
            return result(InteractionKind.SELECT, 'waiting', 'Waiting for selectable cards', spec)
        if spec.operation is None or spec.maximum is None:
            return result(InteractionKind.SELECT, 'blocked', 'Selection purpose or count is unavailable', spec)
        return result(InteractionKind.SELECT, selection=spec)
    if screen == 'SHOP':
        inventory = (state.get('shop') or {}).get('is_open') is True or 'close_shop_inventory' in action_set
        return result(InteractionKind.SHOP_INVENTORY if inventory else InteractionKind.SHOP_ROOM)
    if screen == 'BUNDLE_SELECTION':
        if 'confirm_bundle' in action_set:
            return result(InteractionKind.BUNDLE_CONFIRM)
        if 'proceed' in action_set:
            return result(InteractionKind.PROCEED)
        if 'choose_bundle' in action_set and ('bundles' not in state or state.get('bundles')):
            return result(InteractionKind.BUNDLE)
        return result(InteractionKind.WAIT, 'waiting', 'Waiting for bundle options')
    if screen == 'CAPSTONE_SELECTION':
        return result(InteractionKind.CAPSTONE)
    if screen == 'MAIN_MENU':
        return result(InteractionKind.TIMELINE if action_set.intersection({'choose_timeline_epoch', 'confirm_timeline_overlay'}) else InteractionKind.MENU)
    if screen == 'CHARACTER_SELECT':
        return result(InteractionKind.CHARACTER)
    if screen == 'REWARD':
        if action_set.intersection({'claim_reward', 'proceed', 'collect_rewards_and_proceed'}):
            return result(InteractionKind.REWARD)
        return result(InteractionKind.WAIT, 'waiting', 'Waiting for reward actions')
    if screen == 'CHEST':
        if 'open_chest' in action_set:
            return result(InteractionKind.CHEST)
        if 'choose_treasure_relic' in action_set:
            chest = state.get('chest') or {}
            if 'relic_options' not in chest or chest.get('relic_options'):
                return result(InteractionKind.RELIC)
            return result(InteractionKind.WAIT, 'waiting', 'Waiting for treasure relic options')
        if 'proceed' in action_set:
            return result(InteractionKind.PROCEED)
        # An empty chest can briefly expose only global actions such as
        # discard_potion while the room's terminal transition settles. Do not
        # synthesize a proceed click against an unavailable client action.
        meaningful = action_set - {'discard_potion', 'use_potion'}
        if not meaningful:
            return result(InteractionKind.WAIT, 'waiting', 'Waiting for empty treasure chest to settle')
        return result(InteractionKind.UNSUPPORTED, 'blocked', 'Treasure chest has no recognized action boundary')
    if screen == 'MAP' and 'choose_map_node' in action_set:
        map_state = state.get('map') or {}
        if 'available_nodes' not in map_state or map_state.get('available_nodes'):
            return result(InteractionKind.MAP)
        return result(InteractionKind.WAIT, 'waiting', 'Waiting for map nodes')
    if screen == 'COMBAT' and state.get('in_combat') and action_set.intersection({'play_card', 'end_turn'}):
        return result(InteractionKind.COMBAT)
    if screen == 'EVENT' and 'choose_event_option' in action_set:
        event = state.get('event') or {}
        options = event.get('options') or []
        if 'options' not in event or any(
                not row.get('is_locked') and type(row.get('index')) is int for row in options):
            return result(InteractionKind.EVENT)
        return result(InteractionKind.WAIT, 'waiting', 'Waiting for event options')
    if screen == 'REST':
        if 'choose_rest_option' in action_set:
            rest = state.get('rest') or {}
            options = rest.get('options') or []
            if 'options' not in rest or any(
                    row.get('is_enabled') and type(row.get('index')) is int for row in options):
                return result(InteractionKind.REST)
            return result(InteractionKind.WAIT, 'waiting', 'Waiting for rest options')
        return result(InteractionKind.PROCEED)
    if 'proceed' in action_set:
        return result(InteractionKind.PROCEED)
    meaningful = action_set - {'discard_potion', 'use_potion'}
    if not meaningful:
        return result(InteractionKind.WAIT, 'waiting', 'Waiting for the next input boundary')
    return result(InteractionKind.UNSUPPORTED, 'blocked', f'Unrecognized input boundary: {screen}')
