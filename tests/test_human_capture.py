import hashlib
import json
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import pytest

from cli.sts2_mod_adapter import ModApiError, Sts2ModAdapter

from controller.human_capture import (
    CAPTURE_PROTOCOL,
    AUTHORITATIVE_SNAPSHOT_SCHEMA,
    DATASET_SCHEMA,
    ActionAttributor,
    assign_dataset_splits,
    JsonlCaptureSession,
    audit_capture_session,
    build_dataset,
    collect_human_actions,
    current_capture_event_paths,
    dataset_group_key,
    dataset_split,
    derive_combat_id,
    normalize_action,
    ObservationSampler,
    settlement_digest,
    validate_authoritative_snapshot,
)
from controller.human_training import (
    ActionFrequencyBaseline,
    HumanDataset,
    load_training_backend,
    run_training,
)


def event(event_id=7):
    return {
        'event_id': event_id,
        'type': 'player_action_observed',
        'timestamp_utc': f'2026-09-20T00:00:{event_id % 60:02d}Z',
        'data': {
            'semantic_action': 'play_card',
            'source': 'combat_action_queue',
            'phase': 'committed',
            'action_class': 'MegaCrit.Sts2.Core.GameActions.PlayCardAction',
            'action_type': 'PlayCard',
            'owner_id': 1,
            'queue_action_id': 9,
            'card_id': 'CARD.BASH',
            'card_instance': {'NetId': 42},
            'target_creature_id': 3,
        },
    }


def state(energy):
    return {
        'state_version': 1,
        'run_id': 'run-a',
        'screen': 'COMBAT',
        'in_combat': True,
        'turn': 2,
        'available_actions': ['play_card', 'end_turn'],
        'combat': {'player': {'energy': energy}, 'hand': [{'card_id': 'BASH'}]},
    }


def snapshot_bundle(snapshot_id='combat_test'):
    snapshot = {
        'Id': snapshot_id,
        'CharacterName': 'IRONCLAD',
        'AscensionLevel': 0,
        'Seed': 'test-seed',
        'RoomJson': json.dumps({'room': 'combat'}, separators=(',', ':')),
        'PlayerJson': json.dumps({'player': 'ironclad'}, separators=(',', ':')),
        'NetState': {
            'Creatures': [{'playerId': 1, 'currentHp': 80, 'maxHp': 80, 'block': 0,
                           'monsterId': None, 'powers': []}],
            'Players': [{'playerId': 1, 'energy': 3, 'piles': []}],
            'Rng': {'Seed': 'test-seed', 'Counters': {}},
        },
        'EnemyCreatureStates': [],
        'EnemyAiStates': [],
        'RunRngStates': [
            {'Name': 'card', 'Counter': 0, 'Seed': 1, 'S0': 1, 'S1': 2, 'S2': 3, 'S3': 4}
        ],
        'PlayerRngStates': [
            {'Name': 'shuffle', 'Counter': 0, 'Seed': 2, 'S0': 5, 'S1': 6, 'S2': 7, 'S3': 8}
        ],
        'RoundNumber': 2,
        'CurrentSide': 0,
        'RelicStates': [],
        'HookStates': [],
        'PlayerCombatState': {'TypeName': 'PlayerCombatState', 'TypeOrdinal': 0, 'Fields': []},
        'PlayerExtraState': {'TypeName': 'PlayerExtraFields', 'TypeOrdinal': 0, 'Fields': []},
        'CombatHistoryEntries': [],
        'ActivePowerRefs': [],
    }
    snapshot_json = json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))
    raw = snapshot_json.encode('utf-8')
    digest = hashlib.sha256(raw).hexdigest()
    metadata = {
        'status': 'complete',
        'schema': AUTHORITATIVE_SNAPSHOT_SCHEMA,
        'snapshot_id': snapshot_id,
        'captured_at_utc': '2026-09-20T00:00:00Z',
        'sha256': digest,
        'bytes': len(raw),
    }
    return metadata, {**metadata, 'snapshot_json': snapshot_json}


