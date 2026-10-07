import copy
import json
import threading
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from cli.sts2_mod_adapter import ModApiError, Sts2ModAdapter
from controller.engine_parity import (
    client_combat_checkpoint,
    client_map_checkpoint,
    compare_checkpoints,
    headless_combat_checkpoint,
    headless_map_checkpoint,
)
from controller.live_client_bridge import (
    JsonlSessionLog,
    LiveClientBridge,
    StaleClientStateError,
    _can_rebase_stale_action,
    event_resolution_equivalent,
)
from controller.live_session import ControlledStop, ManagedControl, SessionJournal, write_json
from scripts.render_live_run_report import _render


def canonical():
    return {'checkpoint': 'combat_play', 'run': {'character': 'IRONCLAD', 'ascension': 0,
        'act': 1, 'floor': 1, 'hp': 70, 'max_hp': 80, 'gold': 99, 'boss': 'BOSS',
        'deck': [{'id': 'STRIKE', 'upgraded': False}], 'relics': [], 'potions': []},
        'turn': 1, 'player': {'hp': 70, 'max_hp': 80, 'block': 0, 'energy': 3, 'powers': []},
        'hand': [], 'enemies': [{'id': 'TEST', 'hp': 20, 'max_hp': 20, 'block': 0,
                              'powers': [], 'intents': [{'type': 'Attack', 'damage': 5, 'hits': 1}]}]}


def client_state():
    return {'state_version': 1, 'run_id': 'test', 'screen': 'COMBAT', 'in_combat': True,
        'turn': 1, 'available_actions': ['end_turn', 'play_card'],
        'run': {'current_hp': 70, 'max_hp': 80, 'gold': 99, 'floor': 1},
        'combat': {'hand': [], 'player': {'energy': 3}, 'enemies': []}}


class FakeMod:
    def __init__(self):
        self.config = SimpleNamespace(timeout_s=0.1)
        self.value = client_state()
        self.last_call_ms = 0.2
        self.last_request_id = 'action-id'
        self.actions = []

    def state(self):
        return copy.deepcopy(self.value)

    def action(self, action, **params):
        self.actions.append((action, params))
        self.value['turn'] += 1
        return {'state': self.state()}


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail('Timed out waiting for test worker')
        time.sleep(0.01)


def test_step_token_releases_exactly_one_action(tmp_path):
    path = tmp_path / 'control.json'
    write_json(path, {'desired': 'paused'})
    control = ManagedControl(path, None)
    sent = []
    def worker():
        try:
            for index in range(3):
                control.checkpoint('running')
                control.before_action({'sequence': index})
                sent.append(index)
        except ControlledStop:
            pass
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        wait_until(lambda: control.state.get('mode') == 'paused')
        assert sent == []
        write_json(path, {'desired': 'step', 'command_id': 'one'})
        wait_until(lambda: sent == [0] and control.state.get('mode') == 'paused')
        time.sleep(0.15)
        assert sent == [0]
        write_json(path, {'desired': 'running'})
        thread.join(2)
        assert sent == [0, 1, 2]
    finally:
        write_json(path, {'desired': 'stop'})
        thread.join(2)


def test_stop_before_action_never_sends_and_persists(tmp_path):
    report = {}
    path = tmp_path / 'control.json'
    write_json(path, {'desired': 'stop'})
    control = ManagedControl(path, None)
    log = JsonlSessionLog(tmp_path / 'session.jsonl')
    log.on_event = SessionJournal(report, lambda: write_json(tmp_path / 'report.json', report), control).record
    mod = FakeMod()
    bridge = LiveClientBridge(mod, log)
    bridge.before_action = control.before_action
    with pytest.raises(ControlledStop):
        bridge.execute_headless_action('combat_play', 'end_turn', {}, mod.state())
    assert mod.actions == []
    saved = json.loads((tmp_path / 'report.json').read_text())
    assert saved['actions'][0]['status'] == 'cancelled'
    assert saved['actions'][0]['client_before']['turn'] == 1
    assert saved['pending_action'] is None


def test_manual_change_cancels_cached_decision():
    mod = FakeMod()
    bridge = LiveClientBridge(mod)
    bridge.before_action = lambda pending: mod.value['run'].update(current_hp=60)
    with pytest.raises(StaleClientStateError, match='stale action cancelled') as caught:
        bridge.execute_headless_action('combat_play', 'end_turn', {}, mod.state())
    assert mod.actions == []
    assert caught.value.observed_state['run']['current_hp'] == 60
    assert bridge.last_action['status'] == 'cancelled'


