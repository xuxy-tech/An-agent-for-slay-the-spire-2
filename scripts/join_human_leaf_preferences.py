"""Join independently captured human choices with headless leaf data."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

def main() -> None:
    parser = argparse.ArgumentParser(description="Build completed-turn demonstration preferences")
    parser.add_argument("--human-input", type=Path, default=Path("data/human_play/raw"))
    parser.add_argument("--leaf-input", type=Path, required=True,
                        help="Current turn-learning JSONL file or directory")
    parser.add_argument("--output", type=Path, default=Path("data/human_play/experiments/current_turn_join"))
    args = parser.parse_args()
    from controller.turn_learning import SCHEMA, preference_record
    paths = sorted(args.leaf_input.glob('*.jsonl')) if args.leaf_input.is_dir() else [args.leaf_input]
    records = []
    exhaustive_turns = 0
    for path in paths:
        with path.open(encoding='utf-8') as stream:
            for line in stream:
                if not line.strip():
                    continue
                turn = json.loads(line)
                if turn.get('schema') != SCHEMA:
                    raise ValueError('Regenerate current turn data; legacy root-action joins are retired')
                records.append(preference_record(turn))
                exhaustive_turns += bool(turn['coverage']['exhaustive'])
    from collections import Counter
    report = {'schema': SCHEMA, 'turns': len(records),
              'fit_ready_rows': sum(row['fit_ready'] for row in records),
              'fit_ready_pairwise_examples': sum(len(row['pairwise_examples']) for row in records),
              'exhaustive_turns': exhaustive_turns,
              'reasons': dict(Counter(row['reason'] for row in records if row['reason'])),
              'label_semantics': 'human_completed_turn_demonstration_preference_not_optimality'}
    args.output.mkdir(parents=True, exist_ok=True)
    records_path = args.output / "matches.jsonl"
    with records_path.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    result = {**report, "matches_path": str(records_path)}
    (args.output / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