def attach_snapshot(session, captured):
    metadata, payload = snapshot_bundle(f"combat_test_{captured['event_id']}")
    captured['data']['authoritative_snapshot'] = metadata
    captured['authoritative_snapshot'] = session.persist_authoritative_snapshot(captured, payload)
    return captured


def capture_health():
    return {
        'protocol_version': CAPTURE_PROTOCOL,
        'status': 'ready',
        'patch_count': 22,
        'authoritative_combat_snapshot': True,
        'snapshot_schema': AUTHORITATIVE_SNAPSHOT_SCHEMA,
        'authoritative_run_save': True,
    }


def set_samples(sampler, samples):
    sampler.samples = deque(samples)


def test_exact_action_normalization_keeps_instance_and_target():
    action = normalize_action(event())
    assert action['type'] == 'play_card'
    assert action['card_id'] == 'CARD.BASH'
    assert action['card_instance'] == {'NetId': 42}
    assert action['target_creature_id'] == 3
    assert action['source'] == 'combat_action_queue'
    assert action['phase'] == 'committed'


def test_noncombat_action_normalization_preserves_extensible_native_fields():
    native = {
        'event_id': 8,
        'type': 'player_action_observed',
        'timestamp_utc': '2026-09-20T00:00:01Z',
        'data': {
            'semantic_action': 'choose_map_node',
            'source': 'map_selection',
            'phase': 'requested',
            'owner_id': 1,
            'map_generation_count': 2,
            'destination_coord': {'col': 3, 'row': 7},
            'future_native_field': {'kept': True},
        },
    }
    action = normalize_action(native)
    assert action['type'] == 'choose_map_node'
    assert action['source'] == 'map_selection'
    assert action['destination_coord'] == {'col': 3, 'row': 7}
    assert action['future_native_field'] == {'kept': True}


def test_potion_normalization_preserves_explicit_no_manual_target_semantics():
    native = event(9)
    native['data'].update({
        'semantic_action': 'use_potion',
        'potion_id': 'FLEX_POTION',
        'target_type': 'Self',
        'requires_target': False,
        'target_player_id': 1,
        'target_semantics_source': 'potion_model',
    })
    action = normalize_action(native)
    assert action['requires_target'] is False
    assert action['target_type'] == 'Self'
    assert action['target_player_id'] == 1


def test_legacy_v1_event_remains_readable_for_existing_raw_sessions():
    legacy = event()
    legacy['type'] = 'player_action_enqueued'
    legacy['data'].pop('source')
    legacy['data'].pop('phase')
    action = normalize_action(legacy)
    assert action['source'] == 'legacy_combat_action_queue'
    assert action['phase'] == 'committed'


def test_capture_session_is_append_only_and_preserves_full_context(tmp_path):
    session = JsonlCaptureSession(tmp_path, {'session_id': 'session-a', 'capture_identity': {'sha256': 'x'}})
    row = session.append_decision(event(), state(3), state(1), 'settled')
    session.close()
    lines = [json.loads(line) for line in session.events_path.read_text(encoding='utf-8').splitlines()]
    assert [line['sequence'] for line in lines] == [1, 2, 3]
    assert row['observation_before']['combat']['player']['energy'] == 3
    assert row['observation_after']['combat']['player']['energy'] == 1
    assert row['label_semantics'].endswith('unchosen_actions_are_unlabeled')
    assert json.loads(session.manifest_path.read_text(encoding='utf-8'))['status'] == 'completed'


def test_dataset_builder_splits_by_run_and_skips_unsettled(tmp_path):
    session = JsonlCaptureSession(tmp_path / 'raw', {'session_id': 'session-a'})
    settled = session.append_decision(event(1), state(3), state(1), 'settled')
    session.append_decision(event(2), state(1), None, 'timeout')
    session.close()
    output = tmp_path / 'dataset'
    manifest = build_dataset([session.events_path], output)
    split = dataset_split(settled)
    examples = [json.loads(line) for line in (output / f'{split}.jsonl').read_text(encoding='utf-8').splitlines()]
    assert manifest['counts'][split] == 1
    assert manifest['skipped_unsettled'] == 1
    assert examples[0]['schema'] == DATASET_SCHEMA
    assert examples[0]['observation']['combat']['hand'][0]['card_id'] == 'BASH'
    assert examples[0]['chosen_action']['card_instance']['NetId'] == 42
    assert manifest['action_counts'] == {'play_card': 1}
    assert manifest['source_counts'] == {'combat_action_queue': 1}
    assert manifest['source_sessions'][0]['events_sha256']