def test_transient_preflight_change_is_confirmed_before_cancelling():
    class TransientMod(FakeMod):
        def __init__(self):
            super().__init__()
            self.state_calls = 0

        def state(self):
            self.state_calls += 1
            value = super().state()
            if self.state_calls == 2:
                value['run']['current_hp'] = 69
            return value

    mod = TransientMod()
    bridge = LiveClientBridge(mod)
    after = bridge.execute_headless_action('combat_play', 'end_turn', {}, mod.state())
    assert after['turn'] == 2
    assert mod.actions == [('end_turn', {})]
    assert bridge.last_action['stale_observation_retry'] is True
    assert bridge.last_action['stale_observation_transient'] is True


def test_transport_counters_do_not_invalidate_decision():
    mod = FakeMod()
    bridge = LiveClientBridge(mod)
    bridge.before_action = lambda pending: mod.value.update(state_version=2)
    after = bridge.execute_headless_action('combat_play', 'end_turn', {}, mod.state())
    assert after['turn'] == 2
    assert len(mod.actions) == 1
    assert bridge.last_action['status'] == 'completed'


def test_same_visible_treasure_relic_can_rebase_after_chest_animation_change():
    before = {
        'run_id': 'test', 'screen': 'CHEST',
        'available_actions': ['choose_treasure_relic'],
        'chest': {'relic_options': [{'index': 0, 'relic_id': 'WHETSTONE'}]},
        'run': {'gold': 100},
    }
    observed = copy.deepcopy(before)
    observed['run']['gold'] = 101
    observed['chest']['animation_complete'] = True

    assert _can_rebase_stale_action(
        'choose_treasure_relic', {'option_index': 0}, before, observed
    ) is True
    observed['chest']['relic_options'][0]['relic_id'] = 'OTHER_RELIC'
    assert _can_rebase_stale_action(
        'choose_treasure_relic', {'option_index': 0}, before, observed
    ) is False


def test_timeout_keeps_unknown_action_outcome(tmp_path):
    mod = FakeMod()
    def disconnected(action, **params):
        raise TimeoutError('connection lost after send')
    mod.action = disconnected
    bridge = LiveClientBridge(mod, JsonlSessionLog(tmp_path / 'session.jsonl'))
    with pytest.raises(TimeoutError):
        bridge.execute_headless_action('combat_play', 'end_turn', {}, mod.state())
    assert bridge.last_action['status'] == 'outcome_unknown'
    events = [json.loads(line) for line in (tmp_path / 'session.jsonl').read_text().splitlines()]
    assert [event['event'] for event in events] == ['action_pending', 'action_started', 'action_failed']


def test_settle_timeout_keeps_request_id_in_failure_record(tmp_path):
    mod = FakeMod()
    def no_op(action, **params):
        return {'state': mod.state()}
    mod.action = no_op
    bridge = LiveClientBridge(mod, JsonlSessionLog(tmp_path / 'session.jsonl'))

    with pytest.raises(ModApiError):
        bridge.execute_headless_action('combat_play', 'end_turn', {}, mod.state())

    assert bridge.last_action['request_id'] == 'action-id'
    assert bridge.last_action['action_wall_ms'] >= 0.0
    timing = bridge.last_action['timing_breakdown']
    assert timing['client_submit_ms'] >= 0.0
    assert timing['client_settle_wait_ms'] >= 0.0
    assert timing['client_transaction_ms'] >= timing['client_settle_wait_ms']


def test_map_boundary_requires_two_stable_observations_after_room_exit():
    before = {
        'run_id': 'test', 'screen': 'CHEST', 'in_combat': False,
        'available_actions': ['proceed'], 'run': {'floor': 1},
        'chest': {'is_opened': True},
    }
    map_state = {
        'run_id': 'test', 'screen': 'MAP', 'in_combat': False,
        'available_actions': ['choose_map_node'], 'run': {'floor': 2},
        'map': {'available_nodes': [{'index': 0, 'row': 2, 'col': 0, 'node_type': 'Unknown'}]},
    }

    class SettlingMapMod(FakeMod):
        def __init__(self):
            super().__init__()
            self.states = [map_state, map_state]

        def state(self):
            if self.states:
                return copy.deepcopy(self.states.pop(0))
            return copy.deepcopy(map_state)

    mod = SettlingMapMod()
    bridge = LiveClientBridge(mod)
    result = bridge._wait_for_decision_boundary('proceed', before, map_state)
    assert result['screen'] == 'MAP'
    # The response frame is the first observation; settlement consumes one
    # follow-up frame and returns only after it matches the response.
    assert len(mod.states) == 1


def test_explicit_transaction_budget_is_forwarded_once_and_recorded():
    class BudgetMod(FakeMod):
        def __init__(self):
            super().__init__()
            self.timeouts = []

        def action(self, action, **params):
            self.timeouts.append(params.pop('timeout_s', None))
            return super().action(action, **params)

    mod = BudgetMod()
    bridge = LiveClientBridge(mod)
    after = bridge.execute_headless_action(
        'combat_play', 'end_turn', {}, mod.state(),
        decision_telemetry={'timing_budget_ms': 250},
    )

    assert after['turn'] == 2
    assert len(mod.timeouts) == 1
    assert 0 < mod.timeouts[0] <= 0.25
    assert bridge.last_action['transaction_budget_explicit'] is True
    assert bridge.last_action['timing_breakdown']['transaction_budget_ms'] == 250.0
    assert bridge.last_action['timing_breakdown']['client_transaction_ms'] < 250.0


