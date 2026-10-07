import pytest

from controller.interaction_state import FlowContext, InteractionKind as K, classify_interaction


@pytest.mark.parametrize('screen,actions,payload,kind', [
    ('MAP', ['choose_map_node'], {}, K.MAP),
    ('COMBAT', ['play_card', 'end_turn'], {'in_combat': True}, K.COMBAT),
    ('COMBAT', ['discard_potion'], {'in_combat': True}, K.WAIT),
    ('SHOP', ['open_shop_inventory', 'proceed'], {'shop': {'is_open': False}}, K.SHOP_ROOM),
    ('SHOP', ['close_shop_inventory'], {'shop': {'is_open': True}}, K.SHOP_INVENTORY),
    ('REWARD', ['claim_reward'], {'reward': {'pending_card_choice': False}}, K.REWARD),
    ('CARD_SELECTION', ['choose_reward_card', 'select_deck_card'],
     {'reward': {'pending_card_choice': True, 'card_options': [{'index': 0}]}}, K.REWARD_CARD),
    ('CARD_SELECTION', ['select_deck_card'], {'selection': {'kind': 'deck_transform_select', 'min_select': 2,
                                                        'max_select': 2, 'cards': [{'index': 0}]}}, K.SELECT),
    ('CARD_SELECTION', ['confirm_selection'], {'selection': {'can_confirm': True}}, K.CONFIRM),
    ('CARD_SELECTION', ['choose_potion_slot'], {}, K.POTION),
    ('EVENT', ['choose_event_option'], {}, K.EVENT),
    ('REST', ['choose_rest_option'], {}, K.REST),
    ('REST', ['proceed'], {}, K.PROCEED),
    ('CHEST', ['open_chest'], {}, K.CHEST),
    ('CHEST', ['choose_treasure_relic'], {}, K.RELIC),
    ('CHEST', ['discard_potion'], {}, K.WAIT),
    ('BUNDLE_SELECTION', ['choose_bundle'], {}, K.BUNDLE),
    ('BUNDLE_SELECTION', ['confirm_bundle'], {}, K.BUNDLE_CONFIRM),
    ('CAPSTONE_SELECTION', ['choose_capstone_option'], {}, K.CAPSTONE),
    ('MODAL', ['confirm_modal', 'play_card'], {'in_combat': True}, K.MODAL),
    ('CARDS_VIEW', ['close_cards_view'], {'in_combat': True}, K.INSPECT),
    ('MAIN_MENU', ['open_character_select'], {}, K.MENU),
    ('MAIN_MENU', ['confirm_timeline_overlay'], {}, K.TIMELINE),
    ('CHARACTER_SELECT', ['embark'], {}, K.CHARACTER),
    ('GAME_OVER', ['return_to_main_menu'], {}, K.TERMINAL),
    ('UNKNOWN', [], {}, K.WAIT),
    ('UNKNOWN', ['unrecognized_command'], {}, K.UNSUPPORTED),
    ('MULTIPLAYER_LOBBY', ['ready_multiplayer_lobby'], {}, K.UNSUPPORTED),
])
def test_native_screen_inventory(screen, actions, payload, kind):
    result = classify_interaction({'screen': screen, 'available_actions': actions, **payload})
    assert result.kind == kind


def test_overlay_keeps_parent_and_selection_constraints_from_shadow():
    context = FlowContext()
    context.observe({'screen': 'EVENT', 'run_id': 'run', 'run': {'floor': 2}})
    context.operation = 'transform'
    state = {'screen': 'CARD_SELECTION', 'run_id': 'run', 'run': {'floor': 2},
             'available_actions': ['select_deck_card'], 'selection': {'kind': 'deck_card_select',
              'min_select': 0, 'max_select': 0, 'cards': [{'index': 0}, {'index': 1}]}}
    context.observe(state)
    result = classify_interaction(state, context, {'decision': 'card_select', 'min_select': 2, 'max_select': 2})
    assert result.scene == 'EVENT'
    assert result.selection.minimum == result.selection.maximum == 2
    assert result.selection.operation == 'transform'
    assert result.selection.count_source == 'headless_selection'
    assert context.stack == ['EVENT', 'CARD_SELECTION']


def test_missing_quantities_are_not_guessed_from_prompt():
    result = classify_interaction({'screen': 'CARD_SELECTION', 'available_actions': ['select_deck_card'],
         'selection': {'kind': 'deck_upgrade_select', 'prompt': 'choose 2 cards', 'min_select': 0,
                       'max_select': 0, 'cards': [{'index': 0}]}})
    assert result.stage == 'blocked'
    assert result.selection.maximum is None


def test_event_prompt_with_card_type_supplies_explicit_count():
    state = {'screen': 'CARD_SELECTION', 'available_actions': ['select_deck_card'],
             'selection': {'kind': 'deck_card_select', 'prompt': '选择[blue]2[/blue]张普通牌加入到你的牌组。',
                           'min_select': 0, 'max_select': 0,
                           'cards': [{'index': index} for index in range(3)]}}
    result = classify_interaction(state)
    assert result.stage == 'ready'
    assert result.selection.minimum == result.selection.maximum == 2
    assert result.selection.count_source == 'explicit_prompt'


def test_known_generic_selector_preserves_existing_pick_fallback():
    result = classify_interaction({'screen': 'CARD_SELECTION', 'available_actions': ['select_deck_card'],
        'selection': {'kind': 'combat_hand_select', 'min_select': 1, 'max_select': 1, 'cards': [{'index': 0}]}})
    assert result.stage == 'ready'
    assert result.selection.operation == 'pick'


def test_unknown_selector_with_visible_cards_uses_generic_pick():
    result = classify_interaction({
        'screen': 'CARD_SELECTION',
        'available_actions': ['select_deck_card'],
        'selection': {
            'kind': 'relic_specific_card_select',
            'min_select': 1,
            'max_select': 1,
            'cards': [{'index': 4, 'card_id': 'BASH'}],
        },
    })
    assert result.stage == 'ready'
    assert result.selection.operation == 'pick'


@pytest.mark.parametrize('state,reason', [
    ({'screen': 'MAP', 'available_actions': ['choose_map_node'],
      'map': {'available_nodes': []}}, 'map nodes'),
    ({'screen': 'EVENT', 'available_actions': ['choose_event_option'],
      'event': {'options': []}}, 'event options'),
    ({'screen': 'REST', 'available_actions': ['choose_rest_option'],
      'rest': {'options': []}}, 'rest options'),
    ({'screen': 'CHEST', 'available_actions': ['choose_treasure_relic'],
      'chest': {'relic_options': []}}, 'treasure relic options'),
    ({'screen': 'BUNDLE_SELECTION', 'available_actions': ['choose_bundle'],
      'bundles': []}, 'bundle options'),
])
def test_explicit_empty_option_lists_wait_instead_of_blocking(state, reason):
    result = classify_interaction(state)
    assert result.kind == K.WAIT
    assert result.stage == 'waiting'
    assert reason in result.reason.lower()


def test_cards_view_without_close_action_waits_for_controls():
    result = classify_interaction({'screen': 'CARDS_VIEW', 'available_actions': [], 'in_combat': True})
    assert result.kind == K.WAIT
    assert result.stage == 'waiting'
