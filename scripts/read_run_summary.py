"""Read only the top-level outcome needed by the long-run harness."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: read_run_summary.py RUN_REPORT")
    path = Path(sys.argv[1])
    with path.open("r", encoding="utf-8") as stream:
        report = json.load(stream)
    print(json.dumps({
        "status": report.get("status"),
        "error": report.get("error"),
        "actions": len(report.get("actions") or []),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