def test_deadline_before_submit_is_cancelled_without_client_input():
    class SlowPreflightMod(FakeMod):
        def state(self):
            time.sleep(0.02)
            return super().state()

    mod = SlowPreflightMod()
    bridge = LiveClientBridge(mod)

    with pytest.raises(ModApiError, match='Transaction deadline expired'):
        bridge.execute_headless_action(
            'combat_play', 'end_turn', {}, mod.state(),
            decision_telemetry={'timing_budget_ms': 1},
        )

    assert mod.actions == []
    assert bridge.last_action['status'] == 'cancelled'
    assert bridge.last_action['verification'] == 'NOT_EXECUTED'


def test_manual_pause_does_not_consume_action_budget():
    mod = FakeMod()
    bridge = LiveClientBridge(mod)
    bridge.before_action = lambda _record: time.sleep(0.02)

    after = bridge.execute_headless_action(
        'combat_play', 'end_turn', {}, mod.state(),
        decision_telemetry={'timing_budget_ms': 10},
    )

    assert after['turn'] == 2
    assert bridge.last_action['status'] == 'completed'


def test_nonfinite_transaction_budget_is_rejected():
    mod = FakeMod()
    bridge = LiveClientBridge(mod)
    with pytest.raises(ModApiError, match='budget must be positive'):
        bridge.execute_headless_action(
            'combat_play', 'end_turn', {}, mod.state(),
            decision_telemetry={'timing_budget_ms': float('nan')},
        )


def test_reward_state_enrichment_uses_remaining_state_budget(monkeypatch):
    adapter = Sts2ModAdapter()
    timeouts = []

    def request(_method, path, _body=None, **kwargs):
        timeouts.append((path, kwargs.get('timeout_s')))
        if path == '/state':
            time.sleep(0.01)
            return {'state_version': 1, 'run_id': 'run', 'screen': 'REWARD',
                    'available_actions': [],
                    'reward': {'pending_card_choice': False, 'rewards': []}}
        return {'contract': 'native-reward-items-v1', 'reward_set_id': 7,
                'pending_card_choice': False, 'rewards': []}

    monkeypatch.setattr(adapter, '_request', request)
    assert adapter.state(timeout_s=0.1)['reward']['reward_set_id'] == 7
    assert [path for path, _timeout in timeouts] == ['/state', '/reward-state']
    assert 0 < timeouts[1][1] < timeouts[0][1] <= 0.1


def test_invalid_multi_selection_is_cancelled_before_any_click():
    mod = FakeMod()
    mod.value = {'run_id': 'run', 'screen': 'CARD_SELECTION',
                 'available_actions': ['select_deck_card'],
                 'selection': {'kind': 'deck_card_select',
                               'cards': [{'index': 0, 'card_id': 'STRIKE'}]}}
    bridge = LiveClientBridge(mod)

    with pytest.raises(ModApiError, match='not visible'):
        bridge.execute_client_action('select_deck_cards', {'indices': [0, 1]}, mod.state())

    assert mod.actions == []
    assert bridge.last_action['status'] == 'cancelled'


def test_state_reads_share_the_transaction_deadline():
    class BudgetMod(FakeMod):
        def __init__(self):
            super().__init__()
            self.timeouts = []

        def state(self, *, timeout_s=None):
            self.timeouts.append(timeout_s)
            return super().state()

    mod = BudgetMod()
    bridge = LiveClientBridge(mod)
    bridge.execute_headless_action(
        'combat_play', 'end_turn', {}, mod.state(),
        decision_telemetry={'timing_budget_ms': 250},
    )

    bounded = [timeout for timeout in mod.timeouts if timeout is not None]
    assert len(bounded) >= 2
    assert all(0 < timeout <= 0.25 for timeout in bounded)


