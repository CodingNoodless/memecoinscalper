"""Position sizing: guarded fractional Kelly plus a drawdown throttle (pure functions, no I/O).

Why not the textbook W - (1 - W) / R: it assumes each loss is the whole stake and that W and R are known. Our per-trade
returns are fat-tailed (a few 3-8x runners, many -25%, some -100% rugs) and estimated from small samples, so the
point estimate is mostly noise. Here:
  1. shift every observed net return down by z standard errors (default 80% lower bound of the mean), so the size is
     what the edge supports *after* estimation error, not what the lucky sample shows;
  2. pick the growth-optimal fraction f of bankroll for that shifted empirical distribution (max mean log(1 + f r)),
     which handles rugs and runners as they actually occurred;
  3. take half of it and cap it (default 10 %, Rohit's planned max per trade);
  4. scale it down while the account is in drawdown: full size above -soft, linear to 0 at -hard.
A bot whose lower bound is <= 0 gets size 0: sizing cannot create an edge, only scale one.
"""

from __future__ import annotations

import math
import statistics

Z80 = 1.2816
GRID = [i / 400 for i in range(0, 400)]     # 0 .. 99.75 % of bankroll


def growth_optimal(rets: list[float]) -> float:
    """f in GRID maximising the mean log growth over the given per-trade returns (on the position)."""
    best_f, best_g = 0.0, 0.0
    for f in GRID[1:]:
        if any(1 + f * r <= 0 for r in rets):
            break
        g = sum(math.log(1 + f * r) for r in rets) / len(rets)
        if g > best_g:
            best_f, best_g = f, g
    return best_f


def kelly_fraction(rets: list[float], z: float = Z80, part: float = 0.5, cap: float = 0.10, min_n: int = 30) -> float:
    """Share of bankroll to put into one trade, from the bot's net per-trade returns."""
    n = len(rets)
    if n < min_n:
        return 0.0
    haircut = z * statistics.stdev(rets) / math.sqrt(n)
    return min(cap, part * growth_optimal([r - haircut for r in rets]))


def drawdown_scale(equity: float, peak: float, soft: float = 0.10, hard: float = 0.25) -> float:
    """1.0 while drawdown is under `soft`, falling linearly to 0 at `hard`."""
    dd = 1 - equity / peak if peak > 0 else 0.0
    return max(0.0, min(1.0, (hard - dd) / (hard - soft)))


def position_size(bankroll: float, peak: float, rets: list[float], **kw) -> float:
    """Position in the bankroll's unit (e.g. SOL)."""
    return bankroll * kelly_fraction(rets, **kw) * drawdown_scale(bankroll, peak)
