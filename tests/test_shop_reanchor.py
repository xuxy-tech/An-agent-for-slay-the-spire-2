import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from controller.action_transaction import make_transaction
from controller.live_client_bridge import StaleClientStateError
from controller.live_flow_runner import run_interactions
from scripts import live_run_demo


class FakeLog:
    def __init__(self):
        self.rows = []

    def write(self, row):
        self.rows.append(dict(row))


class FakeCli:
    def __init__(self):
        self.loads = []
        self.actions = []
        self._stderr_tail = []

    def load_save(self, path, **kwargs):
        self.loads.append((str(path), dict(kwargs)))
        return {'type': 'decision', 'decision': 'map_select'}

    def load_save_json(self, save_json, **kwargs):
        self.loads.append(('json', save_json, dict(kwargs)))
        return {'type': 'decision', 'decision': 'map_select'}

    def get_map(self):
        return {'rows': []}

    def action(self, action, payload, timeout_s=20):
        self.actions.append((action, dict(payload), timeout_s))
        return {'type': 'decision', 'decision': 'map_select'}

    def stop(self):
        pass

    def start(self):
        pass


class FakeControl:
    def checkpoint(self, *args, **kwargs):
        pass

    def publish(self, *args, **kwargs):
        pass


class FakeLive:
    def __init__(self, report, observed_state=None):
        self.report = report
        self.observed_state = observed_state
        self.sequence = 0
        self.transactions = []
        self.mod = SimpleNamespace(exact_save=lambda: {
            'schema': 'sts2.run_save.authoritative.v1',
            'sha256': 'client-save-sha',
            'save_json': '{"schema_version":1}',
        }, rng_snapshot=lambda: {'schema_version': 1})

    def observe(self):
        if self.observed_state is None:
            raise AssertionError('Test must provide the visible client state')
        return self.observed_state

    def pace_after_verification(self, **kwargs):
        return None

    def execute_transaction(self, transaction, **kwargs):
        self.sequence += 1
        self.transactions.append(transaction)
        self.report["actions"].append({"sequence": self.sequence})
        self.observed_state = {
            "screen": "GAME_OVER",
            "available_actions": ["return_to_main_menu"],
            "game_over": {"is_victory": True},
        }
        return self.observed_state


def test_stale_noncombat_decision_reclassifies_fresh_client_state(monkeypatch, tmp_path):
    import controller.interaction_flow as interaction_flow
    from controller.live_noncombat import ClientDecision

    monkeypatch.setattr(interaction_flow, 'choose_client_event', lambda state: ClientDecision(
        'choose_event_option', {'option_index': 0}, None,
        {'policy': 'explicit_proceed', 'option_id': 'PROCEED'},
    ))
    initial = {
        'screen': 'EVENT', 'run_id': 'run', 'run': {'floor': 3},
        'available_actions': ['choose_event_option'],
        'event': {'event_id': 'ABYSSAL_BATHS'},
    }
    terminal = {
        'screen': 'GAME_OVER', 'run_id': 'run',
        'available_actions': ['return_to_main_menu'],
        'game_over': {'is_victory': True},
    }
    report = {
        'flow_context': {}, 'completed_combat_count': 0, 'state_coverage': [],
        'actions': [], 'client_only_segments': [], 'reanchors': [],
        'parity_checkpoints': [], 'map_choices': [], 'rewards': [],
        'anchor_save': str(tmp_path / 'anchor.save'),
    }

    class StaleOnceLive(FakeLive):
        def execute_transaction(self, transaction, **kwargs):
            self.observed_state = terminal
            raise StaleClientStateError('stale action cancelled', terminal)

    live = StaleOnceLive(report, initial)
    args = SimpleNamespace(
        target_combats=1, deck_profile=None, session_dir=tmp_path,
        save_report=lambda: None,
    )
    log = FakeLog()

    final_client, final_shadow = run_interactions(
        FakeCli(), None, live, initial,
        {'decision': 'event_choice', 'options': [{'index': 0, 'text_key': 'PROCEED'}]},
        args, Path(__file__).resolve().parents[1], log, report, FakeControl(),
    )

    assert final_client == terminal
    assert final_shadow['decision'] == 'event_choice'
    assert report['status'] == 'VICTORY'
    assert not report['client_only_segments']
    assert any(row.get('event') == 'stale_action_replanned' for row in log.rows)


