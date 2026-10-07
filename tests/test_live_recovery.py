from pathlib import Path

import pytest

from controller.action_transaction import make_transaction
from controller.live_client_bridge import JsonlSessionLog
from scripts.live_run_demo import _advance_shadow, _recorded_shadow_command


RNG = {'schema_version': 1, 'complete': True, 'run_seed': 'test', 'run_streams': {'Test': {'counter': 0, 'seed': 1, 's0': 1, 's1': 2, 's2': 3, 's3': 4}}, 'players': []}

def test_shadow_timeout_replays_only_completed_actions_without_client_calls(tmp_path):
    (tmp_path / 'anchor.save').write_bytes(b'anchor')
    class Cli:
        _stderr_tail = ['test timeout']
        recovered = False
        actions = []
        def action(self, action, payload, **kwargs):
            if not self.recovered:
                raise TimeoutError('test timeout')
            self.actions.append((action, payload))
            return {'decision': 'combat_play'}
        def stop(self): pass
        def start(self): self.recovered = True
        def load_save(self, path, **kwargs): return {'decision': 'map_select'}
        def get_rng_snapshot(self): return RNG
    cli = Cli()
    report = {'anchor_save': str(tmp_path / 'anchor.save'), 'actions': [
        {'sequence': 1, 'status': 'completed', 'headless_action': 'select_map_node', 'headless_args': {'row': 1, 'col': 0}},
        {'sequence': 2, 'status': 'cancelled', 'headless_action': 'end_turn', 'headless_args': {}},
        {'sequence': 3, 'status': 'completed', 'headless_action': 'play_card', 'headless_args': {'card_index': 0}}]}
    report['actions'][-1]['client_rng_after'] = RNG
    result = _advance_shadow(cli, 'play_card', {'card_index': 0}, JsonlSessionLog(tmp_path / 'session.jsonl'), report)
    assert result['decision'] == 'combat_play'
    assert cli.actions == [('select_map_node', {'row': 1, 'col': 0}), ('play_card', {'card_index': 0})]
    assert report['shadow_recoveries'][0]['status'] == 'REPLAYED_RNG_VERIFIED_AWAITING_CHECKPOINT'


def test_shadow_timeout_is_read_from_transaction_telemetry(tmp_path):
    seen = []
    class Cli:
        _stderr_tail = []
        def action(self, action, payload, **kwargs):
            seen.append(kwargs.get('timeout_s'))
            return {'decision': 'combat_play'}
        def get_rng_snapshot(self): return RNG

    tx = make_transaction(
        'play_card', {'card_index': 0}, shadow_action='play_card',
        shadow_args={'card_index': 0},
        telemetry={'shadow_timeout_ms': 3750},
    ).to_dict()
    report = {'actions': [
        {'sequence': 1, 'status': 'completed', 'transaction': tx,
         'client_rng_after': RNG},
    ]}
    log = JsonlSessionLog(tmp_path / 'session.jsonl')
    _advance_shadow(Cli(), 'play_card', {'card_index': 0}, log, report)

    assert seen == [3.75]
    assert report['actions'][0]['timing_breakdown']['shadow_timeout_ms'] == 3750.0


def test_schema_three_recovery_uses_transaction_shadow_commands(tmp_path):
    (tmp_path / 'anchor.save').write_bytes(b'anchor')
    class Cli:
        _stderr_tail = ['test timeout']
        recovered = False
        actions = []
        def action(self, action, payload, **kwargs):
            if not self.recovered:
                raise TimeoutError('test timeout')
            self.actions.append((action, payload))
            return {'decision': 'combat_play'}
        def stop(self): pass
        def start(self): self.recovered = True
        def load_save(self, path, **kwargs): return {'decision': 'map_select'}
        def get_rng_snapshot(self): return RNG
    client_only = make_transaction('claim_reward', {'option_index': 0}).to_dict()
    mirrored = make_transaction(
        'choose_map_node', {'option_index': 2},
        shadow_action='select_map_node', shadow_args={'row': 1, 'col': 0},
    ).to_dict()
    combat = make_transaction(
        'play_card', {'card_index': 4},
        shadow_action='play_card', shadow_args={'card_index': 0},
    ).to_dict()
    report = {
        'schema_version': 3,
        'anchor_save': str(tmp_path / 'anchor.save'),
        'actions': [
            {'sequence': 1, 'status': 'completed', 'transaction': client_only},
            {'sequence': 2, 'status': 'completed', 'transaction': mirrored,
             'headless_action': 'wrong_legacy_value'},
            {'sequence': 3, 'status': 'completed', 'transaction': combat},
        ],
    }
    cli = Cli()
    report['actions'][-1]['client_rng_after'] = RNG
    _advance_shadow(
        cli, 'play_card', {'card_index': 0},
        JsonlSessionLog(tmp_path / 'session.jsonl'), report,
    )
    assert cli.actions == [
        ('select_map_node', {'row': 1, 'col': 0}),
        ('play_card', {'card_index': 0}),
    ]