def test_dataset_builder_deduplicates_one_transition_by_action_specificity(tmp_path):
    session = JsonlCaptureSession(tmp_path / 'raw', {'session_id': 'session-a'})
    generic = event(1)
    generic['data'].update({
        'semantic_action': 'submit_player_choice',
        'source': 'player_choice',
        'choice_id': 4,
        'choice_type': 'CombatCard',
    })
    session.append_decision(generic, state(3), state(1), 'settled')
    exact = event(2)
    exact['timestamp_utc'] = generic['timestamp_utc']
    exact_row = session.append_decision(exact, state(3), state(1), 'settled')
    session.close()
    output = tmp_path / 'dataset'
    manifest = build_dataset([session.events_path], output)
    split = dataset_split(exact_row)
    examples = [json.loads(line) for line in (output / f'{split}.jsonl').read_text(encoding='utf-8').splitlines()]
    assert manifest['skipped_duplicate_transitions'] == 1
    assert len(examples) == 1
    assert examples[0]['chosen_action']['type'] == 'play_card'


def test_sampler_uses_last_observation_before_mod_event_time():
    sampler = ObservationSampler(None)
    set_samples(sampler, [(100.0, state(3)), (101.0, state(2)), (102.0, state(1))])
    selected = sampler.before('1970-01-01T00:01:41+00:00')
    assert selected['combat']['player']['energy'] == 2


def test_attributor_settles_one_isolated_action_after_stable_observation():
    sampler = ObservationSampler(None, interval_s=0.05)
    set_samples(sampler, [
        (100.0, state(3)),
        (100.2, state(1)),
        (100.35, state(1)),
    ])
    captured = event(1)
    captured['timestamp_utc'] = '1970-01-01T00:01:40.100000+00:00'
    attributor = ActionAttributor(sampler, settle_timeout_s=5, attribution_grace_s=0.1)
    attributor.add(captured)
    rows = attributor.ready(now=101.0)
    assert len(rows) == 1
    assert rows[0][3] == 'settled'
    assert rows[0][1]['combat']['player']['energy'] == 3
    assert rows[0][2]['combat']['player']['energy'] == 1


def test_attributor_rejects_actions_overlapping_one_observer_transition():
    sampler = ObservationSampler(None, interval_s=0.05)
    set_samples(sampler, [
        (100.0, state(3)),
        (100.3, state(1)),
        (100.45, state(1)),
    ])
    first = event(1)
    first['timestamp_utc'] = '1970-01-01T00:01:40.100000+00:00'
    second = event(2)
    second['timestamp_utc'] = '1970-01-01T00:01:40.200000+00:00'
    attributor = ActionAttributor(sampler, settle_timeout_s=5, attribution_grace_s=0.1)
    attributor.add(first)
    attributor.add(second)
    rows = attributor.ready(now=101.0)
    assert [row[3] for row in rows] == ['ambiguous_overlap', 'ambiguous_overlap']
    assert attributor.pending == []


def test_attributor_ignores_observer_version_and_derived_view_only_changes():
    before = state(3)
    before['state_version'] = 1
    before['agent_view'] = {'version': 1}
    volatile = {**before, 'state_version': 2, 'agent_view': {'version': 2}}
    assert settlement_digest(before) == settlement_digest(volatile)
    sampler = ObservationSampler(None, interval_s=0.05)
    set_samples(sampler, [(100.0, before), (100.2, volatile), (100.4, volatile)])
    captured = event(1)
    captured['timestamp_utc'] = '1970-01-01T00:01:40.100000+00:00'
    attributor = ActionAttributor(sampler, settle_timeout_s=0.5, attribution_grace_s=0.1)
    attributor.add(captured)
    rows = attributor.ready(now=101.0)
    assert len(rows) == 1
    assert rows[0][3] == 'timeout'