def test_native_lifecycle_is_recorded_for_non_card_actions():
    class LifecycleMod(FakeMod):
        def __init__(self):
            super().__init__()
            self.revision = 4
            self.lifecycle_timeouts = []

        def action_lifecycle(self, **kwargs):
            self.lifecycle_timeouts.append(kwargs.get('timeout_s'))
            return {
                'schema': 'sts2.native_action_lifecycle.v1',
                'epoch': 2,
                'revision': self.revision,
                'next_action_id': 9,
                'queue_empty': True,
                'actions': [],
            }

        def action(self, action, **params):
            self.revision += 1
            return super().action(action, **params)

    mod = LifecycleMod()
    bridge = LiveClientBridge(mod)
    bridge.execute_headless_action(
        'combat_play', 'end_turn', {}, mod.state(),
        decision_telemetry={'timing_budget_ms': 250},
    )

    evidence = bridge.last_action['native_lifecycle']
    assert evidence['epoch'] == 2
    assert evidence['revision_before'] == 4
    assert evidence['revision_after'] == 5
    assert bridge.last_action['native_preflight_stamp'][:2] == (2, 4)
    assert bridge.last_action['native_postflight_stamp'][:2] == (2, 5)
    assert mod.lifecycle_timeouts
    assert all(timeout is not None and 0 < timeout <= 0.25
               for timeout in mod.lifecycle_timeouts)


def test_visible_delay_is_explicit_and_has_no_state_work(tmp_path, monkeypatch):
    mod = FakeMod()
    order = []
    original_state = mod.state

    def state():
        order.append('state')
        return original_state()

    mod.state = state
    monkeypatch.setattr('controller.live_client_bridge.time.sleep',
                        lambda seconds: order.append(('delay', seconds)))
    bridge = LiveClientBridge(mod, JsonlSessionLog(tmp_path / 'session.jsonl'),
                              visible_delay_ms=200)
    bridge.execute_headless_action('combat_play', 'end_turn', {}, mod.state())

    assert ('delay', 0.2) not in order
    state_calls_after_settlement = order.count('state')
    order.append('verified')
    bridge.pace_after_verification(verification='test_checkpoint')
    assert order[-2:] == ['verified', ('delay', 0.2)]
    assert order.count('state') == state_calls_after_settlement
    timing = bridge.last_action['timing_breakdown']
    assert timing['visible_pacing_target_ms'] == 200.0
    assert timing['visible_pacing_ms'] >= 0.0
    assert bridge.last_action['pacing_after_verification'] == 'test_checkpoint'


def test_visible_delay_requires_a_verification_label():
    bridge = LiveClientBridge(FakeMod(), visible_delay_ms=200)
    with pytest.raises(ValueError, match='verification label'):
        bridge.pace_after_verification(verification='')


@pytest.mark.parametrize(('delay_ms', 'expected_sleep'), [
    (0, []),
    (200, [0.2]),
    (700, [0.7]),
])
def test_visible_delay_value_only_controls_pacing(delay_ms, expected_sleep, monkeypatch):
    sleeps = []
    monkeypatch.setattr('controller.live_client_bridge.time.sleep', sleeps.append)
    bridge = LiveClientBridge(FakeMod(), visible_delay_ms=delay_ms)
    bridge.pace_after_verification(verification='test_checkpoint')
    assert sleeps == expected_sleep


def test_map_entry_waits_for_two_stable_populated_combat_snapshots():
    before = {
        'state_version': 1,
        'run_id': 'map-entry',
        'screen': 'MAP',
        'in_combat': False,
        'available_actions': ['choose_map_node'],
        'map': {'available_nodes': [{'index': 0, 'row': 1, 'col': 2}]},
    }
    loading = {
        **before,
        'state_version': 2,
        'screen': 'COMBAT',
        'in_combat': True,
        'turn': 1,
        'available_actions': ['end_turn'],
        'combat': {'enemies': []},
    }
    ready = copy.deepcopy(loading)
    ready['state_version'] = 3
    ready['available_actions'] = ['end_turn', 'play_card']
    ready['combat']['enemies'] = [{
        'enemy_id': 'TWIG_SLIME_S',
        'current_hp': 8,
        'max_hp': 8,
        'intents': [{'intent_type': 'Attack', 'damage': 4, 'hits': 1}],
    }]

    class MapTransitionMod:
        config = SimpleNamespace(timeout_s=1.0)
        last_call_ms = 0.0
        last_request_id = 'map-action'

        def __init__(self):
            self.submitted = False
            self.polls = 0

        def state(self):
            if not self.submitted:
                return copy.deepcopy(before)
            self.polls += 1
            return copy.deepcopy(loading if self.polls == 1 else ready)

        def action(self, action, **params):
            self.submitted = True
            return {'state': copy.deepcopy(loading)}

    mod = MapTransitionMod()
    after = LiveClientBridge(mod).execute_client_action(
        'choose_map_node', {'option_index': 0}, expected_client_state=before,
    )
    assert after['combat']['enemies'][0]['enemy_id'] == 'TWIG_SLIME_S'
    assert mod.polls >= 3


