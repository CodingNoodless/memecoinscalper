"""Graduated-token ("runner") strategies on 1-minute candles, with real PumpSwap costs.

    uv run python -m tradeslave.research.runners

Pre-registered (2026-10-17, before looking at the candle data) from what memecoin traders
commonly do with tokens that survived graduation; judged on the newest 40% of tokens (by
graduation time) after any choice made on the oldest 60%:

  S1 breakout   new 60-min closing high + volume >= 3x the 60-min median -> trail 15% / TP +30% / 60 min
  S2 dip        token >= 2x its graduation price, pulls back 30% from its high -> TP +20% / SL 15% / 60 min
  S3 flush      one candle closes >= 20% below the previous close on >= 3x volume -> TP +15% / SL 15% / 30 min
  S4 survivor   alive 6 h after graduation (mcap >= 1,500 SOL) -> hold 24 h, SL 50%
  S5 momentum   +25% over 15 min with rising volume -> trail 15% / 60 min

Costs: the official fee tier for the token's market cap at fill time (pump.fun FEE_PROGRAM_README,
fees.png, checked 2026-10-17), price impact on a constant-product pool whose SOL side is
estimated from price (85 SOL at graduation, x ~ sqrt(price)), network fee per transaction.
Fills: signal on a candle's close -> buy at the NEXT candle's open. Stops / targets checked on
each later candle's low / high; a candle that gaps through a stop fills at its open; a candle
touching both stop and target counts as the stop. Minutes without a candle had no trades.
"""

from __future__ import annotations

import bisect
import math
import statistics
from dataclasses import dataclass

SUPPLY = 1_000_000_000
GRAD_SOL = 85.0
# (market cap upper bound in SOL, total fee per side) - pump.fun canonical PumpSwap pools.
FEE_TIERS = [(420, 0.0125), (1470, 0.0120), (2460, 0.0115), (3440, 0.0110), (4420, 0.0105), (9820, 0.0100),
             (14740, 0.0095), (19650, 0.0090), (24560, 0.0085), (29470, 0.0080), (34380, 0.0075), (39300, 0.0070),
             (44210, 0.0065), (49120, 0.0060), (54030, 0.0055), (58940, 0.00525), (63860, 0.0050),
             (68770, 0.00475), (73681, 0.0045), (78590, 0.00425), (83500, 0.0040), (88400, 0.00375),
             (93330, 0.0035), (98240, 0.00325), (math.inf, 0.0030)]
NETWORK_FEE_SOL = 0.0003
# Fixed design/test cut (60% of the 2,848 graduations recorded Oct 13-17): 2026-10-15 17:29 UTC.
# Tokens graduating after it are the TEST set: never used to choose or tune a rule.
TEST_FROM = 1792085381.0


def fee_for(price_sol: float) -> float:
    mcap = price_sol * SUPPLY
    for bound, fee in FEE_TIERS:
        if mcap < bound:
            return fee
    return 0.003


@dataclass
class Tok:
    mint: str
    t_grad: float
    t: list
    o: list
    h: list
    l: list
    c: list
    v: list
    p0: float           # price at graduation (first candle open)

    def pool_sol(self, price: float) -> float:
        return GRAD_SOL * math.sqrt(max(price, 1e-18) / self.p0)


def to_tok(d: dict) -> Tok | None:
    c = d.get("c") or []
    if len(c) < 5:
        return None
    t, o, h, l, cl, v = (list(x) for x in zip(*c))
    return Tok(d["m"], float(d["t_grad"]), t, o, h, l, cl, v, o[0])


def trade(tok: Tok, i_sig: int, size: float, tp: float | None, sl: float | None, hold_min: float,
          trail: float | None = None):
    """Net return of a buy at the open of the candle after i_sig, and the exit index."""
    i = i_sig + 1
    if i >= len(tok.t) or tok.t[i] - tok.t[i_sig] > 600:     # no trade in the next 10 min: can't fill at a known price
        return None
    entry = tok.o[i]
    x = tok.pool_sol(entry)                                   # constant-product pool: x SOL, x / price tokens
    dx = size * (1 - fee_for(entry))
    tokens = (x / entry) * dx / (x + dx)
    peak = entry
    end_t = tok.t[i] + hold_min * 60
    exit_px, j = None, i
    while j < len(tok.t) and tok.t[j] <= end_t:
        lo, hi, op = tok.l[j], tok.h[j], tok.o[j]
        stop = entry * (1 - sl) if sl else None
        if trail:
            stop = max(stop or 0, peak * (1 - trail))
        if stop and lo <= stop:
            exit_px = min(op, stop) if j > i else stop
            break
        if tp and hi >= entry * (1 + tp):
            exit_px = max(op, entry * (1 + tp)) if j > i else entry * (1 + tp)
            break
        peak = max(peak, hi)
        j += 1
    if exit_px is None:                                         # time stop: last known price by then
        j = max(bisect.bisect_right(tok.t, end_t) - 1, i)
        exit_px = tok.c[j]
    x2 = tok.pool_sol(exit_px)                                # the pool as observed, then with our buy still in it
    x2b, y2b = x2 + dx, x2 / exit_px - tokens
    gross = x2b * tokens / (y2b + tokens) if y2b > 0 else 0.0
    proceeds = gross * (1 - fee_for(exit_px)) - NETWORK_FEE_SOL
    return proceeds / (size + NETWORK_FEE_SOL) - 1, j