def test_noncombat_decision_does_not_fabricate_combat_id(tmp_path):
    session = JsonlCaptureSession(tmp_path, {'session_id': 'session-a'})
    noncombat = state(3)
    noncombat['screen'] = 'MAP'
    noncombat['in_combat'] = False
    row = session.append_decision(event(1), noncombat, {**noncombat, 'screen': 'EVENT'}, 'settled')
    assert row['combat_id'] is None


def test_combat_id_uses_run_location_not_stale_combat_payload():
    observation = state(3)
    observation['run'] = {'act_id': '2', 'floor': 19}
    observation['map'] = {'current': '3,4'}
    assert derive_combat_id(observation, 'run-a') == 'run-a:act=2:floor=19:map=3,4'
    observation['in_combat'] = False
    assert derive_combat_id(observation, 'run-a') is None


def test_unknown_runs_split_by_session_instead_of_one_global_unknown_run():
    first = {'run_id': 'run_unknown', 'session_id': 'session-a', 'decision_id': 'a'}
    second = {'run_id': 'run_unknown', 'session_id': 'session-b', 'decision_id': 'b'}
    assert dataset_group_key(first) == 'session:session-a'
    assert dataset_group_key(second) == 'session:session-b'


def test_small_dataset_forces_one_whole_run_group_into_train():
    index = 0
    while True:
        run_id = f'nontrain-{index}'
        row = {'run_id': run_id, 'session_id': 'session-a', 'decision_id': 'a'}
        if dataset_split(row) != 'train':
            break
        index += 1
    assignments = assign_dataset_splits([row])
    assert assignments[run_id] == 'train'


def test_replaceable_training_port_records_dataset_and_backend_identity(tmp_path):
    raw = tmp_path / 'raw'
    session = JsonlCaptureSession(raw, {'session_id': 'session-a'})
    for index in range(8):
        session.append_decision(event(index), state(3), state(1), 'settled')
    session.close()
    dataset = tmp_path / 'dataset'
    build_dataset([session.events_path], dataset)
    output = tmp_path / 'training-run'
    result = run_training(dataset, output, ActionFrequencyBaseline())
    assert result['backend']['purpose'] == 'pipeline_feasibility_only'
    assert result['dataset']['counts']['train'] == 8
    assert (output / 'model.json').is_file()
    assert (output / 'training_run.json').is_file()
    assert result['schema'] == 'sts2.human_training.run.v2'


def test_training_dataset_streams_splits_and_dynamic_backend_factory(tmp_path):
    raw = tmp_path / 'raw'
    session = JsonlCaptureSession(raw, {'session_id': 'session-a'})
    for index in range(3):
        session.append_decision(event(index), state(3), state(1), 'settled')
    session.close()
    dataset_dir = tmp_path / 'dataset'
    build_dataset([session.events_path], dataset_dir)
    dataset = HumanDataset.open(dataset_dir)
    assert sum(1 for _ in dataset.iter_split('train')) == 3
    backend = load_training_backend(
        'controller.human_training:build_frequency_backend',
        {'context_fields': ['screen']},
    )
    assert backend.identity['config']['context_fields'] == ['screen']


