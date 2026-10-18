"""Strategy Q: liquidity-shock reversal / continuation on graduated tokens (PumpSwap), at low fee tiers.

    uv run python -m tradeslave.research.shock [--test]

Data: research.candles (1-minute PumpSwap candles in SOL, from graduation on, every graduation the
recorder saw). Event: a minute whose close is >= D below (dump) or above (pump) the previous close,
with the token's market cap >= MC SOL (price x 1e9 supply). Entry at the NEXT minute's open (>= ~1 s
after the shock, realistic for us), exit at the close H minutes later. Costs: the official PumpSwap
fee tier for the market cap at entry and at exit (fees.png), our impact on a constant-product pool
whose SOL side is sqrt(k * price) with k from the graduation pool (85 SOL x 206.9M tokens; LP
changes ignored, assumption), 0.002 SOL per transaction. One event per token per hour.
Design = tokens graduated before the median graduation time; test = after (judged once, --test).
"""

from __future__ import annotations

import itertools
import json
import math
import statistics
import sys

from .candles import CANDLE_DIR

SIZE = 0.5
PRIO = 0.002
K_POOL = 85 * 206.9e6
TIERS = [(420, 0.0125), (1470, 0.012), (2460, 0.0115), (3440, 0.011), (4420, 0.0105), (9820, 0.01), (14740, 0.0095),
         (19650, 0.009), (24560, 0.0085), (29470, 0.008), (34380, 0.0075), (39300, 0.007), (44210, 0.0065),
         (49120, 0.006), (54030, 0.0055), (58940, 0.00525), (63860, 0.005), (68770, 0.00475), (73681, 0.0045),
         (78590, 0.00425), (83500, 0.004), (88400, 0.00375), (93330, 0.0035), (98240, 0.00325)]


def fee(mc_sol: float) -> float:
    for hi, f in TIERS:
        if mc_sol < hi:
            return f
    return 0.003


def round_trip(p_in: float, p_out: float) -> float:
    """Net return of SIZE SOL bought at price p_in and sold at p_out, with tiered fees and own impact."""
    s_in = math.sqrt(K_POOL * p_in)
    t_in = s_in / p_in
    net = SIZE * (1 - fee(p_in * 1e9))
    tokens = t_in * net / (s_in + net)
    s_out = math.sqrt(K_POOL * p_out)
    t_out = s_out / p_out
    got = s_out * tokens / (t_out + tokens) * (1 - fee(p_out * 1e9))
    return (got - SIZE - 2 * PRIO) / SIZE


def load():
    toks = []
    for p in CANDLE_DIR.glob("*.json"):
        d = json.loads(p.read_text())
        c = [x for x in d.get("c") or [] if x[1] > 0 and x[4] > 0]
        if len(c) >= 30:
            toks.append((d.get("t_grad") or c[0][0], d["m"], c))
    toks.sort()
    return toks


def events(c, D, MC, H, kind):
    out, last_t = [], -1e18
    for i in range(1, len(c) - 1):
        t, o, h, lo, cl, v = c[i]
        prev = c[i - 1][4]
        if c[i - 1][0] != t - 60:                       # need consecutive minutes
            continue
        r = cl / prev - 1
        hit = r <= -D if kind == "dump" else r >= D
        if not hit or cl * 1e9 < MC or t - last_t < 3600:
            continue
        if c[i + 1][0] != t + 60:                       # next minute must trade (we need a fill)
            continue
        p_in = c[i + 1][1]
        end = t + 60 + H * 60
        j = i + 1
        while j + 1 < len(c) and c[j + 1][0] <= end:
            j += 1
        out.append(round_trip(p_in, c[j][4]))
        last_t = t
    return out


def stats(xs) -> str:
    if len(xs) < 5:
        return f"n={len(xs)}"
    m, sd = statistics.mean(xs), statistics.pstdev(xs)
    return (f"n={len(xs):5d} mean {m:+6.2%} med {statistics.median(xs):+6.2%} win {sum(x > 0 for x in xs) / len(xs):4.0%} "
            f"t {m / (sd / math.sqrt(len(xs))) if sd else 0:+5.2f}")


GRID = list(itertools.product(("dump", "pump"), (0.05, 0.10, 0.20, 0.30), (1470, 9820, 49120), (1, 3, 10, 30, 120)))


def main() -> None:
    toks = load()
    h = len(toks) // 2
    design, test = toks[:h], toks[h:]
    print(f"{len(toks)} graduated tokens with >= 30 candles; design {len(design)}, test {len(test)}")
    res = []
    for kind, D, MC, H in GRID:
        xs = [x for _, _, c in design for x in events(c, D, MC, H, kind)]
        if len(xs) >= 30:
            res.append((statistics.mean(xs), (kind, D, MC, H), xs))
    res.sort(key=lambda r: -r[0])
    print("design, best rules (kind, move, min mcap SOL, hold min):")
    for m, prm, xs in res[:12]:
        print(f"  {prm}: {stats(xs)}")
    print("design, all dump rules at mcap >= 9820:")
    for m, prm, xs in sorted(res, key=lambda r: r[1]):
        if prm[0] == "dump" and prm[2] == 9820:
            print(f"  {prm}: {stats(xs)}")
    if "--test" in sys.argv and res:
        kind, D, MC, H = res[0][1]
        xs = [x for _, _, c in test for x in events(c, D, MC, H, kind)]
        print(f"\nTEST {res[0][1]}: {stats(xs)}  halves {stats(xs[:len(xs) // 2])} | {stats(xs[len(xs) // 2:])}")


if __name__ == "__main__":
    main()
