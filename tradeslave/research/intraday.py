"""Round 2 on memecoin majors: hourly event rules E1-E6 (pre-registered; vault note
"2026-10-17 memecoin majors systematic").

    uv run python -m tradeslave.research.intraday

Signals use the hour's close; entries fill at that close, exits at the close `hold` hours later.
Returns per trade are net of cost per side and of funding paid over the hold (longs pay positive funding).
"""

from __future__ import annotations

import math
import statistics

import numpy as np

from .majors import TEST_FROM
from .perps_data import load, load_ref

H = 3600
PERP, SPOT = 0.001, 0.003


def hourly():
    data = load()
    syms = [d["sym"] for d in data]
    t0 = min(d["k"][0][0] for d in data) // H
    t1 = max(d["k"][-1][0] for d in data) // H
    n = int(t1 - t0 + 1)
    C = np.full((n, len(syms)), np.nan)
    V = np.full((n, len(syms)), np.nan)
    F = np.zeros((n, len(syms)))
    for j, d in enumerate(data):
        for t, o, h, l, c, v in d["k"]:
            C[int(t // H - t0), j], V[int(t // H - t0), j] = c, v
        for t, r in d["f"]:
            i = int(t // H - t0)
            if 0 <= i < n:
                F[i, j] += r
    btc = np.full(n, np.nan)
    for t, o, h, l, c, v in load_ref("BTC")["k"]:
        i = int(t // H - t0)
        if 0 <= i < n:
            btc[i] = c
    times = (np.arange(n) + t0) * H
    return syms, times, C, V, F, btc


def leg(C, F, i, j, hold, side, cost):
    """One trade on coin j from hour i's close to hour i+hold's close."""
    k = i + hold
    if k >= len(C) or np.isnan(C[i, j]) or np.isnan(C[k, j]):
        return None
    r = side * (C[k, j] / C[i, j] - 1)
    fund = side * F[i + 1:k + 1, j].sum()
    return r - fund - 2 * cost


def events(name, syms, times, C, V, F, btc, cost_mult=1.0):
    """[(time, net return)] for each trade of rule `name` (non-overlapping per coin)."""
    out = []
    n, m = C.shape
    hist = np.cumsum(~np.isnan(C), axis=0)
    elig = hist >= 60 * 24
    if name == "E1":
        b1 = np.full(n, np.nan)
        b1[1:] = btc[1:] / btc[:-1] - 1
        free = -1
        for i in range(n):
            if i <= free or np.isnan(b1[i]) or abs(b1[i]) < 0.015:
                continue
            side = 1 if b1[i] > 0 else -1
            rs = [leg(C, F, i, j, 4, side, PERP * cost_mult) for j in range(m) if elig[i, j]]
            rs = [r for r in rs if r is not None]
            if rs:
                out.append((times[i], float(np.mean(rs))))
                free = i + 3
        return out
    if name in ("E2", "E3"):
        side = 1 if name == "E2" else -1
        for j in range(m):
            free = -1
            for i in range(24 * 7, n):
                if i <= free or not elig[i, j] or np.isnan(C[i - 1, j]):
                    continue
                r1 = C[i, j] / C[i - 1, j] - 1
                if r1 < 0.05:
                    continue
                med = np.nanmedian(V[i - 24 * 7:i, j])
                if not med or V[i, j] < 5 * med:
                    continue
                r = leg(C, F, i, j, 4, side, PERP * cost_mult)
                if r is not None:
                    out.append((times[i], r))
                    free = i + 3
        return out
    if name == "E4":
        for j in range(m):
            free = -1
            for i in np.where(F[:, j] <= -0.0005)[0]:
                if i <= free or not elig[i, j]:
                    continue
                r = leg(C, F, i, j, 24, 1, PERP * cost_mult)
                if r is not None:
                    out.append((times[i], r))
                    free = i + 23
        return out
    if name == "E5":
        split = int(np.searchsorted(times, TEST_FROM))
        R = np.full_like(C, np.nan)
        R[1:] = C[1:] / C[:-1] - 1
        R = np.where(elig, R, np.nan)
        basket = np.nanmean(R, axis=1)
        hours = ((times // H) % 24).astype(int)
        mean_by_h = [np.nanmean(basket[:split][hours[:split] == h]) for h in range(24)]
        best = sorted(range(24), key=lambda h: -mean_by_h[h])[:4]
        print(f"  E5 best design hours (UTC): {sorted(best)}  means {[f'{mean_by_h[h] * 100:+.3f}%' for h in sorted(best)]}")
        # Hold the basket during the chosen hours; pay entry/exit each contiguous block.
        i = 0
        while i < n - 1:
            if hours[i + 1] in best and not np.isnan(basket[i + 1]):
                r, k = 1.0, i + 1
                while k < n and hours[k] in best and not np.isnan(basket[k]):
                    r *= 1 + basket[k]
                    k += 1
                out.append((times[i], r - 1 - 2 * PERP * cost_mult))
                i = k
            else:
                i += 1
        return out
    if name == "E6":
        cost = (PERP + SPOT) * cost_mult
        for j in range(m):
            on, acc, start = False, 0.0, None
            for i in range(72, n):
                f3 = F[i - 71:i + 1, j].sum() / 9            # 3-day mean, per 8 h
                if not on and elig[i, j] and f3 >= 0.0002:
                    on, acc, start = True, 0.0, i
                elif on:
                    acc += F[i, j]                           # short perp receives positive funding
                    if f3 < 0.00005 or np.isnan(C[i, j]):
                        out.append((times[start], acc - 2 * cost))
                        on = False
        return out
    raise KeyError(name)


def stats(rows) -> str:
    ys = [r for _, r in rows]
    if len(ys) < 5:
        return f"n={len(ys)}"
    m, sd = statistics.mean(ys), statistics.pstdev(ys)
    return (f"n={len(ys):5d} mean={m * 100:+6.3f}% med={statistics.median(ys) * 100:+6.3f}% "
            f"win={sum(y > 0 for y in ys) / len(ys):4.0%} t={m / (sd / math.sqrt(len(ys))) if sd else 0:+5.2f} "
            f"sum={sum(ys) * 100:+8.1f}%")


def main() -> None:
    syms, times, C, V, F, btc = hourly()
    for name in ("E1", "E2", "E3", "E4", "E5", "E6"):
        rows = events(name, syms, times, C, V, F, btc)
        d = [r for r in rows if r[0] < TEST_FROM]
        t = [r for r in rows if r[0] >= TEST_FROM]
        h = len(t) // 2
        r2 = [r for r in events(name, syms, times, C, V, F, btc, 2.0) if r[0] >= TEST_FROM]
        print(f"{name}: design {stats(d)}")
        print(f"    TEST   {stats(t)}")
        print(f"    halves {stats(t[:h])[:44]} | {stats(t[h:])[:44]}")
        print(f"    2x cost TEST {stats(r2)}\n", flush=True)


if __name__ == "__main__":
    main()
