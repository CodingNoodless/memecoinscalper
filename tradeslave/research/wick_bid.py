"""Strategy Y8: rest a limit bid below the price on graduated coins and catch wicks (pre-registered; vault:
"2026-10-20 aged memecoin survivors (Y)", section Y8).

    uv run python -m tradeslave.research.wick_bid design    # grid on design coins, freeze the best cell
    uv run python -m tradeslave.research.wick_bid test      # judge the frozen cell once on test coins
    (append "b" for Y8b: the same on coins >= 6 / 24 h past graduation)

Unlike Q (research.shock: market-buy at the next open AFTER a dump), the order rests before the wick: at each
minute close (market cap >= MC SOL, age since graduation >= A min) a bid sits D below that close for the next
minute. It fills only if the next candle's low reaches 1% BELOW the bid (a keeper / the pool must trade through
it), at the bid. Exits: a take-profit limit at +TP fills when a later high reaches 1% above it; a stop on closes
(-ST) fills at the next open; otherwise the close H minutes after entry. Costs: shock.round_trip (official
PumpSwap fee tier at entry and exit, our impact on the pool, 0.002 SOL/tx, 0.5 SOL). One position per coin
at a time. Split as Q: design = coins graduated before the median graduation time, test = after.
Pass: test mean > 0 with t >= 2, positive in both test halves, and still positive with fees doubled.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from itertools import product

from ..paths import DATA_DIR
from .shock import fee, load, round_trip

FROZEN = DATA_DIR / "wick_bid_rule.json"
GRID = list(product((0.1, 0.2, 0.3), (0.1, 0.2, 0.4), (0.3, None), (30, 120), (1500, 5000), (0, 60)))
KEYS = ("D", "TP", "ST", "H", "MC", "A")
if "b" in sys.argv[2:]:                                    # Y8b: older survivors only
    GRID = list(product((0.1, 0.15), (0.05, 0.1), (0.15, 0.3), (30, 120), (1500, 5000), (360, 1440)))
    FROZEN = DATA_DIR / "wick_bid_b_rule.json"


def trades(c, tg, D, TP, ST, H, MC, A, double=False):
    out, k = [], 0
    while k < len(c) - 1:
        t, o, hi, lo, cl = c[k][:5]
        nx = c[k + 1]
        if cl * 1e9 < MC or t < tg + A * 60 or nx[0] - t > 60:
            k += 1
            continue
        bid = cl * (1 - D)
        if nx[3] > bid * 0.99:
            k += 1
            continue
        e, x, j = bid, None, k + 1
        tp, end = e * (1 + TP), nx[0] + H * 60
        # the fill candle itself: only its close counts for the stop (the wick may come before or after the high)
        if ST is not None and nx[4] <= e * (1 - ST):
            x = c[j + 1][1] if j + 1 < len(c) else nx[4]
            j += 1
        while x is None:
            j += 1
            if j >= len(c):
                x, j = c[-1][4], len(c) - 1
                break
            tj, oj, hj, lj, cj = c[j][:5]
            if tj > end:
                x, j = c[j - 1][4], j - 1
                break
            if hj >= tp * 1.01:
                x = tp
                break
            if ST is not None and cj <= e * (1 - ST):
                x = c[j + 1][1] if j + 1 < len(c) else cj
                j += 1
                break
        r = round_trip(e, x)
        if double:                                  # pay the fee tiers a second time
            r -= fee(e * 1e9) + fee(x * 1e9) * x / e
        out.append((nx[0], r))
        k = j + 1
    return out


def run(toks, p, double=False):
    out = []
    for tg, m, c in toks:
        out += trades(c, tg, double=double, **p)
    return sorted(out)


def stats(xs):
    v = [r for _, r in xs]
    if len(v) < 3:
        return len(v), 0.0, 0.0, 0.0
    m, s = statistics.mean(v), statistics.stdev(v)
    return len(v), m, m / s * math.sqrt(len(v)) if s else 0.0, statistics.median(v)


def fmt(xs):
    n, m, t, med = stats(xs)
    return f"n {n:5d} mean {m:+.2%} t {t:+.2f} median {med:+.2%}"


def split():
    toks = sorted(load())
    mid = toks[len(toks) // 2][0]
    return [x for x in toks if x[0] < mid], [x for x in toks if x[0] >= mid]


def design():
    des, _ = split()
    print(f"design coins {len(des)}")
    best = None
    for cell in GRID:
        p = dict(zip(KEYS, cell))
        xs = run(des, p)
        n, m, t, _ = stats(xs)
        print(f"{p}: {fmt(xs)}", flush=True)
        if n >= 50 and (best is None or m > best[0]):
            best = (m, t, p)
    FROZEN.write_text(json.dumps(best[2]))
    print(f"FROZEN {best[2]} design mean {best[0]:+.2%} t {best[1]:+.2f}")


def test():
    _, te = split()
    p = json.loads(FROZEN.read_text())
    xs = run(te, p)
    mid = xs[len(xs) // 2][0] if xs else 0
    print(f"test coins {len(te)}\nTEST {p}: {fmt(xs)}")
    print(f"   halves {fmt([x for x in xs if x[0] < mid])} / {fmt([x for x in xs if x[0] >= mid])}")
    print(f"   fees doubled {fmt(run(te, p, double=True))}")


def main() -> None:
    {"design": design, "test": test}[sys.argv[1]]()


if __name__ == "__main__":
    main()