def test_live_session_audit_requires_settled_cases_and_current_protocol(tmp_path):
    session = JsonlCaptureSession(tmp_path, {
        'session_id': 'session-a',
        'capture_health': {
            **capture_health(),
        },
    })
    card = attach_snapshot(session, event(1))
    session.append('capture_event', {'native_event': card})
    session.append_decision(card, state(3), state(1), 'settled')
    potion = event(2)
    potion['data'].update({
        'semantic_action': 'use_potion',
        'potion_index': 0,
        'potion_id': 'FLEX_POTION',
        'target_type': 'Self',
        'requires_target': False,
        'target_player_id': 1,
        'target_semantics_source': 'potion_model',
    })
    attach_snapshot(session, potion)
    session.append('capture_event', {'native_event': potion})
    session.append_decision(potion, state(1), state(2), 'settled')
    session.close()
    report = audit_capture_session(
        session.events_path,
        ['play_card', 'use_potion:no_manual_target', 'use_potion:targetless',
         'source:combat_action_queue'],
    )
    assert report['ok'] is True
    assert report['missing_required_cases'] == []
    assert report['potion_target_semantics'] == {'no_manual_target': 1}
    missing = audit_capture_session(
        session.events_path, ['use_potion:manual_target', 'use_potion:targeted']
    )
    assert missing['ok'] is False
    assert missing['missing_required_cases'] == [
        'use_potion:manual_target', 'use_potion:targeted'
    ]


def test_live_session_audit_uses_model_semantics_not_resolved_target_ids(tmp_path):
    session = JsonlCaptureSession(tmp_path, {
        'session_id': 'session-a',
        'capture_health': {
            **capture_health(),
        },
    })
    potion = event(1)
    potion['data'].update({
        'semantic_action': 'use_potion',
        'potion_index': 0,
        'potion_id': 'FIRE_POTION',
        'target_type': 'AnyEnemy',
        'requires_target': True,
        'target_index_space': 'enemies',
        'target_creature_id': 3,
        'target_semantics_source': 'potion_model',
    })
    attach_snapshot(session, potion)
    session.append('capture_event', {'native_event': potion})
    session.append_decision(potion, state(3), state(1), 'settled')
    session.close()
    report = audit_capture_session(
        session.events_path, ['use_potion:manual_target', 'use_potion:targeted']
    )
    assert report['ok'] is True
    assert report['potion_target_semantics'] == {'manual_target': 1}


def test_live_session_audit_rejects_unknown_potion_target_semantics(tmp_path):
    session = JsonlCaptureSession(tmp_path, {
        'session_id': 'session-a',
        'capture_health': {
            **capture_health(),
        },
    })
    potion = event(1)
    potion['data'].update({
        'semantic_action': 'use_potion',
        'potion_index': 0,
        'target_player_id': 1,
    })
    session.append('capture_event', {'native_event': potion})
    session.append_decision(potion, state(3), state(1), 'settled')
    session.close()
    report = audit_capture_session(
        session.events_path, ['use_potion:no_manual_target', 'use_potion:targetless']
    )
    assert report['ok'] is False
    assert report['potion_target_semantics'] == {'unknown': 1}
    assert report['missing_required_cases'] == [
        'use_potion:no_manual_target', 'use_potion:targetless'
    ]
    assert any('no explicit requires_target semantics' in error
               for error in report['integrity_errors'])


def test_live_session_audit_rejects_capture_event_gaps(tmp_path):
    session = JsonlCaptureSession(tmp_path, {
        'session_id': 'session-a',
        'capture_health': {
            **capture_health(),
        },
    })
    first = attach_snapshot(session, event(1))
    third = attach_snapshot(session, event(3))
    session.append('capture_event', {'native_event': first})
    session.append_decision(first, state(3), state(2), 'settled')
    session.append('capture_event', {'native_event': third})
    session.append_decision(third, state(2), state(1), 'settled')
    session.close()
    report = audit_capture_session(session.events_path)
    assert report['ok'] is False
    assert any('discontinuity' in error for error in report['integrity_errors'])


