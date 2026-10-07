"""Capture and persist reusable headless combat snapshots.

This module keeps raw engine snapshots separate from the sanitized search input.
It is deliberately independent from the live runner so the dashboard and
offline tools can share the same artifact format.
"""
from __future__ import annotations

import hashlib
import copy
import json
import os
import platform
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Optional


SCHEMA_VERSION = "sts2.combat_snapshot.v1"
VALIDATION_CONTRACT = 5
VALIDATION_SET_LIMIT = 48


def snapshot_index(log_root: Path, *, include_legacy: bool = False) -> list[dict[str, Any]]:
    """Metadata files are the index; incomplete writes never appear as success."""
    paths = list((log_root / 'combat_validation_set').glob('*/metadata.json'))
    paths.extend((log_root / 'combat_snapshots').glob('*/metadata.json'))
    if include_legacy:
        paths.extend(log_root.glob('*/combat_snapshots/*/metadata.json'))
    rows = []
    for path in paths:
        try:
            row = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(row, dict):
                continue
            row['artifact_dir'] = str(path.parent.resolve())
            # Legacy same-process restore was not an independent parity check.
            if row.get('schema') != 'sts2.combat_snapshot.v2':
                row.update(status='LEGACY_UNVERIFIED', reusable=False)
            rows.append(row)
        except (OSError, ValueError):
            continue
    return sorted(rows, key=lambda row: float(row.get('created_at_utc') or 0), reverse=True)


