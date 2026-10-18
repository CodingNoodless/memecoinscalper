"""Strategy U2: learned exits (research.managed) on GRADUATED coins (PumpSwap, 1-minute candles, up to 24 h).

    uv run python -m tradeslave.research.grad_managed design

State at every minute close with market cap >= grad_trader.MIN_MC (the S2 screener + chart features). Entry needs
the S2 tradability gate (>= $3,000 traded in the last 15 min); holding does not. Fill = next minute's open (this
close if nobody traded). C(x) = E[realized exit / close] under the current policy, LSM rounds as in
research.managed. Exact costs: grad_trader.round_trip (tiered fee + constant-product impact, 1 SOL, 0.001 SOL/tx).
Split by graduation time: fit on the first 70% of the design coins, choose (th_e, th_x) on the rest.
Vault: "2026-10-18 learned exits (U)", section U2.
"""

from __future__ import annotations

import statistics
import sys

import numpy as np

from . import grad_trader as G
from . import managed as U
from .shock import fee

HORIZON = 24 * 3600


def states(tg, c):
    t, o, h, lo, cl, v = c.T
    n = len(c)
    ath = np.maximum.accumulate(h)
    ath_i = np.zeros(n, int)
    for i in range(1, n):
        ath_i[i] = i if h[i] >= ath[i - 1] else ath_i[i - 1]
    rows = []
    for i in range(5, n - 1):
        if t[i] > tg + HORIZON:
            break

        def back(mins):
            j = np.searchsorted(t, t[i] - 60 * mins, side="right") - 1
            return cl[i] / cl[j] - 1 if j >= 0 else cl[i] / o[0] - 1

        def vol(mins):
            j = np.searchsorted(t, t[i] - 60 * mins, side="right")
            return v[j:i + 1].sum()

        j60 = np.searchsorted(t, t[i] - 3600, side="right")
        r = cl[max(0, i - 14):i + 1]
        rr = r[1:] / r[:-1] - 1
        greens = 0
        for x in rr[::-1]:
            if x <= 0:
                break
            greens += 1
        gains, losses = rr[rr > 0].sum(), -rr[rr < 0].sum()
        f = [(t[i] - tg) / 60, cl[i] * 1e9, back(1), back(5), back(15), back(60), back(240), v[i], vol(5), vol(15),
             vol(60), v[i] / (vol(60) / max(1, i + 1 - j60)), cl[i] / ath[i], (t[i] - t[ath_i[i]]) / 60,
             cl[i] / lo[j60:i + 1].min() - 1, cl[i] / h[j60:i + 1].max() - 1, float(np.std(rr)), greens,
             100 - 100 / (1 + gains / losses) if losses > 0 else 100.0, i + 1 - j60, (t[i] - t[i - 1]) / 60]
        fill = o[i + 1] if t[i + 1] - t[i] <= 60 else cl[i]
        rows.append((f, cl[i], fill, t[i], cl[i] * 1e9 >= G.MIN_MC and vol(15) >= G.MIN_V15))
    return rows


def load_all():
    """Every graduated coin with candles (grad_trader.load drops coins with < 30 candles: survivorship bias)."""
    import json
    toks = []
    for p in G.CANDLE_DIR.glob("*.json"):
        d = json.loads(p.read_text())
        c = [x for x in d.get("c") or [] if x[1] > 0 and x[4] > 0]
        if c:
            toks.append((d.get("t_grad") or c[0][0], d["m"], np.array(c, float)))
    toks.sort(key=lambda x: x[0])
    return toks


def build():
    X, P, F, TT, M, TG, GATE, TERM = [], [], [], [], [], [], [], []
    for tg, m, c in load_all():
        rows = states(tg, c)
        if len(rows) < 2:
            continue
        term = rows[-1][2]
        for f, p, fl, t, g in rows:
            X.append(f)
            P.append(p)
            F.append(fl)
            TT.append(t)
            M.append(m)
            TG.append(tg)
            GATE.append(g)
            TERM.append(term)
    return (np.array(X), np.array(P), np.array(F), np.array(TT), np.array(M), np.array(TG), np.array(GATE),
            np.array(TERM))


def simulate(paths, C, P, F, TT, M, GATE, TERM, th_e, th_x):
    out = []
    for a, b in paths:
        k = a
        while k < b:
            cost = (1 - fee(P[k] * 1e9)) ** 2
            if not GATE[k] or C[k] * cost - 2 * G.TX / G.SIZE - 1 < th_e:
                k += 1
                continue
            j = k + 1
            while j < b and C[j] >= F[j] / P[j] * (1 + th_x):
                j += 1
            p_out = F[j] if j < b else TERM[a]
            out.append((M[k], TT[k], G.round_trip(F[k], p_out)))
            k = j + 1
    return out


def main() -> None:
    X, P, F, TT, M, TG, GATE, TERM = build()
    ut = np.sort(np.unique(TG))
    cut_d, cut_f = ut[int(len(ut) * 0.6)], ut[int(len(ut) * 0.42)]
    fm, vm = TG < cut_f, (TG >= cut_f) & (TG < cut_d)
    print(f"states {len(P)} ({len(ut)} coins); fit {fm.sum()} / {len(set(M[fm]))}, val {vm.sum()} / {len(set(M[vm]))}; "
          f"gated entry states val {(vm & GATE).sum()}")
    model = U.fit_lsm(X[fm], P[fm], F[fm], TERM[fm], M[fm])
    C = model.predict(X)
    paths = [(a, b) for a, b in U._paths(M) if vm[a]]
    y = U.hold_targets(P, F, TERM, U._paths(M), C)
    yn = U.hold_targets(P, F, TERM, U._paths(M), None)
    g = vm & GATE
    print(f"val gated states: enter + learned exit {np.mean(y[g] * 0.976 - 1):+.2%}, enter + hold to 24 h "
          f"{np.mean(np.clip(yn[g], 0, 10) * 0.976 - 1):+.2%} (approx. 2.4% fees)")
    for th_e in (0.0, 0.05, 0.10, 0.20):
        for th_x in (0.0, 0.03):
            tr = simulate(paths, C, P, F, TT, M, GATE, TERM, th_e, th_x)
            xs = [r[2] for r in tr]
            if len(xs) >= 5:
                print(f"  th_e {th_e:.2f} th_x {th_x:.2f}: n {len(xs)} coins {len({r[0] for r in tr})} mean "
                      f"{statistics.mean(xs):+.2%} med {statistics.median(xs):+.2%} win {np.mean(np.array(xs) > 0):.0%}")
            else:
                print(f"  th_e {th_e:.2f} th_x {th_x:.2f}: n {len(xs)}")


if __name__ == "__main__" and (len(sys.argv) < 2 or sys.argv[1] == "design"):
    main()
