"""DC: fetch the pump.fun curve trade history around each of decu's coins (Helius), for learning his style.
For each coin: creation time (first tx), then every curve trade from 10 min before his first buy to 15 min after his last trade.
Writes data/m1/decu/<mint>.json = dict(created, a, b, capped, rows=[[slot, ts, user, buy, sol, tok, vs, vt], ...]).

    uv run python -u -m tradeslave.research.dc_fetch
"""

from __future__ import annotations

import json
import time

from ..paths import DATA_DIR
from ..scalper.trade_events import parse_trade_events
from .bw_test import gtfa
from .fl_test import helius_url

OUT = DATA_DIR / "m1" / "decu"


def main() -> None:
    url = helius_url()
    OUT.mkdir(parents=True, exist_ok=True)
    d = json.loads((DATA_DIR / "m1" / "st_decu_trades.json").read_text())
    todo = [m for m in d if m.endswith("pump") and not (OUT / f"{m}.json").exists()]
    print(f"{len(todo)} coins to fetch", flush=True)
    for n, m in enumerate(todo):
        ts = [e[0] for e in d[m]]
        first, err = gtfa(url, m, 0, time.time(), details="signatures", limit=1, max_pages=1)
        created = (first[0].get("blockTime") if first else None) or min(ts)
        a, b = max(created, min(ts) - 600), max(ts) + 900
        txs, err = gtfa(url, m, a, b, max_pages=60)
        rows = []
        for tx in txs:
            for e in parse_trade_events((tx.get("meta") or {}).get("logMessages") or []):
                if e["mint"] == m:
                    rows.append([tx.get("slot"), e["ts"], e["user"], e["buy"], e["sol"] / 1e9, e["tok"] / 1e6, e["vs"] / 1e9, e["vt"] / 1e6])
        (OUT / f"{m}.json").write_text(json.dumps(dict(created=created, a=a, b=b, capped=bool(err), rows=rows)))
        if n % 10 == 0:
            print(f"{n}/{len(todo)} {m[:8]}… {len(txs)} txs, {len(rows)} trades, capped {bool(err)}", flush=True)
    print("done")


if __name__ == "__main__":
    main()
