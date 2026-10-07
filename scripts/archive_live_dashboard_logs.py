from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import zipfile
from pathlib import Path
from controller.snapshot_evidence import seal_session_snapshots


SESSION_RE = re.compile(r"^\d{8}_\d{6}_\d{6}$")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _session_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*") if path.is_file())


def archive_session(directory: Path, archive_dir: Path) -> dict[str, int | str]:
    root = archive_dir.parent.resolve()
    source = directory.resolve()
    archive_dir = archive_dir.resolve()
    if not _inside(source, root) or source.parent != root:
        raise RuntimeError(f"Refusing to archive path outside the session root: {source}")
    if not _inside(archive_dir, root):
        raise RuntimeError(f"Archive path escaped the session root: {archive_dir}")
    if not SESSION_RE.fullmatch(source.name):
        raise RuntimeError(f"Refusing non-session directory: {source.name}")

    report = json.loads((source / 'run_report.json').read_text(encoding='utf-8'))
    incomplete = 0
    for snapshot_root in (root / 'combat_validation_set', root / 'combat_snapshots'):
        evidence = seal_session_snapshots(snapshot_root, source, report)
        incomplete += evidence['incomplete']

    files = _session_files(source)
    source_bytes = sum(path.stat().st_size for path in files)
    output = archive_dir / f"{source.name}.zip"
    temporary = archive_dir / f".{source.name}.zip.tmp"
    if output.exists():
        raise FileExistsError(f"Archive already exists: {output}")
    temporary.unlink(missing_ok=True)

    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for path in files:
                archive.write(path, path.relative_to(source).as_posix())
        with zipfile.ZipFile(temporary, "r") as archive:
            entries = [entry for entry in archive.infolist() if not entry.is_dir()]
            if len(entries) != len(files):
                raise RuntimeError(f"Archive entry count mismatch for {source.name}")
            if sum(entry.file_size for entry in entries) != source_bytes:
                raise RuntimeError(f"Archive byte count mismatch for {source.name}")
            failed = archive.testzip()
            if failed is not None:
                raise RuntimeError(f"Archive CRC failure for {source.name}: {failed}")
        os.replace(temporary, output)
        archive_bytes = output.stat().st_size
        if archive_bytes <= 0:
            raise RuntimeError(f"Archive is empty: {output}")
        if not _inside(source, root) or source.parent != root:
            raise RuntimeError(f"Source path changed before deletion: {source}")
        shutil.rmtree(source)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    return {
        "session": source.name,
        "source_bytes": source_bytes,
        "archive_bytes": archive_bytes,
        "freed_bytes": source_bytes - archive_bytes,
        "incomplete_snapshot_evidence": incomplete,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Archive verified, obsolete live-dashboard session logs."
    )
    parser.add_argument("--root", type=Path, default=Path("logs/live_dashboard"))
    parser.add_argument(
        "--before",
        default="20260921",
        help="Archive sessions whose YYYYMMDD prefix is earlier than this date.",
    )
    parser.add_argument("--keep", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    root = args.root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    if not re.fullmatch(r"\d{8}", args.before):
        raise ValueError("--before must use YYYYMMDD")
    archive_dir = root / "archive"
    keep = set(args.keep)
    targets = sorted(
        path
        for path in root.iterdir()
        if path.is_dir()
        and SESSION_RE.fullmatch(path.name)
        and path.name[:8] < args.before
        and path.name not in keep
    )
    source_bytes = sum(
        path.stat().st_size for directory in targets for path in _session_files(directory)
    )
    if not args.apply:
        print(json.dumps({
            "mode": "dry_run",
            "sessions": len(targets),
            "source_bytes": source_bytes,
            "targets": [path.name for path in targets],
        }, ensure_ascii=False))
        return

    archive_dir.mkdir(parents=False, exist_ok=True)
    rows = []
    for directory in targets:
        row = archive_session(directory, archive_dir)
        rows.append(row)
        print(json.dumps({"event": "archived", **row}), flush=True)
    print(json.dumps({
        "mode": "applied",
        "sessions": len(rows),
        "source_bytes": sum(int(row["source_bytes"]) for row in rows),
        "archive_bytes": sum(int(row["archive_bytes"]) for row in rows),
        "freed_bytes": sum(int(row["freed_bytes"]) for row in rows),
    }))


if __name__ == "__main__":
    main()
