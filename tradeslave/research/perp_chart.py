"""Strategy T: chart reading on memecoin perps (where costs are small enough for chart skill to show).

    uv run python -m tradeslave.research.perp_chart design
    uv run python -m tradeslave.research.perp_chart test

Rohit (2026-10-18): consistent accurate chart readings make money. On pump.fun the chart IS readable
(S: AUC 0.77) but 3.3% round-trip costs and gap-through-stop dumps eat it. Perps cost ~0.14% round trip.
A trader's chart read, at every 15-minute mark: returns over 1 min..1 day, volume vs normal, realized
volatility, position in the 1 h / 4 h / 1 day range, drawdown from the day's high, RSI, VWAP distance,
BTC's move, hour of day. GBM regressors predict the net return of long and short over 15 / 60 / 240 min.
Entry at the next minute's open, exit at the close H minutes later, 0.07%/side, one position per coin.

Data (Binance USD-M 1-minute klines, price source only):
  fit   = 35 prominent memecoins 2024-01..2025-06
  val   = same coins 2025-07..2026-02 (choose label + threshold here, once)
  test  = same coins 2026-03..2026-10-16, AND the 18 newer memecoins (never fit on) 2024-11..2026-09.
Pass bar (vault "2026-10-18 perp chart reading (T)"): mean > 0 and day-clustered t >= 2 on BOTH test sets.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
from scipy.ndimage import maximum_filter1d, minimum_filter1d

from ..paths import DATA_DIR
from .perps_data import SYMBOLS

FEE = 0.0007
STEP = 15
HS = (15, 60, 240)
NEWER = ["1000RATS", "MYRO", "SLERF", "BAN", "PIPPIN", "VINE", "MUBARAK", "BROCCOLI714", "TST", "DEGEN", "1000WHY",
         "1MBABYDOGE", "KOMA", "HIPPO", "BANANAS31", "1000000MOG", "JELLYJELLY", "TOSHI"]
VAL_FROM = datetime(2025, 7, 1, tzinfo=timezone.utc).timestamp()
TEST_FROM = datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp()
FROZEN = DATA_DIR / "perp_chart_rule.json"
LABELS = [(d, h) for d in (1, -1) for h in HS]


def load(sym: str, dirs=("perps_1m_hl_back", "perps_1m_hl", "perps_1m_extra")) -> np.ndarray | None:
    parts = [np.load(DATA_DIR / d / f"{sym}.npy") for d in dirs if (DATA_DIR / d / f"{sym}.npy").exists()]
    if not parts:
        return None
    a = np.concatenate(parts)
    _, k = np.unique(a[:, 0], return_index=True)
    return a[k]


def _trail(x, w, fn):
    return fn(x, size=w, origin=(w - 1) // 2)          # window ending at i (inclusive)


def _rmean(x, w):
    c = np.cumsum(np.r_[0.0, x])
    out = np.full(len(x), np.nan)
    out[w - 1:] = (c[w:] - c[:-w]) / w
    return out


def features(a: np.ndarray, btc: dict) -> tuple[np.ndarray, np.ndarray]:
    t, o, h, lo, c, qv = a.T
    n = len(a)
    r1 = np.r_[0.0, c[1:] / c[:-1] - 1]

    def ret(L):
        out = np.full(n, np.nan)
        out[L:] = c[L:] / c[:-L] - 1
        return out

    sd60 = np.sqrt(np.maximum(_rmean(r1 ** 2, 60) - _rmean(r1, 60) ** 2, 0))
    sd1440 = np.sqrt(np.maximum(_rmean(r1 ** 2, 1440) - _rmean(r1, 1440) ** 2, 0))
    q5, q60, q1440 = _rmean(qv, 5), _rmean(qv, 60), _rmean(qv, 1440)
    F = [ret(1), ret(3), ret(5), ret(15), ret(60), ret(240), ret(1440), q5 / q1440, q60 / q1440, sd60, sd1440,
         sd60 / sd1440]
    for w in (60, 240, 1440):
        hi, mn = _trail(h, w, maximum_filter1d), _trail(lo, w, minimum_filter1d)
        F += [(c - mn) / np.maximum(hi - mn, 1e-12), c / hi - 1]
    up, dn = _rmean(np.maximum(r1, 0), 14), _rmean(np.maximum(-r1, 0), 14)
    F.append(100 - 100 / (1 + up / np.maximum(dn, 1e-12)))
    vw = _rmean(qv * c, 240) / np.maximum(_rmean(qv, 240), 1e-12)
    F.append(c / vw - 1)
    bi = np.searchsorted(btc["t"], t)
    bi = np.clip(bi, 0, len(btc["t"]) - 1)
    for L in (5, 60, 1440):
        F.append(btc[L][bi])
    F.append((t // 3600) % 24)
    return np.column_stack(F), r1


def labels(a: np.ndarray, i: np.ndarray) -> np.ndarray:
    o, c = a[:, 1], a[:, 4]
    out = []
    for d, H in LABELS:
        x = c[i + H] / o[i + 1] - 1
        out.append(d * x - 2 * FEE)
    return np.column_stack(out)


def btc_ref():
    b = load("BTC")
    c = b[:, 4]
    ref = {"t": b[:, 0]}
    for L in (5, 60, 1440):
        x = np.full(len(c), np.nan)
        x[L:] = c[L:] / c[:-L] - 1
        ref[L] = x
    return ref


def dataset(syms, btc):
    X, Y, S, T = [], [], [], []
    for s in syms:
        a = load(s)
        if a is None or len(a) < 5000:
            continue
        F, _ = features(a, btc)
        t = a[:, 0]
        i = np.arange(1440, len(a) - max(HS) - 1, STEP)
        contiguous = (t[i + max(HS)] - t[i]) == 60 * max(HS)
        i = i[contiguous & np.isfinite(F[i]).all(1)]
        X.append(F[i])
        Y.append(labels(a, i))
        S += [s] * len(i)
        T.append(t[i])
        print(f"  {s}: {len(i)} points", flush=True)
    return np.concatenate(X), np.concatenate(Y), np.array(S), np.concatenate(T)


def fit(X, Y):
    from sklearn.ensemble import HistGradientBoostingRegressor
    return [HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=1000,
                                          l2_regularization=1.0, random_state=0).fit(X, np.clip(Y[:, k], -0.2, 0.2))
            for k in range(Y.shape[1])]


def trade(P, Y, S, T, k, thr):
    """Non-overlapping trades per coin when predicted net return of label k >= thr."""
    H = LABELS[k][1]
    busy, rows = {}, []
    for j in np.argsort(T, kind="stable"):
        if P[j, k] < thr or T[j] < busy.get(S[j], -1):
            continue
        busy[S[j]] = T[j] + 60 * H
        rows.append((T[j], S[j], float(Y[j, k])))
    return rows


def day_t(rows) -> float:
    by = defaultdict(float)
    for t, _, x in rows:
        by[int(t // 86400)] += x
    v = list(by.values())
    if len(v) < 5:
        return 0.0
    m, sd = statistics.mean(v), statistics.pstdev(v)
    return m / (sd / math.sqrt(len(v))) if sd else 0.0


def summary(rows, label):
    xs = [r[2] for r in rows]
    if len(xs) < 5:
        return f"{label}: n={len(xs)}"
    return (f"{label}: n={len(xs)} mean {statistics.mean(xs):+.3%} med {statistics.median(xs):+.3%} "
            f"win {np.mean(np.array(xs) > 0):.0%} day-clustered t {day_t(rows):+.2f} "
            f"coins+ {sum(statistics.mean([r[2] for r in rows if r[1] == s]) > 0 for s in {r[1] for r in rows})}"
            f"/{len({r[1] for r in rows})}")


THRS = (0.0, 0.001, 0.002, 0.004, 0.008)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "design"
    btc = btc_ref()
    print("building prominent-coin dataset")
    X, Y, S, T = dataset(SYMBOLS, btc)
    fm, vm, tm = T < VAL_FROM, (T >= VAL_FROM) & (T < TEST_FROM), T >= TEST_FROM
    print(f"points fit {fm.sum()}, val {vm.sum()}, test {tm.sum()}")
    if cmd == "design":
        ms = fit(X[fm], Y[fm])
        P = np.column_stack([m.predict(X[vm]) for m in ms])
        res = []
        for k, (d, H) in enumerate(LABELS):
            print(f"  every point {'long' if d > 0 else 'short'} {H}m: val mean {Y[vm, k].mean():+.3%}")
            for thr in THRS:
                rows = trade(P, Y[vm], S[vm], T[vm], k, thr)
                if len(rows) >= 200:
                    m = statistics.mean(r[2] for r in rows)
                    res.append((day_t(rows), m, (k, thr), rows))
        res = [r for r in res if r[1] >= 0.001]
        res.sort(key=lambda r: -r[0])
        print("validation, best (by day-clustered t, mean >= 0.1%):")
        for tt, m, (k, thr), rows in res[:10]:
            d, H = LABELS[k]
            print("  ", summary(rows, f"{'long' if d > 0 else 'short'} {H}m thr {thr}"))
        if res:
            FROZEN.write_text(json.dumps({"k": res[0][2][0], "thr": res[0][2][1]}))
            print("frozen", LABELS[res[0][2][0]], res[0][2][1])
        else:
            print("nothing passes the validation floor")
    else:
        fr = json.loads(FROZEN.read_text())
        k, thr = fr["k"], fr["thr"]
        dm = fm | vm
        ms = fit(X[dm], Y[dm])
        P = np.column_stack([m.predict(X[tm]) for m in ms])
        rows = trade(P, Y[tm], S[tm], T[tm], k, thr)
        print(f"TEST {LABELS[k]} thr {thr} — prominent coins 2026-03..10:", summary(rows, ""))
        print("building newer-memecoin dataset (never fit on)")
        Xn, Yn, Sn, Tn = dataset(NEWER, btc)
        Pn = np.column_stack([m.predict(Xn) for m in ms])
        rn = trade(Pn, Yn, Sn, Tn, k, thr)
        print(f"TEST {LABELS[k]} thr {thr} — 18 newer memecoins:", summary(rn, ""))


if __name__ == "__main__":
    main()