def test_dataset_integrity_gate_accepts_valid_session_and_rejects_gap(tmp_path):
    valid = JsonlCaptureSession(tmp_path / 'raw-valid', {
        'session_id': 'session-valid',
        'capture_health': {
            **capture_health(),
        },
    })
    captured = attach_snapshot(valid, event(1))
    valid.append('capture_event', {'native_event': captured})
    valid.append_decision(captured, state(3), state(1), 'settled')
    valid.close()
    manifest = build_dataset(
        [valid.events_path], tmp_path / 'valid-dataset', require_integrity=True
    )
    assert manifest['integrity_gate'] == 'required'

    invalid = JsonlCaptureSession(tmp_path / 'raw-invalid', {
        'session_id': 'session-invalid',
        'capture_health': {
            **capture_health(),
        },
    })
    first = attach_snapshot(invalid, event(1))
    third = attach_snapshot(invalid, event(3))
    invalid.append('capture_event', {'native_event': first})
    invalid.append_decision(first, state(3), state(2), 'settled')
    invalid.append('capture_event', {'native_event': third})
    invalid.append_decision(third, state(2), state(1), 'settled')
    invalid.close()
    with pytest.raises(ValueError, match='integrity audit failed'):
        build_dataset(
            [invalid.events_path], tmp_path / 'invalid-dataset', require_integrity=True
        )


def test_collection_pipeline_persists_event_before_settled_decision(tmp_path):
    class FakeMod:
        def __init__(self):
            self.action_emitted = threading.Event()
            self.emitted_at = 0.0

        def health(self):
            return {'protocol_version': '2026-03-11-v1', 'status': 'ready'}

        def capture_health(self):
            return capture_health()

        def capture_identity(self):
            return {'capture_assembly': {'sha256': 'capture'}, 'game_assembly': {'sha256': 'game'}}

        def state(self):
            if self.action_emitted.is_set() and time.time() - self.emitted_at >= 0.06:
                return state(1)
            return state(3)

        def capture_events(self):
            time.sleep(0.05)
            captured = event(1)
            metadata, self.snapshot_payload = snapshot_bundle('combat_live_1')
            captured['data']['authoritative_snapshot'] = metadata
            captured['timestamp_utc'] = datetime.now(timezone.utc).isoformat()
            self.emitted_at = time.time()
            self.action_emitted.set()
            yield captured
            time.sleep(2)

        def capture_snapshot(self, snapshot_id):
            assert snapshot_id == 'combat_live_1'
            return self.snapshot_payload

    directory = collect_human_actions(
        FakeMod(),
        tmp_path,
        poll_interval_s=0.02,
        settle_timeout_s=1,
        attribution_grace_s=0.05,
        max_decisions=1,
    )
    rows = [json.loads(line) for line in (directory / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
    capture_index = next(index for index, row in enumerate(rows) if row['record_type'] == 'capture_event')
    decision_index = next(index for index, row in enumerate(rows) if row['record_type'] == 'decision')
    assert capture_index < decision_index
    assert rows[decision_index]['settlement'] == 'settled'
    assert json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))['status'] == 'completed'
    assert audit_capture_session(directory / 'events.jsonl')['ok'] is True


def test_manifest_replace_retries_when_dashboard_temporarily_holds_manifest(tmp_path, monkeypatch):
    from pathlib import Path

    original_replace = Path.replace
    attempts = {'count': 0}

    def flaky_replace(path, target):
        if path.name == 'manifest.json.tmp' and attempts['count'] < 2:
            attempts['count'] += 1
            raise PermissionError(5, 'locked by dashboard')
        return original_replace(path, target)

    monkeypatch.setattr(Path, 'replace', flaky_replace)
    session = JsonlCaptureSession(tmp_path / 'raw', {'session_id': 'retry'})
    session.close()
    assert attempts['count'] == 2
    assert json.loads(session.manifest_path.read_text(encoding='utf-8'))['status'] == 'completed'