def capture_client_combat_snapshot(mod: Any, output_dir: Path, *, cli_config: Any,
                                   context: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Archive the live root without restoring or advancing the live shadow."""
    from controller.human_capture import validate_authoritative_snapshot
    from controller.interaction_state import classify_interaction, InteractionKind
    from controller.live_client_bridge import gameplay_observation
    from controller.rng_parity import compare_rng_snapshots
    from controller.run_agent import sanitize_snapshot_json_for_search
    from controller.engine_parity import (client_combat_checkpoint, headless_combat_checkpoint,
                                          compare_checkpoints)

    started = time.perf_counter()
    capture_id = time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:12]
    artifact = output_dir.resolve() / capture_id
    artifact.mkdir(parents=True, exist_ok=False)
    report = dict(schema='sts2.combat_snapshot.v2', snapshot_id=capture_id,
                  created_at_utc=time.time(), artifact_dir=str(artifact),
                  context=dict(context or {}), status='CAPTURING', reusable=False)
    try:
        before = mod.state()
        interaction = classify_interaction(before)
        if interaction.kind != InteractionKind.COMBAT or interaction.stage != 'ready':
            raise ValueError('Capture requires a stable player combat decision')
        _write_json(artifact / 'client_observation.json', before)
        before_rng = mod.rng_snapshot()
        _write_json(artifact / 'client_rng.json', before_rng)
        payload = mod.current_combat_snapshot_raw()
        _write_json(artifact / 'capture_response.json', payload)
        # Preserve the exact bytes even if envelope validation fails.
        if isinstance(payload.get('snapshot_json'), str):
            (artifact / 'raw_snapshot.json').write_bytes(payload['snapshot_json'].encode('utf-8'))
        snapshot, raw, digest = validate_authoritative_snapshot(payload)
        after = mod.state()
        after_rng = mod.rng_snapshot()
        _write_json(artifact / 'client_observation_after.json', after)
        _write_json(artifact / 'client_rng_after.json', after_rng)
        stability = compare_rng_snapshots(before_rng, after_rng)
        if gameplay_observation(before) != gameplay_observation(after) or not stability.passed:
            raise ValueError('Client state or RNG changed during capture')
        if snapshot.get('Seed') != before.get('run_id'):
            raise ValueError('Snapshot belongs to a different run')
        # The Mod's combat envelope does not contain the run location or selected
        # boss. Bind those fields from the stable, same-seed client observation.
        # Preserve raw_snapshot.json byte-for-byte. Verify an unsanitized,
        # context-bound copy before validating the derived search input.
        client_checkpoint = client_combat_checkpoint(before)
        run_context = {
            'ActIndex': int((before.get('run') or {})['act_id']),
            'ActFloor': int(client_checkpoint['run']['floor']),
            'BossEncounterId': client_checkpoint['run']['boss'],
        }
        restore_raw = _bind_run_context(raw.decode('utf-8'), run_context)
        search_raw, sanitize = sanitize_snapshot_json_for_search(restore_raw)
        (artifact / 'restore_snapshot.json').write_bytes(restore_raw.encode('utf-8'))
        (artifact / 'search_snapshot.json').write_bytes(search_raw.encode('utf-8'))
        report.update(status='CAPTURED', raw_snapshot='raw_snapshot.json',
                      restore_snapshot='restore_snapshot.json',
                      restore_sha256=_sha256_bytes(restore_raw.encode('utf-8')),
                      search_snapshot='search_snapshot.json', raw_sha256=digest, raw_bytes=len(raw),
                      search_sha256=_sha256_bytes(search_raw.encode('utf-8')), sanitize=sanitize,
                      run_context_source='stable_client_observation', run_context=run_context,
                      capture_protocol=payload.get('capture_protocol'), capture_stability='PASS',
                      run_id=before.get('run_id'), turn=before.get('turn'),
                      floor=(before.get('run') or {}).get('floor'))
        _write_json(artifact / 'metadata.json', report)
        dll = cli_config.repo_root / cli_config.dll_relpath
        report['runtime'] = dict(dll=str(dll), sha256=_sha256_bytes(dll.read_bytes()))
        report['compatibility'] = snapshot_compatibility(cli_config)
        report['source_sha256'] = {
            name: _sha256_bytes((cli_config.repo_root / name).read_bytes())
            for name in ('controller/search/combat_search.py', 'controller/run_agent.py',
                         'controller/combat_snapshot.py', 'scripts/profile_search_snapshot.py')
            if (cli_config.repo_root / name).is_file()
        }
        _verify_saved_snapshot(artifact, report, cli_config, before, before_rng)
        report.update(status='RESTORE_VERIFIED', reusable=True)
    except Exception as exc:
        report.update(status='VALIDATION_FAILED' if report.get('raw_sha256') else 'CAPTURE_FAILED',
                      reusable=False, error=str(exc))
    finally:
        report['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 3)
        _write_json(artifact / 'metadata.json', report)
    return report


def _bind_run_context(raw: str, run_context: dict[str, Any]) -> str:
    envelope = json.loads(raw)
    for key, value in run_context.items():
        if key in envelope and envelope[key] != value:
            raise ValueError(f'Client and combat snapshot disagree on {key}')
        envelope[key] = value
    return json.dumps(envelope, ensure_ascii=False, separators=(',', ':'))


def _search_expected_checkpoint(client_checkpoint: dict[str, Any],
                                sanitize: dict[str, Any]) -> dict[str, Any]:
    expected = copy.deepcopy(client_checkpoint)
    removed = list(sanitize.get('removed_potion_ids') or [])
    if len(removed) != int(sanitize.get('potions_removed') or 0):
        raise ValueError('Search potion removal record is incomplete')
    if not removed:
        return expected
    potions = expected['run']['potions']
    for potion_id in removed:
        if potion_id not in potions:
            raise ValueError(f'Search removed potion absent from client: {potion_id}')
        potions.remove(potion_id)
    return expected


def _runtime_roundtrip_differences(original: dict[str, Any],
                                   roundtrip: dict[str, Any]) -> list[str]:
    """Compare runtime values using the wire type's documented defaults.

    The Mod emits sparse dictionaries; the headless RuntimeValueSnapshot DTO
    emits its non-nullable IsPlayerCreature=false even on unrelated values.
    That default does not carry state and must not reject an otherwise exact
    restore. Keep all other fields strict, including true and object refs.
    """
    def canonical(value: Any) -> Any:
        if isinstance(value, dict):
            result = {key: canonical(item) for key, item in value.items()
                      if item is not None}
            if 'Kind' in result:
                result.setdefault('IsPlayerCreature', False)
            return result
        if isinstance(value, list):
            return [canonical(item) for item in value]
        return value

    checked = ('HookStates', 'PlayerCombatState', 'PlayerExtraState',
               'CombatHistoryEntries', 'ActivePowerRefs')
    differences = [key for key in checked
                   if canonical(original.get(key)) != canonical(roundtrip.get(key))]
    def card_piles(snapshot: dict[str, Any]) -> Any:
        players = ((snapshot.get('NetState') or {}).get('Players') or [])
        return [player.get('piles') for player in players]
    if canonical(card_piles(original)) != canonical(card_piles(roundtrip)):
        differences.append('NetState.Players.piles')
    return differences


def _verify_saved_snapshot(artifact: Path, report: dict[str, Any], cli_config: Any,
                           before: dict[str, Any], before_rng: dict[str, Any]) -> None:
    from cli.sts2_cli_adapter import Sts2CliAdapter
    from controller.engine_parity import (client_combat_checkpoint, headless_combat_checkpoint,
                                          compare_checkpoints)
    from controller.rng_parity import compare_rng_snapshots

    client_checkpoint = client_combat_checkpoint(before)
    search_expected = _search_expected_checkpoint(client_checkpoint, report.get('sanitize') or {})
    restore_raw = (artifact / 'restore_snapshot.json').read_bytes()
    search_raw = (artifact / 'search_snapshot.json').read_bytes()
    if _sha256_bytes(restore_raw) != report.get('restore_sha256'):
        raise ValueError('Unsanitized restore snapshot digest changed after capture')
    if _sha256_bytes(search_raw) != report.get('search_sha256'):
        raise ValueError('Search snapshot digest changed after capture')
    report['validation'] = {}

    def verify_one(raw: bytes, label: str, expected: dict[str, Any]) -> None:
        verifier = Sts2CliAdapter(cli_config)
        try:
            verifier.start()
            imported = verifier.import_combat_snapshot(raw.decode('utf-8'), report['snapshot_id'], timeout_s=20)
            if imported.get('success') is not True:
                raise ValueError(f'{label} independent import failed: {imported}')
            restored = verifier.restore_combat_snapshot(report['snapshot_id'], allow_full=True, timeout_s=20)
            suffix = '_raw' if label == 'raw' and restore_raw != search_raw else ''
            _write_json(artifact / f'restore_response{suffix}.json', restored)
            if restored.get('type') == 'error' or restored.get('decision') != 'combat_play':
                raise ValueError(f'{label} independent restore did not reach a player combat decision')
            if restored.get('restore_sanity_warning'):
                raise ValueError(f"{label} independent restore sanity warning: {restored['restore_sanity_warning']}")
            search = verifier.get_search_state(timeout_s=20).get('combat_state_for_search') or {}
            rng_response = verifier.send({'cmd': 'get_rng_snapshot'}, timeout_s=20)
            if rng_response.get('success') is not True or not isinstance(rng_response.get('rng'), dict):
                raise ValueError(f'{label} independent verifier returned incomplete RNG state')
            rng = rng_response['rng']
            _write_json(artifact / f'search_state{suffix}.json', search)
            _write_json(artifact / f'headless_rng{suffix}.json', rng)
            parity = compare_checkpoints(expected,
                                         headless_combat_checkpoint(restored, search, before['run_id']))
            rng_parity = compare_rng_snapshots(before_rng, rng)
            result = dict(fields_status=parity.status, differences=parity.differences,
                          rng_status='PASS' if rng_parity.passed else 'FAIL',
                          rng_differences=rng_parity.differences)
            report.setdefault('validation', {})[label] = result
            if parity.status != 'PASS' or not rng_parity.passed:
                raise ValueError(f'{label} independent restore field/RNG comparison failed')
            if label == 'raw':
                captured = verifier.capture_combat_snapshot(report['snapshot_id'] + '_roundtrip',
                                                           timeout_s=20)
                if captured.get('success') is not True:
                    raise ValueError(f'Raw restore could not be recaptured: {captured}')
                exported = verifier.export_combat_snapshot(report['snapshot_id'] + '_roundtrip',
                                                          timeout_s=20)
                if exported.get('success') is not True:
                    raise ValueError(f'Raw restore could not be exported: {exported}')
                roundtrip = json.loads(exported['snapshot_json'])
                original = json.loads(restore_raw)
                differences = _runtime_roundtrip_differences(original, roundtrip)
                result['runtime_roundtrip'] = {'status': 'PASS' if not differences else 'FAIL',
                                               'differences': differences}
                if differences:
                    raise ValueError(f'Independent runtime state roundtrip differs: {differences}')
        finally:
            verifier.stop()

    verify_one(restore_raw, 'raw', client_checkpoint)
    if restore_raw == search_raw:
        report['validation']['search'] = {'status': 'IDENTICAL_TO_RAW'}
    else:
        verify_one(search_raw, 'search', search_expected)
    report['validation']['scope'] = 'raw_checkpoint_rng_runtime_roundtrip_and_search_transform'


def revalidate_saved_combat_snapshot(artifact: Path, *, cli_config: Any) -> dict[str, Any]:
    """Recheck a collected root after a runtime change without touching the game."""
    artifact = artifact.resolve()
    report = json.loads((artifact / 'metadata.json').read_text(encoding='utf-8'))
    try:
        from controller.engine_parity import client_combat_checkpoint
        from controller.human_capture import validate_authoritative_snapshot
        from controller.live_client_bridge import gameplay_observation
        from controller.rng_parity import compare_rng_snapshots

        if report.get('schema') != 'sts2.combat_snapshot.v2':
            raise ValueError('Unsupported capture format')
        raw = (artifact / 'raw_snapshot.json').read_bytes()
        if _sha256_bytes(raw) != report.get('raw_sha256'):
            raise ValueError('Raw snapshot digest changed after capture')
        before = json.loads((artifact / 'client_observation.json').read_text(encoding='utf-8'))
        before_rng = json.loads((artifact / 'client_rng.json').read_text(encoding='utf-8'))
        after = json.loads((artifact / 'client_observation_after.json').read_text(encoding='utf-8'))
        after_rng = json.loads((artifact / 'client_rng_after.json').read_text(encoding='utf-8'))
        if gameplay_observation(before) != gameplay_observation(after) or not compare_rng_snapshots(before_rng, after_rng).passed:
            raise ValueError('Archived client capture was not stable')
        response = json.loads((artifact / 'capture_response.json').read_text(encoding='utf-8'))
        snapshot, validated_raw, _ = validate_authoritative_snapshot(response)
        if validated_raw != raw or snapshot.get('Seed') != before.get('run_id'):
            raise ValueError('Archived client snapshot does not match its observation')
        checkpoint = client_combat_checkpoint(before)
        expected_context = {'ActIndex': int((before.get('run') or {})['act_id']),
                            'ActFloor': int(checkpoint['run']['floor']),
                            'BossEncounterId': checkpoint['run']['boss']}
        restore_raw = _bind_run_context(raw.decode('utf-8'), expected_context)
        restore_path = artifact / 'restore_snapshot.json'
        if restore_path.exists() and restore_path.read_text(encoding='utf-8') != restore_raw:
            raise ValueError('Archived unsanitized restore snapshot changed')
        restore_path.write_bytes(restore_raw.encode('utf-8'))
        report['restore_snapshot'] = restore_path.name
        report['restore_sha256'] = _sha256_bytes(restore_raw.encode('utf-8'))
        from controller.run_agent import sanitize_snapshot_json_for_search
        expected_search, sanitize = sanitize_snapshot_json_for_search(restore_raw)
        search_raw = (artifact / 'search_snapshot.json').read_text(encoding='utf-8')
        if json.loads(search_raw) != json.loads(expected_search):
            raise ValueError('Archived search snapshot differs from declared search transform')
        report['sanitize'] = sanitize
        _verify_saved_snapshot(artifact, report, cli_config, before, before_rng)
        dll = cli_config.repo_root / cli_config.dll_relpath
        report['runtime'] = dict(dll=str(dll), sha256=_sha256_bytes(dll.read_bytes()))
        report['compatibility'] = snapshot_compatibility(cli_config)
        report.update(status='RESTORE_VERIFIED', reusable=True)
        report.pop('error', None)
    except Exception as exc:
        report.update(status='VALIDATION_FAILED', reusable=False, error=str(exc))
    _write_json(artifact / 'metadata.json', report)
    return report


def snapshot_compatibility(cli_config: Any) -> dict[str, Any]:
    """Only dependencies of native capture, restore and parity invalidate roots."""
    root = cli_config.repo_root
    files = {
        'headless': root / cli_config.dll_relpath,
        'game': root / 'third_party/sts2-cli/lib/sts2.dll',
        'mod': root / 'mod/STS2HumanCapture/bin/Release/net9.0/STS2HumanCapture.dll',
    }
    return {'contract': VALIDATION_CONTRACT, **{
        key: _sha256_bytes(path.read_bytes()) if path.is_file() else None
        for key, path in files.items()
    }}


class CombatValidationSet:
    """Bounded, diverse, independently checked live-client combat roots."""

    def __init__(self, root: Path, *, cli_config: Any, limit: int = VALIDATION_SET_LIMIT):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.cli_config = cli_config
        self.limit = limit
        self.compatibility = snapshot_compatibility(cli_config)
        self.rows: list[dict[str, Any]] = []
        self.reserve_rows: list[dict[str, Any]] = []
        self.pending_stale_rows: list[dict[str, Any]] = []
        revalidated = 0
        for path in sorted(self.root.glob('*/metadata.json')):
            try:
                row = json.loads(path.read_text(encoding='utf-8'))
                if row.get('context', {}).get('source') != 'automatic_validation_set':
                    continue
                if int((row.get('compatibility') or {}).get('contract') or 0) < VALIDATION_CONTRACT:
                    # Preserve old evidence for diagnosis, but never count it as a
                    # current replay root or spend the new set's quota on it.
                    row.update(status='LEGACY_UNVERIFIED', reusable=False)
                    _write_json(path, row)
                    continue
                if row.get('compatibility') != self.compatibility:
                    if revalidated >= 2:
                        self._admit(row, pending=True)
                        continue
                    row = revalidate_saved_combat_snapshot(path.parent, cli_config=cli_config)
                    revalidated += 1
                if row.get('status') == 'RESTORE_VERIFIED' and row.get('reusable'):
                    self._admit(row)
                else:
                    self._retire(path.parent, row)
            except (OSError, ValueError, TypeError, KeyError):
                # Unknown or partially written artifacts are not silently deleted.
                continue

    def _admit(self, row: dict[str, Any], *, pending: bool = False) -> None:
        allocated = self.rows + self.pending_stale_rows
        act = (row.get('context') or {}).get('act')
        act_limit = min(self.limit, {1: 24, 2: 16, 3: 8}.get(act, 8))
        if (len(allocated) >= self.limit
                or sum((item.get('context') or {}).get('act') == act for item in allocated) >= act_limit):
            self.reserve_rows.append(row)
        elif pending:
            self.pending_stale_rows.append(row)
        else:
            self.rows.append(row)

    def _retire(self, artifact: Path, row: dict[str, Any]) -> None:
        # Keep the failed capture for contract diagnosis without letting it
        # consume a slot in the usable validation set.
        if artifact.is_symlink() or artifact.resolve().parent != self.root:
            raise ValueError('Refusing to retire a snapshot outside the validation set')
        archive_root = self.root / 'rejected_archive'
        archive_root.mkdir(exist_ok=True)
        archive_path = archive_root / f'{artifact.name}.zip'
        temporary = archive_root / f'.{artifact.name}.zip.tmp'
        if archive_path.exists():
            raise FileExistsError(archive_path)
        files = sorted(path for path in artifact.rglob('*') if path.is_file())
        if any(path.is_symlink() or not path.resolve().is_relative_to(artifact.resolve())
               for path in files):
            raise ValueError('Refusing to archive linked snapshot files')
        try:
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=6) as archive:
                for path in files:
                    archive.write(path, path.relative_to(artifact).as_posix())
            with zipfile.ZipFile(temporary) as archive:
                if len(archive.namelist()) != len(files) or archive.testzip() is not None:
                    raise RuntimeError('Rejected snapshot archive verification failed')
            os.replace(temporary, archive_path)
        finally:
            temporary.unlink(missing_ok=True)
        with (self.root / 'rejections.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({
                'snapshot_id': row.get('snapshot_id'), 'error': row.get('error'),
                'status': row.get('status'), 'context': row.get('context'),
                'retired_at_utc': time.time(), 'archive': str(archive_path),
            }, ensure_ascii=False) + '\n')
        shutil.rmtree(artifact)

    @staticmethod
    def _band(floor: int) -> str:
        return f'{max(0, (floor - 1) // 6)}'

    def wants(self, *, act: int, floor: int, encounter: str,
              room_type: str, turn: int) -> bool:
        allocated = self.rows + self.pending_stale_rows
        if len(allocated) >= self.limit or not encounter:
            return False
        rows = [row.get('context') or {} for row in allocated]
        act_limit = min(self.limit, {1: 24, 2: 16, 3: 8}.get(act, 8))
        if sum(r.get('act') == act for r in rows) >= act_limit:
            return False
        same = [r for r in rows if r.get('encounter_id') == encounter]
        if len(same) >= 3:
            return False
        band = self._band(floor)
        same_band = sum(r.get('act') == act and r.get('floor_band') == band for r in rows)
        same_room = sum(r.get('room_type') == room_type for r in rows)
        later = sum(int(r.get('turn') or 0) >= 2 for r in rows)
        if not same:
            return True
        if later < 8 and len(same) < 2:
            return turn >= 2
        return same_band < 8 and same_room < 16 and len(same) < 2

    def capture(self, mod: Any, *, client_state: dict[str, Any],
                encounter: str, room_type: str, session_dir: Path,
                combat_number: int | None = None) -> dict[str, Any] | None:
        run = client_state.get('run') or {}
        act = int(run.get('act_id') or 0) + 1
        floor = int(run.get('floor') or 0)
        turn = int(client_state.get('turn') or 0)
        if not self.wants(act=act, floor=floor, encounter=encounter,
                          room_type=room_type, turn=turn):
            return None
        context = {'source': 'automatic_validation_set', 'session_dir': str(session_dir),
                   'run_id': client_state.get('run_id'), 'act': act, 'floor': floor,
                   'floor_band': self._band(floor), 'encounter_id': encounter,
                   'room_type': room_type, 'turn': turn,
                   'combat_number': combat_number}
        report = capture_client_combat_snapshot(
            mod, self.root, cli_config=self.cli_config, context=context)
        if report.get('status') == 'RESTORE_VERIFIED':
            report['compatibility'] = self.compatibility
            _write_json(Path(report['artifact_dir']) / 'metadata.json', report)
            self.rows.append(report)
        else:
            self._retire(Path(report['artifact_dir']), report)
        return report

    def summary(self) -> dict[str, Any]:
        return {'valid': len(self.rows), 'target': self.limit,
                'pending_stale': len(self.pending_stale_rows),
                'reserve': len(self.reserve_rows),
                'collecting': len(self.rows) + len(self.pending_stale_rows) < self.limit,
                'encounters': len({(r.get('context') or {}).get('encounter_id') for r in self.rows})}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _response_ok(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("success") is not False and payload.get("type") != "error"


def capture_reusable_combat_snapshot(
    cli: Any,
    output_dir: Path | str,
    *,
    snapshot_id: str = "combat_capture",
    client_state: Optional[dict[str, Any]] = None,
    client_rng: Optional[dict[str, Any]] = None,
    context: Optional[dict[str, Any]] = None,
    require_restore: bool = False,
) -> dict[str, Any]:
    """Capture a raw snapshot and validate that headless can restore it.

    The function never advances a game action. ``cli`` is expected to be at a
    stable player decision boundary. A failed validation returns a report with
    ``reusable=False`` and does not add the artifact to any index.
    """
    if not snapshot_id or not all(c.isalnum() or c in '_-' for c in snapshot_id):
        raise ValueError('Invalid snapshot id')
    root = Path(output_dir).resolve()
    started = time.perf_counter()
    report: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "snapshot_id": str(snapshot_id),
        "reusable": False,
        "status": "CAPTURING",
        "created_at_utc": time.time(),
        "context": dict(context or {}),
        "platform": platform.platform(),
    }
    try:
        capture = cli.capture_combat_snapshot(snapshot_id, fingerprint_mode="all")
        report["capture_response"] = capture
        if not _response_ok(capture):
            raise RuntimeError(f"capture_combat_snapshot failed: {capture}")
        exported = cli.export_combat_snapshot(snapshot_id)
        if not _response_ok(exported) or not isinstance(exported.get("snapshot_json"), str):
            raise RuntimeError(f"export_combat_snapshot failed: {exported}")
        raw = exported["snapshot_json"]
        raw_bytes = raw.encode("utf-8")

        # Keep the unmodified engine payload. Search sanitization is a separate
        # artifact so future search models can be evaluated against the same root.
        from controller.run_agent import sanitize_snapshot_json_for_search

        search_raw, sanitize = sanitize_snapshot_json_for_search(raw)
        search_state_response = cli.get_search_state(timeout_s=20.0)
        search_state = (search_state_response or {}).get("combat_state_for_search") or search_state_response
        if not isinstance(search_state, dict):
            raise RuntimeError("headless returned no search state")

        restore = None
        restored_state = None
        if require_restore:
            restore = cli.restore_combat_snapshot(
                snapshot_id, allow_full=False, compact=True, timeout_s=20.0
            )
            if not _response_ok(restore):
                raise RuntimeError(f"restore_combat_snapshot failed: {restore}")
            restored_response = cli.get_search_state(timeout_s=20.0)
            restored_state = (restored_response or {}).get("combat_state_for_search") or restored_response
            if not isinstance(restored_state, dict):
                raise RuntimeError("headless returned no search state after restore")

        artifact = root / str(snapshot_id)
        artifact.mkdir(parents=True, exist_ok=True)
        (artifact / "raw_snapshot.json").write_bytes(raw_bytes)
        (artifact / "search_snapshot.json").write_text(search_raw, encoding="utf-8", newline="\n")
        _write_json(artifact / "search_state.json", search_state)
        if client_state is not None:
            _write_json(artifact / "client_observation.json", client_state)
        if client_rng is not None:
            _write_json(artifact / "client_rng.json", client_rng)
        if restored_state is not None:
            _write_json(artifact / "headless_state_after_restore.json", restored_state)

        report.update({
            "status": "RESTORE_ONLY" if require_restore else "CAPTURED",
            "reusable": False,
            "artifact_dir": str(artifact),
            "raw_snapshot": "raw_snapshot.json",
            "search_snapshot": "search_snapshot.json",
            "raw_sha256": _sha256_bytes(raw_bytes),
            "raw_bytes": len(raw_bytes),
            "search_sha256": _sha256_bytes(search_raw.encode("utf-8")),
            "search_bytes": len(search_raw.encode("utf-8")),
            "sanitize": sanitize,
            "restore_response": restore,
            "client_observation_saved": client_state is not None,
            "client_rng_saved": client_rng is not None,
        })
        _write_json(artifact / "metadata.json", report)
        return report
    except Exception as exc:
        report.update({
            "status": "FAILED",
            "error": str(exc),
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        })
        _write_json(root / f"{snapshot_id}.failed.json", report)
        return report
