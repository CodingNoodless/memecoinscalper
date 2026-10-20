"""Strategy Y11: resting limit bids that catch 1-minute crashes on established Solana memecoins (pre-registered;
vault: "2026-10-20 aged memecoin survivors (Y)", section Y11).

    uv run python -m tradeslave.research.major_wick design     # Hyperliquid perp 1-min OHLC 2024-25 (price proxy)
    uv run python -m tradeslave.research.major_wick fetch      # GeckoTerminal 1-min OHLC of each coin's Solana pool
    uv run python -m tradeslave.research.major_wick test       # judge the frozen cell once on the DEX candles

Each minute a bid rests D below the previous close; it fills when the minute's low trades 0.2% through it, at the
bid. Exit: a take-profit limit at +T (fills when a later high clears it by 0.2%), else the close H minutes after
the fill. One position per coin. COST per side.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
import time
from collections import defaultdict
from itertools import product

import numpy as np
import requests

from ..paths import DATA_DIR
from .listings import CG, GT, get
from .perp_chart import load

OUT = DATA_DIR / "major_wick"
FROZEN = OUT / "rule.json"
COINS = {"WIF": "dogwifcoin", "1000BONK": "bonk", "POPCAT": "popcat", "FARTCOIN": "fartcoin", "PENGU": "pudgy-penguins",
         "TRUMP": "official-trump", "MEW": "cat-in-a-dogs-world", "BOME": "book-of-meme", "GOAT": "goatseus-maximus",
         "PNUT": "peanut-the-squirrel", "MOODENG": "moo-deng", "ACT": "act-i-the-ai-prophecy",
         "CHILLGUY": "just-a-chill-guy", "AI16Z": "ai16z", "ZEREBRO": "zerebro", "GRIFFAIN": "griffain",
         "USELESS": "useless-3", "PUMP": "pump-fun", "PONKE": "ponke"}
DESIGN = (1704067200, 1767225600)                  # 2024-01-01 .. 2026-01-01
YEAR2 = 1735689600
GRID = list(product((0.02, 0.03, 0.05, 0.08), (0.01, 0.02, 0.03, 0.05), (60, 240, 1440)))
COST, COST2, THRU = 0.003, 0.005, 0.002


def trades(a, D, T, H, cost=COST):
    """a: rows (t, o, h, l, c), 1-minute, oldest first. Returns [(t_fill, net return)]."""
    t, h, l, c = a[:, 0], a[:, 2], a[:, 3], a[:, 4]
    out, k, n = [], 1, len(a)
    while k < n:
        if t[k] - t[k - 1] > 60:
            k += 1
            continue
        bid = c[k - 1] * (1 - D)
        if l[k] > bid * (1 - THRU):
            k += 1
            continue
        tp, end, x, j = bid * (1 + T), t[k] + H * 60, None, k
        if c[k] >= tp * (1 + THRU):                 # the fill minute closed above the target: the wick filled us
            x = tp                                  # then the rebound took it (order: low before high, assumption)
        while x is None:
            j += 1
            if j >= n or t[j] > end:
                j -= 1
                x = c[j]
                break
            if h[j] >= tp * (1 + THRU):
                x = tp
        out.append((t[k], (x / bid) * (1 - cost) / (1 + cost) - 1))
        k = j + 1
    return out


def perp(sym, lo, hi):
    a = load(sym)
    if a is None or a.shape[1] < 5:
        return None
    a = a[(a[:, 0] >= lo) & (a[:, 0] < hi)]
    return a[:, :5] if len(a) else None


def daily(xs):
    d = defaultdict(float)
    for t, r in xs:
        d[int(t // 86400)] += r
    return list(d.values())


def fmt(xs):
    v = [r for _, r in xs]
    if len(v) < 3:
        return f"n {len(v)}"
    m, dd = statistics.mean(v), daily(xs)
    td = statistics.mean(dd) / statistics.stdev(dd) * math.sqrt(len(dd)) if len(dd) > 2 and statistics.stdev(dd) else 0
    return f"n {len(v):5d} mean {m:+.2%} median {statistics.median(v):+.2%} win {sum(r > 0 for r in v) / len(v):.0%} " \
           f"days {len(dd)} t(daily) {td:+.2f}"


def design():
    OUT.mkdir(parents=True, exist_ok=True)
    data = {s: perp(s, *DESIGN) for s in COINS}
    data = {s: a for s, a in data.items() if a is not None}
    print(f"design coins {len(data)}: {sorted(data)}")
    best = None
    for D, T, H in GRID:
        xs = sorted(x for a in data.values() for x in trades(a, D, T, H))
        y1, y2 = [x for x in xs if x[0] < YEAR2], [x for x in xs if x[0] >= YEAR2]
        m = statistics.mean([r for _, r in xs]) if xs else -1
        ok = len(xs) >= 100 and all(ys and statistics.mean([r for _, r in ys]) > 0 for ys in (y1, y2))
        print(f"D {D:.2f} T {T:.2f} H {H:4d}: {fmt(xs)} | 2024 {fmt(y1)} | 2025 {fmt(y2)}{'  *' if ok else ''}", flush=True)
        if ok and (best is None or m > best[0]):
            best = (m, dict(D=D, T=T, H=H))
    if best is None:
        print("NO CELL passes the design filter (n >= 100 and both years positive): rejected at design")
        return
    FROZEN.write_text(json.dumps(best[1]))
    print("FROZEN", best[1], f"design mean {best[0]:+.2%}")


def fetch():
    OUT.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    for sym, cid in COINS.items():
        f = OUT / f"dex_{sym}.npy"
        if f.exists():
            continue
        info = get(s, f"{CG}/coins/{cid}", dict(localization="false", tickers="false", market_data="false",
                                               community_data="false", developer_data="false"), gap=7) or {}
        mint = (info.get("platforms") or {}).get("solana")
        if not mint:
            print(sym, "no solana mint", flush=True)
            continue
        pools = (get(s, f"{GT}/networks/solana/tokens/{mint}/pools") or {}).get("data") or []
        pools = [p for p in pools if p["relationships"]["base_token"]["data"]["id"] == f"solana_{mint}"]
        if not pools:
            print(sym, "no pool", flush=True)
            continue
        pool = max(pools, key=lambda p: float(p["attributes"].get("reserve_in_usd") or 0))["attributes"]["address"]
        rows, end = {}, int(time.time())
        for _ in range(300):                         # 1000 minutes per call, back ~180 days
            d = get(s, f"{GT}/networks/solana/pools/{pool}/ohlcv/minute",
                    dict(aggregate=1, before_timestamp=end, limit=1000, currency="usd", token="base")) or {}
            lst = (d.get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
            if not lst:
                break
            for r in lst:
                rows[int(r[0])] = r[:5]
            end = min(int(r[0]) for r in lst)
        a = np.array(sorted(rows.values()), dtype=float)
        np.save(f, a)
        print(sym, pool, len(a), flush=True)


def test():
    p = json.loads(FROZEN.read_text())
    xs = []
    for sym in COINS:
        f = OUT / f"dex_{sym}.npy"
        if f.exists():
            a = np.load(f)
            xs += trades(a, **p)
    xs.sort()
    mid = xs[len(xs) // 2][0] if xs else 0
    print(f"TEST {p}: {fmt(xs)}\n   halves {fmt([x for x in xs if x[0] < mid])} / {fmt([x for x in xs if x[0] >= mid])}")
    xs2 = []
    for sym in COINS:
        f = OUT / f"dex_{sym}.npy"
        if f.exists():
            xs2 += trades(np.load(f), cost=COST2, **p)
    print(f"   0.5%/side {fmt(sorted(xs2))}")


def main() -> None:
    {"design": design, "fetch": fetch, "test": test}[sys.argv[1]]()


if __name__ == "__main__":
    main()
