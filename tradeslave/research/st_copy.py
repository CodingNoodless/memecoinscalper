"""ST copy test: would copying a streamer's wallet have made money? His own pump.fun curve trades (Helius history), then our copy
filled 1 / 5 / 15 s after his first buy and first sell, priced from the coin's own trade events (exact curve state).

    uv run python -u -m tradeslave.research.st_copy <wallet> [days]
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from collections import defaultdict

from ..paths import DATA_DIR
from ..scalper.trade_events import parse_trade_events
from .bw_test import gtfa
from .fl_test import helius_url

FEE, TXR, DELAYS = 0.0125, 2 * 0.0005 / 0.1, (1, 5, 15)
OUT = DATA_DIR / "m1" / "st_copy"


def events(url, addr, a, b, pages=40):
    txs, _ = gtfa(url, addr, a, b, max_pages=pages)
    ev = []
    for tx in txs:
        for e in parse_trade_events((tx.get("meta") or {}).get("logMessages") or []):
            ev.append(e)
    ev.sort(key=lambda e: e["ts"])
    return ev


def price_at(url, mint, t, cache):
    """Curve price (vs/vt) of the last trade at or before t (looks in [t - 30, t])."""
    key = (mint, int(t))
    if key not in cache:
        ev = [e for e in events(url, mint, t - 30, t + 1, pages=3) if e["mint"] == mint and e["ts"] <= t]
        cache[key] = (ev[-1]["vs"] / ev[-1]["vt"]) if ev else None
    return cache[key]


def main() -> None:
    url = helius_url()
    w = sys.argv[1]
    days = float(sys.argv[2]) if len(sys.argv) > 2 else 14
    mine = [e for e in events(url, w, time.time() - days * 86400, time.time(), pages=60) if e["user"] == w]
    by = defaultdict(list)
    for e in mine:
        by[e["mint"]].append(e)
    print(f"wallet {w[:8]}…: {len(mine)} pump.fun curve trades in {days:g} days across {len(by)} coins", flush=True)
    cache, rows = {}, []
    for mint, ev in by.items():
        buys = [e for e in ev if e["buy"]]
        sells = [e for e in ev if not e["buy"]]
        if not buys:
            continue
        si, so = sum(e["sol"] for e in buys) / 1e9, sum(e["sol"] for e in sells) / 1e9
        tb = buys[0]["ts"]
        ts_ = next((e["ts"] for e in sells if e["ts"] > tb), None)
        row = dict(mint=mint, t=tb, spent=si, got=so, own=(so - si) / si if si else None, sold=ts_ is not None,
                   hold_min=((ts_ - tb) / 60) if ts_ else None, dev=ev[0]["vs"] / ev[0]["vt"] <= 1.05 * (30e9 / 1.073e15))
        if ts_ is None:
            rows.append(row)
            continue
        for d in DELAYS:
            pe, px = price_at(url, mint, tb + d, cache), price_at(url, mint, ts_ + d, cache)
            row[f"copy+{d}"] = (px / pe * (1 - FEE) ** 2 - 1 - TXR) if pe and px else None
        rows.append(row)
        own = f"{row['own']:+7.1%}" if row["own"] is not None else "   n/a "
        print(f"  {mint[:8]}… spent {si:6.2f} SOL got {so:6.2f} own {own} hold {row['hold_min']:6.1f} min "
              + " ".join(f"+{d}s {row[f'copy+{d}']:+7.1%}" if row.get(f"copy+{d}") is not None else f"+{d}s   n/a  " for d in DELAYS)
              + (" (dev coin)" if row["dev"] else ""), flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{w}.json").write_text(json.dumps(rows))
    done = [r for r in rows if r["sold"] and r["own"] is not None]
    print(f"\ncoins with a buy and a sell: {len(done)} (still holding / sold off-curve: {len(rows) - len(done)})")
    if done:
        tot_in, tot_out = sum(r["spent"] for r in done), sum(r["got"] for r in done)
        print(f"his own result: spent {tot_in:.2f} SOL, got {tot_out:.2f} SOL -> {tot_out / tot_in - 1:+.1%}; per coin median "
              f"{statistics.median(r['own'] for r in done):+.1%}, win {sum(r['own'] > 0 for r in done) / len(done):.0%}, "
              f"median hold {statistics.median(r['hold_min'] for r in done):.1f} min")
        for d in DELAYS:
            v = [r[f"copy+{d}"] for r in done if r.get(f"copy+{d}") is not None]
            if v:
                print(f"copy +{d:2d} s: n {len(v)} mean {statistics.mean(v):+6.1%} median {statistics.median(v):+6.1%} win {sum(x > 0 for x in v) / len(v):.0%}")


if __name__ == "__main__":
    main()
