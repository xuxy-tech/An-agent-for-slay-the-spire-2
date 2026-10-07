from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from controller.action_transaction import ActionTransaction, make_mirrored_transaction, make_transaction, map_object_indices
from controller.deck_profile import card_id_from_row, choose_profile_bundle, load_deck_profile
from controller.crystal_sphere import board_identity, choose_divination
from controller.interaction_state import FlowContext, InteractionKind as K, classify_interaction
from controller.live_noncombat import ClientDecision, choose_client_event, choose_client_rest, choose_client_selection
from controller.run_agent import (
    choose_card_reward,
    choose_forced_card_reward,
    choose_map_node,
    choose_shop_action,
    score_card_reward_options,
)
from controller.route_strategy import choose_weighted_route


class FlowBlocked(RuntimeError):
    pass


FlowDecision = ActionTransaction


def decision(action, params=None, policy=None, operation=None, shadow=None, completion='state_change', **telemetry):
    kwargs = dict(
        completion=completion,
        operation=operation,
        telemetry={'policy': policy or action, **telemetry},
    )
    if shadow:
        return make_mirrored_transaction(
            action, params or {}, shadow_action=shadow[0], shadow_args=shadow[1], **kwargs
        )
    return make_transaction(action, params or {}, **kwargs)


def from_client_decision(
    chosen: ClientDecision,
    *,
    shadow: tuple[str, dict] | None = None,
    completion: str = 'state_change',
    **telemetry,
) -> ActionTransaction:
    combined_telemetry = {**chosen.telemetry, **telemetry}
    kwargs = dict(completion=completion, operation=chosen.operation, telemetry=combined_telemetry)
    if shadow:
        return make_mirrored_transaction(
            chosen.action, chosen.params,
            shadow_action=shadow[0], shadow_args=shadow[1], **kwargs
        )
    return make_transaction(chosen.action, chosen.params, **kwargs)


def client_authoritative(
    command: ActionTransaction,
    *,
    reason: str,
    requires_reanchor: bool,
) -> ActionTransaction:
    """Keep a legal visible action when the shadow has no matching UI step."""

    if command.client is None:
        raise ValueError('Client-authoritative transaction requires a client command')
    return make_transaction(
        command.client.action,
        command.client.params,
        completion=command.completion,
        operation=command.operation,
        telemetry={
            **command.telemetry,
            'client_authoritative': True,
            'client_authoritative_reason': reason,
            'requires_reanchor': requires_reanchor,
        },
        atomic=command.atomic,
    )


def semantic_fingerprint(state):
    # Ignore transport counters and duplicate text projections.
    value = {key: item for key, item in state.items() if key not in {'state_version', 'agent_view'}}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _visible_act(state: dict[str, Any]) -> int | None:
    act_id = (state.get('run') or {}).get('act_id')
    try:
        return int(act_id) + 1 if act_id is not None else None
    except (TypeError, ValueError):
        return None


def client_shop_decision_state(state: dict[str, Any]) -> dict[str, Any]:
    """Project the visible shop into the policy shape used by choose_shop_action."""

    run = state.get('run') or {}
    shop = state.get('shop') or {}
    removal = shop.get('card_removal') or {}
    cards = []
    for position, visible in enumerate(shop.get('cards') or []):
        card = dict(visible)
        card['id'] = card_id_from_row(visible)
        card['index'] = visible.get('index', position)
        card['cost'] = visible.get('price')
        cards.append(card)
    return {
        'decision': 'shop',
        'run': {'act_id': run.get('act_id')},
        'player': {
            'gold': run.get('gold', 0),
            'deck': list(run.get('deck') or []),
        },
        'cards': cards,
        'card_removal_cost': removal.get('price'),
        'card_removal_available': bool(
            removal.get('available')
            and not removal.get('used')
            and removal.get('enough_gold')
        ),
    }


def client_reward_card_decision_state(state: dict[str, Any]) -> dict[str, Any]:
    """Project visible card rewards into the canonical reward policy shape."""

    run = state.get('run') or {}
    reward = state.get('reward') or {}
    cards = []
    for position, visible in enumerate(reward.get('card_options') or []):
        card = dict(visible)
        card['id'] = card_id_from_row(visible)
        card['index'] = visible.get('index', position)
        cards.append(card)
    return {
        'decision': 'card_reward',
        'run': {'act_id': run.get('act_id')},
        'player': {'deck': list(run.get('deck') or [])},
        'cards': cards,
        'can_skip': reward.get('can_skip'),
    }


def _reward_kind(row):
    value = str((row or {}).get('reward_type') or (row or {}).get('type') or '')
    value = value.split('.')[-1]
    return value[:-6] if value.endswith('Reward') else value


def _native_reward_offer(rows):
    if not isinstance(rows, list):
        raise FlowBlocked('Complete ordered native reward offers are unavailable')
    for position, row in enumerate(rows):
        if not isinstance(row, dict) or type(row.get('native_index', row.get('index'))) is not int:
            raise FlowBlocked(f'Native reward item {position} has no stable index')
        # SpecialCard rewards can be direct claimable items without a card
        # selection payload. Validate candidate identity whenever the native
        # offer actually exposes candidates, but do not invent that contract
        # for a direct special reward.
        if _reward_kind(row) == 'Card' or (
                _reward_kind(row) == 'SpecialCard' and row.get('cards') is not None):
            cards = row.get('cards')
            if not isinstance(cards, list) or any(
                    not isinstance(card, dict) or not card_id_from_row(card)
                    or type(card.get('upgraded')) is not bool for card in cards):
                raise FlowBlocked(f'Native card reward {position} has incomplete candidate identity')
    return [[row.get('native_index', row.get('index')), _reward_kind(row),
             row.get('model_id'), row.get('amount'), copy.deepcopy(row.get('cards'))]
            for row in rows]


def _native_reward_flags(rows):
    if not isinstance(rows, list):
        raise FlowBlocked('Native reward selection flags are unavailable')
    if any(not isinstance(row, dict) or type(row.get('successfully_selected')) is not bool
           for row in rows):
        raise FlowBlocked('Native reward selection flags are incomplete')
    return [row.get('successfully_selected') for row in rows]


def _reward_card_identity(row):
    if not isinstance(row, dict) or type(row.get('upgraded')) is not bool:
        raise FlowBlocked('Reward card option has no explicit upgrade status')
    card_id = card_id_from_row(row)
    if not card_id:
        raise FlowBlocked('Reward card option has no card identity')
    return card_id, row['upgraded']


def shop_inventories_match(state: dict[str, Any], shadow: dict[str, Any]) -> bool:
    """Return whether visible and headless shops describe the same purchasable cards."""

    if shadow.get('decision') != 'shop':
        return False

    def signature(rows, price_key):
        return [
            (
                card_id_from_row(row) if row.get('is_stocked', True) else None,
                int(row.get(price_key) if row.get(price_key) is not None else -1)
                if row.get('is_stocked', True) else None,
                bool(row.get('is_stocked', True)),
            )
            for row in rows or []
        ]

    shop = state.get('shop') or {}
    return signature(shop.get('cards'), 'price') == signature(shadow.get('cards'), 'cost')


