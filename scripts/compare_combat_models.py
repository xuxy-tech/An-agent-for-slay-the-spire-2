"""Run a persistent, headless paired combat comparison job."""
from __future__ import annotations

import argparse
from pathlib import Path

from controller.combat_comparison import run_job


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-dir", type=Path, required=True)
    args = parser.parse_args()
    run_job(Path.cwd().resolve(), args.job_dir.resolve())


if __name__ == "__main__":
    main()