def test_event_entry_waits_for_resolved_dynamic_values_and_stability():
    before = {
        'state_version': 1,
        'run_id': 'event-entry',
        'screen': 'MAP',
        'in_combat': False,
        'available_actions': ['choose_map_node'],
        'map': {'available_nodes': [{'index': 0, 'row': 1, 'col': 2}]},
    }
    loading = {
        'state_version': 2,
        'run_id': 'event-entry',
        'screen': 'EVENT',
        'in_combat': False,
        'available_actions': ['choose_event_option'],
        'run': {'floor': 2, 'current_hp': 72, 'max_hp': 80, 'gold': 122},
        'event': {
            'event_id': 'BRAIN_LEECH',
            'options': [
                {'index': 0, 'text_key': 'SHARE_KNOWLEDGE',
                 'title': 'Share knowledge',
                 'description': 'Choose {CardChoiceCount} card'},
                {'index': 1, 'text_key': 'RIP', 'title': 'Rip it out',
                 'description': 'Lose {RipHpLoss} HP'},
            ],
        },
    }
    ready = copy.deepcopy(loading)
    ready['state_version'] = 3
    ready['event']['options'][0]['description'] = 'Choose 1 card'
    ready['event']['options'][1]['description'] = 'Lose 5 HP'

    class EventTransitionMod:
        config = SimpleNamespace(timeout_s=1.0)
        last_call_ms = 0.0
        last_request_id = 'event-action'

        def __init__(self):
            self.submitted = False
            self.polls = 0

        def state(self):
            if not self.submitted:
                return copy.deepcopy(before)
            self.polls += 1
            return copy.deepcopy(loading if self.polls == 1 else ready)

        def action(self, action, **params):
            self.submitted = True
            return {'state': copy.deepcopy(loading)}

    mod = EventTransitionMod()
    after = LiveClientBridge(mod).execute_client_action(
        'choose_map_node', {'option_index': 0}, expected_client_state=before,
    )
    assert after['event']['options'][1]['description'] == 'Lose 5 HP'
    assert mod.polls >= 3


def test_resume_accepts_only_event_template_resolution():
    unresolved = {
        'run_id': 'event-entry',
        'screen': 'EVENT',
        'available_actions': ['choose_event_option'],
        'run': {'current_hp': 72, 'max_hp': 80, 'gold': 122},
        'event': {
            'event_id': 'BRAIN_LEECH',
            'options': [
                {'index': 0, 'text_key': 'SHARE_KNOWLEDGE',
                 'title': 'Share knowledge', 'description': 'Choose {Count} card',
                 'is_locked': False, 'is_proceed': False, 'will_kill_player': False},
                {'index': 1, 'text_key': 'RIP', 'title': 'Rip it out',
                 'description': 'Lose {HpLoss} HP', 'is_locked': False,
                 'is_proceed': False, 'will_kill_player': False},
            ],
        },
    }
    resolved = copy.deepcopy(unresolved)
    resolved['event']['options'][0]['description'] = 'Choose 1 card'
    resolved['event']['options'][1]['description'] = 'Lose 5 HP'
    assert event_resolution_equivalent(unresolved, resolved)

    changed_run = copy.deepcopy(resolved)
    changed_run['run']['current_hp'] = 71
    assert not event_resolution_equivalent(unresolved, changed_run)

    changed_option = copy.deepcopy(resolved)
    changed_option['event']['options'][1]['text_key'] = 'OTHER'
    assert not event_resolution_equivalent(unresolved, changed_option)


def test_cancelled_client_only_action_does_not_block_resume():
    from scripts.live_run_demo import _blocking_client_only_segments

    segment = {'segment_id': 1, 'status': 'AWAITING_BOUNDARY', 'start_sequence': 30}
    cancelled = {
        'client_only_segments': [segment],
        'actions': [{'sequence': 30, 'status': 'cancelled',
                     'verification': 'NOT_EXECUTED'}],
    }
    assert _blocking_client_only_segments(cancelled) == []

    executed = copy.deepcopy(cancelled)
    executed['actions'][0].update(
        status='completed', verification='CLIENT_ONLY_UNVERIFIED',
    )
    assert _blocking_client_only_segments(executed) == [segment]


def test_end_turn_waits_for_next_player_boundary():
    mod = FakeMod()
    def action(name, **params):
        response = mod.state()
        response['available_actions'] = []
        mod.value['turn'] += 1
        return {'state': response}
    mod.action = action
    bridge = LiveClientBridge(mod)
    after = bridge.execute_headless_action('combat_play', 'end_turn', {}, mod.state())
    assert after['turn'] == 2


def test_complete_match_and_real_difference():
    left, right = canonical(), canonical()
    assert compare_checkpoints(left, right).status == 'PASS'
    right['player']['energy'] = 2
    result = compare_checkpoints(left, right)
    assert result.status == 'FAIL'
    assert result.differences[0]['path'] == 'player.energy'