def _median(xs):
    return statistics.median(xs) if xs else 0.0


def signals(tok: Tok, name: str):
    """Candle indices where strategy `name` fires (known at that candle's close)."""
    t, c, v, h = tok.t, tok.c, tok.v, tok.h
    out = []
    hi_so_far = 0.0
    for i in range(len(t)):
        age_min = (t[i] - tok.t_grad) / 60
        hi_so_far = max(hi_so_far, h[i])
        k = bisect.bisect_left(t, t[i] - 3600)
        if name == "S1":
            if i - k >= 20 and age_min >= 30 and c[i] > max(c[k:i]) and v[i] >= 3 * _median(v[k:i]):
                out.append(i)
        elif name == "S2":
            if age_min >= 30 and hi_so_far >= 2 * tok.p0 and c[i] <= 0.7 * hi_so_far and c[i - 1] > 0.7 * hi_so_far:
                out.append(i)
        elif name == "S3":
            if i >= 1 and age_min >= 30 and c[i] <= 0.8 * c[i - 1] and v[i] >= 3 * _median(v[k:i] or [math.inf]):
                out.append(i)
        elif name == "S4":
            if age_min >= 360 and (i == 0 or (t[i - 1] - tok.t_grad) / 60 < 360) and c[i] * SUPPLY >= 1500:
                if t[i] - tok.t_grad <= 360 * 60 + 600:      # still trading at the 6 h mark
                    out.append(i)
        elif name == "S5":
            j = bisect.bisect_left(t, t[i] - 900)
            if age_min >= 30 and j < i and c[j] > 0 and c[i] >= 1.25 * c[j]:
                vol_now = sum(v[j:i + 1])
                m = bisect.bisect_left(t, t[i] - 1800)
                if vol_now > sum(v[m:j]):
                    out.append(i)
    return out


RULES = {
    "S1": dict(tp=0.30, sl=None, hold_min=60, trail=0.15),
    "S2": dict(tp=0.20, sl=0.15, hold_min=60),
    "S3": dict(tp=0.15, sl=0.15, hold_min=30),
    "S4": dict(tp=None, sl=0.50, hold_min=1440),
    "S5": dict(tp=None, sl=None, hold_min=60, trail=0.15),
}


def backtest(toks: list[Tok], name: str, size: float = 0.5, rules: dict | None = None):
    rules = rules or RULES[name]
    out = []
    for tok in toks:
        free = -1
        for i in signals(tok, name):
            if i <= free:
                continue
            r = trade(tok, i, size, **rules)
            if r is None:
                continue
            out.append((tok.t_grad, r[0], tok.mint))
            free = r[1]
    return out


def stats(rows) -> str:
    if len(rows) < 5:
        return f"n={len(rows)}"
    ys = [r[1] for r in rows]
    m, sd = statistics.mean(ys), statistics.pstdev(ys)
    per = {}
    for r in rows:
        per[r[2]] = per.get(r[2], 0) + r[1]
    gains = sum(x for x in per.values() if x > 0)
    top = max(per.values()) / gains if gains > 0 else 1
    return (f"n={len(ys):5d} tok={len(per):4d} mean={m * 100:+6.2f}% med={statistics.median(ys) * 100:+6.2f}% "
            f"win={sum(y > 0 for y in ys) / len(ys):4.0%} t={m / (sd / math.sqrt(len(ys))) if sd else 0:+5.1f} "
            f"top-token share={top:.0%}")


def main() -> None:
    from .candles import load
    toks = [x for x in (to_tok(d) for d in load()) if x is not None]
    toks.sort(key=lambda x: x.t_grad)
    design = [x for x in toks if x.t_grad < TEST_FROM]
    test = [x for x in toks if x.t_grad >= TEST_FROM]
    print(f"{len(toks)} graduated tokens with >= 5 candles; design {len(design)}, test {len(test)}\n")
    for name in RULES:
        print(f"{name}: design {stats(backtest(design, name))}")
        print(f"    test   {stats(backtest(test, name))}")


if __name__ == "__main__":
    main()
