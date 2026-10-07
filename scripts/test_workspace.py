"""Run tests with one output home, or prune old temporary test outputs.

The prune command deliberately ignores logs, snapshots, models and fixed
regression fixtures. It only recognizes this script's marked runs and old
pytest basetemp directories created at the repository root or under artifacts.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / "artifacts" / "test_runs"
RUN_NAME = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")
LEGACY_ROOT = re.compile(r"^(?:\.pytest_|_pytest_)[A-Za-z0-9_.-]+$")
LEGACY_ARTIFACT = re.compile(r"^pytest[-_][A-Za-z0-9_.-]+$")


def _direct_child(path: Path, parent: Path) -> bool:
    """Reject symlinks and paths escaping their expected immediate parent."""
    if path.is_symlink() or not path.is_dir() or not parent.is_dir():
        return False
    return path.resolve().parent == parent.resolve()


def _candidates(*, include_legacy: bool) -> list[Path]:
    rows: list[Path] = []
    if OUTPUT_ROOT.is_dir():
        for path in OUTPUT_ROOT.iterdir():
            if not (RUN_NAME.fullmatch(path.name) and _direct_child(path, OUTPUT_ROOT)
                    and (path / "manifest.json").is_file()):
                continue
            try:
                manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if manifest.get("status") != "running":
                rows.append(path)
    if include_legacy:
        rows.extend(path for path in ROOT.iterdir()
                    if LEGACY_ROOT.fullmatch(path.name) and _direct_child(path, ROOT))
        artifact_root = ROOT / "artifacts"
        if artifact_root.is_dir():
            rows.extend(path for path in artifact_root.iterdir()
                        if LEGACY_ARTIFACT.fullmatch(path.name)
                        and _direct_child(path, artifact_root))
    return rows


def prune(days: int, *, apply: bool, include_legacy: bool = True) -> tuple[int, int, int]:
    if days < 1:
        raise ValueError("Retention must be at least one day")
    cutoff = time.time() - days * 86400
    selected = [path for path in _candidates(include_legacy=include_legacy)
                if path.stat().st_mtime < cutoff]
    total = 0
    failed = 0
    for path in sorted(selected):
        parent = OUTPUT_ROOT if path.parent == OUTPUT_ROOT else path.parent
        if not _direct_child(path, parent):
            raise RuntimeError(f"Unsafe test output path: {path}")
        print(f"{'REMOVE' if apply else 'WOULD REMOVE'} {path.relative_to(ROOT)}")
        if apply:
            try:
                shutil.rmtree(path)
                total += 1
            except OSError as exc:
                failed += 1
                print(f"SKIPPED {path.relative_to(ROOT)}: {exc}", file=sys.stderr)
    print(f"{'Removed' if apply else 'Eligible'} {total if apply else len(selected)} directories; failed {failed}")
    return len(selected), total, failed


def run(pytest_args: list[str]) -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = OUTPUT_ROOT / f"{stamp}-{uuid.uuid4().hex[:6]}"
    output.mkdir(parents=True, exist_ok=False)
    manifest = output / "manifest.json"
    manifest.write_text(json.dumps({"created_at_utc": stamp,
                                    "pytest_args": pytest_args,
                                    "status": "running"}, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    args = pytest_args[1:] if pytest_args[:1] == ["--"] else pytest_args
    if not args:
        args = ["tests", "-q"]
    command = [sys.executable, "-m", "pytest", *args,
               "-p", "no:cacheprovider", "--basetemp", str(output / "tmp")]
    print(f"Test output: {output}", flush=True)
    try:
        result = subprocess.run(command, cwd=ROOT, check=False)
        code = result.returncode
    except BaseException:
        manifest.write_text(json.dumps({"created_at_utc": stamp,
                                        "pytest_args": args,
                                        "status": "interrupted"}, ensure_ascii=False) + "\n",
                            encoding="utf-8")
        raise
    manifest.write_text(json.dumps({"created_at_utc": stamp,
                                    "pytest_args": args,
                                    "status": "passed" if code == 0 else "failed",
                                    "exit_code": code}, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    runner = commands.add_parser("run", help="Run pytest with one managed output directory")
    runner.add_argument("pytest_args", nargs=argparse.REMAINDER)
    cleaner = commands.add_parser("prune", help="Delete expired managed and legacy pytest outputs")
    cleaner.add_argument("--days", type=int, default=7)
    cleaner.add_argument("--apply", action="store_true", help="Actually remove directories")
    cleaner.add_argument("--managed-only", action="store_true",
                         help="Only scan outputs made by this script")
    args = parser.parse_args()
    if args.command == "run":
        return run(args.pytest_args)
    _, _, failed = prune(args.days, apply=args.apply,
                         include_legacy=not args.managed_only)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
