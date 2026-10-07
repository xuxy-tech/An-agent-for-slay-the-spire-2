"""Preserve historical combat snapshots before pruning old live sessions."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import zipfile
from pathlib import Path


SESSION_RE = re.compile(r'^\d{8}_\d{6}_\d{6}$')
FINISHED = {'VICTORY', 'DEFEAT', 'FAIL', 'BLOCKED', 'WATCHDOG', 'INTERRUPTED',
            'STOPPED', 'COMPLETED'}


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_session(path: Path, root: Path) -> bool:
    return (SESSION_RE.fullmatch(path.name) is not None
            and path.parent == root and path.resolve() == path
            and not path.is_symlink() and not path.is_junction())


def _snapshot_files(root: Path) -> list[Path]:
    files = []
    for candidate in root.iterdir():
        if candidate.name in {'combat_validation_set', 'combat_snapshots'}:
            snapshot_root = candidate
        elif _safe_session(candidate, root):
            snapshot_root = candidate / 'combat_snapshots'
        else:
            continue
        if not snapshot_root.is_dir() or snapshot_root.is_symlink() or snapshot_root.is_junction():
            continue
        for path in snapshot_root.rglob('*'):
            if path.is_file():
                if path.is_symlink() or path.resolve() != path or not path.is_relative_to(root):
                    raise RuntimeError(f'Unsafe snapshot file: {path}')
                files.append(path)
    return sorted(files)


def preserve(root: Path, output: Path) -> dict:
    root = root.resolve()
    output = output.absolute()
    if output.exists() or output.with_suffix('.zip.tmp').exists():
        raise FileExistsError(output)
    if output.parent != root / 'archive':
        raise ValueError('Snapshot archive must be under the live_dashboard archive directory')
    files = _snapshot_files(root)
    manifest = {'schema': 'sts2.snapshot_preservation.v1',
                'source_root': str(root), 'files': []}
    temporary = output.with_suffix('.zip.tmp')
    output.parent.mkdir(exist_ok=True)
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6, allowZip64=True) as archive:
            for path in files:
                relative = path.relative_to(root).as_posix()
                manifest['files'].append({'path': relative, 'bytes': path.stat().st_size,
                                          'sha256': _digest(path)})
                archive.write(path, relative)
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False,
                                                        separators=(',', ':')))
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise RuntimeError('Snapshot archive failed CRC verification')
            if len(archive.namelist()) != len(files) + 1:
                raise RuntimeError('Snapshot archive file count mismatch')
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return {'archive': str(output), 'files': len(files),
            'snapshots': sum(path.name == 'metadata.json' for path in files),
            'source_bytes': sum(row['bytes'] for row in manifest['files']),
            'archive_bytes': output.stat().st_size, 'sha256': _digest(output)}


def prune(root: Path, output: Path, before: str) -> dict:
    root = root.resolve()
    if not re.fullmatch(r'\d{8}', before):
        raise ValueError('before must use YYYYMMDD')
    if output.resolve().parent != root / 'archive' or not output.is_file():
        raise ValueError('Verified snapshot archive is required before pruning')
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise RuntimeError('Snapshot archive failed CRC verification')
        manifest = json.loads(archive.read('manifest.json'))
    archived = {row['path']: row['sha256'] for row in manifest['files']}
    targets = sorted(path for path in root.iterdir()
                     if path.is_dir() and _safe_session(path, root)
                     and path.name[:8] < before)
    skipped = []
    deleted = []
    for path in targets:
        try:
            report_path = path / 'run_report.json'
            report = json.loads(report_path.read_text(encoding='utf-8'))
            if report.get('status') not in FINISHED:
                # Old runs marked RUNNING can be abandoned after an agent or
                # dashboard crash.  Only prune them when the report itself
                # predates the retention boundary; the caller must first
                # confirm there is no active dashboard worker.
                from datetime import datetime
                cutoff = datetime.strptime(before, '%Y%m%d').timestamp()
                if report.get('status') != 'RUNNING' or report_path.stat().st_mtime >= cutoff:
                    raise ValueError(f"unfinished status {report.get('status')!r}")
            for item in (path / 'combat_snapshots').rglob('*') if (path / 'combat_snapshots').is_dir() else ():
                if item.is_file():
                    relative = item.relative_to(root).as_posix()
                    if archived.get(relative) != _digest(item):
                        raise ValueError(f'Snapshot absent or changed since archive: {relative}')
            for item in path.rglob('*'):
                if item.is_symlink() or item.is_junction():
                    raise ValueError(f'Session contains a link or junction: {item}')
            if not _safe_session(path, root):
                raise ValueError('Session path changed')
            bytes_before = sum(item.stat().st_size for item in path.rglob('*') if item.is_file())
            shutil.rmtree(path)
            deleted.append({'session': path.name, 'bytes': bytes_before})
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            skipped.append({'session': path.name, 'reason': str(exc)})
    return {'before': before, 'deleted': len(deleted),
            'freed_bytes': sum(row['bytes'] for row in deleted),
            'skipped': skipped, 'remaining_old': len(targets) - len(deleted)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices={'preserve', 'prune'})
    parser.add_argument('--root', type=Path, default=Path('logs/live_dashboard'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--before', default='20260930')
    args = parser.parse_args()
    result = (preserve(args.root, args.output) if args.mode == 'preserve'
              else prune(args.root, args.output, args.before))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