def test_collection_discards_events_buffered_before_collection_start(tmp_path):
    class FakeMod:
        def __init__(self):
            self.action_emitted = threading.Event()
            self.emitted_at = 0.0

        def health(self):
            return {'protocol_version': '2026-03-11-v1', 'status': 'ready'}

        def capture_health(self):
            return capture_health()

        def capture_identity(self):
            return {'capture_assembly': {'sha256': 'capture'}, 'game_assembly': {'sha256': 'game'}}

        def state(self):
            if self.action_emitted.is_set() and time.time() - self.emitted_at >= 0.06:
                return state(1)
            return state(3)

        def capture_events(self):
            stale = event(1)
            stale['timestamp_utc'] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
            yield stale
            time.sleep(0.05)
            current = event(2)
            metadata, self.snapshot_payload = snapshot_bundle('combat_live_2')
            current['data']['authoritative_snapshot'] = metadata
            current['timestamp_utc'] = datetime.now(timezone.utc).isoformat()
            self.emitted_at = time.time()
            self.action_emitted.set()
            yield current
            time.sleep(2)

        def capture_snapshot(self, snapshot_id):
            assert snapshot_id == 'combat_live_2'
            return self.snapshot_payload

    directory = collect_human_actions(
        FakeMod(), tmp_path / 'raw', poll_interval_s=0.02,
        settle_timeout_s=1, attribution_grace_s=0.05, max_decisions=1,
    )
    rows = [json.loads(line) for line in (directory / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
    discarded = [row for row in rows if row['record_type'] == 'capture_event_discarded']
    captured = [row for row in rows if row['record_type'] == 'capture_event']
    assert [row['native_event']['event_id'] for row in discarded] == [1]
    assert [row['native_event']['event_id'] for row in captured] == [2]
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['discarded_capture_events'] == 1
    assert audit_capture_session(directory / 'events.jsonl')['discarded_capture_events'] == 1


def test_acceptance_command_runs_audit_dataset_and_training(tmp_path):
    session = JsonlCaptureSession(tmp_path / 'raw', {
        'session_id': 'session-a',
        'capture_health': {
            **capture_health(),
        },
    })
    captured = attach_snapshot(session, event(1))
    session.append('capture_event', {'native_event': captured})
    session.append_decision(captured, state(3), state(1), 'settled')
    session.close()
    output = tmp_path / 'acceptance'
    completed = subprocess.run(
        [
            sys.executable, '-X', 'utf8', '-m', 'scripts.accept_human_pipeline',
            '--input', str(session.directory), '--output', str(output),
            '--require', 'play_card',
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    reports = list(output.glob('acceptance_*/acceptance_report.json'))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding='utf-8'))
    assert report['audit']['ok'] is True
    assert report['dataset']['counts']['train'] == 1
    assert report['training']['backend']['purpose'] == 'pipeline_feasibility_only'


def test_capture_client_rejects_partial_harmony_binding():
    adapter = Sts2ModAdapter()
    adapter._request = lambda *args, **kwargs: {
        'protocol_version': CAPTURE_PROTOCOL,
        'status': 'ready',
        'patch_count': 21,
    }
    with pytest.raises(ModApiError, match='only 21 Harmony patches'):
        adapter.capture_health()


def test_authoritative_snapshot_validation_rejects_tampered_payload():
    metadata, payload = snapshot_bundle()
    payload['snapshot_json'] += ' '
    with pytest.raises(ValueError, match='digest mismatch'):
        validate_authoritative_snapshot(payload, metadata)


def test_live_session_audit_rejects_missing_authoritative_combat_snapshot(tmp_path):
    session = JsonlCaptureSession(tmp_path, {
        'session_id': 'session-a',
        'capture_health': capture_health(),
    })
    captured = event(1)
    session.append('capture_event', {'native_event': captured})
    session.append_decision(captured, state(3), state(1), 'settled')
    session.close()
    report = audit_capture_session(session.events_path)
    assert report['ok'] is False
    assert any('no complete authoritative snapshot' in error
               for error in report['integrity_errors'])


def test_current_capture_event_paths_excludes_archived_protocol_sessions(tmp_path):
    current = JsonlCaptureSession(tmp_path, {
        'session_id': 'current', 'capture_health': capture_health(),
    })
    current.close()
    legacy_health = capture_health()
    legacy_health['protocol_version'] = '2026-09-20-human-capture-v3'
    legacy = JsonlCaptureSession(tmp_path, {
        'session_id': 'legacy', 'capture_health': legacy_health,
    })
    legacy.close()
    assert current_capture_event_paths(tmp_path) == [current.events_path]
