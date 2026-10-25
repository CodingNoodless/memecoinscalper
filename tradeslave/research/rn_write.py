"""Append hand-written RN1000 write-ups (JSON list on stdin or a file) to data/runners/writeups.json.
Each item: {"m": mint, "why": ..., "attention": ..., "chart": ..., "category": optional corrected category}.

    uv run python -m tradeslave.research.rn_write <file.json>
"""

from __future__ import annotations

import json
import sys

from ..paths import DATA_DIR

OUT = DATA_DIR / "runners" / "writeups.json"


def main() -> None:
    items = json.loads(open(sys.argv[1], encoding="utf-8").read())
    W = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    for it in items:
        W[it["m"]] = {k: v for k, v in it.items() if k != "m"}
    OUT.write_text(json.dumps(W, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"{len(items)} added, {len(W)} write-ups total")


if __name__ == "__main__":
    main()