class ProgressGuard:
    def __init__(self, timeout_s=30.0, max_visits=8):
        self.timeout_s = timeout_s
        self.max_visits = max_visits
        self.visits = {}
        self.wait_started = None

    def waiting(self, interaction):
        if self.wait_started is None:
            self.wait_started = time.monotonic()
        if time.monotonic() - self.wait_started >= self.timeout_s:
            raise FlowBlocked(f'Transition timeout: {interaction.reason or interaction.kind.value}')

    def before(self, state, command):
        self.wait_started = None
        key = (semantic_fingerprint(state), command.client.action, json.dumps(command.client.params, sort_keys=True))
        self.visits[key] = self.visits.get(key, 0) + 1
        if self.visits[key] > self.max_visits:
            raise FlowBlocked(f'Repeated state/action loop: {command.client.action}')


class InteractionFlow:
    """Scene handlers choose a UI transaction; strategy implementations stay external."""

    def __init__(self, repo_root: Path, context: FlowContext | None = None,
                 deck_profile: dict | None = None):
        self.repo_root = repo_root
        self.context = context or FlowContext()
        self.deck_profile = deck_profile
        self.handlers = {
            K.MAP: self.map, K.REWARD: self.rewards, K.REWARD_CARD: self.reward_card,
            K.SHOP_ROOM: self.shop_room, K.SHOP_INVENTORY: self.shop_inventory,
            K.EVENT: self.event, K.REST: self.rest, K.SELECT: self.selection,
            K.CRYSTAL_SPHERE: self.crystal_sphere,
            K.CONFIRM: self.confirm, K.CHEST: self.chest, K.RELIC: self.relic,
            K.BUNDLE: self.bundle, K.BUNDLE_CONFIRM: self.bundle_confirm,
            K.CAPSTONE: self.capstone, K.PROCEED: self.proceed,
            K.MODAL: self.modal, K.INSPECT: self.inspect,
            K.MENU: self.menu, K.CHARACTER: self.character, K.TIMELINE: self.timeline,
            K.POTION: self.potion, K.TERMINAL: self.terminal,
        }

    def classify(self, state, shadow=None):
        self.context.observe(state)
        return classify_interaction(state, self.context, shadow)

    def invalidate_reward_mapping(self):
        self.context.reward_reanchor_generation += 1
        self.context.reward_set_mapping = None
        self.context.resolved_reward_items.clear()
        self.context.active_reward_item = None

    def crystal_sphere(self, state, shadow, current):
        if shadow.get('decision') != 'crystal_sphere':
            raise FlowBlocked('Crystal Sphere has no matching headless decision')
        visible = state.get('crystal_sphere') or {}
        try:
            if board_identity(visible) != board_identity(shadow):
                raise FlowBlocked('Crystal Sphere board differs before divination')
            params = choose_divination(visible)
        except ValueError as exc:
            raise FlowBlocked(str(exc)) from exc
        return decision(
            'crystal_sphere_divine', params, policy='visible_sphere_coverage',
            shadow=('crystal_sphere_divine',
                    {key: params[key] for key in ('x', 'y', 'tool')}),
            completion='decision_boundary',
        )

    @staticmethod
    def verify_crystal_sphere_transition(command, client_before, shadow_before,
                                         client_after, shadow_after):
        relevant = (client_before.get('screen') == 'CRYSTAL_SPHERE'
                    or client_after.get('screen') == 'CRYSTAL_SPHERE'
                    or shadow_before.get('decision') == 'crystal_sphere'
                    or shadow_after.get('decision') == 'crystal_sphere')
        if not relevant:
            return
        client_active = client_after.get('screen') == 'CRYSTAL_SPHERE'
        shadow_active = shadow_after.get('decision') == 'crystal_sphere'
        if client_active != shadow_active:
            # The native screen has a separate proceed click after the last
            # divination; headless settles that click with the final reveal.
            native_done = ((client_after.get('crystal_sphere') or {}).get('remaining') == 0
                           and (client_after.get('crystal_sphere') or {}).get('phase')
                           in {'proceed', 'settling'})
            if not (client_active and native_done and not shadow_active):
                raise FlowBlocked('Crystal Sphere endpoints reached different decisions')
        if client_active and shadow_active:
            try:
                if board_identity(client_after['crystal_sphere']) != board_identity(shadow_after):
                    raise FlowBlocked('Crystal Sphere board differs after action')
            except ValueError as exc:
                raise FlowBlocked(str(exc)) from exc

    def verify_native_reward_transition(self, command, client_before, shadow_before,
                                        client_after, shadow_after):
        mapping = self.context.reward_set_mapping
        action = command.client.action
        if mapping is None or command.shadow is None or action not in {
                'claim_reward', 'choose_reward_card', 'skip_reward_cards'}:
            return
        client_set_before = (client_before.get('reward') or {}).get('reward_set_id')
        shadow_set_before = shadow_before.get('reward_set_id')
        # The headless card-reward boundary can omit reward_set_id while it
        # still exposes the mapped card candidates.  A missing identifier is
        # acceptable only in that explicit intermediate state; an explicit
        # different identifier remains a hard protocol failure.
        shadow_set_omitted_in_card_boundary = (
            shadow_set_before is None
            and shadow_before.get('decision') == 'card_reward'
            and action in {'choose_reward_card', 'skip_reward_cards'})
        shadow_card_set_changed = (
            shadow_before.get('decision') == 'card_reward'
            and shadow_set_before is not None
            and shadow_set_before != mapping['shadow_set_id'])
        if shadow_card_set_changed:
            nested_set = mapping.get('card_set_id')
            if nested_set is None:
                mapping['card_set_id'] = shadow_set_before
            elif nested_set != shadow_set_before:
                raise FlowBlocked('Native card reward set changed during mapped reward action')
        client_set_omitted_in_card_boundary = (
            client_set_before is None
            and client_before.get('screen') == 'CARD_SELECTION'
            and action in {'choose_reward_card', 'skip_reward_cards'})
        if (client_set_before != mapping['client_set_id']
                and not client_set_omitted_in_card_boundary
                or (shadow_set_before != mapping['shadow_set_id']
                    and not shadow_set_omitted_in_card_boundary
                    and not shadow_card_set_changed)):
            raise FlowBlocked('Native reward set changed during mapped reward action')
        before_offer = _native_reward_offer((client_before.get('reward') or {}).get('offered_rewards'))
        if before_offer != _native_reward_offer(shadow_before.get('offered_rewards')):
            raise FlowBlocked('Native reward contents differ before mapped reward action')
        before_flags = _native_reward_flags((client_before.get('reward') or {}).get('offered_rewards'))
        if before_flags != _native_reward_flags(shadow_before.get('offered_rewards')):
            raise FlowBlocked('Native reward selection flags differ before mapped action')
        if action == 'claim_reward':
            native_index = command.telemetry.get('reward_native_index')
        else:
            active = self.context.active_reward_item
            if active is None:
                raise FlowBlocked('Reward card action has no active mapped reward item')
            try:
                native_index = int(active.rsplit(':', 1)[1])
            except (ValueError, IndexError) as exc:
                raise FlowBlocked('Reward card action has invalid active reward identity') from exc
        positions = [position for position, row in enumerate(before_offer) if row[0] == native_index]
        if len(positions) != 1:
            raise FlowBlocked('Mapped reward action has no unique native reward item')
        reward_position = positions[0]
        if action == 'choose_reward_card':
            if before_offer[reward_position][1] not in {'Card', 'SpecialCard'}:
                raise FlowBlocked('Selected card does not belong to an active card reward')
            visible_cards = (client_before.get('reward') or {}).get('card_options') or []
            shadow_cards = shadow_before.get('cards') or []
            client_index = command.client.params.get('option_index')
            shadow_index = command.shadow.params.get('card_index')
            visible_positions = [i for i, row in enumerate(visible_cards)
                                 if row.get('index') == client_index]
            shadow_positions = [i for i, row in enumerate(shadow_cards)
                                if row.get('index') == shadow_index]
            if len(visible_positions) != 1 or len(shadow_positions) != 1:
                raise FlowBlocked('Selected reward card has no unique option index')
            offered_cards = before_offer[reward_position][4]
            if not isinstance(offered_cards, list):
                raise FlowBlocked('Selected reward has no mapped card candidates')
            chosen = _reward_card_identity(visible_cards[visible_positions[0]])
            if chosen != _reward_card_identity(shadow_cards[shadow_positions[0]]):
                raise FlowBlocked('Selected reward card instance differs from the mapped offer')
            offered_positions = [i for i, row in enumerate(offered_cards)
                                 if _reward_card_identity(row) == chosen]
            if len(offered_positions) == 1:
                card_position = offered_positions[0]
            elif (visible_positions[0] == shadow_positions[0]
                  and visible_positions[0] in offered_positions):
                # Duplicate IDs need a concrete option position; removing by
                # name would allow the other copy to disappear instead.
                card_position = visible_positions[0]
            else:
                raise FlowBlocked('Selected duplicate reward card has no shared candidate position')
            expected_remaining_cards = offered_cards[:card_position] + offered_cards[card_position + 1:]
            decks = (
                (client_before.get('run') or {}).get('deck'),
                (client_after.get('run') or {}).get('deck'),
                (shadow_before.get('player') or {}).get('deck'),
                (shadow_after.get('player') or {}).get('deck'),
            )
            if not all(isinstance(deck, list) for deck in decks):
                raise FlowBlocked('Claimed card reward has incomplete deck observations')
            client_relics = (client_before.get('run') or {}).get('relics') or []
            shadow_relics = (shadow_before.get('player') or {}).get('relics') or []
            client_bing_bong = any(row.get('relic_id') == 'BING_BONG' for row in client_relics)
            shadow_bing_bong = any(row.get('id', '').split('.')[-1] == 'BING_BONG'
                                   for row in shadow_relics)
            if client_bing_bong != shadow_bing_bong:
                raise FlowBlocked('Reward copy relic differs between client and shadow')
            added_count = 2 if client_bing_bong else 1
            for earlier, later in ((decks[0], decks[1]), (decks[2], decks[3])):
                if (len(later) != len(earlier) + added_count
                        or [_reward_card_identity(row) for row in earlier]
                        != [_reward_card_identity(row) for row in later[:-added_count]]
                        or any(_reward_card_identity(row) != chosen
                               for row in later[-added_count:])):
                    raise FlowBlocked('Claimed reward card was not added to the deck')
        elif action == 'skip_reward_cards':
            if before_offer[reward_position][1] not in {'Card', 'SpecialCard'}:
                raise FlowBlocked('Skipped reward is not an active card reward')
            if (client_before.get('screen') != 'CARD_SELECTION'
                    or client_after.get('screen') != 'REWARD'
                    or (client_after.get('reward') or {}).get('pending_card_choice') is not False
                    or shadow_before.get('decision') != 'card_reward'
                    or shadow_after.get('decision') != 'combat_reward'):
                raise FlowBlocked('Skipped card reward did not close both selection views')
            client_deck_before = (client_before.get('run') or {}).get('deck')
            client_deck_after = (client_after.get('run') or {}).get('deck')
            shadow_deck_before = (shadow_before.get('player') or {}).get('deck')
            shadow_deck_after = (shadow_after.get('player') or {}).get('deck')
            if (not all(isinstance(deck, list) for deck in (
                    client_deck_before, client_deck_after,
                    shadow_deck_before, shadow_deck_after))
                    or client_deck_before != client_deck_after
                    or shadow_deck_before != shadow_deck_after):
                raise FlowBlocked('Skipped card reward unexpectedly changed a deck')

        # Claiming the final item is a legal terminal transition for this
        # reward occurrence. The client closes the reward screen and the
        # shadow advances to its next decision, so both reward-set identities
        # disappear together. Treat that as closure; only an active reward
        # screen must retain the mapped set identity.
        client_reward = client_after.get('reward') or {}
        shadow_active = shadow_after.get('decision') in {'combat_reward', 'card_reward'}
        client_active = client_after.get('screen') in {'REWARD', 'CARD_SELECTION'}
        after_client_set = client_reward.get('reward_set_id')
        after_shadow_set = shadow_after.get('reward_set_id')
        client_closed = not client_active and not client_reward
        closed_together = client_closed and not shadow_active
        shadow_set_omitted_in_card_boundary_after = (
            after_shadow_set is None
            and shadow_after.get('decision') == 'card_reward')
        shadow_card_set_changed_after = (
            shadow_after.get('decision') == 'card_reward'
            and after_shadow_set is not None
            and after_shadow_set != mapping['shadow_set_id'])
        if shadow_card_set_changed_after:
            nested_set = mapping.get('card_set_id')
            if nested_set is None:
                mapping['card_set_id'] = after_shadow_set
            elif nested_set != after_shadow_set:
                raise FlowBlocked('Native card reward set changed after mapped reward action')
        # The visible client can close the reward panel immediately after the
        # last claim while the headless engine still exposes its legal
        # ``finish_combat_rewards`` boundary. This is an in-flight close, not
        # a changed reward set; the next mirrored finish action must remain
        # available to settle the shadow side.
        shadow_closing = client_closed and shadow_active and action == 'claim_reward'
        card_closed_before_shadow_finish = (
            client_closed and action == 'choose_reward_card'
            and shadow_after.get('decision') == 'combat_reward')
        if card_closed_before_shadow_finish:
            after_offer = _native_reward_offer(shadow_after.get('offered_rewards'))
            after_flags = _native_reward_flags(shadow_after.get('offered_rewards'))
            options = (client_after.get('event') or {}).get('options') or []
            proceed = [row for row in options
                       if str(row.get('text_key') or '').split('.')[-1].upper() == 'PROCEED'
                       and row.get('is_locked') is not True
                       and type(row.get('index')) is int]
            if (after_shadow_set != mapping['shadow_set_id']
                    or shadow_after.get('rewards') != []
                    or len(after_offer) != len(before_offer)
                    or [row[:4] for row in after_offer] != [row[:4] for row in before_offer]
                    or after_offer[reward_position][4] != expected_remaining_cards
                    or not after_flags or not all(flag is True for flag in after_flags)
                    or client_after.get('screen') != 'EVENT'
                    or len(proceed) != 1
                    or client_after.get('run_id') != client_before.get('run_id')):
                raise FlowBlocked('Final card reward closed without a verified shadow finish boundary')
            self.context.pending_reward_finish = {
                'run_id': client_before.get('run_id'),
                'shadow_set_id': mapping['shadow_set_id'],
            }
            mapping['current_offer'] = []
            mapping['selected_flags'] = []
            return
        if (after_client_set != mapping['client_set_id']
                or (after_shadow_set != mapping['shadow_set_id']
                    and not shadow_set_omitted_in_card_boundary_after
                    and not shadow_card_set_changed_after)):
            if closed_together or shadow_closing:
                mapping['current_offer'] = []
                mapping['selected_flags'] = []
                return
            raise FlowBlocked('Native reward set changed during mapped reward action')
        after_offer = _native_reward_offer((client_after.get('reward') or {}).get('offered_rewards'))
        if after_offer != _native_reward_offer(shadow_after.get('offered_rewards')):
            raise FlowBlocked('Native reward contents differ after mapped action')
        if [row[0] for row in after_offer] != [row[0] for row in before_offer]:
            raise FlowBlocked('Native reward item identities changed during mapped action')
        if action == 'choose_reward_card' and after_offer[reward_position][4] != expected_remaining_cards:
            raise FlowBlocked('Selected reward card instance was not removed from its group')
        after_flags = _native_reward_flags((client_after.get('reward') or {}).get('offered_rewards'))
        if after_flags != _native_reward_flags(shadow_after.get('offered_rewards')):
            raise FlowBlocked('Native reward selection flags differ after mapped action')
        if action == 'choose_reward_card' and after_flags[reward_position] is not True:
            raise FlowBlocked('Claimed card reward is not marked selected')
        if action == 'skip_reward_cards' and (
                before_flags[reward_position] is not False
                or after_flags[reward_position] is not False):
            raise FlowBlocked('Skipped card reward changed native selection status')
        mapping['current_offer'] = after_offer
        mapping['selected_flags'] = after_flags

    def choose(self, state, shadow):
        current = self.classify(state, shadow)
        if current.stage == 'waiting':
            return current, None
        if current.stage == 'blocked':
            raise FlowBlocked(current.reason)
        handler = self.handlers.get(current.kind)
        if handler is None:
            raise FlowBlocked(f'No flow handler for {current.kind.value}')
        command = handler(state, shadow, current)
        command = self._normalize_persistent_boundary(command, current, shadow)
        if command is not None and self.context.reanchor_required and command.shadow is not None:
            command = client_authoritative(
                command,
                reason='continuing_visible_flow_until_map_reanchor',
                requires_reanchor=True,
            )
        if command is not None:
            native = 'select_deck_card' if command.client.action == 'select_deck_cards' else command.client.action
            if native not in current.actions:
                raise FlowBlocked(f'{current.kind.value} cannot execute {native}; actions={current.actions}')
        return current, command

    @staticmethod
    def _normalize_persistent_boundary(command, current, shadow):
        """Do not re-anchor for a UI click after the shadow already advanced.

        The visible client may still expose a final ``proceed`` click while the
        native engine has already reached its next persistent boundary.  That
        click is part of the UI protocol, not a second game-state transition.
        Treating it as client-authoritative caused the runner to load an
        unrelated, stale official save and replay history against the wrong
        time point.
        """
        if command is None or command.shadow is not None:
            return command
        if not command.telemetry.get('requires_reanchor'):
            return command
        if shadow.get('decision') != 'map_select':
            return command
        client = command.client
        if client is None or client.action not in {'proceed', 'close_shop_inventory'}:
            return command
        telemetry = dict(command.telemetry)
        telemetry.update({
            'client_authoritative': True,
            'client_authoritative_reason': 'ui_boundary_already_satisfied',
            'requires_reanchor': False,
            'shadow_already_at_boundary': True,
            'boundary_verification': 'existing_shadow',
        })
        return replace(command, telemetry=telemetry)

    def complete(self, command, before, after):
        self.context.completed(command, before, after)

    def map(self, state, shadow, current):
        run = state.get('run') or {}
        visible_choices = []
        for row in (state.get('map') or {}).get('available_nodes') or []:
            coord = row.get('coord') or row
            if coord.get('row') is None or coord.get('col') is None:
                continue
            visible_choices.append({
                'row': coord.get('row'), 'col': coord.get('col'),
                'type': row.get('node_type') or row.get('type'),
            })
        visible_policy = {
            'player': {
                'hp': run.get('current_hp', run.get('hp')),
                'max_hp': run.get('max_hp'), 'gold': run.get('gold'),
            },
            'context': {'floor': run.get('floor')},
            'choices': visible_choices,
        }
        shadow_ready = shadow.get('decision') == 'map_select' and bool(shadow.get('choices'))
        if shadow_ready:
            try:
                chosen, route = choose_weighted_route(shadow.get('choices') or [], state.get('map') or {})
                route_policy = 'fixed_weight_full_route'
            except ValueError:
                chosen = choose_map_node(shadow)
                route = []
                route_policy = 'local_route_fallback'
        else:
            chosen = choose_map_node(visible_policy)
            route = []
            route_policy = 'visible_client_fallback'
        options = (state.get('map') or {}).get('available_nodes') or []
        matches = [row for row in options if (row.get('coord') or row).get('row') == chosen['row']
                   and (row.get('coord') or row).get('col') == chosen['col']]
        if shadow_ready and (len(matches) != 1 or type(matches[0].get('index')) is not int):
            chosen = choose_map_node(visible_policy)
            route = []
            route_policy = 'visible_client_fallback'
            shadow_ready = False
            matches = [row for row in options if (row.get('coord') or row).get('row') == chosen['row']
                       and (row.get('coord') or row).get('col') == chosen['col']]
        if len(matches) != 1 or type(matches[0].get('index')) is not int:
            raise FlowBlocked('Map destination has no unique visible index')
        mirrored = shadow_ready
        return decision('choose_map_node', {'option_index': matches[0]['index']}, route_policy,
                        shadow=('select_map_node', chosen) if mirrored else None,
                        completion='room_change', selected=chosen,
                        node_type=matches[0].get('node_type'), planned_route=route,
                        client_authoritative=not mirrored,
                        client_authoritative_reason='headless_map_step_unavailable' if not mirrored else None,
                        requires_reanchor=not mirrored)

    def shop_inventory(self, state, shadow, current):
        shop = state.get('shop') or {}
        policy_state = client_shop_decision_state(state)
        choice = choose_shop_action(policy_state, self.repo_root, self.deck_profile)
        inventories_match = shop_inventories_match(state, shadow)
        # A shop is a stateful transaction boundary.  Once the visible room
        # has opened, every purchase/removal must address the same inventory
        # on both endpoints.  Continuing with a stale shadow creates a split
        # shop that only becomes observable at the later map checkpoint.
        if shadow.get('decision') != 'shop' or (
                choice and choice['action'] == 'buy_card' and not inventories_match):
            raise FlowBlocked(
                'Shop entry parity failed: '
                f"client_decision=shop shadow_decision={shadow.get('decision')!r} "
                f"inventory_match={inventories_match}"
            )
        if choice and choice['action'] == 'buy_card' and 'buy_card' in current.actions:
            wanted = next((row for row in shop.get('cards') or []
                           if int(row.get('index', -1)) == int(choice['card_index'])), None)
            if wanted is None or type(wanted.get('index')) is not int:
                raise FlowBlocked('Chosen visible shop card is absent from the current inventory')
            wanted_id = card_id_from_row(wanted)
            try:
                positions = map_object_indices(
                    shop.get('cards') or [], shadow.get('cards') or [], [wanted['index']],
                    lambda row: (card_id_from_row(row), bool(row.get('upgraded')))
                    if row.get('is_stocked', True) else None,
                )
            except ValueError as exc:
                raise FlowBlocked(str(exc)) from exc
            mirrored = True
            shadow_command = ('buy_card', {'card_index': positions[0]})
            return decision(
                'buy_card', {'option_index': wanted['index']}, 'visible_ordered_deck_profile',
                shadow=shadow_command, completion='shop_purchase', card_id=wanted_id,
                price=wanted.get('price'), shop_inventory_match=inventories_match,
                requires_reanchor=not mirrored,
            )
        if (choice and choice['action'] == 'remove_card'
                and 'remove_card_at_shop' in current.actions):
            mirrored = shadow.get('decision') == 'shop'
            return decision(
                'remove_card_at_shop', {}, 'remove_basic_card', operation='remove',
                shadow=('remove_card', {}) if mirrored else None, completion='selection_pending',
                remove_target=choice.get('remove_target'),
                shop_inventory_match=inventories_match,
                client_authoritative=not mirrored,
                client_authoritative_reason='headless_shop_step_unavailable' if not mirrored else None,
                requires_reanchor=not mirrored,
            )
        return decision('close_shop_inventory', policy='leave_shop', completion='shop_closed')

    def shop_room(self, state, shadow, current):
        if self.context.shop_stage == 'arrived' and 'open_shop_inventory' in current.actions:
            return decision('open_shop_inventory', completion='shop_open')
        reanchor = self.context.shop_reanchor_required
        return decision(
            'proceed', policy='leave_shop', completion='leave_room',
            shadow=None if reanchor else (('leave_room', {}) if shadow.get('decision') == 'shop' else None),
            client_authoritative=reanchor,
            client_authoritative_reason='continuing_shop_segment' if reanchor else None,
            requires_reanchor=reanchor,
        )

    def rewards(self, state, shadow, current):
        rewards = (state.get('reward') or {}).get('rewards') or []
        claimable = [r for r in rewards if r.get('claimable') is True]
        potions = (state.get('run') or {}).get('potions') or []
        open_slot = any(p.get('occupied') is False for p in potions)
        set_id = (state.get('reward') or {}).get('reward_set_id')
        shadow_set_id = shadow.get('reward_set_id')
        # Native IDs are required for current runtime offers. Type-only matching
        # is retained solely for old observer fixtures with unique reward kinds.
        native_offer = shadow_set_id is not None
        if native_offer:
            if type(set_id) is not int or type(shadow_set_id) is not int:
                raise FlowBlocked('Native reward set identity differs or is unavailable')
            client_offer = (state.get('reward') or {}).get('offered_rewards')
            shadow_offer = shadow.get('offered_rewards')
            offer = _native_reward_offer(client_offer)
            if offer != _native_reward_offer(shadow_offer):
                raise FlowBlocked('Complete ordered native reward offers differ')
            flags = _native_reward_flags(client_offer)
            if flags != _native_reward_flags(shadow_offer):
                raise FlowBlocked('Native reward selection flags differ')
            mapping = self.context.reward_set_mapping
            run_id = state.get('run_id')
            generation = self.context.reward_reanchor_generation
            if mapping is None:
                self.context.reward_occurrence += 1
                mapping = {
                    'run_id': run_id,
                    'reanchor_generation': generation,
                    'occurrence': self.context.reward_occurrence,
                    'client_set_id': set_id,
                    'shadow_set_id': shadow_set_id,
                    'ordered_offer': offer,
                    'current_offer': copy.deepcopy(offer),
                    'selected_flags': flags,
                    'verified': False,
                }
                self.context.reward_set_mapping = mapping
            elif (mapping['run_id'] != run_id
                  or mapping['reanchor_generation'] != generation
                  or mapping['client_set_id'] != set_id
                  or mapping['shadow_set_id'] != shadow_set_id):
                raise FlowBlocked('Native reward set mapping changed within one reward occurrence')
            # The original offer is identity evidence, not a prediction of
            # later contents. A native callback can change another item while
            # a claim settles; the two current native offers were compared
            # above at this decision boundary.
        # Card selection policy is unchanged; resolve each group separately.
        for reward in claimable:
            kind = _reward_kind(reward)
            native_index = reward.get('native_index')
            if native_offer and type(native_index) is not int:
                raise FlowBlocked('Claimable native reward has no stable item identity')
            item_key = (f'{self.context.reward_reanchor_generation}:'
                        f'{self.context.reward_occurrence}:{set_id}:{native_index}') if native_offer else None
            if item_key is not None and item_key in self.context.resolved_reward_items:
                continue
            shadow_matches = [r for r in shadow.get('rewards') or []
                              if _reward_kind(r) == kind and (not native_offer or r.get('index') == native_index)]
            if (not native_offer and kind in {'Card', 'SpecialCard'}
                    and self.context.reward_card_resolved and not shadow_matches):
                continue
            if kind == 'Potion' and not open_slot:
                continue
            if type(reward.get('index')) is not int:
                raise FlowBlocked('Reward item has no stable index')
            if not shadow_matches:
                if native_offer:
                    raise FlowBlocked('Claimable native reward is absent from the shadow pending items')
                return decision(
                    'claim_reward', {'option_index': reward['index']},
                    policy='claim_visible_reward', completion='reward_changed', reward_type=kind,
                    reward_description=reward.get('description'), client_authoritative=True,
                    client_authoritative_reason='visible_reward_missing_from_headless',
                    requires_reanchor=True,
                )
            if not native_offer and len(shadow_matches) > 1:
                raise FlowBlocked('Repeated rewards require native set and item identities')
            shadow_index = shadow_matches[0].get('index')
            if type(shadow_index) is not int:
                return decision(
                    'claim_reward', {'option_index': reward['index']},
                    policy='claim_visible_reward', completion='reward_changed', reward_type=kind,
                    reward_description=reward.get('description'), client_authoritative=True,
                    client_authoritative_reason='headless_reward_item_has_no_stable_index',
                    requires_reanchor=True,
                )
            return decision('claim_reward', {'option_index': reward['index']},
                            shadow=('claim_combat_reward', {'reward_index': shadow_index,
                                    **({'reward_set_id': shadow_set_id} if native_offer else {})}),
                            policy='claim_visible_reward', completion='reward_changed', reward_type=kind,
                            reward_item_key=item_key,
                            reward_native_index=native_index if native_offer else None,
                            reward_set_mapping=mapping if native_offer else None,
                            reward_description=reward.get('description'))
        skipped = [{'type': r.get('reward_type'), 'reason': 'potion_slots_full' if r.get('reward_type') == 'Potion'
                    else 'card_reward_skipped'} for r in claimable]
        action = 'proceed' if 'proceed' in current.actions else 'collect_rewards_and_proceed'
        mirrored = shadow.get('decision') == 'combat_reward'
        return decision(
            action, shadow=('finish_combat_rewards', {}) if mirrored else None,
            policy='finish_rewards', completion='leave_rewards', skipped_rewards=skipped,
            client_authoritative=not mirrored,
            client_authoritative_reason='headless_rewards_already_finished' if not mirrored else None,
            requires_reanchor=not mirrored and shadow.get('decision') != 'map_select',
        )

    def reward_card(self, state, shadow, current):
        visible_policy = client_reward_card_decision_state(state)
        shadow_ready = shadow.get('decision') == 'card_reward'
        # The visible offer is authoritative for the client click. The shadow
        # is used only to mirror the same card by identity when possible.
        policy_state = visible_policy
        offered_scores = score_card_reward_options(policy_state, self.repo_root, self.deck_profile)
        if self.deck_profile is None:
            choice = choose_card_reward(policy_state, self.repo_root)
        else:
            choice = choose_card_reward(policy_state, self.repo_root, deck_profile=self.deck_profile)
        if choice is None:
            can_skip = ('skip_reward_cards' in current.actions
                        and (state.get('reward') or {}).get('can_skip') is not False)
            if not can_skip:
                choice = choose_forced_card_reward(policy_state, self.repo_root, self.deck_profile)
                if choice is None:
                    raise FlowBlocked('Mandatory reward has no selectable cards')
        if choice is None:
            mirrored = shadow_ready and shadow.get('can_skip') is not False
            return decision(
                'skip_reward_cards', policy=(self.deck_profile or {}).get('id', 'deck_profile'),
                shadow=('skip_card_reward', {}) if mirrored else None,
                completion='card_reward_closed', offered_scores=offered_scores,
                choice_reason='no_eligible_archetype_card', client_authoritative=not mirrored,
                client_authoritative_reason='headless_card_reward_skip_unavailable' if not mirrored else None,
                requires_reanchor=not mirrored,
            )
        wanted = next((r for r in visible_policy.get('cards') or []
                       if r.get('index') == choice['card_index']), None)
        if wanted is None:
            raise FlowBlocked('Chosen reward index is not present in the policy input')
        card_id = card_id_from_row(wanted)
        if self.context.reward_set_mapping is not None and shadow_ready:
            wanted_identity = _reward_card_identity(wanted)
            visible_options = visible_policy.get('cards') or []
            shadow_options = shadow.get('cards') or []
            visible_position = next((i for i, row in enumerate(visible_options)
                                     if row.get('index') == choice['card_index']), None)
            shadow_matches = [row for row in shadow_options
                              if _reward_card_identity(row) == wanted_identity]
            if len(shadow_matches) > 1:
                if (visible_position is None or visible_position >= len(shadow_options)
                        or _reward_card_identity(shadow_options[visible_position]) != wanted_identity):
                    raise FlowBlocked('Duplicate reward card has no shared candidate position')
                shadow_matches = [shadow_options[visible_position]]
            if not shadow_matches:
                raise FlowBlocked('Selected reward card upgrade or identity differs between endpoints')
            matched = wanted
        else:
            visible_matches = [r for r in visible_policy.get('cards') or [] if card_id_from_row(r) == card_id]
            shadow_matches = [r for r in shadow.get('cards') or [] if card_id_from_row(r) == card_id]
            matched = next((r for r in visible_matches if r.get('index') == choice['card_index']), None)
            if matched is None and len(visible_matches) == 1:
                matched = visible_matches[0]
        if matched is None:
            raise FlowBlocked('Chosen reward card has no unambiguous visible counterpart')
        mirrored = shadow_ready and len(shadow_matches) == 1
        shadow_choice = {'card_index': shadow_matches[0]['index']} if mirrored else None
        chosen_score = next((row for row in offered_scores if row.get('index') == choice['card_index']), None)
        return decision(
            'choose_reward_card', {'option_index': matched['index']},
            (self.deck_profile or {}).get('id', 'deck_profile'),
            shadow=('select_card_reward', shadow_choice) if mirrored else None,
            completion='card_reward_closed', offered=visible_policy.get('cards'),
            offered_scores=offered_scores, chosen_score=chosen_score,
            choice_reason='mandatory_reward_fallback' if chosen_score and not chosen_score.get('eligible')
            else 'eligible_profile_card', client_authoritative=not mirrored,
            client_authoritative_reason='visible_reward_card_has_no_unique_headless_match'
            if not mirrored else None,
            requires_reanchor=not mirrored,
        )

    def event(self, state, shadow, current):
        chosen = choose_client_event(state)
        pending_finish = self.context.pending_reward_finish
        if pending_finish is not None:
            if (chosen.telemetry.get('policy') != 'explicit_proceed'
                    or state.get('run_id') != pending_finish['run_id']
                    or shadow.get('decision') != 'combat_reward'
                    or shadow.get('reward_set_id') != pending_finish['shadow_set_id']
                    or shadow.get('rewards') != []
                    or not shadow.get('offered_rewards')
                    or not all(row.get('successfully_selected') is True
                               for row in shadow['offered_rewards'])):
                raise FlowBlocked('Pending final reward has no matching proceed and shadow finish')
            return from_client_decision(
                chosen, shadow=('finish_combat_rewards', {}),
                reward_finalization=True,
            )
        if chosen.telemetry.get('policy') == 'explicit_proceed':
            if shadow.get('decision') == 'map_select':
                normalize = lambda value: str(value or '').split('.')[-1].upper()
                client_relics = [normalize(row.get('relic_id'))
                                 for row in (state.get('run') or {}).get('relics') or []]
                shadow_relics = [normalize(row.get('id'))
                                 for row in (shadow.get('player') or {}).get('relics') or []]
                reconcile = None
                if client_relics and client_relics != shadow_relics:
                    reconcile = ('reconcile_relics', {'relic_ids': ','.join(client_relics)})
                return from_client_decision(chosen, shadow=reconcile)
            if shadow.get('decision') != 'event_choice':
                return from_client_decision(
                    chosen,
                    client_authoritative=True,
                    client_authoritative_reason='headless_event_has_no_proceed_step',
                    requires_reanchor=True,
                )
            matches = [
                row for row in shadow.get('options') or []
                if str(row.get('text_key') or '').split('.')[-1].upper() == 'PROCEED'
            ]
            if len(matches) != 1 or type(matches[0].get('index')) is not int:
                return from_client_decision(
                    chosen,
                    client_authoritative=True,
                    client_authoritative_reason='headless_event_proceed_option_unavailable',
                    requires_reanchor=True,
                )
            return from_client_decision(
                chosen, shadow=('choose_option', {'option_index': matches[0]['index']})
            )
        if shadow.get('decision') != 'event_choice':
            return from_client_decision(
                chosen,
                client_authoritative=True,
                client_authoritative_reason='headless_event_decision_unavailable',
                requires_reanchor=True,
            )
        matches = [r for r in shadow.get('options') or [] if r.get('text_key') == chosen.telemetry.get('option_id')]
        if len(matches) != 1:
            return from_client_decision(
                chosen,
                client_authoritative=True,
                client_authoritative_reason='visible_event_option_has_no_unique_headless_match',
                requires_reanchor=True,
            )
        return from_client_decision(
            chosen, shadow=('choose_option', {'option_index': matches[0]['index']})
        )

    def rest(self, state, shadow, current):
        chosen = choose_client_rest(state, self.repo_root)
        if shadow.get('decision') != 'rest_site':
            return from_client_decision(
                chosen,
                client_authoritative=True,
                client_authoritative_reason='headless_rest_decision_unavailable',
                requires_reanchor=True,
            )
        key = chosen.telemetry.get('option_id')
        matches = [r for r in shadow.get('options') or [] if (r.get('option_id') or r.get('id')) == key]
        if len(matches) != 1:
            return from_client_decision(
                chosen,
                client_authoritative=True,
                client_authoritative_reason='visible_rest_option_has_no_unique_headless_match',
                requires_reanchor=True,
            )
        return from_client_decision(
            chosen, shadow=('choose_option', {'option_index': matches[0]['index']})
        )

    def selection(self, state, shadow, current):
        spec = current.selection
        if not (state.get('selection') or {}).get('cards'):
            for action in ('cancel_selection', 'skip_reward', 'proceed'):
                if action in current.actions:
                    return decision(
                        action, operation=spec.operation if spec else None,
                        completion='selection_done', client_authoritative=True,
                        client_authoritative_reason='visible_selection_has_no_cards',
                        requires_reanchor=True,
                    )
            raise FlowBlocked('Selection has no cards and no legal cancellation action')
        adapted = copy.deepcopy(state)
        adapted['selection'].update(min_select=spec.minimum, max_select=spec.maximum)
        if self.deck_profile is None:
            chosen = choose_client_selection(adapted, self.repo_root, spec.operation)
        else:
            chosen = choose_client_selection(adapted, self.repo_root, spec.operation, self.deck_profile)
        indices = chosen.params.get('indices', [chosen.params.get('option_index')])
        if shadow.get('decision') != 'card_select':
            chosen.telemetry.update(selection_spec=current.to_dict()['selection'])
            return from_client_decision(
                chosen,
                completion='selection_done',
                client_authoritative=True,
                client_authoritative_reason='headless_selection_step_unavailable',
                requires_reanchor=True,
            )
        native = (state.get('selection') or {}).get('cards') or []
        options = shadow.get('cards') or []
        try:
            positions = map_object_indices(
                native, options, indices,
                lambda row: (str(row.get('card_id') or row.get('id') or '').split('.')[-1].upper(),
                             bool(row.get('upgraded'))),
            )
        except ValueError:
            chosen.telemetry.update(selection_spec=current.to_dict()['selection'])
            return from_client_decision(
                chosen, completion='selection_done', client_authoritative=True,
                client_authoritative_reason='visible_selection_has_no_unambiguous_headless_match',
                requires_reanchor=True,
            )
        chosen.telemetry.update(selection_spec=current.to_dict()['selection'])
        return from_client_decision(
            chosen,
            shadow=('select_cards', {'indices': ','.join(map(str, positions))}),
            completion='selection_done',
        )

    def confirm(self, state, shadow, current):
        if state.get('in_combat'):
            spec = current.selection
            if shadow.get('decision') != 'card_select':
                raise FlowBlocked('Combat confirmation has no matching shadow selection')
            if spec.selected != () or int(shadow.get('min_select') or 0) != 0:
                raise FlowBlocked('Combat confirmation requires explicit selected-card mapping')
            return decision('confirm_selection', operation=spec.operation,
                            shadow=('select_cards', {'indices': ''}), completion='selection_done')
        return decision('confirm_selection', operation=current.selection.operation, completion='selection_done')

    def chest(self, state, shadow, current):
        mirrored = shadow.get('decision') == 'treasure'
        return decision(
            'open_chest', shadow=('open_chest', {}) if mirrored else None,
            client_authoritative=not mirrored,
            client_authoritative_reason='headless_treasure_step_unavailable' if not mirrored else None,
            requires_reanchor=not mirrored,
        )

    def relic(self, state, shadow, current):
        rows = (state.get('chest') or {}).get('relic_options') or []
        if not rows or type(rows[0].get('index')) is not int:
            raise FlowBlocked('Treasure relic options unavailable')
        visible = rows[0]
        relic_id = str(visible.get('relic_id') or '').split('.')[-1].upper()
        matches = [row for row in shadow.get('relics') or []
                   if str(row.get('relic_id') or row.get('id') or '').split('.')[-1].upper() == relic_id]
        mirrored = (
            shadow.get('decision') == 'treasure_relic'
            and bool(relic_id) and len(matches) == 1
            and type(matches[0].get('index')) is int
        )
        return decision(
            'choose_treasure_relic', {'option_index': visible['index']},
            policy='claim_visible_treasure_relic',
            shadow=('choose_treasure_relic', {'relic_index': matches[0]['index']}) if mirrored else None,
            relic_id=visible.get('relic_id'),
            client_authoritative=not mirrored,
            client_authoritative_reason='visible_treasure_relic_has_no_headless_counterpart'
            if not mirrored else None,
            requires_reanchor=not mirrored,
        )

    def bundle(self, state, shadow, current):
        rows = state.get('bundles') or []
        if not rows:
            raise FlowBlocked('Bundle options unavailable')
        profile = self.deck_profile or load_deck_profile()
        visible_choice = choose_profile_bundle(
            profile, rows, (state.get('run') or {}).get('deck') or [],
            act=_visible_act(state),
        )
        if visible_choice is None:
            valid = [
                (int(row.get('index', position)), row)
                for position, row in enumerate(rows)
                if type(row.get('index', position)) is int
            ]
            if not valid:
                raise FlowBlocked('Visible bundle has no stable option index')
            index, row = valid[0]
            visible_choice = {'index': index, 'reason': 'visible_bundle_fallback'}
        visible = next((row for row in rows if int(row.get('index', -1)) == visible_choice['index']), None)
        if visible is None:
            raise FlowBlocked('Chosen visible bundle is absent from the current options')
        visible_ids = [card_id_from_row(card) for card in visible.get('cards') or []]
        shadow_rows = shadow.get('bundles') or []
        shadow_matches = [
            row for row in shadow_rows
            if [card_id_from_row(card) for card in row.get('cards') or []] == visible_ids
        ]
        mirrored = shadow.get('decision') == 'bundle_select' and len(shadow_matches) == 1
        shadow_index = shadow_matches[0].get('index') if mirrored else None
        if mirrored and type(shadow_index) is not int:
            mirrored = False
        return decision(
            'choose_bundle', {'option_index': visible['index']}, 'ordered_deck_profile',
            shadow=('select_bundle', {'bundle_index': shadow_index}) if mirrored else None,
            bundle_audit=visible_choice,
            client_authoritative=not mirrored,
            client_authoritative_reason='visible_bundle_has_no_headless_counterpart'
            if not mirrored else None,
            requires_reanchor=not mirrored,
        )

    def bundle_confirm(self, state, shadow, current):
        return decision('confirm_bundle')

    def capstone(self, state, shadow, current):
        raise FlowBlocked('Capstone recognized; Mod does not export options for the existing policy')

    def proceed(self, state, shadow, current):
        if state.get('screen') == 'CRYSTAL_SPHERE':
            sphere = state.get('crystal_sphere') or {}
            if sphere.get('phase') != 'proceed' or sphere.get('remaining') != 0:
                raise FlowBlocked('Crystal Sphere is not ready to proceed')
            if shadow.get('decision') == 'crystal_sphere':
                raise FlowBlocked('Headless Crystal Sphere has not completed')
            if shadow.get('decision') == 'combat_reward':
                offered = shadow.get('offered_rewards') or []
                if shadow.get('rewards') != [] or not offered or not all(
                        row.get('successfully_selected') is True for row in offered):
                    raise FlowBlocked('Crystal Sphere rewards are not fully claimed in shadow')
                return decision('proceed', shadow=('finish_combat_rewards', {}),
                                completion='leave_room', policy='crystal_sphere_proceed')
            if shadow.get('decision') != 'map_select':
                raise FlowBlocked('Crystal Sphere has no matching headless exit boundary')
            return decision('proceed', completion='leave_room',
                            policy='crystal_sphere_proceed',
                            client_authoritative=True,
                            client_authoritative_reason='headless_sphere_final_reveal_already_settled')
        phase = shadow.get('decision')
        command = ('leave_room', {}) if phase in {
            'shop', 'rest_site', 'treasure', 'treasure_complete',
        } else None
        context = shadow.get('context') or {}
        if context.get('room_type') == 'Boss' and phase not in {'combat_play', 'card_select', 'card_reward', 'event_choice', 'map_select'}:
            command = ('proceed', {})
        mirrored = command is not None
        return decision(
            'proceed', shadow=command, completion='leave_room',
            client_authoritative=not mirrored,
            client_authoritative_reason='headless_proceed_boundary_unavailable' if not mirrored else None,
            requires_reanchor=not mirrored,
        )

    def modal(self, state, shadow, current):
        if self.context.authorized_modal and 'confirm_modal' in current.actions:
            return decision('confirm_modal')
        if 'dismiss_modal' in current.actions:
            return decision('dismiss_modal', policy='dismiss_unrequested_modal')
        raise FlowBlocked('Modal requires an explicit confirmation decision')

    def inspect(self, state, shadow, current):
        return decision('close_cards_view')

    def menu(self, state, shadow, current):
        if 'close_main_menu_submenu' in current.actions:
            return decision('close_main_menu_submenu')
        raise FlowBlocked('Main menu: use Start New Run')

    def character(self, state, shadow, current):
        raise FlowBlocked('Character selection is controlled by new-run setup')

    def timeline(self, state, shadow, current):
        if 'confirm_timeline_overlay' in current.actions:
            return decision('confirm_timeline_overlay')
        return decision('close_main_menu_submenu')

    def potion(self, state, shadow, current):
        for action in ('cancel_potion', 'skip_reward', 'cancel_selection', 'proceed'):
            if action in current.actions:
                return decision(action, policy='keep_existing_potions')
        raise FlowBlocked('Potion replacement recognized but no skip action is exposed')

    def terminal(self, state, shadow, current):
        return None