def test_shop_reanchor_is_copied_into_the_session(monkeypatch, tmp_path):
    source = tmp_path / 'current_run.save'
    source.write_bytes(b'fresh-map-save')
    report = {
        'anchor_save': str(tmp_path / 'official_map_anchor.save'),
        'actions': [{'sequence': 8}],
        'reanchors': [],
        'parity_checkpoints': [],
    }
    Path(report['anchor_save']).write_bytes(b'opening-anchor')
    comparison = SimpleNamespace(
        status='PASS', client={'floor': 14}, headless={'floor': 14}, differences=[],
        client_digest='client', headless_digest='headless',
    )
    monkeypatch.setattr(live_run_demo, 'client_map_checkpoint', lambda state: state)
    monkeypatch.setattr(live_run_demo, 'headless_map_checkpoint', lambda *args, **kwargs: {})
    monkeypatch.setattr(live_run_demo, 'compare_checkpoints', lambda *args, **kwargs: comparison)

    cli = FakeCli()
    state, row = live_run_demo._reanchor_from_official_save(
        cli,
        {'run_id': 'run', 'screen': 'MAP'},
        source,
        (0, 0, 'old'),
        FakeLog(),
        report,
        1,
    )

    snapshot = tmp_path / 'reanchor_segment_1.save'
    assert state['decision'] == 'map_select'
    assert snapshot.read_bytes() == b'fresh-map-save'
    assert row['save_path'] == str(snapshot)
    assert row['action_sequence'] == 8
    assert row['save_sha256'] == hashlib.sha256(b'fresh-map-save').hexdigest()
    assert cli.loads == [(str(snapshot), {'lang': 'en'})]


def test_authoritative_client_save_is_loaded_directly(monkeypatch, tmp_path):
    source = tmp_path / 'current_run.save'
    source.write_bytes(b'irrelevant-persistence-file')
    anchor = tmp_path / 'official_map_anchor.save'
    anchor.write_bytes(b'opening-anchor')
    report = {
        'anchor_save': str(anchor),
        'actions': [{'sequence': 11}],
        'reanchors': [],
        'parity_checkpoints': [],
    }
    comparison = SimpleNamespace(
        status='PASS', client={'floor': 6}, headless={'floor': 6}, differences=[],
        client_digest='client', headless_digest='headless',
    )
    monkeypatch.setattr(live_run_demo, 'client_map_checkpoint', lambda state: state)
    monkeypatch.setattr(live_run_demo, 'headless_map_checkpoint', lambda *args, **kwargs: {})
    monkeypatch.setattr(live_run_demo, 'compare_checkpoints', lambda *args, **kwargs: comparison)

    cli = FakeCli()
    state, row = live_run_demo._reanchor_from_official_save(
        cli,
        {'run_id': 'run', 'screen': 'MAP'},
        source,
        (0, 0, 'old'),
        FakeLog(),
        report,
        3,
        authoritative_save_json='{"schema_version":1}',
    )

    assert state['decision'] == 'map_select'
    assert row['reanchor_mode'] == 'CLIENT_EXACT_SAVE'
    assert row['source_save_path'] == 'client://exact-save'
    assert cli.loads == [('json', '{"schema_version":1}', {'lang': 'en'})]


def test_stale_save_room_replay_is_rejected_without_authoritative_client_save(monkeypatch, tmp_path):
    source = tmp_path / 'current_run.save'
    source.write_bytes(b'stale-room-entry')
    anchor = tmp_path / 'official_map_anchor.save'
    anchor.write_bytes(b'opening-anchor')
    report = {
        'anchor_save': str(anchor),
        'actions': [{
            'sequence': 8,
            'status': 'completed',
            'transaction': make_transaction(
                'choose_event_option', {'option_index': 0},
                shadow_action='choose_option', shadow_args={'option_index': 0},
            ).to_dict(),
        }],
        'reanchors': [],
        'parity_checkpoints': [],
    }
    comparison = SimpleNamespace(
        status='PASS', client={'floor': 5}, headless={'floor': 5}, differences=[],
        client_digest='client', headless_digest='headless',
    )
    monkeypatch.setattr(live_run_demo, 'client_map_checkpoint', lambda state: state)
    monkeypatch.setattr(live_run_demo, 'headless_map_checkpoint', lambda *args, **kwargs: {})
    monkeypatch.setattr(live_run_demo, 'compare_checkpoints', lambda *args, **kwargs: comparison)

    class ReplayCli(FakeCli):
        def load_save(self, path, **kwargs):
            self.loads.append((str(path), dict(kwargs)))
            if kwargs.get('resume_room'):
                return {'type': 'decision', 'decision': 'event_choice'}
            return {'type': 'decision', 'decision': 'map_select'}

        def action(self, action, payload, timeout_s=20):
            self.actions.append((action, dict(payload), timeout_s))
            return {'type': 'decision', 'decision': 'map_select'}

    cli = ReplayCli()
    with pytest.raises(RuntimeError, match='authoritative client exact save'):
        live_run_demo._reanchor_from_official_save(
            cli,
            {'run_id': 'run', 'screen': 'MAP'},
            source,
            live_run_demo._file_fingerprint(source),
            FakeLog(),
            report,
            2,
            room_replay_before_sequence=9,
            max_wait_s=0,
        )
    assert cli.actions == []


