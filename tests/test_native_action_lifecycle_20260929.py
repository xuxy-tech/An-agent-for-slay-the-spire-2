"""A native play-card choice is a pause of its parent action, not completion."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from cli.sts2_mod_adapter import ModApiError
from controller.action_transaction import make_mirrored_transaction
from controller.live_client_bridge import LiveClientBridge, StaleClientStateError
from controller.live_client_bridge import JsonlSessionLog
from controller.live_session import ManagedControl, SessionJournal, recover_report_tail
from controller.transaction_runtime import execute_transaction
from scripts.live_run_demo import _resume_shadow, _settle_parent_choice


def combat_state(strength=0):
    return {'run_id': 'run', 'screen': 'COMBAT', 'in_combat': True, 'turn': 1,
            'available_actions': ['play_card', 'end_turn'],
            'run': {'current_hp': 70},
            'combat': {'hand': [{'index': 0, 'card_id': 'BRAND'}],
                       'player': {'strength': strength}, 'enemies': []}}


def choice_state():
    state = combat_state()
    state.update(screen='CARD_SELECTION', available_actions=['select_deck_card'],
                 selection={'kind': 'combat_deck_card_select',
                            'cards': [{'index': 0, 'card_id': 'STRIKE_IRONCLAD'},
                                      {'index': 1, 'card_id': 'DEFEND_IRONCLAD'}]})
    return state


def rng(counter):
    return {'schema_version': 1, 'complete': True, 'run_seed': 'seed',
            'run_streams': {'test': {'counter': counter, 'seed': 1,
                                     's0': 1, 's1': 2, 's2': 3, 's3': 4}},
            'players': []}


class BrandMod:
    def __init__(self, finish=True, early_selection=False):
        self.config = SimpleNamespace(timeout_s=0.06)
        self.value = combat_state()
        self.phase = 'before'
        self.revision = 1
        self.finish = finish
        self.early_selection = early_selection
        self.last_request_id = 'request'

    def state(self):
        return copy.deepcopy(self.value)

    def action_lifecycle(self):
        states = {'before': [], 'running': [{'id': 7, 'semantic_action': 'play_card',
                                            'card_id': 'BRAND', 'state': 'Executing',
                                            'status': 'running', 'pause_type': None,
                                            'completion_finished': False, 'failed': False}],
                  'choice': [{'id': 7, 'semantic_action': 'play_card',
                                           'card_id': 'BRAND',
                                           'state': 'GatheringPlayerChoice',
                                           'status': 'awaiting_input',
                                           'pause_type': 'player_choice',
                                           'completion_finished': False, 'failed': False}],
                  'done': [{'id': 7, 'semantic_action': 'play_card', 'card_id': 'BRAND',
                            'state': 'Finished', 'status': 'completed',
                            'pause_type': None, 'completion_finished': True,
                            'failed': False}]}
        return {'schema': 'sts2.native_action_lifecycle.v1', 'epoch': 3,
                'revision': self.revision, 'next_action_id': 7 if self.phase == 'before' else 8,
                'queue_empty': self.phase in {'before', 'done'},
                'actions': states[self.phase]}

    def submit_action(self, action, **params):
        if action == 'play_card':
            self.phase = 'running' if self.early_selection else 'choice'
            self.value = choice_state()
        elif action == 'select_deck_card':
            if self.finish:
                self.phase = 'done'
                self.value = combat_state(strength=2)
        else:
            raise AssertionError(action)
        self.revision += 1
        return {'state': self.state(), 'status': 'pending' if self.phase == 'choice'
                else 'completed', 'stable': self.phase == 'done'}

    action = submit_action


class ReportLog:
    def __init__(self, report):
        self.report = report

    def write(self, row):
        if row.get('event') == 'action_pending':
            self.report['actions'].append(row)


def test_brand_parent_waits_for_choice_and_final_native_completion():
    mod = BrandMod()
    report = {'actions': []}
    live = LiveClientBridge(mod, ReportLog(report))
    live._capture_rng = lambda **_kwargs: rng(0 if mod.phase == 'before' else 1
                                             if mod.phase == 'choice' else 2)
    play = make_mirrored_transaction('play_card', {'card_index': 0},
                                     shadow_action='play_card', shadow_args={'card_index': 0})
    first = execute_transaction(play, live=live, client_state=mod.state(),
                                log=None, report=report,
                                advance_shadow=lambda *_: {'decision': 'card_select'},
                                operation_label='play BRAND')
    parent = report['actions'][0]
    assert first.shadow_state['decision'] == 'card_select'
    assert parent['client_completed'] is False
    assert parent['shadow_completed'] is False
    assert parent['boundary_verified'] is False
    assert parent['transaction_status'] == 'both_awaiting_input'
    assert parent['native_action_id'] == 7
    assert first.client_state['combat']['player']['strength'] == 0

    select = make_mirrored_transaction('select_deck_card', {'option_index': 1},
                                       shadow_action='select_cards',
                                       shadow_args={'indices': [1]})
    second = execute_transaction(select, live=live, client_state=first.client_state,
                                 log=None, report=report,
                                 advance_shadow=lambda *_: {'decision': 'combat_play'},
                                 operation_label='resolve BRAND choice')
    assert second.client_state['combat']['player']['strength'] == 2
    assert report['actions'][1]['parent_sequence'] == parent['sequence']
    assert report['actions'][1]['native_action_phase'] == 'completed'
    assert live.pending_native_parent is None
    parent['shadow_rng_before'] = rng(0)
    report['actions'][1]['shadow_rng_after'] = rng(2)
    settled = _settle_parent_choice(report, second.client_state)
    assert settled is parent
    assert parent['client_completed'] is True
    assert parent['shadow_completed'] is True
    assert parent['rng_verified'] is True
    assert parent['settlement_phase'] == 'BOTH_SETTLED'
    assert parent['boundary_verified'] is False


def test_native_choice_pause_survives_report_and_journal_replay(tmp_path):
    mod = BrandMod()
    report = {'actions': []}
    log = JsonlSessionLog(tmp_path / 'session.jsonl', compact_lifecycle=True)
    log.on_event = SessionJournal(report, lambda: None, ManagedControl(None, None)).record
    live = LiveClientBridge(mod, log)
    live._capture_rng = lambda **_kwargs: rng(0 if mod.phase == 'before' else 1
                                             if mod.phase == 'choice' else 2)
    play = make_mirrored_transaction('play_card', {'card_index': 0},
                                     shadow_action='play_card', shadow_args={'card_index': 0})

    def pause_shadow(_action, _payload, _log, active_report):
        active_report['actions'][-1]['shadow_rng_before'] = rng(0)
        return {'decision': 'card_select'}

    execute_transaction(play, live=live, client_state=mod.state(), log=log,
                        report=report,
                        advance_shadow=pause_shadow,
                        operation_label='play BRAND')

    parent = report['actions'][0]
    assert parent['event'] == 'action_awaiting_input'
    assert parent['status'] == 'awaiting_input'
    assert parent['transaction_status'] == 'both_awaiting_input'
    assert parent['native_action_id'] == 7
    recovered = recover_report_tail({'actions': [], 'session_log_offset': 0}, log.path)
    replayed = recovered['actions'][0]
    assert replayed['status'] == 'awaiting_input'
    assert replayed['transaction_status'] == 'both_awaiting_input'
    assert replayed['shadow_completed'] is False
    assert replayed['native_action_id'] == 7
    assert recovered['pending_action']['sequence'] == replayed['sequence']
    assert replayed['transaction']['shadow']['action'] == 'play_card'
    assert replayed['shadow_rng_before'] == rng(0)

    select = make_mirrored_transaction('select_deck_card', {'option_index': 1},
                                       shadow_action='select_cards',
                                       shadow_args={'indices': [1]})

    def finish_shadow(_action, _payload, _log, active_report):
        active_report['actions'][-1]['shadow_rng_after'] = rng(2)
        return {'decision': 'combat_play'}

    second = execute_transaction(select, live=live, client_state=mod.state(), log=log,
                                 report=report, advance_shadow=finish_shadow,
                                 operation_label='resolve BRAND choice')
    settled = _settle_parent_choice(report, second.client_state)
    log.write({'event': 'parent_action_settled', 'sequence': settled['sequence'],
               'parent_record': settled})
    replayed = recover_report_tail({'actions': [], 'session_log_offset': 0}, log.path)['actions'][0]
    assert replayed['status'] == 'completed'
    assert replayed['event'] == 'live_action'
    assert replayed['transaction_status'] == 'both_settled_awaiting_checkpoint'


def _paused_resume_fixture(tmp_path):
    mod = BrandMod()
    mod.rng_snapshot = lambda: rng(0 if mod.phase == 'before' else 1
                                  if mod.phase == 'choice' else 2)
    source = {'actions': []}
    live = LiveClientBridge(mod, ReportLog(source))
    live._capture_rng = lambda **_kwargs: rng(0 if mod.phase == 'before' else 1)
    play = make_mirrored_transaction('play_card', {'card_index': 0},
                                     shadow_action='play_card', shadow_args={'card_index': 0})
    execute_transaction(play, live=live, client_state=mod.state(), log=None,
                        report=source, advance_shadow=lambda *_: {'decision': 'card_select'},
                        operation_label='play BRAND')
    anchor = tmp_path / 'anchor.save'
    anchor.write_bytes(b'anchor')
    source['identity'] = {'run_id': 'run', 'anchor_sha256': hashlib.sha256(b'anchor').hexdigest()}
    source['schema_version'] = 3
    path = tmp_path / 'run_report.json'
    path.write_text(json.dumps(source), encoding='utf-8')
    args = SimpleNamespace(resume_report=path, anchor_map_save=anchor,
                           recover_pending_selection=False)
    return mod, args


def test_resume_native_choice_rebuilds_parent_ticket_only_after_evidence(tmp_path):
    mod, args = _paused_resume_fixture(tmp_path)
    live = LiveClientBridge(mod)
    calls = []
    report = {'actions': []}

    class Cli:
        def action(self, action, payload, **_kwargs):
            calls.append((action, payload))
            return {'decision': 'card_select'}

    resumed, _count = _resume_shadow(
        Cli(), args, mod.state(), report,
        JsonlSessionLog(tmp_path / 'resume.jsonl'),
        bootstrap_state={'decision': 'combat_play'}, live=live,
    )
    assert resumed['decision'] == 'card_select'
    assert calls == [('play_card', {'card_index': 0})]
    assert report['actions'][0]['transaction_status'] == 'both_awaiting_input'
    assert report['actions'][0]['recovered_parent_reference'] is True
    assert live.sequence == 1
    assert live.pending_native_parent == {
        'epoch': 3, 'id': 7, 'parent_sequence': 1, 'expected_card_id': 'BRAND',
    }

    select = make_mirrored_transaction('select_deck_card', {'option_index': 1},
                                       shadow_action='select_cards',
                                       shadow_args={'indices': [1]})
    live.log = ReportLog(report)
    second = execute_transaction(select, live=live, client_state=mod.state(),
                                 log=ReportLog(report), report=report,
                                 advance_shadow=lambda *_: {'decision': 'combat_play'},
                                 operation_label='resolve BRAND choice')
    assert report['actions'][1]['sequence'] == 2
    assert report['actions'][1]['parent_sequence'] == 1
    report['actions'][0]['shadow_rng_before'] = rng(0)
    report['actions'][1]['shadow_rng_after'] = rng(2)
    assert _settle_parent_choice(report, second.client_state) is report['actions'][0]


def test_repeated_resume_replays_pending_parent_once(tmp_path):
    mod, args = _paused_resume_fixture(tmp_path)

    class Cli:
        def __init__(self):
            self.calls = []

        def action(self, action, payload, **_kwargs):
            self.calls.append((action, payload))
            return {'decision': 'card_select'}

    first_report = {'actions': [], 'schema_version': 3}
    _resume_shadow(Cli(), args, mod.state(), first_report,
                   JsonlSessionLog(tmp_path / 'resume1.jsonl'),
                   bootstrap_state={'decision': 'combat_play'},
                   live=LiveClientBridge(mod))
    first_report['identity'] = {
        'run_id': 'run', 'anchor_sha256': hashlib.sha256(b'anchor').hexdigest(),
    }
    second_path = tmp_path / 'second_report.json'
    second_path.write_text(json.dumps(first_report), encoding='utf-8')
    second_args = SimpleNamespace(resume_report=second_path,
                                  anchor_map_save=args.anchor_map_save,
                                  recover_pending_selection=False)
    cli = Cli()

    _resume_shadow(cli, second_args, mod.state(), {'actions': []},
                   JsonlSessionLog(tmp_path / 'resume2.jsonl'),
                   bootstrap_state={'decision': 'combat_play'},
                   live=LiveClientBridge(mod))
    assert cli.calls == [('play_card', {'card_index': 0})]


def test_resume_native_choice_rejects_changed_native_revision(tmp_path):
    mod, args = _paused_resume_fixture(tmp_path)
    mod.revision += 1
    calls = []

    class Cli:
        def action(self, *_args, **_kwargs):
            calls.append(True)
            return {'decision': 'card_select'}

    with pytest.raises(RuntimeError, match='native action identity or pause changed'):
        _resume_shadow(Cli(), args, mod.state(), {'actions': []},
                       JsonlSessionLog(tmp_path / 'resume.jsonl'),
                       bootstrap_state={'decision': 'combat_play'},
                       live=LiveClientBridge(mod))
    assert calls == []


def test_parent_final_rng_difference_blocks_verification():
    parent = {'sequence': 1, 'transaction_status': 'both_awaiting_input',
              'native_action_id': 7, 'native_action_epoch': 3,
              'client_rng_before': rng(0), 'shadow_rng_before': rng(0)}
    child = {'sequence': 2, 'parent_sequence': 1, 'native_action_id': 7,
             'native_action_epoch': 3, 'native_action_phase': 'completed',
             'shadow_completed': True, 'client_rng_after': rng(2),
             'shadow_rng_after': rng(3)}
    with pytest.raises(RuntimeError, match='Parent combat action RNG diverged'):
        _settle_parent_choice({'actions': [parent, child]}, combat_state(strength=2))
    assert parent['rng_verified'] is False


def test_brand_timeout_keeps_parent_unknown_and_does_not_send_shadow_choice():
    mod = BrandMod(finish=False)
    report = {'actions': []}
    live = LiveClientBridge(mod, ReportLog(report))
    play = make_mirrored_transaction('play_card', {'card_index': 0},
                                     shadow_action='play_card', shadow_args={'card_index': 0})
    first = execute_transaction(play, live=live, client_state=mod.state(), log=None,
                                report=report, advance_shadow=lambda *_: {'decision': 'card_select'},
                                operation_label='play BRAND')
    select = make_mirrored_transaction('select_deck_card', {'option_index': 1},
                                       shadow_action='select_cards',
                                       shadow_args={'indices': [1]})
    shadow_calls = []
    with pytest.raises(ModApiError, match='completion is unknown'):
        execute_transaction(select, live=live, client_state=first.client_state,
                            log=None, report=report,
                            advance_shadow=lambda *args: shadow_calls.append(args),
                            operation_label='resolve BRAND choice')
    assert shadow_calls == []
    assert report['actions'][-1]['status'] == 'outcome_unknown'
    assert report['actions'][0]['transaction_status'] == 'both_awaiting_input'


def test_visible_selection_does_not_settle_native_action_still_running():
    mod = BrandMod(early_selection=True)
    report = {'actions': []}
    live = LiveClientBridge(mod, ReportLog(report))
    play = make_mirrored_transaction('play_card', {'card_index': 0},
                                     shadow_action='play_card', shadow_args={'card_index': 0})
    shadow_calls = []
    with pytest.raises(ModApiError, match='completion is unknown'):
        execute_transaction(play, live=live, client_state=mod.state(), log=None,
                            report=report,
                            advance_shadow=lambda *args: shadow_calls.append(args),
                            operation_label='play BRAND')
    assert shadow_calls == []
    assert report['actions'][-1]['settlement_phase'] == 'UNKNOWN'
    assert report['actions'][-1]['status'] == 'outcome_unknown'


def test_native_version_change_before_submission_cancels_without_click():
    mod = BrandMod()
    original = mod.action_lifecycle
    calls = 0

    def changing_lifecycle():
        nonlocal calls
        calls += 1
        if calls == 2:
            mod.revision += 1
        return original()

    mod.action_lifecycle = changing_lifecycle
    live = LiveClientBridge(mod)
    with pytest.raises(StaleClientStateError, match='pre-action state and RNG'):
        live.execute_client_action('play_card', {'card_index': 0}, mod.state())
    assert mod.phase == 'before'
    assert live.last_action['status'] == 'cancelled'
    assert live.last_action.get('input_submitted') is not True
