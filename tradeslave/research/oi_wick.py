"""Strategy Y16: Y11's resting crash bids, resting only while open interest is crowded (pre-registered; vault:
"2026-10-20 aged memecoin survivors (Y)", section Y16).

    uv run python -m tradeslave.research.oi_wick design
    uv run python -m tradeslave.research.oi_wick test

OI ratio = Binance OI (USD, 5-min) / its trailing 7-day median, using the last value before the fill minute.
A Y11 fill counts only when the ratio was >= Q (the bid would have been resting).
"""

from __future__ import annotations

import json
import sys
from itertools import product

import numpy as np

from ..paths import DATA_DIR
from .major_wick import COINS, COST2, OUT, fmt, trades
from .perp_chart import load

FROZEN = OUT / "oi_rule.json"
METRICS = DATA_DIR / "perp_metrics"
EXTRA = DATA_DIR / "perps_1m_extra"
DESIGN, TEST = (1719792000, 1767225600), (1767225600, 1800000000)
T, H = 0.05, 1440
GRID = list(product((0.05, 0.08), (1.0, 1.25, 1.5)))


def universe():
    out = {}
    for f in sorted(METRICS.glob("*.npy")):
        s = f.stem
        a = load(s) if s in COINS else (np.load(EXTRA / f"{s}.npy") if (EXTRA / f"{s}.npy").exists() else None)
        if a is None or a.shape[1] < 5:
            continue
        m = np.load(f)
        t, oi = m[:, 0], m[:, 2]
        hourly = oi[::12]                                       # 5-min rows: one sample per hour
        med = np.full(len(hourly), np.nan)
        if len(hourly) > 168:
            med[168:] = np.median(np.lib.stride_tricks.sliding_window_view(hourly, 168)[:-1], axis=1)
        ratio = oi / np.repeat(med, 12)[:len(oi)]               # trailing 7-day median of earlier hours
        out[s] = (a[:, :5], t, ratio)
    return out


def run(u, D, Q, lo, hi, cost=None):
    xs = []
    for s, (a, mt, ratio) in u.items():
        a = a[(a[:, 0] >= lo) & (a[:, 0] < hi)]
        if not len(a):
            continue
        for t, r in (trades(a, D, T, H) if cost is None else trades(a, D, T, H, cost=cost)):
            k = int(np.searchsorted(mt, t)) - 1
            if Q is None or (k >= 0 and t - mt[k] <= 900 and ratio[k] >= Q):
                xs.append((t, r))
    return sorted(xs)


def design():
    u = universe()
    print(f"coins {len(u)}")
    best = None
    for D in (0.05, 0.08):
        base = run(u, D, None, *DESIGN)
        print(f"D {D} unconditional: {fmt(base)}", flush=True)
        bm = np.mean([r for _, r in base])
        for Q in (1.0, 1.25, 1.5):
            xs = run(u, D, Q, *DESIGN)
            m = np.mean([r for _, r in xs]) if xs else -1
            ok = len(xs) >= 100 and m > bm
            print(f"D {D} Q {Q}: {fmt(xs)}{'  *' if ok else ''}", flush=True)
            if ok and (best is None or m > best[0]):
                best = (m, dict(D=D, Q=Q))
    if best is None:
        print("NO CELL passes: rejected at design")
        return
    FROZEN.write_text(json.dumps(best[1]))
    print("FROZEN", best[1], f"design mean {best[0]:+.2%}")


def test():
    u, p = universe(), json.loads(FROZEN.read_text())
    xs = run(u, p["D"], p["Q"], *TEST)
    mid = xs[len(xs) // 2][0] if xs else 0
    print(f"TEST {p}: {fmt(xs)}\n   halves {fmt([x for x in xs if x[0] < mid])} / {fmt([x for x in xs if x[0] >= mid])}")
    print(f"   0.5%/side {fmt(run(u, p['D'], p['Q'], *TEST, cost=COST2))}")
    print(f"   unconditional same D: {fmt(run(u, p['D'], None, *TEST))}")


def main() -> None:
    {"design": design, "test": test}[sys.argv[1]]()


if __name__ == "__main__":
    main()