def test_new_act_map_allows_null_current_coordinate_but_still_compares_it():
    run = {'character_id': 'IRONCLAD', 'ascension': 0, 'act_id': '1', 'floor': 17, 'current_hp': 9,
           'max_hp': 80, 'gold': 203, 'boss_id': 'THE_INSATIABLE_BOSS',
           'deck': [], 'relics': [], 'potions': []}
    client = client_map_checkpoint({'screen': 'MAP', 'run': run, 'map': {
        'current_node': None,
        'boss_node': {'row': 16, 'col': 3},
        'available_nodes': [{'row': 0, 'col': 3, 'node_type': 'Ancient'}],
        'nodes': [],
    }})
    shadow = headless_map_checkpoint({
        'decision': 'map_select',
        'ascension': 0,
        'player': {'name': 'Ironclad', 'hp': 9, 'max_hp': 80, 'gold': 203,
                   'deck': [], 'relics': [], 'potions': []},
        'context': {'act': 2, 'floor': 0, 'boss': {'id': 'THE_INSATIABLE_BOSS'}},
        'choices': [{'row': 0, 'col': 3, 'type': 'Ancient'}],
    }, {
        'type': 'map',
        'current_coord': None,
        'boss': {'row': 16, 'col': 3},
        'rows': [],
    }, 'test')
    assert client['current_coord'] is None
    assert compare_checkpoints(client, shadow).status == 'PASS'

    del shadow['current_coord']
    assert compare_checkpoints(client, shadow).status == 'INCOMPLETE'


@pytest.mark.parametrize('path', [('run', 'hp'), ('run', 'deck'), ('player', 'powers'), ('player', 'energy')])
def test_both_missing_is_incomplete(path):
    checkpoint = canonical()
    del checkpoint[path[0]][path[1]]
    assert compare_checkpoints(checkpoint, checkpoint).status == 'INCOMPLETE'


def test_empty_raw_states_never_pass():
    left = client_combat_checkpoint({'run': {'ascension': 0}, 'combat': {'player': {}}, 'in_combat': True})
    right = headless_combat_checkpoint({'player': {}, 'context': {}}, {'combat': {'player': {}}}, 'test')
    assert compare_checkpoints(left, right).status == 'INCOMPLETE'


def test_x_costs_use_symbolic_cost_but_fixed_costs_remain_exact():
    raw = {'run': {}, 'in_combat': True, 'combat': {'player': {}, 'hand': [
        {'index': 0, 'card_id': 'WHIRLWIND', 'costs_x': True, 'energy_cost': 0},
        {'index': 1, 'card_id': 'BASH', 'costs_x': False, 'energy_cost': 2},
        {'index': 2, 'card_id': 'DAZED', 'costs_x': False, 'energy_cost': -1}]}}
    client = client_combat_checkpoint(raw)
    shadow = headless_combat_checkpoint({'player': {}, 'context': {}, 'hand': [
        {'index': 0, 'id': 'CARD.WHIRLWIND', 'costs_x': True, 'cost': 3},
        {'index': 1, 'id': 'CARD.BASH', 'costs_x': False, 'cost': 2},
        {'index': 2, 'id': 'CARD.DAZED', 'costs_x': False, 'cost': 0, 'display_cost': -1}]},
        {'combat': {'player': {}, 'hand': [{'upgrade': 0}, {'upgrade': 0}, {'upgrade': 0}]}}, 'test')
    assert [c['cost'] for c in client['hand']] == ['X', 2, -1]
    assert [c['cost'] for c in shadow['hand']] == ['X', 2, -1]


def test_failure_report_is_not_green_success():
    document = _render({'status': 'FAIL', 'error': '<bad>', 'identity': {'run_id': 'new-run'}}, [], [])
    assert 'EFCUTG3JXA' not in document
    assert '0/0 PASS' not in document
    assert 'new-run' in document
    assert '&lt;bad&gt;' in document
    assert 'FAIL' in document


def test_reanchor_is_not_counted_as_mismatch():
    document = _render({'status': 'COMPLETED'}, [], [{'status': 'REANCHORED_PASS'}])
    assert '0 异常 / 1 重新对齐' in document


def test_missing_historical_timing_is_not_zero():
    from scripts.render_live_run_report import _combat_summary
    summary = _combat_summary(1, [{'action': 'end_turn'}], [{'action': 'end_turn'}], [])
    assert summary['avg_search_ms'] is None
    assert summary['searches'] == 0
    assert '未记录' in _render({'status': 'STOPPED'}, [summary], [])


@pytest.fixture
def manager(tmp_path, monkeypatch):
    from scripts.live_dashboard import DashboardManager
    monkeypatch.setattr(threading.Thread, 'start', lambda self: None)
    value = DashboardManager(tmp_path, tmp_path / 'logs' / 'live_dashboard', 'http://localhost:9999')
    value.mod = FakeMod()
    value.observer = value.mod
    return value


def test_dead_worker_cannot_override_manager_status(manager, tmp_path):
    manager.status_file = tmp_path / 'status.json'
    write_json(manager.status_file, {'mode': 'running'})
    manager.manager_mode = 'error'
    assert manager.status()['mode'] == 'error'


