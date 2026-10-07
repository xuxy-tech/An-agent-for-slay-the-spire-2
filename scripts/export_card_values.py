from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_ROOT = REPO_ROOT / 'third_party' / 'sts2-cli'
PROJECT = CLI_ROOT / 'src' / 'Sts2Headless' / 'Sts2Headless.csproj'
DLL = CLI_ROOT / 'src' / 'Sts2Headless' / 'bin' / 'Release' / 'net9.0' / 'Sts2Headless.dll'
DATASET = REPO_ROOT / 'data' / 'card_stats' / 'sts2_linear_metadata_dataset.csv'
FALLBACK_DATASET = REPO_ROOT / 'data' / 'card_stats' / 'gamersky_sts2_card_stats.csv'
OUTPUT = REPO_ROOT / 'data' / 'card_stats' / 'sts2_card_values.json'


def _card_ids() -> list[str]:
    card_ids: set[str] = set()
    with DATASET.open('r', encoding='utf-8-sig', newline='') as stream:
        card_ids.update(str(row.get('card_id') or '').strip().upper()
                        for row in csv.DictReader(stream) if row.get('card_id'))
    with FALLBACK_DATASET.open('r', encoding='utf-8-sig', newline='') as stream:
        card_ids.update(str(row.get('cardEnName') or '').strip().upper()
                        for row in csv.DictReader(stream) if row.get('cardEnName'))
    return sorted(card_ids)


def _read_response(process: subprocess.Popen[str]) -> dict[str, Any]:
    while True:
        line = process.stdout.readline() if process.stdout else ''
        if not line:
            stderr = process.stderr.read() if process.stderr else ''
            raise RuntimeError(f'Headless engine stopped before responding: {stderr}')
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value


def export_card_values(build: bool = True) -> dict[str, Any]:
    if build:
        subprocess.run(
            ['dotnet', 'build', str(PROJECT), '-c', 'Release', '--nologo'],
            cwd=CLI_ROOT,
            check=True,
        )
    if not DLL.is_file():
        raise FileNotFoundError(f'Headless engine not built: {DLL}')

    env = os.environ.copy()
    env['STS2_LIB'] = str(CLI_ROOT / 'lib')
    env['STS2_GAME_DIR'] = str(CLI_ROOT / 'lib')
    process = subprocess.Popen(
        ['dotnet', str(DLL)],
        cwd=CLI_ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
    )
    try:
        ready = _read_response(process)
        if ready.get('type') != 'ready':
            raise RuntimeError(f'Unexpected headless response: {ready}')
        request = {'cmd': 'inspect_cards', 'card_ids': _card_ids()}
        assert process.stdin is not None
        process.stdin.write(json.dumps(request, ensure_ascii=True) + '\n')
        process.stdin.flush()
        result = _read_response(process)
        if result.get('type') != 'inspect_cards_result' or not result.get('success'):
            raise RuntimeError(f'Card inspection failed: {result}')
        document = {
            'schema_version': 1,
            'source': 'Slay the Spire 2 sts2.dll DynamicVars',
            'cards': {card['id']: card for card in result.get('cards', [])},
            'missing': result.get('missing') or [],
        }
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return document
    finally:
        if process.poll() is None and process.stdin is not None:
            process.stdin.write('{"cmd":"quit"}\n')
            process.stdin.flush()
            process.wait(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description='Export base and upgraded STS2 card values')
    parser.add_argument('--no-build', action='store_true')
    args = parser.parse_args()
    result = export_card_values(build=not args.no_build)
    print(f"Exported {len(result['cards'])} cards to {OUTPUT}")
    if result['missing']:
        print(f"Missing {len(result['missing'])}: {', '.join(result['missing'])}")


if __name__ == '__main__':
    main()
