import copy
import pytest

from controller import combat_regressions as cr


def action(card):
    return {'action_type': 'play_card', 'metadata': {'card_id': card}}


def test_labels_allow_equivalent_first_actions_without_sequence_scripts():
    label = {'required_cards': ['RUPTURE'], 'forbidden_cards': ['SLIMED'],
             'acceptable_first_cards': ['RUPTURE', 'STRIKE_IRONCLAD']}
    assert cr.matches_label([action('RUPTURE'), action('STRIKE_IRONCLAD')], label)
    assert cr.matches_label([action('STRIKE_IRONCLAD'), action('RUPTURE')], label)
    assert not cr.matches_label([action('STRIKE_IRONCLAD'), action('SLIMED')], label)


def test_import_is_self_contained_and_refuses_unknown_outcomes(tmp_path):
    session = tmp_path / 'session'
    session.mkdir()
    (session / 'official_map_anchor.save').write_bytes(b'anchor')
    report = {'actions': [
        {'sequence': 1, 'status': 'outcome_unknown', 'transaction': {'shadow': None}},
        {'sequence': 2, 'status': 'completed', 'transaction': {'shadow': None}}]}
    path = session / 'run_report.json'
    cr.write_json(path, report)
    with pytest.raises(ValueError, match='Unknown/incomplete'):
        cr.import_case(path, 2, tmp_path / 'case', note='test')
    report['actions'][0]['status'] = 'completed'
    cr.write_json(path, report)
    case = tmp_path / 'case'
    cr.import_case(path, 2, case, note='test')
    cr.verify_integrity(case)
    assert (case / 'anchor.save').read_bytes() == b'anchor'
    with pytest.raises(ValueError, match='overwrite'):
        cr.import_case(path, 2, case, note='test')
    (case / 'evidence.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='integrity mismatch'):
        cr.verify_integrity(case)


def test_missing_manifest_entry_cannot_pass_as_frozen_data(tmp_path):
    cr.synthetic_case(tmp_path / 'case')
    case = tmp_path / 'case'
    manifest = cr.read_json(case / 'integrity.json')
    del manifest['replay.json']
    cr.write_json(case / 'integrity.json', manifest)
    with pytest.raises(ValueError, match='missing from integrity'):
        cr.verify_integrity(case)


def test_rank_checker_uses_current_evaluator_and_requires_strict_margin(tmp_path, monkeypatch):
    from controller.search import evaluator
    case = tmp_path / 'case'
    cr.synthetic_case(case)
    replay = cr.read_json(case / 'replay.json')
    base = replay['candidates'][0]
    good, bad = copy.deepcopy(base), copy.deepcopy(base)
    good.update(id='good', line=[action('RUPTURE')])
    bad.update(id='bad', line=[action('SLIMED')])
    good['leaf_state']['test_score'] = 10.0
    bad['leaf_state']['test_score'] = 10.0
    good['leaf_state']['test_preferred'] = True
    replay['candidates'] = [good, bad]
    cr.write_json(case / 'replay.json', replay)
    cr.write_json(case / 'label.json', {
        'schema_version': 1, 'kind': 'policy', 'review_status': 'proposed', 'author': 'test',
        'rationale': 'test', 'required_cards': ['RUPTURE'], 'minimum_margin': 0.01})
    manifest = cr.read_json(case / 'integrity.json')
    manifest['replay.json'] = cr.digest(case / 'replay.json')
    cr.write_json(case / 'integrity.json', manifest)
    monkeypatch.setattr(evaluator, 'explain_leaf_score', lambda state, *args: {'total': state['test_score']})
    assert cr.check_case(case, score_mode='legacy')['status'] == 'FAIL'
    # Change the function, not the frozen inputs, to demonstrate a real re-evaluation.
    monkeypatch.setattr(evaluator, 'explain_leaf_score',
                        lambda state, *args: {'total': state['test_score'] + int(state.get('test_preferred', False))})
    assert cr.check_case(case, score_mode='legacy')['status'] == 'PASS'
    label = cr.read_json(case / 'label.json')
    label['minimum_margin'] = 0
    cr.write_json(case / 'label.json', label)
    with pytest.raises(ValueError, match='positive'):
        cr.check_case(case)


def test_seeded_corpus_integrity_and_label_structure():
    cases = sorted(cr.DEFAULT_CORPUS.glob('*/label.json'))
    assert len(cases) >= 8
    for path in cases:
        cr.verify_integrity(path.parent)
        cr.validate_label(cr.read_json(path))
        replay_path = path.parent / 'replay.json'
        if replay_path.exists():
            replay = cr.read_json(replay_path)
            if replay['origin'] == 'offline_reconstruction':
                assert replay['root_checkpoint']['status'] == 'PASS'
                assert replay['runtime']['assembly_sha256']
    # These tests validate the harness; known policy failures belong in check --strict.
