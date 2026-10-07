"""Isolated headless sandbox sessions, immutable scenarios and preference records."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import re
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.card_catalog import load_card_catalog, resolve_card_description
from controller.combat_regressions import require_ok, verify_integrity
from controller.deck_profile import load_deck_profile
from controller.live_session import read_json, write_json
from controller.preference_model import LinearPreferenceModel, Scorer
from controller.combat_scoring import CombatScoring
from controller.sandbox_features import OBSERVATION_MODE, extract_features, schema
from controller.sandbox_history import action_command, with_restore_recipe, recipe_with_prefix
from controller.search.actions import SearchAction, cli_payload_for_action
from controller.search.combat_search import CombatSearcher, CombatSpec
from controller.search.state_cache import canonicalize_search_state_for_plan_reuse as semantic
from controller.search.state_cache import diff_plan_reuse_states

ENCOUNTERS = {'FUZZY_WURM_CRAWLER_WEAK': '单体 · 绒毛蠕虫',
              'CORPSE_SLUGS_WEAK': '多体 · 腐尸蛞蝓', 'SLIMES_WEAK': '多体 · 史莱姆'}
CASE_NAMES = {'rupture_vs_slimed': '撕裂与粘液', 'rupture_before_self_damage': '撕裂与自伤',
              'potion_search_budget': '药水与搜索预算', 'explosive_ampoule_timeout': '爆炸安瓿',
              'summoned_enemy_drift': '召唤敌人状态', 'potion_generated_hand_drift': '药水生成手牌'}


class StaleSandboxFixtureError(ValueError):
    """A recorded engine fixture belongs to a different headless runtime."""


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', value):
        raise ValueError('Invalid identifier')
    return value


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


class SandboxRuntime:
    def __init__(self, root: Path):
        self.root = root
        self.cli = Sts2CliAdapter(CliConfig(root))
        self.state = {}
        self.decision = {}
        self.root_state = {}
        self.turn_history = []
        self._turn_number = None

    def observe(self, result: dict) -> dict:
        require_ok(result, 'sandbox action')
        self.decision = result
        decision = result.get('decision')
        if decision in {'card_select', 'bundle_select'}:
            if decision == 'bundle_select':
                raise ValueError('Bundle selection is not enabled in this sandbox')
            return self.state
        if decision != 'combat_play':
            builder = CombatSearcher(None, CombatSpec('Ironclad', 'SANDBOX', 'fixed'))
            builder._root_summary = builder._combat_summary(self.root_state)
            self.state = builder._build_terminal_search_state(result)
            return self.state
        self.state = require_ok(self.cli.get_search_state(timeout_s=20), 'read sandbox')['combat_state_for_search']
        return self.state

    def restore(self, scenario: dict):
        self.cli.start()
        recipe = scenario.get('restore_recipe')
        if recipe:
            if recipe['kind'] == 'recorded_prefix':
                result = require_ok(self.cli.send({'cmd': 'load_save', 'json': recipe['save_json'],
                                    'resume_room': recipe['anchor_room'], 'lang': 'en'}, timeout_s=20), 'load history anchor')
            elif recipe['kind'] == 'generated_prefix':
                require_ok(self.cli.start_run(seed=recipe['seed']), 'start generated anchor')
                require_ok(self.cli.set_player(deck=recipe['deck'], hp=recipe['configure']['hp'], potions=[]), 'restore deck')
                result = require_ok(self.cli.enter_room('combat', encounter=recipe['encounter']), 'restore encounter')
                configure = copy.deepcopy(recipe['configure'])
                if configure.get('enemy_hp_value'):
                    current = self.cli.get_search_state()['combat_state_for_search']
                    configure['enemy_hp'] = [configure.pop('enemy_hp_value')] * len(current['combat']['enemies'])
                result = require_ok(self.cli.send(configure, timeout_s=20), 'restore scene configuration')
            else:
                raise ValueError('Unsupported restore recipe')
            for command in recipe['commands']:
                # Archived prefixes predate explicit reward item actions. Walk
                # the native reward boundary before replaying their next step;
                # the live controller always mirrors each visible claim itself.
                while result.get('decision') == 'combat_reward':
                    rewards = result.get('rewards') or []
                    noncard = next((row for row in rewards if row.get('reward_type') != 'Card'), None)
                    card = next((row for row in rewards if row.get('reward_type') == 'Card'), None)
                    if noncard is not None:
                        reward_action = ('claim_combat_reward', {'reward_index': noncard['index']})
                    elif card is not None and command['action'] in {'select_card_reward', 'skip_card_reward'}:
                        reward_action = ('claim_combat_reward', {'reward_index': card['index']})
                    else:
                        reward_action = ('finish_combat_rewards', {})
                    result = require_ok(self.cli.action(*reward_action, timeout_s=20), 'restore reward boundary')
                before = result
                result = require_ok(self.cli.action(command['action'], command['params'], timeout_s=20), 'restore action prefix')
                self._track_history(command, before, result)
            self.observe(result)
        else:
            require_ok(self.cli.import_combat_snapshot(scenario['snapshot_json'], 'sandbox_root'), 'import sandbox')
            self.observe(require_ok(self.cli.restore_combat_snapshot('sandbox_root'), 'restore sandbox'))
        differences = diff_plan_reuse_states(semantic(scenario['root_state']), semantic(self.state))
        if differences:
            raise ValueError(f'Sandbox root mismatch: {differences}')
        self.root_state = copy.deepcopy(self.state)

    def close(self):
        self.cli.stop()

    def apply(self, action: dict):
        before = self.decision
        if action['action_type'] == 'select_cards':
            result = self.cli.action('select_cards', {'indices': ','.join(map(str, action['indices']))}, timeout_s=20)
        else:
            result = self.cli.action(*cli_payload_for_action(SearchAction(**action)), timeout_s=20)
        require_ok(result, 'sandbox action')
        self._track_history(action_command(action), before, result)
        return self.observe(result)

    def _track_history(self, command, before, after):
        name, params = command['action'], command['params']
        if name == 'select_map_node':
            self.turn_history = []
            self._turn_number = None
        if name == 'play_card':
            index = params.get('card_index')
            card = next((row for row in before.get('hand') or [] if row.get('index') == index), {})
            self.turn_history.append({'action_type': name, 'card_index': index,
                                      'target_index': params.get('target_index'),
                                      'metadata': {'card_id': str(card.get('id', '')).replace('CARD.', '')}})
        elif name == 'use_potion':
            self.turn_history.append({'action_type': name, 'target_index': params.get('target_index'),
                                      'metadata': {'potion_index': params.get('potion_index')}})
        old_turn = before.get('round', self._turn_number)
        new_turn = after.get('round')
        if name == 'end_turn' or type(old_turn) is int and type(new_turn) is int and new_turn > old_turn:
            self.turn_history = []
        if type(new_turn) is int:
            self._turn_number = new_turn


class SandboxStore:
    def __init__(self, root: Path, directory: Path):
        self.root, self.directory = root, directory
        for kind in ('scenarios', 'plans', 'labels', 'drafts', 'demonstrations', 'skips'):
            (directory / kind).mkdir(parents=True, exist_ok=True)

    def put(self, kind: str, value: dict):
        path = self.directory / kind / (identifier(value['id']) + '.json')
        if path.exists():
            raise ValueError('Immutable record already exists')
        write_json(path, value)

    def get(self, kind: str, record_id: str) -> dict:
        return read_json(self.directory / kind / (identifier(record_id) + '.json'))

    def all(self, kind: str) -> list[dict]:
        rows = [read_json(path) for path in (self.directory / kind).glob('*.json')]
        return sorted(rows, key=lambda row: (row.get('created_at', 0), row['id']))

    def scenario(self, scene_id: str) -> dict:
        identifier(scene_id)
        cached = self.directory / 'scenarios' / (scene_id + '.json')
        if cached.is_file():
            return with_restore_recipe(read_json(cached), self.root)
        if scene_id.startswith('case_'):
            case_id = scene_id[5:]
            folder = self.root / 'data/combat_regressions' / identifier(case_id)
            verify_integrity(folder)
            value = read_json(folder / 'replay.json')
            if not value.get('snapshot_json'):
                raise ValueError('This case has no engine snapshot')
            evidence = read_json(folder / 'evidence.json')
            scenario = {'schema_version': 1, 'id': scene_id, 'title': CASE_NAMES.get(case_id, case_id),
                    'source': 'recorded', 'stage': 'mid', 'observation_mode': OBSERVATION_MODE,
                    'snapshot_json': value['snapshot_json'], 'root_state': value['root_state'],
                    'profile': load_deck_profile(), 'provenance': evidence['source'],
                    'family_id': evidence['source']['session'],
                    'runtime_hash': (value.get('runtime') or {}).get('headless_sha256')}
            self.put('scenarios', scenario)
            return with_restore_recipe(scenario, self.root)
        return with_restore_recipe(self.get('scenarios', scene_id), self.root)

    def scenes(self) -> list[dict]:
        scenes = []
        for path in sorted((self.root / 'data/combat_regressions').glob('*/replay.json')):
            value = read_json(path)
            if value.get('snapshot_json'):
                scenes.append({'id': 'case_' + path.parent.name, 'title': CASE_NAMES.get(path.parent.name, path.parent.name),
                               'source': 'recorded', 'stage': 'mid'})
        scenes.extend({key: value[key] for key in ('id', 'title', 'source', 'stage')} for value in self.all('scenarios'))
        return list({scene['id']: scene for scene in scenes}.values())

    def dataset(self) -> dict:
        plans = self.all('plans')
        scene_ids = {plan['scenario_id'] for plan in plans} | {scene['id'] for scene in self.all('scenarios')}
        return {'schema_version': 2, 'feature_schema': schema(),
                'scenarios': [self.scenario(scene_id) for scene_id in sorted(scene_ids)],
                'plans': plans, 'labels': self.all('labels'),
                'demonstrations': self.all('demonstrations'), 'skips': self.all('skips')}


class SandboxManager:
    def __init__(self, root: Path, directory: Path, profile_path: Path, scorer: Scorer | None = None):
        self.root, self.profile_path = root, profile_path
        self.store = SandboxStore(root, directory)
        self.cards = {card['id']: card for card in load_card_catalog(root)}
        self.names = {}
        for kind in ('monsters', 'powers', 'potions'):
            path = root / 'third_party/sts2-cli/localization_zhs' / (kind + '.json')
            self.names.update(read_json(path) if path.is_file() else {})
        self.card_texts = read_json(root / 'third_party/sts2-cli/localization_zhs/cards.json')
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='sandbox')
        self.lock = threading.RLock()
        self.runtime = None
        self.scene = None
        self.history = []
        self.future = []
        self.revision = 0
        self.explored = False
        self.busy = False
        self.closed = False
        self.job = None
        self.requests = {}
        self.batch = {'source': 'random', 'stage': 'mixed', 'position': 'mixed'}
        self.last_submission = None
        self.model = scorer
        self.scoring = CombatScoring()
        self.agent_hint = None
        self.hint_events = []
        self.runtime_hash = hashlib.sha256((root / CliConfig(root).dll_relpath).read_bytes()).hexdigest()
        self.published = {'active': False, 'revision': 0}
        draft = self.store.directory / 'drafts/current.json'
        if draft.is_file() and not self._draft_eligible(load_deck_profile(self.profile_path)):
            previous = read_json(draft)
            # Keep rejected old-profile work as an immutable draft-history record.
            archive = self.store.directory / 'drafts/history' / (fingerprint(previous) + '.json')
            if not archive.exists():
                write_json(archive, previous)

    def name(self, raw: str) -> str:
        return self.names.get(raw + '.title') or self.names.get(raw + '.name') or raw.replace('_', ' ').title()

    def _score(self, features: dict) -> dict:
        if self.model is None:
            return self.scoring.explain_features(features)
        scores = self.model.score_batch([features])
        if len(scores) != 1 or not math.isfinite(float(scores[0])):
            raise ValueError('Scorer must return one finite score per feature record')
        explain = getattr(self.model, 'explain', None)
        details = explain(features) if callable(explain) else {
            'kind': getattr(self.model, 'kind', type(self.model).__name__), 'contributions': []}
        return {**details, 'score': float(scores[0]), 'feature_version': features['version']}

    def catalog(self) -> dict:
        profile = load_deck_profile(self.profile_path)
        available, excluded = self._eligible_scenes(profile)
        return {'scenarios': available, 'excluded_scenarios': excluded, 'feature_schema': schema(),
                'encounters': [{'id': 'random', 'name': '随机遭遇'}] + [{'id': key, 'name': value} for key, value in ENCOUNTERS.items()],
                'cards': list(self.cards.values()), 'whitelist': profile['cards'],
                'draft_available': self._draft_eligible(profile)}

    def _outside_profile(self, scene: dict, profile: dict) -> list[str]:
        allowed = {row['id'] for row in profile['cards']}
        player = json.loads(json.loads(scene['snapshot_json'])['PlayerJson'])
        ids = {row['id']['Entry'] for row in player.get('deck', [])}
        # Status cards created by enemies remain valid game effects; unrelated playable cards do not.
        for pile in ('hand', 'draw_pile', 'discard_pile', 'exhaust_pile'):
            for card in (scene['root_state'].get('combat') or {}).get(pile, []):
                card_id = card.get('card_id')
                if str(self.cards.get(card_id, {}).get('type', '')).lower() not in {'status', 'curse'}:
                    ids.add(card_id)
        return sorted(str(value) for value in ids - allowed if value)

    def _eligible_scenes(self, profile: dict) -> tuple[list, int]:
        available, excluded = [], 0
        for row in self.store.scenes():
            try:
                scene = self.store.scenario(row['id'])
                if self._outside_profile(scene, profile):
                    excluded += 1
                else:
                    available.append(row)
            except (ValueError, KeyError, OSError):
                excluded += 1
        return available, excluded

    def _draft_eligible(self, profile: dict) -> bool:
        path = self.store.directory / 'drafts/current.json'
        if not path.is_file():
            return False
        try:
            return not self._outside_profile(self.store.scenario(read_json(path)['scenario_id']), profile)
        except (ValueError, KeyError, OSError):
            return False

    def status(self) -> dict:
        with self.lock:
            return {**copy.deepcopy(self.published), 'busy': self.busy, 'job': copy.deepcopy(self.job)}

    def submit(self, command: str, options: dict, revision: int, request_id: str) -> dict:
        identifier(request_id)
        allowed = {'load', 'generate', 'resume', 'action', 'undo', 'redo', 'reset', 'save_plan', 'agent', 'label',
                   'next', 'demonstrate', 'skip', 'checkpoint'}
        if command not in allowed:
            raise ValueError('Unknown sandbox command')
        payload_hash = fingerprint([command, options, revision])
        with self.lock:
            if request_id in self.requests:
                if self.requests[request_id] != payload_hash:
                    raise ValueError('Request ID reused with different content')
                return self.status()
            if self.closed or self.busy:
                raise ValueError('Sandbox is busy or closed')
            if revision != self.revision:
                raise ValueError('State changed; refresh before submitting')
            self.requests[request_id] = payload_hash
            if len(self.requests) > 512:
                self.requests.pop(next(iter(self.requests)))
            self.busy = True
            self.job = {'id': request_id, 'command': command, 'status': 'running'}
        self.executor.submit(self._run, command, copy.deepcopy(options))
        return self.status()

    def _run(self, command, options):
        try:
            getattr(self, '_cmd_' + command)(options)
            self._publish()
            with self.lock:
                self.job['status'] = 'completed'
        except Exception as exc:
            with self.lock:
                self.job.update(status='failed', error=str(exc))
            if command in {'action', 'undo', 'redo', 'reset', 'resume'}:
                # Any unknown engine outcome requires rebuilding from a recorded prefix.
                self.published['blocked'] = True
        finally:
            with self.lock:
                self.busy = False
            if self.closed and self.runtime:
                self.runtime.close()

    def _load(self, scene: dict):
        recorded_runtime = scene.get('runtime_hash') if scene.get('source') == 'recorded' else None
        if recorded_runtime and recorded_runtime != self.runtime_hash:
            raise StaleSandboxFixtureError(
                'STALE_FIXTURE: recorded sandbox runtime does not match the current headless engine; '
                'recapture this scene before using it as an executable test'
            )
        outside = self._outside_profile(scene, load_deck_profile(self.profile_path))
        if outside:
            raise ValueError('Scene contains cards outside the current whitelist: ' + ', '.join(outside))
        scene = with_restore_recipe(scene, self.root)
        candidate = SandboxRuntime(self.root)
        try:
            candidate.restore(scene)
        except Exception:
            candidate.close()
            raise
        if self.runtime:
            self.runtime.close()
        self.runtime, self.scene = candidate, scene
        self.scoring = CombatScoring(scene['stage'])
        self.agent_hint = None
        hint_path = self.store.directory / 'drafts/hint_exposures.json'
        self.hint_events = (read_json(hint_path) if hint_path.is_file() else {}).get(scene['id'], [])
        self.root_turn_history = copy.deepcopy(candidate.turn_history)
        if scene.get('restore_recipe'):
            self.scene = {**scene, 'turn_context': {**scene.get('turn_context', {}),
                          'position': 'mid' if candidate.turn_history else 'start',
                          'cards_played_before_root': sum(row['action_type'] == 'play_card' for row in candidate.turn_history),
                          'actions_before_root': len(candidate.turn_history), 'history_complete': True}}
        self.history, self.future = [], []
        exposure_path = self.store.directory / 'drafts/exposures.json'
        exposures = read_json(exposure_path) if exposure_path.is_file() else {}
        self.explored = scene.get('authored', scene['source'] == 'generated') or bool(exposures.get(scene['id']))

    def _cmd_load(self, options):
        self._load(self.store.scenario(options['id']))

    def _cmd_generate(self, options):
        profile = load_deck_profile(self.profile_path)
        stage = options.get('stage', 'mid')
        if stage not in {'early', 'mid', 'late'}:
            raise ValueError('Unknown stage')
        encounter = options.get('encounter', 'FUZZY_WURM_CRAWLER_WEAK')
        if encounter not in ENCOUNTERS and encounter != 'random':
            raise ValueError('Unknown encounter template')
        seed = str(options.get('seed') or uuid.uuid4().hex[:12])
        rng = random.Random(seed)
        if encounter == 'random':
            encounter = rng.choice(list(ENCOUNTERS))
        deck = []
        pool = [card['id'] for card in profile['cards'] for _ in range(card['max_copies'])]
        rng.shuffle(pool)
        deck += pool[:{'early': 6, 'mid': 10, 'late': 16}[stage]]
        if not deck:
            raise ValueError('Add cards to the whitelist before generating scenes')
        requested_hand = options.get('hand') or []
        if not isinstance(requested_hand, list) or len(requested_hand) > 10:
            raise ValueError('Hand must contain at most 10 cards')
        caps = {card['id']: card['max_copies'] for card in profile['cards']}
        for card, count in Counter(requested_hand).items():
            if card not in caps or count > caps[card]:
                raise ValueError('Hand exceeds whitelist/copy limits')
            deck.extend([card] * max(0, count - deck.count(card)))
        if any(card not in self.cards for card in deck):
            raise ValueError('Generated deck contains a card absent from the current catalog')
        hp = int(options.get('hp', 60))
        energy = int(options.get('energy', 3))
        if hp < 1 or hp > 80 or energy < 0 or energy > 10:
            raise ValueError('HP/energy out of range')
        runtime = SandboxRuntime(self.root)
        try:
            runtime.cli.start()
            require_ok(runtime.cli.start_run(seed=seed), 'start sandbox run')
            require_ok(runtime.cli.set_player(deck=deck, hp=hp, potions=[]), 'set sandbox deck')
            runtime.observe(require_ok(runtime.cli.enter_room('combat', encounter=encounter), 'enter sandbox combat'))
            configure = {'cmd': 'configure_sandbox', 'hp': hp, 'energy': energy,
                         'upgrade_hand': bool(options.get('upgrade_hand'))}
            if requested_hand:
                configure['hand'] = requested_hand
            enemy_hp = int(options.get('enemy_hp') or 0)
            if enemy_hp:
                configure['enemy_hp'] = [enemy_hp] * len(runtime.state['combat']['enemies'])
            runtime.observe(require_ok(runtime.cli.send(configure, timeout_s=20), 'configure sandbox'))
            position = options.get('position', 'start')
            if position not in {'start', 'mid', 'mixed'}:
                raise ValueError('Unknown scene position')
            if position == 'mixed':
                position = rng.choice(['start', 'mid'])
            warmup_count = int(options.get('warmup_steps', rng.randint(1, 3))) if position == 'mid' else 0
            if not 0 <= warmup_count <= 8:
                raise ValueError('Warmup steps must be 0-8')
            recipe = {'version': 1, 'kind': 'generated_prefix', 'seed': seed, 'deck': deck,
                      'encounter': encounter, 'configure': copy.deepcopy(configure), 'commands': []}
            played = 0
            for _ in range(warmup_count):
                choices = [row for row in runtime.state['combat']['available_actions'] if row['action_type'] == 'play_card']
                if not choices:
                    break
                action = rng.choice(choices)
                runtime.apply(action)
                recipe['commands'].append(action_command(action))
                played += 1
                if runtime.decision.get('decision') == 'card_select':
                    selection = runtime.decision
                    minimum = selection.get('min_select')
                    if type(minimum) is not int:
                        raise ValueError('Warmup selection count is unavailable')
                    indices = rng.sample([row['index'] for row in selection['cards']], minimum)
                    choice = {'action_type': 'select_cards', 'indices': indices}
                    runtime.apply(choice)
                    recipe['commands'].append(action_command(choice))
                if runtime.decision.get('decision') != 'combat_play':
                    raise ValueError('Warmup ended combat; choose another seed or fewer steps')
            if position == 'mid' and played == 0:
                raise ValueError('Warmup had no legal card action; choose another seed or more energy')
            if options.get('collection_mode') == 'batch_blind' and not any(
                    row['action_type'] == 'play_card' for row in runtime.state['combat']['available_actions']):
                raise ValueError('Warmup left no meaningful card choice; try another seed')
            require_ok(runtime.cli.capture_combat_snapshot('sandbox_created'), 'capture sandbox')
            snapshot = require_ok(runtime.cli.export_combat_snapshot('sandbox_created'), 'export sandbox')['snapshot_json']
            scene = {'schema_version': 1, 'id': 'scene_' + uuid.uuid4().hex,
                     'title': str(options.get('title') or ENCOUNTERS[encounter])[:100],
                     'source': 'generated', 'stage': stage, 'observation_mode': OBSERVATION_MODE,
                     'authored': options.get('collection_mode') != 'batch_blind',
                     'snapshot_json': snapshot, 'root_state': copy.deepcopy(runtime.state),
                     'profile': profile, 'parameters': {**options, 'seed': seed, 'deck': deck, 'resolved_encounter': encounter},
                     'runtime_hash': self.runtime_hash,
                     'restore_recipe': recipe,
                     'turn_context': {'position': position, 'cards_played_before_root': played,
                                      'root_turn': runtime.state['combat']['turn_number'], 'history_complete': True},
                     'created_at': time.time(),
                     'family_id': 'generated_' + fingerprint([profile, stage, encounter])[:16]}
        finally:
            runtime.close()
        # Fresh-process round trip is required before publishing generated scenes.
        self._load(scene)
        self.store.put('scenarios', scene)

    def _batch_options(self, options):
        batch = {**self.batch, **(options.get('batch') or {})}
        if (batch['source'] not in {'existing', 'random'} or batch['stage'] not in {'mixed', 'early', 'mid', 'late'}
                or batch['position'] not in {'mixed', 'start', 'mid'}):
            raise ValueError('Invalid batch settings')
        self.batch = batch

    def _cmd_next(self, options):
        self._batch_options(options)
        if self.batch['source'] == 'existing':
            visited = {row['scenario_id'] for kind in ('demonstrations', 'skips') for row in self.store.all(kind)}
            choices = [row for row in self._eligible_scenes(load_deck_profile(self.profile_path))[0] if row['id'] not in visited
                       and (self.batch['stage'] == 'mixed' or row['stage'] == self.batch['stage'])]
            choices.sort(key=lambda row: row['id'] == (self.scene or {}).get('id'))
            for choice in choices:
                scene = self.store.scenario(choice['id'])
                if self.batch['position'] != 'mixed' and scene.get('turn_context', {}).get('position') != self.batch['position']:
                    continue
                self._load(scene)
                return
            self.last_submission = {**(self.last_submission or {}), 'queue_exhausted': True}
            return
        for attempt in range(6):
            try:
                self._cmd_generate({'encounter': 'random', 'stage': random.choice(['early', 'mid', 'late'])
                                    if self.batch['stage'] == 'mixed' else self.batch['stage'],
                                    'position': self.batch['position'], 'collection_mode': 'batch_blind'})
                return
            except ValueError as exc:
                if 'Warmup' not in str(exc) or attempt == 5:
                    raise

    def _verify_trace(self):
        verifier = SandboxRuntime(self.root)
        try:
            verifier.restore(self.scene)
            for row in self.history:
                after = verifier.apply(row['action'])
                if (row['decision_after'] != verifier.decision.get('decision')
                        or diff_plan_reuse_states(semantic(row['after']), semantic(after))):
                    raise ValueError('Independent replay differs; demonstration not admitted')
        finally:
            verifier.close()

    def _observation(self, state: dict, native: dict) -> dict:
        combat = state.get('combat') or {}
        costs = {row['index']: row.get('display_cost', row.get('cost')) for row in native.get('hand') or []}
        player = combat.get('player') or {}
        return {'observation_mode': OBSERVATION_MODE, 'stage': self.scene['stage'],
                'turn': combat.get('turn_number'),
                'player': {key: copy.deepcopy(player.get(key)) for key in ('hp', 'max_hp', 'energy', 'block', 'powers')},
                'enemies': copy.deepcopy(combat.get('enemies') or []),
                'hand': [{**copy.deepcopy(card), 'index': i, 'observed_cost': costs.get(i)}
                         for i, card in enumerate(combat.get('hand') or [])]}

    def _cmd_demonstrate(self, options):
        if not self.runtime or not self.history or self.published.get('blocked'):
            raise ValueError('Play at least one action before submitting a demonstration')
        if self.runtime.decision.get('decision') == 'card_select':
            raise ValueError('Complete the pending card selection first')
        confidence = options.get('confidence', 'medium')
        if confidence not in {'low', 'medium', 'high'}:
            raise ValueError('Invalid confidence')
        self._verify_trace()
        decisions = []
        for index, row in enumerate(self.history):
            if row['action']['action_type'] == 'select_cards':
                if decisions:
                    decisions[-1].setdefault('selection_choices', []).append(copy.deepcopy(row['action']))
                continue
            observation = self._observation(row['before'], row.get('decision_before_payload') or {})
            prior_actions = copy.deepcopy(self.root_turn_history)
            for previous_row in self.history[:index]:
                if previous_row['action']['action_type'] == 'end_turn':
                    prior_actions = []
                elif previous_row['action']['action_type'] != 'select_cards':
                    prior_actions.append(copy.deepcopy(previous_row['action']))
            observation['turn_history'] = prior_actions
            decisions.append({'step': index, 'observation': observation,
                              'observation_hash': fingerprint(observation),
                              'state_hash': fingerprint(semantic(row['before'])),
                              'chosen_action': copy.deepcopy(row['action']),
                              'legal_actions': copy.deepcopy(row['before']['combat']['available_actions']),
                              'prefix': [action_command(item['action']) for item in self.history[:index]],
                              'hint_seen_before': bool(row.get('hint_seen_before')),
                              'label_semantics': 'chosen_action_accepted; alternatives_unlabeled'})
        content = fingerprint([self.scene['id'], [row['action'] for row in self.history], confidence,
                               str(options.get('note', ''))[:2000]])
        previous = next((row for row in self.store.all('demonstrations') if row.get('content_hash') == content), None)
        if previous is None:
            record = {'schema_version': 1, 'id': 'demo_' + uuid.uuid4().hex,
                      'annotation_kind': 'decision_demonstration', 'author': 'human',
                      'scenario_id': self.scene['id'], 'family_id': self.scene['family_id'],
                      'turn_context': copy.deepcopy(self.scene.get('turn_context') or {}),
                      'observation_mode': OBSERVATION_MODE, 'confidence': confidence,
                      'note': str(options.get('note', ''))[:2000], 'explored': self.explored,
                      'assisted': any(row['hint_seen_before'] for row in decisions),
                      'hint_exposures': copy.deepcopy(self.hint_events),
                      'decisions': decisions, 'trace': copy.deepcopy(self.history),
                      'completion': 'combat_terminal' if self.runtime.state.get('terminal_decision') else
                                    'turn_complete' if self.finished() else 'partial',
                      'verified': True, 'history_complete': bool(self.scene.get('restore_recipe')),
                      'runtime_hash': self.runtime_hash, 'content_hash': content, 'created_at': time.time()}
            self.store.put('demonstrations', record)
        else:
            record = previous
        self.last_submission = {'id': record['id'], 'kind': 'demonstration', 'steps': len(decisions), 'saved': True,
                                'scenario_id': self.scene['id']}
        # Publish the durable receipt before preparing another scene; failures cannot hide a successful save.
        self._publish()
        if options.get('next'):
            self._cmd_next(options)

    def _cmd_skip(self, options):
        if self.scene:
            self.store.put('skips', {'schema_version': 1, 'id': 'skip_' + uuid.uuid4().hex,
                                   'scenario_id': self.scene['id'], 'reason': 'uncertain_or_skipped',
                                   'note': str(options.get('note', ''))[:2000], 'created_at': time.time()})
        self._cmd_next(options)

    def _cmd_checkpoint(self, options):
        if (not self.runtime or self.runtime.decision.get('decision') != 'combat_play'
                or self.published.get('blocked') or not self.history):
            raise ValueError('Need a completed player-decision boundary after an action')
        self._verify_trace()
        require_ok(self.runtime.cli.capture_combat_snapshot('sandbox_checkpoint'), 'capture decision point')
        snapshot = require_ok(self.runtime.cli.export_combat_snapshot('sandbox_checkpoint'), 'export decision point')['snapshot_json']
        since_end = []
        for row in self.history:
            if row['action']['action_type'] == 'end_turn':
                since_end = []
            else:
                since_end.append(row)
        same_turn = self.runtime.state['combat']['turn_number'] == self.scene['root_state']['combat']['turn_number']
        initial_plays = self.scene.get('turn_context', {}).get('cards_played_before_root') or 0
        played = (initial_plays if same_turn else 0) + sum(row['action']['action_type'] == 'play_card' for row in since_end)
        scene = {**copy.deepcopy(self.scene), 'schema_version': 2, 'id': 'scene_' + uuid.uuid4().hex,
                 'title': str(options.get('title') or self.scene['title'] + ' · 决策点')[:100],
                 'snapshot_json': snapshot, 'root_state': copy.deepcopy(self.runtime.state),
                 'restore_recipe': recipe_with_prefix(self.scene, self.history),
                 'parent_scenario_id': self.scene['id'], 'created_at': time.time(),
                 'turn_context': {'position': 'mid' if played else 'start', 'cards_played_before_root': played,
                                  'root_turn': self.runtime.state['combat']['turn_number'], 'history_complete': True}}
        self._load(scene)
        self.store.put('scenarios', scene)
        self.explored = True

    def _legal(self) -> list[dict]:
        if self.runtime is None or self.finished():
            return []
        if self.runtime.decision.get('decision') == 'card_select':
            return []
        return [row for row in (self.runtime.state.get('combat') or {}).get('available_actions', [])
                if row['action_type'] in {'play_card', 'end_turn', 'use_potion'}
                and (row.get('metadata') or {}).get('potion_id') not in
                {'LIQUID_MEMORIES', 'COLORLESS_POTION', 'STABLE_SERUM'}]

    def finished(self) -> bool:
        return bool(self.runtime and (self.runtime.state.get('terminal_decision') or
                    self.history and self.history[-1]['action']['action_type'] == 'end_turn'))

    def _perform(self, action):
        before = copy.deepcopy(self.runtime.state)
        decision_before = copy.deepcopy(self.runtime.decision)
        after = copy.deepcopy(self.runtime.apply(action))
        if action['action_type'] == 'end_turn' and not after.get('terminal_decision'):
            if (after.get('combat') or {}).get('turn_number', 0) <= (before.get('combat') or {}).get('turn_number', 0):
                raise ValueError('Enemy turn did not settle')
        self.history.append({'action': copy.deepcopy(action), 'before': before, 'after': after,
                             'hint_seen_before': bool(self.hint_events),
                             'decision_after': self.runtime.decision.get('decision'),
                             'after_state_complete': self.runtime.decision.get('decision') != 'card_select',
                             'decision_before_payload': decision_before,
                             'decision_after_payload': copy.deepcopy(self.runtime.decision)})
        exposure_path = self.store.directory / 'drafts/exposures.json'
        exposures = read_json(exposure_path) if exposure_path.is_file() else {}
        exposures[self.scene['id']] = True
        write_json(exposure_path, exposures)

    def _cmd_action(self, options):
        if self.published.get('blocked') or self.finished():
            raise ValueError('Reset or undo before continuing')
        if self.runtime.decision.get('decision') == 'card_select':
            choice = self.runtime.decision
            indices = options.get('indices')
            legal = {card['index'] for card in choice.get('cards', [])}
            minimum, maximum = choice.get('min_select'), choice.get('max_select')
            if (not isinstance(indices, list) or any(type(i) is not int for i in indices)
                    or len(set(indices)) != len(indices) or not set(indices) <= legal
                    or minimum is None or maximum is None or not minimum <= len(indices) <= maximum):
                raise ValueError('Invalid selection')
            action = {'action_type': 'select_cards', 'indices': indices}
        else:
            action_id = options.get('action_id')
            actions = {f'{self.revision}:{i}': row for i, row in enumerate(self._legal())}
            if action_id not in actions:
                raise ValueError('Action is no longer legal')
            action = actions[action_id]
        self._perform(action)
        self.future = []

    def _rebuild(self, prefix: list[dict]):
        runtime = SandboxRuntime(self.root)
        try:
            runtime.restore(self.scene)
            for row in prefix:
                after = runtime.apply(row['action'])
                if row.get('decision_after') != runtime.decision.get('decision'):
                    raise ValueError('Replay decision differs')
                if diff_plan_reuse_states(semantic(row['after']), semantic(after)):
                    raise ValueError('Replay diverged; preserving the original trajectory')
        except Exception:
            runtime.close()
            raise
        self.runtime.close()
        self.runtime = runtime

    def _cmd_undo(self, options):
        if not self.history:
            raise ValueError('No action to undo')
        prefix = self.history[:-1]
        self._rebuild(prefix)
        self.future.append(self.history[-1])
        self.history = prefix
        self.explored = True

    def _cmd_redo(self, options):
        if not self.future:
            raise ValueError('No action to redo')
        row = self.future[-1]
        self._rebuild(self.history + [row])
        self.history.append(row)
        self.future.pop()

    def _cmd_reset(self, options):
        self._rebuild([])
        self.history, self.future = [], []
        self.explored = True

    def _cmd_resume(self, options):
        draft = read_json(self.store.directory / 'drafts/current.json')
        self._load(self.store.scenario(draft['scenario_id']))
        self._rebuild(draft['history'])
        self.history = draft['history']
        self.future = draft.get('future', [])
        self.explored = True
        self.batch = draft.get('batch') or self.batch

    def _save(self, runtime, history, source, name, explored):
        verifier = SandboxRuntime(self.root)
        try:
            verifier.restore(self.scene)
            for row in history:
                after = verifier.apply(row['action'])
                if diff_plan_reuse_states(semantic(row['after']), semantic(after)):
                    raise ValueError('Independent plan replay differs; annotation not admitted')
            if verifier.state.get('terminal_decision') and verifier.state['terminal_decision'] not in {
                    'victory', 'card_reward', 'map_select', 'treasure', 'shop', 'rest_site', 'game_over', 'defeat'}:
                raise ValueError('Unresolved terminal cannot be saved as a completed plan')
        finally:
            verifier.close()
        features = self.scoring.features(self.scene['root_state'], runtime.state, history)
        plan = {'schema_version': 1, 'id': 'plan_' + uuid.uuid4().hex,
                'scenario_id': self.scene['id'], 'family_id': self.scene['family_id'],
                'name': str(name or ('人工方案' if source == 'human' else 'Agent方案'))[:100],
                'source': source, 'explored': explored, 'observation_mode': OBSERVATION_MODE,
                'assisted': any(row.get('hint_seen_before') for row in history),
                'preference_scope': 'realized_outcome', 'verified': True, 'trace': copy.deepcopy(history),
                'leaf_state': copy.deepcopy(runtime.state), 'features': features,
                'score': self._score(features), 'created_at': time.time()}
        plan['runtime_hash'] = self.runtime_hash
        plan['snapshot_sha256'] = hashlib.sha256(self.scene['snapshot_json'].encode('utf-8')).hexdigest()
        self.store.put('plans', plan)
        return plan

    def _cmd_save_plan(self, options):
        if not self.finished() or self.published.get('blocked'):
            raise ValueError('Complete the turn before saving a plan')
        self._save(self.runtime, self.history, 'human', options.get('name'), self.explored)

    def _cmd_agent(self, options):
        from controller.combat_step import CombatStepConfig, PlanState, decide_combat_action
        from controller.search.combat_search import CombatWorkerPool
        from controller.run_agent import resolve_live_planned_action
        if not self.runtime or self.published.get('blocked'):
            raise ValueError('Load a valid scene before requesting an agent choice')
        current = options.get('origin', 'root') == 'current'
        if current and (self.finished() or self.runtime.decision.get('decision') != 'combat_play'):
            raise ValueError('Current-position hint requires a live player decision')
        prefix = copy.deepcopy(self.history) if current else []
        position_state = copy.deepcopy(self.runtime.state if current else self.scene['root_state'])
        # Mark hint exposure durably even if search fails or the user reloads later.
        self.hint_events.append({'position_hash': fingerprint(semantic(position_state)),
                                 'prefix_length': len(prefix), 'time': time.time(),
                                 'scorer': self.scoring.identity})
        hint_path = self.store.directory / 'drafts/hint_exposures.json'
        exposures = read_json(hint_path) if hint_path.is_file() else {}
        exposures[self.scene['id']] = self.hint_events
        write_json(hint_path, exposures)
        self.agent_hint = None
        self._publish()
        runtime = SandboxRuntime(self.root)
        cfg = CliConfig(self.root)
        pool = CombatWorkerPool(cfg)
        history = []
        try:
            runtime.restore(self.scene)
            for row in prefix:
                runtime.apply(row['action'])
            if diff_plan_reuse_states(semantic(position_state), semantic(runtime.state)):
                raise ValueError('Hint start state differs after history replay')
            pool.prewarm(2)
            plan = PlanState()
            config = CombatStepConfig(cli_cfg=cfg, spec=CombatSpec('Ironclad', 'SANDBOX', 'fixed'),
                                      max_workers=2, user_parallel=True, worker_pool=pool,
                                      reuse_cli_processes=True, max_search_ms=3000, capture_root_topk=8,
                                      scorer_stage=self.scene['stage'], scorer_model=self.scoring.payload)
            decision = decide_combat_action(runtime.cli, runtime.state, config, plan)
            if decision.fell_back or decision.search_failed or decision.chosen is None:
                raise ValueError('Agent did not produce a usable searched plan')
            for action in [decision.chosen] + plan.sequence:
                resolved = resolve_live_planned_action(runtime.state, action)
                if resolved is None:
                    raise ValueError('Agent sequence no longer legal during replay')
                metadata = dict(action.metadata or {})
                if 'potion_index' in resolved[1]:
                    metadata['potion_index'] = resolved[1]['potion_index']
                raw = {'action_type': resolved[0], 'card_index': resolved[1].get('card_index'),
                       'target_index': resolved[1].get('target_index'), 'metadata': metadata}
                before = copy.deepcopy(runtime.state)
                decision_before = copy.deepcopy(runtime.decision)
                after = copy.deepcopy(runtime.apply(raw))
                history.append({'action': raw, 'before': before, 'after': after,
                                'decision_after': runtime.decision.get('decision'),
                                'after_state_complete': runtime.decision.get('decision') != 'card_select',
                                'decision_before_payload': decision_before,
                                'decision_after_payload': copy.deepcopy(runtime.decision)})
            if not history or not (runtime.state.get('terminal_decision') or history[-1]['action']['action_type'] == 'end_turn'):
                raise ValueError('Agent plan did not reach a comparable boundary')
            verification_diff = diff_plan_reuse_states(semantic(decision.leaf_state or {}), semantic(runtime.state))
            replay_score = self.scoring.explain(position_state, runtime.state, history)
            score_matches = math.isclose(float(decision.search_score), replay_score['score'], rel_tol=1e-8, abs_tol=1e-6)
            verified = not verification_diff and score_matches
            self.agent_hint = {
                'position_hash': fingerprint(semantic(position_state)), 'prefix_length': len(prefix),
                'origin': 'current' if current else 'root',
                'line': [row['action'] for row in history],
                'score': decision.search_score, 'explanation': decision.score_explanation,
                'scorer': self.scoring.identity, 'replay_score': replay_score['score'],
                'verification': 'PASS' if verified else 'FAIL', 'differences': verification_diff,
                'score_matches': score_matches, 'search_ms': decision.timing.get('search_ms'),
                'audit': decision.decision_audit, 'candidates': decision.root_candidates or [],
                'budget_ms': 3000, 'information_scope': 'full_snapshot_search',
            }
            if verified and not current:
                self._save(runtime, history, 'agent', 'Agent方案', True)
        finally:
            pool.close()
            runtime.close()

    def _cmd_label(self, options):
        a = self.store.get('plans', options['a'])
        b = self.store.get('plans', options['b'])
        if a['id'] == b['id'] or a['scenario_id'] != b['scenario_id'] or a['scenario_id'] != self.scene['id']:
            raise ValueError('Compare two different plans from this scene')
        preference = options.get('preference')
        if preference not in {'a', 'b', 'tie', 'unsure'}:
            raise ValueError('Invalid preference')
        confidence = options.get('confidence', 'medium')
        if confidence not in {'low', 'medium', 'high'}:
            raise ValueError('Invalid confidence')
        previous = [label for label in self.store.all('labels')
                    if {label['a'], label['b']} == {a['id'], b['id']}]
        self.store.put('labels', {'schema_version': 1, 'id': 'label_' + uuid.uuid4().hex,
                                 'scenario_id': self.scene['id'], 'a': a['id'], 'b': b['id'],
                                 'preference': preference, 'confidence': confidence,
                                 'note': str(options.get('note', ''))[:2000], 'author': 'human',
                                 'review_status': 'confirmed', 'created_at': time.time(),
                                 'supersedes': max(previous, key=lambda row: row['created_at'])['id'] if previous else None,
                                 'preference_scope': 'realized_outcome'})

    def _publish(self):
        if not self.runtime:
            return
        self.revision += 1
        state = self.runtime.state
        combat = state.get('combat') or {}
        native = {card['index']: card for card in self.runtime.decision.get('hand') or []}
        hand = []
        for index, row in enumerate(combat.get('hand') or []):
            details = native.get(index) or {}
            card = self.cards.get(row['card_id'], {
                'id': row['card_id'], 'name_zh': self.card_texts.get(row['card_id'] + '.title', row['card_id']),
                'description_zh': self.card_texts.get(row['card_id'] + '.description', ''),
                'type': details.get('type', '?')})
            cost = details.get('display_cost', details.get('cost', row.get('current_cost')))
            if cost is None:
                cost = card.get('cost_upgraded' if row.get('upgrade') else 'cost', '?')
            if isinstance(cost, (int, float)) and cost < 0:
                cost = '—'
            text = card.get('description_zh_template') or card.get('description_zh') or ''
            if details.get('stats'):
                text = resolve_card_description(text, details['stats'], upgraded=bool(row.get('upgrade')), language='zh')
            elif row.get('upgrade'):
                text = card.get('description_zh_upgraded', text)
            text = resolve_card_description(text, details.get('stats') or card.get('stats'),
                                            upgraded=bool(row.get('upgrade')), language='zh')
            hand.append({'index': index, 'id': row['card_id'], 'name': card.get('name_zh') or card.get('name_en'),
                         'cost': cost, 'upgraded': bool(row.get('upgrade')), 'description': text,
                         'type': card.get('type', '?')})
        player = copy.deepcopy(combat.get('player') or (state.get('terminal_result') or {}).get('player') or {})
        player = {key: player.get(key) for key in ('hp', 'max_hp', 'energy', 'block', 'powers')}
        for power in player.get('powers') or []:
            power['name'] = self.name(power.get('id', ''))
        enemies = copy.deepcopy(combat.get('enemies') or [])
        for enemy in enemies:
            enemy['name'] = self.name(enemy.get('monster_id', ''))
            for power in enemy.get('powers') or []:
                power['name'] = self.name(power.get('id', ''))
        actions = [{**copy.deepcopy(row), 'id': f'{self.revision}:{i}'} for i, row in enumerate(self._legal())]
        potions = {}
        for action in actions:
            if action['action_type'] == 'use_potion':
                meta = action['metadata']
                potions[meta['potion_index']] = {'index': meta['potion_index'], 'id': meta['potion_id'],
                                                'name': self.name(meta['potion_id'])}
        features = self.scoring.features(self.scene['root_state'], state, self.history)
        plans = []
        for plan in self.store.all('plans'):
            if plan['scenario_id'] == self.scene['id']:
                plans.append({key: plan[key] for key in ('id', 'name', 'source', 'verified', 'explored', 'features', 'score')})
                plans[-1]['line'] = [row['action'] for row in plan['trace']]
                # Display today's scorer without modifying historical stored annotations.
                current_features = self.scoring.features(self.scene['root_state'], plan['leaf_state'], plan['trace'])
                plans[-1]['recorded_score'] = plan['score']
                plans[-1]['features'] = current_features
                plans[-1]['score'] = self._score(current_features)
        selection = None
        if self.runtime.decision.get('decision') == 'card_select':
            selection = {key: self.runtime.decision.get(key) for key in ('cards', 'min_select', 'max_select', 'prompt')}
        demonstrations = self.store.all('demonstrations')
        stats = {'scenes': len({row['scenario_id'] for row in demonstrations}),
                 'demonstrations': len(demonstrations),
                 'decisions': len({(decision['state_hash'], fingerprint(decision['chosen_action']))
                                   for row in demonstrations for decision in row['decisions']}),
                 'skipped': len({row['scenario_id'] for row in self.store.all('skips')})}
        with self.lock:
            self.published = {'active': True, 'revision': self.revision,
                'scene': {key: self.scene[key] for key in ('id', 'title', 'source', 'stage', 'observation_mode')},
                'turn_context': self.scene.get('turn_context') or {},
                'root_turn_history': self.root_turn_history,
                'scorer': self.scoring.identity, 'agent_hint': self.agent_hint,
                'hint_current': bool(self.agent_hint and self.agent_hint['position_hash'] == fingerprint(semantic(state))),
                'hint_seen': bool(self.hint_events),
                'batch': self.batch, 'collection': stats, 'last_submission': self.last_submission,
                'can_demonstrate': bool(self.history) and selection is None,
                'can_checkpoint': bool(self.history) and self.runtime.decision.get('decision') == 'combat_play',
                'player': player, 'enemies': enemies, 'hand': hand, 'actions': actions,
                'potions': list(potions.values()),
                'turn': combat.get('turn_number'), 'finished': self.finished(),
                'observation_phase': 'awaiting_selection' if selection else 'completed_boundary',
                'terminal': state.get('terminal_decision'), 'selection': selection,
                'can_undo': bool(self.history), 'can_redo': bool(self.future), 'explored': self.explored,
                'history': [row['action'] for row in self.history], 'plans': plans,
                'labels': [label for label in self.store.all('labels') if label['scenario_id'] == self.scene['id']],
                'features': features, 'score': self._score(features)}
        write_json(self.store.directory / 'drafts/current.json', {'scenario_id': self.scene['id'],
                   'history': self.history, 'future': self.future, 'explored': self.explored, 'batch': self.batch})

    def close(self):
        self.closed = True
        if not self.busy and self.runtime:
            self.runtime.close()
        self.executor.shutdown(wait=False, cancel_futures=True)
