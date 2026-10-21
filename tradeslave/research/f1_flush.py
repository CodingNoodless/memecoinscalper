"""F1: buy after the first flush once the price holds (pre-registered; vault: "2026-10-21 F1 flush-and-hold entry").

    uv run python -m tradeslave.research.f1_flush

Coins: M1's scored, untraded coins that reached $4-7k, trade-by-trade (data/m1/trades_cache, creation to +20 min).
"""

from __future__ import annotations

import gzip
import json
import math
import statistics
from collections import Counter
from pathlib import Path

from ..paths import DATA_DIR
from .c1_curve import CURVE_FEE, T0, V0, sell_curve

CACHE = DATA_DIR / "m1" / "trades_cache"
SOL_USD, ZONE = 107.0, (4000.0, 7000.0)
FLUSH, FLUSH_WIN, HOLD_S, HOLD_WIN, LAT = 0.25, 180, 20, 60, 2
TP, STOP, MAX_HOLD = 2.0, 0.70, 600
SIZE, TX = 0.1, 0.0005


def mc(p):
    return p * 1e9 * SOL_USD


def trade(tr):
    """tr rows: [slot, idx, t, wallet, buy, sol, tokens, amm, priceSol] (curve only). (ret, exit) or None."""
    k = next((i for i, y in enumerate(tr) if ZONE[0] <= mc(y[8]) <= ZONE[1]), None)
    if k is None:
        return None
    tz = tr[k][2]
    hi, low, t_low, flushed, entry_t = tr[k][8], None, None, False, None
    for i in range(k, len(tr)):
        y = tr[i]
        if y[2] > tz + FLUSH_WIN + HOLD_WIN:
            return None
        p = y[8]
        if not flushed:
            hi = max(hi, p)
            if y[2] <= tz + FLUSH_WIN and p <= hi * (1 - FLUSH):
                flushed, low, t_low = True, p, y[2]
            continue
        if p < low:
            low, t_low = p, y[2]
            continue
        if y[2] - t_low >= HOLD_S:
            flow = sum(z[5] if z[4] else -z[5] for z in tr if t_low < z[2] <= y[2])
            if flow > 0:
                entry_t = y[2]
                break
            t_low = y[2]                                          # no buyers yet: keep waiting
    if entry_t is None:
        return None
    j = max(i for i, y in enumerate(tr) if y[2] <= entry_t + LAT)
    p0 = tr[j][8]
    v = math.sqrt(p0 * V0 * T0)
    q = V0 * T0 / v - V0 * T0 / (v + SIZE * (1 - CURVE_FEE))
    for i in range(j + 1, len(tr)):
        y = tr[i]
        if y[2] > tr[j][2] + MAX_HOLD:
            return (sell_curve(q, tr[i - 1][8]) - 2 * TX) / SIZE - 1, "TIME"
        if y[8] >= p0 * TP:
            return (sell_curve(q, p0 * TP) - 2 * TX) / SIZE - 1, "TP"
        if y[8] <= p0 * STOP:
            nxt = tr[i + 1][8] if i + 1 < len(tr) else y[8]
            return (sell_curve(q, nxt) - 2 * TX) / SIZE - 1, "STOP"
    return (sell_curve(q, tr[-1][8]) - 2 * TX) / SIZE - 1, "END"


def stats(v):
    if len(v) < 3:
        return f"n {len(v)}"
    m, sd = statistics.mean(v), statistics.stdev(v)
    w, ls = [x for x in v if x > 0], [x for x in v if x <= 0]
    return (f"n {len(v)} mean {m:+.2%} median {statistics.median(v):+.2%} win {len(w) / len(v):.0%} "
            f"PF {sum(w) / -sum(ls) if ls and sum(ls) else float('inf'):.2f} t {m / sd * math.sqrt(len(v)):+.2f} "
            f"ex-top2 {statistics.mean(sorted(v)[:-2]):+.2%}")


def main() -> None:
    score = {}
    for x in (DATA_DIR / "m1" / "cands.jsonl").open(encoding="utf-8"):
        c = json.loads(x)
        if "score" in c:
            score.setdefault(c["m"], c["score"])
    rows = []
    for f in sorted(CACHE.glob("*.json.gz")):
        r = trade(json.load(gzip.open(f, "rt")))
        if r:
            rows.append((f.name.split(".")[0], *r))
    v = [r for _, r, _ in rows]
    print(f"coins {len(list(CACHE.glob('*.json.gz')))}, F1 entries {len(rows)}, exits {dict(Counter(e for *_, e in rows))}")
    print("ALL      ", stats(v))
    print("score >=7", stats([r for m, r, _ in rows if (score.get(m) or 0) >= 7]))
    print("score <7 ", stats([r for m, r, _ in rows if (score.get(m) or 0) < 7]))
    if len(v) > 2:
        m, sd = statistics.mean(v), statistics.stdev(v)
        checks = dict(n=len(v) >= 60, mean=m >= 0.05, t=m / sd * math.sqrt(len(v)) >= 3,
                      ex2=statistics.mean(sorted(v)[:-2]) > 0)
        print("PASS checks:", checks, "->", "PASS" if all(checks.values()) else "REJECT")


if __name__ == "__main__":
    main()
