"""Read-only client-log replay in an isolated headless process."""
import argparse
import json
from pathlib import Path

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter


def replay(path):
    report = json.loads(path.read_text(encoding='utf-8'))
    cli = Sts2CliAdapter(CliConfig(Path(__file__).resolve().parents[1]))
    cli.start()
    try:
        anchors = [a for a in report.get('reanchors', []) if a.get('status') == 'REANCHORED_PASS']
        anchor = anchors[-1] if anchors else None
        state = cli.load_save(anchor['save_path'] if anchor else report['anchor_save'],
                              resume_room=False if anchor else report.get('anchor_room', False))
        print(json.dumps({'loaded': state.get('decision'), 'error': state.get('message')}, ensure_ascii=False))
        start = anchor.get('action_sequence', 0) if anchor else 0
        for row in report.get('actions', []):
            if row['sequence'] <= start:
                continue
            if row.get('status') != 'completed':
                raise RuntimeError(f"Refusing unknown or incomplete action {row['sequence']}")
            command = (row.get('transaction') or {}).get('shadow')
            if not command:
                continue
            state = cli.action(command['action'], command.get('params'), timeout_s=30)
            print(json.dumps({'seq': row['sequence'], 'action': command, 'decision': state.get('decision'),
                              'error': state.get('message'), 'context': state.get('context'),
                              'rng': {k: v.get('counter') for k, v in cli.get_rng_snapshot().get('run_streams', {}).items()},
                              'detail': state if row is report['actions'][-1] else None}, ensure_ascii=False))
            if state.get('type') == 'error':
                break
    finally:
        cli.stop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('report', type=Path)
    replay(parser.parse_args().report)
