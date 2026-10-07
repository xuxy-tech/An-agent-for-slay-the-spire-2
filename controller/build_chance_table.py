from __future__ import annotations

import argparse
import json
from pathlib import Path

from cli.sts2_cli_adapter import CliConfig
from controller.search.enemy_chance_model import build_lookup_table


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an enemy chance lookup table from combat sampling")
    parser.add_argument("--encounter", required=True)
    parser.add_argument("--character", default="Ironclad")
    parser.add_argument("--ascension", type=int, default=0)
    parser.add_argument("--lang", default="en")
    parser.add_argument("--seed-start", type=int, default=1)
    parser.add_argument("--num-seeds", type=int, default=20)
    parser.add_argument("--source", choices=["auto", "model", "sample"], default="sample")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    cli_cfg = CliConfig(repo_root=repo_root)
    table = build_lookup_table(
        cli_cfg=cli_cfg,
        encounter=args.encounter,
        character=args.character,
        ascension=args.ascension,
        lang=args.lang,
        seed_start=args.seed_start,
        num_seeds=args.num_seeds,
        source=args.source,
    )
    table.save(args.output)
    print(json.dumps({"output": args.output, "encounter": args.encounter, "rows": len(table.rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
