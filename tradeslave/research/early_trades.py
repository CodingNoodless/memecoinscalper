"""Exact first-2-minute trades of every coin an M bot scored >= 7 (for the early-entry test, SN).

    uv run python -m tradeslave.research.early_trades

Output: data/m1/early_trades/<mint>.json.gz = {"m", "t0", "trades": [[slot, idx, unix_t, wallet, is_buy, sol, tokens, amm]]}
Paced at 0.15 calls/s so the running paper bots keep most of the swap-api rate limit.
"""

from __future__ import annotations

import gzip
import json

import requests

from ..paths import DATA_DIR
from . import full_trades
from .entry_bt import universe

OUT = DATA_DIR / "m1" / "early_trades"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    full_trades.WINDOW_S = 120
    u = sorted(universe(), key=lambda x: x["t0"])
    todo = [x for x in u if not (OUT / f"{x['m']}.json.gz").exists()]
    print(len(u), "coins;", len(todo), "to fetch", flush=True)
    s = requests.Session()
    s.headers.update({"Origin": "https://pump.fun", "Accept": "application/json"})
    pace = full_trades.Pace(0.15)
    pace.good = lambda: None                        # no speed-ups
    for i, x in enumerate(todo, 1):
        rows = full_trades.fetch(s, pace, x["m"], x["t0"])
        if rows is None:
            continue
        with gzip.open(OUT / f"{x['m']}.json.gz", "wt", encoding="utf-8") as fh:
            json.dump({"m": x["m"], "t0": x["t0"], "trades": rows}, fh)
        if i % 100 == 0:
            print(f"  {i}/{len(todo)}", flush=True)


if __name__ == "__main__":
    main()