def test_schema_three_action_without_transaction_is_rejected():
    with pytest.raises(RuntimeError, match='missing its transaction'):
        _recorded_shadow_command(
            {'sequence': 9, 'status': 'completed', 'headless_action': 'end_turn'},
            {'decision': 'combat_play'},
            require_transaction=True,
        )


def test_recovery_rng_mismatch_blocks_continuation_and_map_anchor_is_not_reopened(tmp_path):
    import copy
    import hashlib
    anchor = tmp_path / 'map.save'
    anchor.write_bytes(b'map checkpoint')
    changed = copy.deepcopy(RNG)
    changed['run_streams']['Test']['counter'] = 1

    class Cli:
        _stderr_tail = []
        recovered = False
        resume = None
        def get_rng_snapshot(self):
            return changed if self.recovered else RNG
        def action(self, action, payload, **kwargs):
            if not self.recovered:
                raise TimeoutError('lost response')
            return {'decision': 'combat_play'}
        def stop(self): pass
        def start(self): self.recovered = True
        def load_save(self, path, **kwargs):
            self.resume = kwargs['resume_room']
            return {'decision': 'map_select'}

    tx = make_transaction('play_card', {'card_index': 0}, shadow_action='play_card',
                          shadow_args={'card_index': 0}).to_dict()
    report = {'schema_version': 3, 'anchor_save': str(anchor), 'anchor_room': True,
              'reanchors': [{'status': 'REANCHORED_PASS', 'save_path': str(anchor),
                             'save_sha256': hashlib.sha256(anchor.read_bytes()).hexdigest(),
                             'action_sequence': 1}],
              'actions': [{'sequence': 2, 'status': 'completed', 'transaction': tx,
                           'client_rng_after': RNG}]}
    cli = Cli()
    with pytest.raises(RuntimeError, match='Recovered shadow RNG differs'):
        _advance_shadow(cli, 'play_card', {}, JsonlSessionLog(tmp_path / 'session.jsonl'), report)
    assert cli.resume is False
    assert report['actions'][-1]['recovery_rng_parity']['status'] == 'FAIL'
    assert report['shadow_recoveries'][-1]['status'] == 'REPLAY_FAILED'


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_authoritative_anchor_preserves_bytes_and_digest(tmp_path, newline):
    import hashlib
    from scripts.live_run_demo import _write_authoritative_anchor
    text = '{' + newline + '  "name": "真人存档"' + newline + '}'
    path = tmp_path / 'anchor.save'
    digest = _write_authoritative_anchor(path, text)
    assert path.read_bytes() == text.encode('utf-8')
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert not path.with_suffix('.save.tmp').exists()


def test_old_runtime_capabilities_block_before_game_actions():
    from cli.sts2_cli_adapter import Sts2CliAdapter, CliConfig
    adapter = Sts2CliAdapter(CliConfig(repo_root=Path(__file__).resolve().parents[1]))
    with pytest.raises(RuntimeError, match='rebuild Sts2Headless'):
        adapter.require_capabilities({'ack_event_reward'})
    adapter.protocol_capabilities = frozenset({'ack_event_reward'})
    adapter.require_capabilities({'ack_event_reward'})


def test_headless_error_precedes_resulting_rng_difference(tmp_path):
    import copy
    before = copy.deepcopy(RNG)
    after = copy.deepcopy(RNG)
    after['run_streams']['Test']['counter'] = 1

    class Cli:
        _stderr_tail = ['MissingMethodException: Godot.Node.GetIndex(Boolean)']
        def __init__(self): self.calls = 0
        def get_rng_snapshot(self):
            self.calls += 1
            return before
        def action(self, *_args, **_kwargs):
            return {'type': 'error', 'message': 'MissingMethodException: Godot.Node.GetIndex(Boolean)',
                    'stack_trace': 'ReattachPower.AfterDeath'}

    row = {'sequence': 289, 'transaction': make_transaction(
        'play_card', {'card_index': 0}, shadow_action='play_card',
        shadow_args={'card_index': 0}).to_dict(),
        'client_rng_before': before, 'client_rng_after': after}
    report = {'actions': [row]}
    with pytest.raises(RuntimeError, match='MissingMethodException'):
        _advance_shadow(Cli(), 'play_card', {'card_index': 0},
                        JsonlSessionLog(tmp_path / 'session.jsonl'), report)
    assert row['rng_parity']['status'] == 'FAIL'
    assert row['shadow_response']['stack_trace'] == 'ReattachPower.AfterDeath'
    assert row['shadow_execution_error'].startswith('MissingMethodException')
    assert row['shadow_stderr_tail']
