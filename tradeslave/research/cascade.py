"""Strategy Y12: cascade-triggered bid ladder on established Solana memecoins (pre-registered; vault:
"2026-10-20 aged memecoin survivors (Y)", section Y12).

    uv run python -m tradeslave.research.cascade design    # trigger + fills on Hyperliquid perp 1-min, 2024-25
    uv run python -m tradeslave.research.cascade test      # trigger from HL perps 2026, fills on DEX 1-min candles

No orders normally. Trigger = a minute where the median 1-min low-return (low / previous close - 1) across the coins
is <= -G. For the next W minutes each coin rests a bid D below its trigger-minute close; fill when a low trades
THRU through it, at the bid; exit at +T (a high clears it by THRU) or the close H minutes after the fill. One fill per
coin per trigger; a new trigger needs COOL seconds since the last one.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
import time
from itertools import product

import numpy as np

from .major_wick import COINS, COST, COST2, DESIGN, OUT, THRU, YEAR2, perp

FROZEN = OUT / "cascade_rule.json"
W, COOL = 60, 6 * 3600
GRID = list(product((0.02, 0.03, 0.04), (0.03, 0.05, 0.08, 0.12), (0.03, 0.05, 0.10, 0.20), (240, 1440)))
OCT10 = "2025-10-10"
TEST = (1767225600, 2000000000)


def triggers(feed, G):
    """feed: sym -> array (t, o, h, l, c). Returns [(t_trigger, {sym: close at trigger})]."""
    lows = {}
    for s, a in feed.items():
        t = a[1:, 0].astype(int)
        ok = (a[1:, 0] - a[:-1, 0]) == 60
        r = a[1:, 3] / a[:-1, 4] - 1
        for ti, ri, ci, o in zip(t, r, a[1:, 4], ok):
            if o:
                lows.setdefault(ti, {})[s] = (ri, ci)
    out, last = [], -1e18
    for ti in sorted(lows):
        d = lows[ti]
        if len(d) >= 5 and np.median([v[0] for v in d.values()]) <= -G and ti - last >= COOL:
            out.append((ti, {s: v[1] for s, v in d.items()}))
            last = ti
    return out


def fills(trig, fills_data, D, T, H, cost=COST, ref=None):
    """ref: sym -> array used for the trigger close when fills come from another venue (DEX): the bid is set from
    the fill venue's own close at the trigger minute (looked up there), so venues with different levels line up."""
    out = []
    for t0, closes in trig:
        for s, a in fills_data.items():
            ts = a[:, 0]
            k0 = int(np.searchsorted(ts, t0))
            if k0 >= len(a) or ts[k0] != t0:
                continue                                  # the fill venue has no candle at the trigger minute
            bid = a[k0, 4] * (1 - D)
            k = k0 + 1
            while k < len(a) and ts[k] <= t0 + W * 60 and a[k, 3] > bid * (1 - THRU):
                k += 1
            if k >= len(a) or ts[k] > t0 + W * 60:
                continue
            tp, end, x, j = bid * (1 + T), ts[k] + H * 60, None, k
            if a[k, 4] >= tp * (1 + THRU):
                x = tp
            while x is None:
                j += 1
                if j >= len(a) or ts[j] > end:
                    x = a[j - 1, 4]
                    break
                if a[j, 2] >= tp * (1 + THRU):
                    x = tp
            out.append((t0, s, (x / bid) * (1 - cost) / (1 + cost) - 1))
    return out


def day(t):
    return time.strftime("%Y-%m-%d", time.gmtime(t))


def fmt(xs):
    v = [r for _, _, r in xs]
    if len(v) < 3:
        return f"n {len(v)}"
    per = {}
    for t, _, r in xs:
        per[t] = per.get(t, 0.0) + r
    pv = list(per.values())
    tt = statistics.mean(pv) / statistics.stdev(pv) * math.sqrt(len(pv)) if len(pv) > 2 and statistics.stdev(pv) else 0
    return (f"n {len(v):4d} mean {statistics.mean(v):+.2%} median {statistics.median(v):+.2%} "
            f"win {sum(r > 0 for r in v) / len(v):.0%} triggers {len(pv)} t(trigger) {tt:+.2f}")


def mean(xs):
    return statistics.mean([r for _, _, r in xs]) if xs else -1.0


def design():
    data = {s: perp(s, *DESIGN) for s in COINS}
    data = {s: a for s, a in data.items() if a is not None}
    best, trig_cache = None, {}
    for G, D, T, H in GRID:
        if G not in trig_cache:
            trig_cache[G] = triggers(data, G)
            print(f"G {G}: {len(trig_cache[G])} triggers", flush=True)
        xs = fills(trig_cache[G], data, D, T, H)
        y1 = [x for x in xs if x[0] < YEAR2]
        y2 = [x for x in xs if x[0] >= YEAR2]
        ex = [x for x in xs if day(x[0]) != OCT10]
        ok = len(xs) >= 60 and mean(y1) > 0 and mean(y2) > 0 and mean(ex) > 0
        print(f"G {G} D {D:.2f} T {T:.2f} H {H:4d}: {fmt(xs)} | 2024 {mean(y1):+.2%} 2025 {mean(y2):+.2%} "
              f"w/o Oct10 {mean(ex):+.2%}{'  *' if ok else ''}", flush=True)
        if ok and (best is None or mean(xs) > best[0]):
            best = (mean(xs), dict(G=G, D=D, T=T, H=H))
    if best is None:
        print("NO CELL passes the design filter: rejected at design")
        return
    FROZEN.write_text(json.dumps(best[1]))
    print("FROZEN", best[1], f"design mean {best[0]:+.2%}")


def test():
    p = json.loads(FROZEN.read_text())
    feed = {s: perp(s, *TEST) for s in COINS}
    feed = {s: a for s, a in feed.items() if a is not None}
    dex = {}
    for s in COINS:
        f = OUT / f"dex_{s}.npy"
        if f.exists():
            dex[s] = np.load(f)
    lo = min(a[0, 0] for a in dex.values())
    trig = [x for x in triggers(feed, p["G"]) if x[0] >= lo]
    print(f"test triggers {len(trig)} (HL perps, from {day(lo)}); DEX coins {len(dex)}")
    xs = sorted(fills(trig, dex, p["D"], p["T"], p["H"]))
    mid = xs[len(xs) // 2][0] if xs else 0
    print(f"TEST {p}: {fmt(xs)}\n   halves {fmt([x for x in xs if x[0] < mid])} / {fmt([x for x in xs if x[0] >= mid])}")
    print(f"   0.5%/side {fmt(sorted(fills(trig, dex, p['D'], p['T'], p['H'], cost=COST2)))}")
    for t0, s, r in xs:
        print(f"     {day(t0)} {s:9s} {r:+.1%}")


def main() -> None:
    {"design": design, "test": test}[sys.argv[1]]()


if __name__ == "__main__":
    main()
