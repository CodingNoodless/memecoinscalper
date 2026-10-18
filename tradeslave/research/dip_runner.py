"""Strategy N3: buy a pullback on a token that already ran, cut losers, sell runners at graduation.

    uv run python -m tradeslave.research.dip_runner

Pre-registered (vault: "2026-10-18 skilled traders and dip-runner"). Data: research.long_paths (random
launches, 1-minute bonding-curve candles priced in SOL). Fills on the exact curve: a candle price p
(SOL per token) is the curve state vs = sqrt(p * 1e3 * K) lamports (price per raw unit = vs^2 / K);
we buy at the next minute's OPEN with our own impact, exits on CLOSES only, a stop fills at the next
open (or that close with --stop-at-close). Graduation (the curve's last candle when the price reached
the graduation level) = sell at that close.
"""

from __future__ import annotations

import itertools
import json
import math
import statistics
import sys

from .long_paths import OUT

K = 30e9 * 1073e12
FEE = 0.0125
PRIO = 0.002
SIZE = 0.25
GRAD_PRICE = 3.9e-7          # SOL per token where the curve completes (vs ~ 115 SOL)
DESIGN_CUT_T = 1792085651.0    # 60th percentile of all logged launch times (fixed 2026-10-18 before any result)


def vs_of(p: float) -> float:
    return math.sqrt(p * 1e3 * K)


def buy(p: float, size: float):
    vs = vs_of(p)
    vt = K / vs
    net = size * (1 - FEE) * 1e9
    return vt * net / (vs + net), net                       # tokens (raw), lamports we added


def sell_value(p: float, tokens: float, our_net: float, fee_mult: float = 1.0) -> float:
    """Sell `tokens` into the curve at observed price p, with our buy still inside it."""
    vs2 = vs_of(p) + our_net                               # the curve as observed, plus our earlier buy
    vt2 = K / vs2
    out = vs2 * tokens / (vt2 + tokens)
    return out / 1e9 * (1 - FEE * fee_mult)


def load():
    toks = []
    for p in OUT.glob("*.json"):
        d = json.loads(p.read_text())
        c = [x for x in d["curve"] if x[0] >= d["t0"] - 60]
        if len(c) >= 3:
            toks.append((d["t0"], d["m"], c))
    toks.sort()
    return toks


def run(c, t0, A, R, D, S, H, V=0.0, fee_mult=1.0, stop_delay=0):
    p_launch = c[0][1]
    peak = 0.0
    for i in range(len(c) - 1):
        t, o, h, lo, cl, v = c[i]
        peak = max(peak, cl)
        age = (t + 60 - t0) / 60
        if age < A or v <= 0 or cl < R * p_launch or cl > (1 - D) * peak or vs_of(cl) / 1e9 < V:
            continue
        if cl >= GRAD_PRICE:
            return None
        entry = c[i + 1][1]
        tokens, net = buy(entry, SIZE)
        stop_px = entry * (1 - S)
        end_t = c[i + 1][0] + H * 60
        j = i + 1
        while j < len(c):
            tj, oj, hj, lj, clj, vj = c[j]
            if clj >= GRAD_PRICE * 0.98:                    # graduating: sell on that close
                return sell_value(clj, tokens, net, fee_mult) - SIZE - 2 * PRIO
            if clj <= stop_px:
                k = min(j + 1 + stop_delay, len(c) - 1)
                px = c[k][1] if k > j else clj
                return sell_value(min(px, clj), tokens, net, fee_mult) - SIZE - 2 * PRIO
            if tj >= end_t:
                return sell_value(clj, tokens, net, fee_mult) - SIZE - 2 * PRIO
            j += 1
        last = c[-1][4]                                     # candles stop: no more trades; dump at the last close
        return sell_value(last, tokens, net, fee_mult) - SIZE - 2 * PRIO
    return None


def stats(rows) -> str:
    xs = [r[1] for r in rows]
    if len(xs) < 5:
        return f"n={len(xs)}"
    m, sd = statistics.mean(xs), statistics.pstdev(xs)
    gains = sum(x for x in xs if x > 0)
    top = max(xs) / gains if gains > 0 else 1
    return (f"n={len(xs):4d} ROI {m / SIZE * 100:+6.1f}%/trade med {statistics.median(xs) / SIZE * 100:+6.1f}% "
            f"win {sum(x > 0 for x in xs) / len(xs):4.0%} t {m / (sd / math.sqrt(len(xs))) if sd else 0:+5.2f} top-share {top:.0%}")


def main() -> None:
    toks = load()
    design = [x for x in toks if x[0] < DESIGN_CUT_T]
    test = [x for x in toks if x[0] >= DESIGN_CUT_T]
    print(f"{len(toks)} random launches with candles; design {len(design)}, test {len(test)}")
    base = [(m, run(c, t0, 5, 0, 0, 0.40, 360)) for t0, m, c in design]
    print(f"baseline (buy every live token at minute 5, stop 40%, 6 h): {stats([b for b in base if b[1] is not None])}\n")
    grid = []
    for A, R, D, S, H in itertools.product((2, 5, 10), (2, 3, 5), (0.15, 0.30, 0.45), (0.25, 0.40), (60, 360)):
        rows = [(m, v) for t0, m, c in design if (v := run(c, t0, A, R, D, S, H)) is not None]
        if len(rows) >= 30:
            grid.append((statistics.mean(r[1] for r in rows), (A, R, D, S, H), rows))
    grid.sort(key=lambda g: -g[0])
    print("top design rules (A min, R x, D pullback, S stop, H min):")
    for m, prm, rows in grid[:8]:
        print(f"  {prm}: {stats(rows)}")
    if "--design-only" in sys.argv or not grid:
        return
    _, prm, _ = grid[0]
    A, R, D, S, H = prm
    tr = [(m, v) for t0, m, c in test if (v := run(c, t0, A, R, D, S, H)) is not None]
    h = len(tr) // 2
    print(f"\nTEST {prm}: {stats(tr)}")
    print(f"  halves: {stats(tr[:h])} | {stats(tr[h:])}")
    print(f"  2x fees: {stats([(m, v) for t0, m, c in test if (v := run(c, t0, A, R, D, S, H, fee_mult=2)) is not None])}")
    print(f"  stops 1 min late: {stats([(m, v) for t0, m, c in test if (v := run(c, t0, A, R, D, S, H, stop_delay=1)) is not None])}")


if __name__ == "__main__":
    main()