def test_inactive_resume_is_rejected(manager):
    with pytest.raises(ValueError, match='No active runner'):
        manager.command('resume', {})


def test_session_paths_cannot_escape_logs(manager, tmp_path):
    write_json(tmp_path / 'outside' / 'run_report.json', {})
    with pytest.raises(ValueError):
        manager.session_path('../outside')
    write_json(tmp_path / 'logs' / 'example' / 'run_report.json', {'status': 'FAIL'})
    assert manager.sessions()[0]['id'] == 'example'


def test_launch_passes_mod_url_and_starts_paused(manager, tmp_path, monkeypatch):
    import scripts.live_dashboard as dashboard
    launched = []
    monkeypatch.setattr(dashboard.subprocess, 'Popen', lambda command, **kw: launched.append(command) or SimpleNamespace(poll=lambda: None))
    manager._launch_runner(tmp_path, tmp_path / 'anchor.save')
    try:
        assert launched[0][launched[0].index('--url') + 1] == 'http://localhost:9999'
        assert launched[0][launched[0].index('--depth') + 1] == str(manager.config['depth'])
        assert launched[0][launched[0].index('--max-search-ms') + 1] == '20000'
        assert launched[0][launched[0].index('--max-workers') + 1] == str(manager.config['max_workers'])
        assert launched[0][launched[0].index('--worker-mode') + 1] == 'adaptive'
        assert Path(launched[0][launched[0].index('--deck-profile') + 1]).name == 'deck_profile.json'
        assert launched[0][1:3] == ['-X', 'utf8']
        assert '--verify-checkpoints' in launched[0]
        assert json.loads((tmp_path / 'control.json').read_text())['desired'] == 'paused'
    finally:
        manager.output_handle.close()


def test_dashboard_search_depth_is_bounded(manager):
    manager._update_config({'depth': 0})
    assert manager.config['depth'] == 1
    manager._update_config({'depth': 99})
    assert manager.config['depth'] == 16
    manager._update_config({'depth': 7})
    assert manager.config['depth'] == 7


def test_dashboard_worker_config_is_bounded_and_validated(manager):
    manager._update_config({'max_workers': 99, 'worker_mode': 'fixed'})
    assert manager.config['max_workers'] == 32
    assert manager.config['worker_mode'] == 'fixed'
    with pytest.raises(ValueError, match='Unsupported worker mode'):
        manager._update_config({'worker_mode': 'turbo'})


def test_new_run_can_leave_game_over_through_supported_action(manager, monkeypatch):
    calls = []
    manager.preparation_live = SimpleNamespace()
    monkeypatch.setattr(
        manager,
        '_prepare_action',
        lambda action, **params: calls.append(action) or {
            'state': {'screen': 'MAIN_MENU', 'available_actions': ['open_character_select']}
        },
    )
    result = manager._ensure_main_menu_for_new_run({
        'screen': 'GAME_OVER', 'available_actions': ['return_to_main_menu']
    })
    assert result['screen'] == 'MAIN_MENU'
    assert calls == ['return_to_main_menu']