def test_missing_headless_map_is_reanchored_before_client_navigation(monkeypatch, tmp_path):
    save = tmp_path / "current_run.save"
    save.write_bytes(b"map-save")
    client = {
        "run_id": "run", "screen": "MAP", "available_actions": ["choose_map_node"],
        "run": {"floor": 3, "current_hp": 50, "max_hp": 80, "gold": 0},
        "map": {"available_nodes": [
            {"index": 7, "row": 0, "col": 1, "node_type": "Monster"},
        ]},
    }
    restored = {
        "decision": "map_select",
        "player": {"hp": 50, "max_hp": 80, "gold": 0},
        "context": {"floor": 3},
        "choices": [{"row": 0, "col": 1, "type": "Monster"}],
    }
    reanchor_calls = []

    def reanchor(cli, state, path, baseline, log, report, segment_id, **kwargs):
        reanchor_calls.append((state, path, baseline, segment_id))
        row = {"save_path": str(path), "save_sha256": "sha"}
        report["reanchors"].append(row)
        return restored, row

    monkeypatch.setattr(live_run_demo, "_latest_official_save", lambda: save)
    monkeypatch.setattr(live_run_demo, "_reanchor_from_official_save", reanchor)
    monkeypatch.setattr(live_run_demo, "_advance_shadow", lambda *args, **kwargs: restored)
    monkeypatch.setattr(live_run_demo, "_write_report", lambda *args, **kwargs: None)
    monkeypatch.setattr(live_run_demo, "_check_map", lambda *args, **kwargs: None)

    report = {
        "flow_context": {}, "completed_combat_count": 0, "state_coverage": [],
        "actions": [], "client_only_segments": [], "reanchors": [],
        "parity_checkpoints": [], "map_choices": [], "rewards": [],
        "anchor_save": str(tmp_path / "anchor.save"),
    }
    live = FakeLive(report, client)
    args = SimpleNamespace(
        target_combats=1, deck_profile=None, session_dir=tmp_path,
    )

    final_client, _ = run_interactions(
        FakeCli(), None, live, client, {"decision": "treasure_complete"},
        args, Path(__file__).resolve().parents[1], FakeLog(), report, FakeControl(),
    )

    assert final_client["screen"] == "GAME_OVER"
    assert reanchor_calls[0][2] == (-1, -1, "")
    assert live.transactions[0].client.params == {"option_index": 7}
    assert live.transactions[0].shadow.action == "select_map_node"
    assert report["client_only_segments"][0]["status"] == "BOUNDARY_VERIFIED"


def test_resume_loads_latest_verified_reanchor_and_skips_earlier_actions(tmp_path):
    opening_anchor = tmp_path / 'official_map_anchor.save'
    opening_anchor.write_bytes(b'opening')
    reanchor = tmp_path / 'reanchor_segment_1.save'
    reanchor.write_bytes(b'after-shop')
    client_state = {
        'run_id': 'run', 'screen': 'MAP', 'in_combat': False,
        'available_actions': ['choose_map_node'],
        'run': {'floor': 14, 'gold': 29, 'deck': []},
    }
    report_path = tmp_path / 'run_report.json'
    transaction_before = make_transaction('buy_card', {'option_index': 2}).to_dict()
    transaction_after = make_transaction(
        'proceed', {}, shadow_action='leave_room', shadow_args={}
    ).to_dict()
    source = {
        'schema_version': 3,
        'identity': {
            'run_id': 'run',
            'anchor_sha256': hashlib.sha256(opening_anchor.read_bytes()).hexdigest(),
        },
        'actions': [
            {'sequence': 1, 'status': 'completed', 'transaction': transaction_before,
             'client_after': client_state},
            {'sequence': 2, 'status': 'completed', 'transaction': transaction_after,
             'client_after': client_state},
        ],
        'client_only_segments': [{'segment_id': 1, 'status': 'BOUNDARY_VERIFIED'}],
        'reanchors': [{
            'segment_id': 1,
            'status': 'REANCHORED_PASS',
            'save_path': str(reanchor),
            'save_sha256': hashlib.sha256(reanchor.read_bytes()).hexdigest(),
            'action_sequence': 1,
        }],
        'completed_combat_count': 4,
        'flow_context': {'scene': 'MAP'},
    }
    report_path.write_text(__import__('json').dumps(source), encoding='utf-8')
    args = SimpleNamespace(
        resume_report=report_path,
        anchor_map_save=opening_anchor,
        recover_pending_selection=False,
        anchor_room=False,
    )
    cli = FakeCli()
    output = {}

    state, completed = live_run_demo._resume_shadow(
        cli, args, client_state, output, FakeLog()
    )

    assert state['decision'] == 'map_select'
    assert completed == 4
    assert cli.loads == [(str(reanchor.resolve()), {'lang': 'en'})]
    assert cli.actions == [('leave_room', {}, 20)]
    assert output['flow_context'] == {'scene': 'MAP'}
