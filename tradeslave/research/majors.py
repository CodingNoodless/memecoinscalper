"""Systematic strategies on memecoin majors (daily rebalance), pre-registered P1-P6.

    uv run python -m tradeslave.research.majors

See the vault note "2026-10-17 memecoin majors systematic" for the rules, split and pass bar.
Portfolio weights are set on day d's close and earn day d+1's close-to-close return, minus
funding over day d+1 (a long pays positive funding) and cost * turnover.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np

from .perps_data import load

DAY = 86400
TEST_FROM = datetime(2025, 7, 1, tzinfo=timezone.utc).timestamp()
MIN_HISTORY_D = 60


def panel():
    data = load()
    syms = [d["sym"] for d in data]
    closes, funds = {}, {}
    for j, d in enumerate(data):
        for t, o, h, l, c, v in d["k"]:
            if int(t) % DAY == DAY - 3600:                       # last hour of the UTC day: daily close
                closes[(int(t) // DAY, j)] = c
        for t, r in d["f"]:
            key = (int(t - 1) // DAY, j)                         # funding settled during that day
            funds[key] = funds.get(key, 0.0) + r
    days = sorted({k[0] for k in closes})
    d0 = days[0]
    n = days[-1] - d0 + 1
    C = np.full((n, len(syms)), np.nan)
    F = np.zeros((n, len(syms)))
    for (d, j), c in closes.items():
        C[d - d0, j] = c
    for (d, j), r in funds.items():
        if 0 <= d - d0 < n:
            F[d - d0, j] = r
    dates = np.array([(d0 + i) * DAY for i in range(n)], dtype=float)
    return syms, dates, C, F


def _ret(C, k):
    R = np.full_like(C, np.nan)
    R[k:] = C[k:] / C[:-k] - 1
    return R


def _eligible(C):
    ok = ~np.isnan(C)
    age = np.cumsum(ok, axis=0)
    return ok & (age >= MIN_HISTORY_D)


def _vol(C, w=30):
    r = _ret(C, 1)
    V = np.full_like(C, np.nan)
    for i in range(w, len(C)):
        V[i] = np.nanstd(r[i - w + 1:i + 1], axis=0)
    return V


def _norm(W):
    g = np.nansum(np.abs(W), axis=1, keepdims=True)
    g[g == 0] = 1
    return np.nan_to_num(W / g)


def _quantile_ls(S, E, frac=0.2, sign=1.0):
    W = np.zeros_like(S)
    for i in range(len(S)):
        idx = np.where(E[i] & ~np.isnan(S[i]))[0]
        if len(idx) < 5:
            continue
        k = max(1, int(round(frac * len(idx))))
        order = idx[np.argsort(S[i, idx])]
        W[i, order[-k:]] = sign / (2 * k)
        W[i, order[:k]] = -sign / (2 * k)
    return W


def weights(name: str, C, F, L: int = 7):
    E = _eligible(C)
    V = _vol(C)
    inv = np.where(E & (V > 0), 1 / V, np.nan)
    if name == "P1":
        return _norm(np.sign(_ret(C, L)) * inv)
    if name == "P2":
        return _norm(np.where(_ret(C, L) > 0, 1.0, 0.0) * inv)
    if name == "P3":
        return _quantile_ls(_ret(C, L), E)
    if name == "P4":
        return _quantile_ls(_ret(C, 1), E, sign=-1.0)
    if name == "P5":
        F3 = np.full_like(F, np.nan)
        for i in range(2, len(F)):
            F3[i] = F[i - 2:i + 1].mean(axis=0)
        return _quantile_ls(F3, E, sign=-1.0)
    if name == "P6":
        S = np.zeros_like(C)
        pos = np.zeros(C.shape[1])
        for i in range(20, len(C)):
            hi20, lo20 = np.nanmax(C[i - 20:i], axis=0), np.nanmin(C[i - 20:i], axis=0)
            hi10, lo10 = np.nanmax(C[i - 10:i], axis=0), np.nanmin(C[i - 10:i], axis=0)
            c = C[i]
            pos = np.where((pos > 0) & (c < lo10), 0, pos)
            pos = np.where((pos < 0) & (c > hi10), 0, pos)
            pos = np.where(c > hi20, 1, np.where(c < lo20, -1, pos))
            pos = np.where(E[i], pos, 0)
            S[i] = pos
        return _norm(S * inv)
    raise KeyError(name)


def run(W, C, F, cost: float):
    """Daily net returns: weights from day i's close earn day i+1."""
    R1 = np.nan_to_num(_ret(C, 1))
    gross = np.sum(W[:-1] * R1[1:], axis=1)
    fund = np.sum(W[:-1] * F[1:], axis=1)
    turn = np.sum(np.abs(np.diff(W, axis=0, prepend=0 * W[:1])), axis=1)[:-1]
    return gross - fund - cost * turn, turn


def summary(r) -> dict:
    r = r[~np.isnan(r)]
    if len(r) < 30 or r.std() == 0:
        return {"n": len(r)}
    m, s = r.mean(), r.std()
    eq = np.cumprod(1 + r)
    dd = (eq / np.maximum.accumulate(eq) - 1).min()
    return {"n": len(r), "ann": (eq[-1] ** (365 / len(r)) - 1), "sharpe": m / s * math.sqrt(365),
            "t": m / s * math.sqrt(len(r)), "mdd": dd}


def fmt(s: dict) -> str:
    if "ann" not in s:
        return f"n={s['n']}"
    return f"days={s['n']:4d} ann={s['ann'] * 100:+8.1f}% sharpe={s['sharpe']:+5.2f} t={s['t']:+5.2f} maxDD={s['mdd'] * 100:6.1f}%"


COST = {"P1": 0.001, "P2": 0.003, "P3": 0.001, "P4": 0.001, "P5": 0.001, "P6": 0.001}


def main() -> None:
    syms, dates, C, F = panel()
    split = int(np.searchsorted(dates, TEST_FROM))       # first test day's index (weights from the day before)
    print(f"{len(syms)} coins, {len(dates)} days ({datetime.fromtimestamp(dates[0], timezone.utc):%Y-%m-%d} .. "
          f"{datetime.fromtimestamp(dates[-1], timezone.utc):%Y-%m-%d}); test from day {split}\n")
    E = _eligible(C)
    bh = _norm(np.where(E, 1.0, np.nan))
    r, _ = run(bh, C, F * 0, 0.0)
    print(f"benchmark equal-weight long:  design {fmt(summary(r[:split - 1]))}\n{'':30s}TEST   {fmt(summary(r[split - 1:]))}\n")
    for name in COST:
        Ls = [3, 7, 14, 30] if name in ("P1", "P2", "P3") else [None]
        best = None
        for L in Ls:
            W = weights(name, C, F, L or 7)
            r, turn = run(W, C, F, COST[name])
            sd = summary(r[:split - 1])
            print(f"{name} L={L}: design {fmt(sd)}  turnover/day {turn[:split - 1].mean():.2f}")
            if best is None or sd.get("sharpe", -9) > best[0].get("sharpe", -9):
                best = (sd, L, W, r)
        sd, L, W, r = best
        test = r[split - 1:]
        h = len(test) // 2
        r2, _ = run(W, C, F, 2 * COST[name])
        print(f"  -> {name} L={L} TEST {fmt(summary(test))}")
        print(f"     halves: {fmt(summary(test[:h]))} | {fmt(summary(test[h:]))}")
        print(f"     2x cost TEST {fmt(summary(r2[split - 1:]))}\n")


if __name__ == "__main__":
    main()
