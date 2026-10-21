"""Strategy Y17: idiosyncratic multi-hour moves on large memecoins, long-only (pre-registered; vault:
"2026-10-20 aged memecoin survivors (Y)", section Y17).

    uv run python -m tradeslave.research.residual design    # Hyperliquid perp 1-min 2024-25, freeze the best cell
    uv run python -m tradeslave.research.residual test      # 2026, judged once

At each hour boundary: residual = coin's L-hour return minus the median L-hour return of all coins. REV buys when
residual <= -Z, MOM when >= +Z. Entry at the close one minute after the hour, exit at the close H hours later.
One position per coin. Excess = trade return minus the equal-weight basket over the same window.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from collections import defaultdict
from itertools import product

import numpy as np

from .major_wick import COINS, COST, COST2, DESIGN, OUT, YEAR2, perp

FROZEN = OUT / "residual_rule.json"
TEST = (1767225600, 2000000000)
GRID = list(product(("REV", "MOM"), (1, 4), (0.04, 0.06, 0.10), (1, 4, 24)))


def panel(lo, hi):
    """Close at every minute, one column per coin (NaN where missing)."""
    data = {s: perp(s, lo, hi) for s in COINS}
    data = {s: a for s, a in data.items() if a is not None}
    t0 = int(min(a[0, 0] for a in data.values()) // 3600 * 3600)
    t1 = int(max(a[-1, 0] for a in data.values()))
    n = (t1 - t0) // 60 + 1
    px = np.full((n, len(data)), np.nan)
    for j, a in enumerate(data.values()):
        px[((a[:, 0] - t0) // 60).astype(int), j] = a[:, 4]
    return t0, px


def trades(t0, px, mode, L, Z, H, cost=COST):
    n, m = px.shape
    out, busy = [], np.zeros(m)
    for k in range(L * 60, n - H * 60 - 1, 60):
        e, x = px[k + 1], px[k + 1 + H * 60]
        r = px[k] / px[k - L * 60] - 1
        ok = ~np.isnan(r)
        if ok.sum() < 5:
            continue
        res = r - np.nanmedian(r)
        fwd = x / e - 1
        good = ok & ~np.isnan(fwd)
        if good.sum() < 5:
            continue
        basket = float(np.nanmean(fwd[good]))
        hit = (res <= -Z) if mode == "REV" else (res >= Z)
        for j in np.where(hit & good & (busy <= k))[0]:
            g = (x[j] / e[j]) * (1 - cost) / (1 + cost) - 1
            out.append((t0 + k * 60, g, g - basket))
            busy[j] = k + 1 + H * 60
    return out


def fmt(xs):
    if len(xs) < 3:
        return f"n {len(xs)}"
    v = [r for _, r, _ in xs]
    d = defaultdict(float)
    for t, r, _ in xs:
        d[int(t // 86400)] += r
    dd = list(d.values())
    td = statistics.mean(dd) / statistics.stdev(dd) * math.sqrt(len(dd)) if len(dd) > 2 and statistics.stdev(dd) else 0
    return (f"n {len(v):5d} mean {statistics.mean(v):+.2%} excess {statistics.mean([x for _, _, x in xs]):+.2%} "
            f"median {statistics.median(v):+.2%} win {sum(r > 0 for r in v) / len(v):.0%} t(daily) {td:+.2f}")


def mean(xs):
    return statistics.mean([r for _, r, _ in xs]) if xs else -1.0


def design():
    t0, px = panel(*DESIGN)
    print(f"design panel {px.shape}", flush=True)
    best = None
    for mode, L, Z, H in GRID:
        xs = trades(t0, px, mode, L, Z, H)
        y1, y2 = [x for x in xs if x[0] < YEAR2], [x for x in xs if x[0] >= YEAR2]
        ok = len(xs) >= 100 and mean(y1) > 0 and mean(y2) > 0
        print(f"{mode} L {L} Z {Z:.2f} H {H:2d}: {fmt(xs)} | 2024 {mean(y1):+.2%} 2025 {mean(y2):+.2%}"
              f"{'  *' if ok else ''}", flush=True)
        if ok and (best is None or mean(xs) > best[0]):
            best = (mean(xs), dict(mode=mode, L=L, Z=Z, H=H))
    if best is None:
        print("NO CELL passes the design filter: rejected at design")
        return
    FROZEN.write_text(json.dumps(best[1]))
    print("FROZEN", best[1], f"design mean {best[0]:+.2%}")


def test():
    p = json.loads(FROZEN.read_text())
    t0, px = panel(*TEST)
    xs = trades(t0, px, **p)
    mid = xs[len(xs) // 2][0] if xs else 0
    print(f"TEST {p}: {fmt(xs)}\n   halves {fmt([x for x in xs if x[0] < mid])} / {fmt([x for x in xs if x[0] >= mid])}")
    print(f"   0.5%/side {fmt(trades(t0, px, cost=COST2, **p))}")


def main() -> None:
    {"design": design, "test": test}[sys.argv[1]]()


if __name__ == "__main__":
    main()
