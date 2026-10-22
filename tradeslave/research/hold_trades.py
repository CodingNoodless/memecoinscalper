"""Exact curve trades from create to entry + 10 min for every NC coin (for the fade-exit test, FX).

    uv run python -m tradeslave.research.hold_trades

Output: data/m1/hold_trades/<mint>.json.gz, same row format as early_trades. Paced at 0.25 calls/s, slowed to 0.12
from 17:00 to 24:00 PT so the paper bots keep the swap-api limit in Rohit's hours.
"""

from __future__ import annotations

import gzip
import json
import time

import requests

from ..paths import DATA_DIR
from . import full_trades
from .nc_data import OUT as NC

OUT = DATA_DIR / "m1" / "hold_trades"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = sorted(json.loads(NC.read_text()), key=lambda r: r["t0"])
    todo = [r for r in rows if not (OUT / f"{r['m']}.json.gz").exists()]
    print(len(rows), "coins;", len(todo), "to fetch", flush=True)
    s = requests.Session()
    s.headers.update({"Origin": "https://pump.fun", "Accept": "application/json"})
    pace = full_trades.Pace(0.25)
    pace.good = lambda: None
    for i, r in enumerate(todo, 1):
        pace.rate = 0.12 if time.localtime().tm_hour >= 17 else 0.25
        full_trades.WINDOW_S = r["te"] - r["t0"] + 600
        tr = full_trades.fetch(s, pace, r["m"], r["t0"])
        if tr is None:
            continue
        with gzip.open(OUT / f"{r['m']}.json.gz", "wt", encoding="utf-8") as fh:
            json.dump({"m": r["m"], "t0": r["t0"], "te": r["te"], "trades": tr}, fh)
        if i % 50 == 0:
            print(f"  {i}/{len(todo)}", flush=True)


if __name__ == "__main__":
    main()
