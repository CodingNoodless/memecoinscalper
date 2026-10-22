"""When to buy a narrative coin: backtest entry timing on 1-minute candles for every coin the M bots scored >= 7.

    uv run python -m tradeslave.research.entry_bt fetch     # candles -> data/m1/pm_candles (cached)
    uv run python -m tradeslave.research.entry_bt run

Why: 78% of the 318 paper trades bought within 60 s of the create, after the launch spike (median 5-min low 0.6x
of our entry). Every pre-entry filter bucket lost money. Question: does waiting for the coin to survive the launch
dump and then buying a recovery do better than buying the first minute?
Fills (assumptions): buy at the candle close after the signal; 1.25% fee per side plus 0.5% impact; a stop fills at
min(stop, candle open) less 3%; when one candle touches both the stop and the target, the stop counts first.
Market cap = price x 1e9 x SOL_USD (SOL_USD = 105, assumption). Split: coins created before 15:00 = design half,
after = check half; a rule counts only if it is positive in both.
"""

from __future__ import annotations

import json
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

from ..paths import DATA_DIR
from .postmortem import CACHE, candles

SOL_USD, FEE = 105.0, 0.0125 + 0.005


def universe():
    seen = {}
    for v in ("m1", "m2", "m3"):
        for line in (DATA_DIR / v / "cands.jsonl").open(encoding="utf-8"):
            c = json.loads(line)
            if c.get("score") is None or "age" not in c:
                continue
            t0 = c["t"] - c["age"]
            if c["m"] not in seen or c["score"] > seen[c["m"]]["score"]:
                seen[c["m"]] = {"m": c["m"], "t0": t0, "score": c["score"], "first": c.get("first"), "name": c.get("name")}
    return [x for x in seen.values() if x["score"] >= 7]


def fetch_all():
    s = requests.Session()
    s.headers.update({"Origin": "https://pump.fun", "Accept": "application/json"})
    CACHE.mkdir(parents=True, exist_ok=True)
    u = universe()
    with ThreadPoolExecutor(6) as ex:
        n = sum(1 for c in ex.map(lambda x: candles(s, x["m"], x["t0"] + 1800), u) if c)
    print(len(u), "coins,", n, "with candles")


def sim(c, i, tp, stop, trail_arm, trail, hold):
    """Enter at the close of candle i; walk forward. Returns net return."""
    p0 = c[i][4] * (1 + FEE)
    peak = c[i][4]
    for k in range(i + 1, len(c)):
        t, o, h, l, cl = c[k][:5]
        if c[k][0] - c[i][0] > hold * 60:
            return c[k - 1][4] * (1 - FEE) / p0 - 1
        if l <= c[i][4] * stop:
            return min(c[i][4] * stop, o) * 0.97 * (1 - FEE) / p0 - 1
        if h >= c[i][4] * tp:
            return c[i][4] * tp * (1 - FEE) / p0 - 1
        peak = max(peak, h)
        if peak >= c[i][4] * trail_arm and l <= peak * trail:
            return min(peak * trail, o) * (1 - FEE) / p0 - 1
    return c[-1][4] * (1 - FEE) / p0 - 1 if len(c) > i + 1 else None


def first_minute(c, t0, x):
    """Today's rule, roughly: first candle within 3 min of create whose close is in the $4-7k band."""
    for i, y in enumerate(c):
        if y[0] - t0 > 180:
            return None
        if 4000 <= y[4] * 1e9 * SOL_USD <= 7000:
            return i
    return None


def recovery(wait, dip, rise, lo_mc, hi_mc):
    """Wait `wait` min after create; need a drawdown to <= `dip` x the running high, then a 1m close >= `rise` x
    the low since that high, with mc in [lo_mc, hi_mc]."""
    def f(c, t0, x):
        hi, lo, dipped = 0.0, 9e9, False
        for i, y in enumerate(c):
            if y[0] - t0 > 6 * 3600:
                return None
            if y[2] > hi and not dipped:
                hi, lo = y[2], 9e9
            lo = min(lo, y[3])
            if lo <= dip * hi:
                dipped = True
            mc = y[4] * 1e9 * SOL_USD
            if y[0] - t0 >= wait * 60 and dipped and y[4] >= rise * lo and lo_mc <= mc <= hi_mc:
                return i
        return None
    return f


def main():
    if sys.argv[1:2] == ["fetch"]:
        return fetch_all()
    u = universe()
    data = []
    for x in u:
        p = CACHE / f"{x['m']}.json"
        if p.exists():
            c = [y for y in json.loads(p.read_text()) if y[0] >= x["t0"] - 60]
            if c:
                data.append((x, c))
    split = 1792623600                                    # 2026-10-21 15:00 PDT
    print(len(data), "coins with candles; design", sum(x["t0"] < split for x, _ in data))
    exits = {"tp2 stop.9 trail.8 10m": (2.0, 0.9, 1.3, 0.8, 10), "tp2 stop.7 trail.8 10m": (2.0, 0.7, 1.3, 0.8, 10),
             "tp3 stop.7 trail.7 60m": (3.0, 0.7, 1.5, 0.7, 60)}
    entries = {"first-minute $4-7k (today)": first_minute}
    for wait in (5, 15, 30):
        for dip in (0.5, 0.7):
            for rise in (1.2, 1.5):
                for lo_mc, hi_mc in ((4000, 10000), (8000, 30000)):
                    entries[f"wait {wait}m dip {dip} rise {rise} mc {lo_mc//1000}-{hi_mc//1000}k"] = recovery(
                        wait, dip, rise, lo_mc, hi_mc)
    out = []
    for en, f in entries.items():
        for ex, a in exits.items():
            half = {0: [], 1: []}
            for x, c in data:
                i = f(c, x["t0"], x)
                if i is None:
                    continue
                r = sim(c, i, *a)
                if r is not None:
                    half[int(x["t0"] >= split)].append(r)
            if half[0] and half[1]:
                out.append((en, ex, len(half[0]), statistics.mean(half[0]), len(half[1]), statistics.mean(half[1])))
    for en, ex, n0, m0, n1, m1 in sorted(out, key=lambda z: -min(z[3], z[5])):
        print(f"{en:42s} {ex:24s} design n {n0:4d} {m0:+6.1%} | check n {n1:4d} {m1:+6.1%}")


if __name__ == "__main__":
    main()