def test_runner_partial_combat_survives_step_then_stop(tmp_path, monkeypatch):
    import scripts.live_run_demo as runner
    control_path = tmp_path / 'control.json'
    write_json(control_path, {'desired': 'step', 'command_id': 'one'})
    control = ManagedControl(control_path, None)
    report = {'combats': [], 'parity_checkpoints': []}
    log = JsonlSessionLog(tmp_path / 'session.jsonl')
    log.on_event = SessionJournal(report, lambda: write_json(tmp_path / 'run_report.json', report), control).record
    mod = FakeMod()
    live = LiveClientBridge(mod, log)
    live.before_action = control.before_action
    def normalized(state):
        result = canonical()
        result['turn'] = state.get('turn', 1)
        return result
    monkeypatch.setattr(runner, 'client_combat_checkpoint', normalized)
    monkeypatch.setattr(runner, 'headless_combat_checkpoint', lambda state, search, run_id: normalized(state))
    encounters = []
    def decide(_cli, _search_state, cfg, _plan):
        encounters.append(cfg.spec.encounter)
        return SimpleNamespace(action='end_turn', payload={}, timing={'search_ms': 0.5},
            fell_back=False, raw_retry_used=False, search_failed=False, nodes=2,
            search_score=1.0, reused_plan=False, searcher_timing_summary={},
            chosen_summary={'action_type': 'end_turn'}, root_candidates=[])
    monkeypatch.setattr(runner, 'decide_combat_action', decide)
    class Cli:
        turn = 1
        def get_search_state(self, **kwargs):
            return {'combat_state_for_search': {
                'combat': {'player': {}, 'encounter_id': 'TEST_ENCOUNTER'}
            }}
        def action(self, *args, **kwargs):
            self.turn += 1
            return {
                'decision': 'combat_play', 'context': {}, 'turn': self.turn,
                'combat': {},
            }
    cli = Cli()
    args = SimpleNamespace(max_actions_per_combat=5, depth=1, chance_depth=0, max_search_ms=100)
    errors = []
    def work():
        try:
            runner._play_combat(cli, None, live, mod.state(), {
                                    'context': {}, 'turn': 1,
                                    # The decision state may omit encounter_id;
                                    # the canonical search state above owns it.
                                    'combat': {},
                                },
                                'test', 1, args, log, report, control)
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=work)
    thread.start()
    try:
        wait_until(lambda: control.state.get('mode') == 'paused' or bool(errors))
        assert not errors
        assert encounters and set(encounters) == {'TEST_ENCOUNTER'}
        assert len(mod.actions) == 1
        partial = json.loads((tmp_path / 'run_report.json').read_text())
        assert partial['combats'][0]['action_count'] == 1
        assert partial['actions'][0]['verification'] == 'COVERED_FIELDS_MATCH'
        assert partial['actions'][0]['shadow_ms'] >= 0
        assert partial['actions'][0]['timing_breakdown']['shadow_total_ms'] >= 0
        assert partial['actions'][0]['timing_breakdown']['shadow_action_ms'] >= 0
        assert partial['actions'][0]['compare_ms'] >= 0
        assert partial['actions'][1]['status'] == 'pending'
    finally:
        write_json(control_path, {'desired': 'stop'})
        thread.join(2)
    assert len(errors) == 1 and isinstance(errors[0], ControlledStop)
    assert report['actions'][1]['status'] == 'cancelled'



def test_before_action_checkpoint_does_not_claim_an_older_pending_action(tmp_path):
    import scripts.live_run_demo as runner
    report = {
        'actions': [{
            'sequence': 7,
            'status': 'completed',
            'verification': 'PENDING',
        }],
        'parity_checkpoints': [],
    }
    log = JsonlSessionLog(tmp_path / 'session.jsonl')
    result = SimpleNamespace(
        client={'checkpoint': 'combat_play'},
        headless={'checkpoint': 'combat_play'},
        status='PASS',
        client_digest='client',
        headless_digest='headless',
        differences=[],
    )

    runner._record_comparison(
        result,
        'combat_2_before_action_1',
        log,
        report,
        attach_pending_action=False,
    )
    assert report['actions'][0]['verification'] == 'PENDING'
    assert 'sequence' not in report['parity_checkpoints'][-1]

    runner._record_comparison(result, 'combat_2_after_action_1', log, report)
    assert report['actions'][0]['verification'] == 'COVERED_FIELDS_MATCH'
    assert report['parity_checkpoints'][-1]['sequence'] == 7

def test_reward_options_are_checked_before_selection():
    from controller.engine_parity import client_reward_checkpoint, headless_reward_checkpoint
    client = client_reward_checkpoint({'screen': 'REWARD', 'available_actions': ['resolve_rewards'], 'reward': {'pending_card_choice': True,
        'card_options': [{'index': 0, 'card_id': 'CARD.BASH'}]}})
    shadow = headless_reward_checkpoint({'decision': 'card_reward', 'cards': [{'index': 0, 'id': 'CARD.STRIKE'}]})
    assert compare_checkpoints(client, shadow).status == 'FAIL'
    incomplete = client_reward_checkpoint({'screen': 'REWARD', 'reward': {}})
    assert compare_checkpoints(incomplete, incomplete).status == 'INCOMPLETE'


def test_preparation_failure_creates_a_report(manager, tmp_path):
    manager._prepare_and_launch()
    reports = list((tmp_path / 'logs').rglob('run_report.json'))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding='utf-8'))
    assert report['status'] == 'FAIL'
    assert 'error' in report


def test_bootstrap_failure_is_persisted_and_engine_closed(tmp_path, monkeypatch):
    import scripts.live_run_demo as runner
    stopped = []
    class BrokenCli:
        def __init__(self, *args): pass
        def start(self): raise RuntimeError('boot failed')
        def stop(self): stopped.append(True)
    monkeypatch.setattr(runner, 'Sts2CliAdapter', BrokenCli)
    monkeypatch.setattr('sys.argv', ['runner', '--anchor-map-save', str(tmp_path / 'anchor.save'),
                                    '--session-dir', str(tmp_path), '--status-file', str(tmp_path / 'status.json')])
    with pytest.raises(RuntimeError, match='boot failed'):
        runner.main()
    assert stopped == [True]
    report = json.loads((tmp_path / 'run_report.json').read_text())
    assert report['status'] == 'FAIL'
    assert report['error'] == 'boot failed'
    assert json.loads((tmp_path / 'status.json').read_text())['mode'] == 'error'