def transition_completed(action, before, after):
    actions = set(after.get('available_actions') or [])
    context = FlowContext()
    context.observe(before)
    previous = classify_interaction(before, context)
    current = classify_interaction(after, context)
    changed = semantic_fingerprint(before) != semantic_fingerprint(after)
    if action == 'end_turn':
        if (changed and after.get('in_combat') and current.kind in {K.SELECT, K.CONFIRM}
                and current.stage == 'ready' and previous.kind == K.COMBAT):
            return True
        # Combat termination is asynchronous on the client.  During the
        # transition it reports ``in_combat=False`` while the screen is still
        # COMBAT and only ``discard_potion`` remains available.  That is not a
        # decision boundary yet: rewards (and their RNG stream) have not been
        # generated.  Wait for a real ready/terminal boundary before sampling
        # client RNG and advancing the mirrored shadow.
        if not after.get('in_combat'):
            return current.stage in {'ready', 'terminal'} and current.kind not in {K.COMBAT, K.WAIT}
        return ((after.get('turn') or 0) > (before.get('turn') or 0)
                and current.kind in {K.COMBAT, K.SELECT, K.CONFIRM})
    if action == 'choose_map_node':
        if not changed or current.stage == 'waiting':
            return False
        # A map click can advance the floor while the previous room's event
        # panel is still visible. Its completed options and RNG are stale.
        if after.get('screen') == 'EVENT' and (after.get('event') or {}).get('is_finished') is True:
            return False
        if after.get('in_combat') or after.get('screen') == 'COMBAT':
            combat = after.get('combat') or {}
            enemies = combat.get('enemies') or []
            ready_actions = set(after.get('available_actions') or [])
            opening_selection = (
                after.get('screen') == 'CARD_SELECTION'
                and (after.get('selection') or {}).get('kind') == 'combat_hand_select'
                and bool(ready_actions.intersection({'select_deck_card', 'confirm_selection'}))
            )
            return bool(
                after.get('in_combat')
                and (after.get('screen') == 'COMBAT' or opening_selection)
                and (after.get('turn') or 0) >= 1
                and enemies
                and (opening_selection or ready_actions.intersection({'play_card', 'end_turn'}))
                and all(
                    enemy.get('enemy_id')
                    and enemy.get('current_hp') is not None
                    and enemy.get('max_hp') is not None
                    and enemy.get('intents')
                    for enemy in enemies
                )
            )
        return current.kind != K.WAIT
    if action in {'play_card', 'use_potion'}:
        return changed and current.stage != 'waiting' and (not after.get('in_combat') or current.kind in {K.COMBAT, K.SELECT, K.CONFIRM, K.REWARD_CARD})
    if action == 'open_shop_inventory':
        return current.kind == K.SHOP_INVENTORY
    if action == 'close_shop_inventory':
        return current.kind != K.SHOP_INVENTORY and changed
    if action in {'buy_card', 'buy_relic', 'buy_potion'}:
        return changed and current.kind == K.SHOP_INVENTORY
    if action in {'remove_card', 'remove_card_at_shop'}:
        return changed and current.kind in {K.SELECT, K.CONFIRM}
    if action == 'claim_reward':
        return changed and (previous.kind != current.kind or before.get('reward') != after.get('reward'))
    if action in {'choose_reward_card', 'skip_reward_cards'}:
        return current.kind != K.REWARD_CARD and changed
    if action in {'select_deck_card', 'select_deck_cards', 'confirm_selection'}:
        if not changed or current.stage not in {'ready', 'terminal'}:
            return False
        if current.kind in {K.SELECT, K.CONFIRM}:
            return before.get('selection') != after.get('selection')
        # A selection can close its overlay before the game has finished the
        # action it resumed.  COMBAT with only global potion controls is a
        # transition frame, not a player decision (for example after an
        # enemy's forced discard at end of turn).
        return current.kind not in {K.WAIT, K.UNSUPPORTED}
    if action in {'resolve_rewards', 'collect_rewards_and_proceed'}:
        return changed and current.kind not in {K.REWARD, K.REWARD_CARD, K.WAIT}
    if action == 'proceed':
        return changed and (current.kind != previous.kind or before.get('event') != after.get('event')
                            or (before.get('run') or {}).get('act_id') != (after.get('run') or {}).get('act_id'))
    return changed and current.kind != K.WAIT and current.stage != 'waiting'
