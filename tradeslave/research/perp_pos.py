"""Strategy T2: chart read + positioning (open interest, top-trader / account long-short, taker flow) on
memecoin perps, with STRICT time splits for both universes (the T leak: test dates overlapping fit dates).

    uv run python -m tradeslave.research.perp_pos design
    uv run python -m tradeslave.research.perp_pos test

Fit = 2024-07..2025-09 on BOTH universes (35 prominent + 18 newer memecoins), validation = 2025-10..2026-02
(choose label + threshold once), test = 2026-03..2026-10-16 on both universes, judged once.
Metrics are aligned with a 5-minute publication lag (we use the row created <= t - 300 s).
Pass bar (vault "2026-10-18 perp positioning (T2)"): mean > 0 and day-clustered t >= 2 on BOTH test sets.
"""

from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime, timezone

import numpy as np

from ..paths import DATA_DIR
from .perp_chart import HS, LABELS, NEWER, STEP, btc_ref, day_t, features, fit, labels, load, summary, trade
from .perp_metrics import OUT as MET
from .perps_data import SYMBOLS

VAL_FROM = datetime(2025, 10, 1, tzinfo=timezone.utc).timestamp()
TEST_FROM = datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp()
START = datetime(2024, 7, 8, tzinfo=timezone.utc).timestamp()
FROZEN = DATA_DIR / "perp_pos_rule.json"
THRS = (0.0, 0.001, 0.002, 0.004, 0.008)


def met_features(t: np.ndarray, m: np.ndarray) -> np.ndarray:
    """Positioning features at decision times t from 5-minute metrics m (publication lag 5 min)."""
    mt = m[:, 0]
    j = np.searchsorted(mt, t - 300, side="right") - 1
    ok = j >= 288 * 7
    j = np.clip(j, 288 * 7, len(m) - 1)
    oi, top_acct, top_pos, acct, taker = m[:, 1], m[:, 3], m[:, 4], m[:, 5], m[:, 6]

    def chg(x, lag):
        return x[j] / x[j - lag] - 1

    def z7(x):
        c = np.cumsum(np.r_[0.0, np.nan_to_num(x)])
        c2 = np.cumsum(np.r_[0.0, np.nan_to_num(x) ** 2])
        w = 288 * 7
        mu = (c[j + 1] - c[j + 1 - w]) / w
        sd = np.sqrt(np.maximum((c2[j + 1] - c2[j + 1 - w]) / w - mu ** 2, 1e-18))
        return (x[j] - mu) / sd

    tk12 = np.convolve(np.nan_to_num(taker, nan=1.0), np.ones(12) / 12, mode="full")[: len(taker)]
    F = [chg(oi, 1), chg(oi, 12), chg(oi, 48), chg(oi, 288), z7(oi), top_acct[j], top_pos[j], acct[j],
         chg(top_pos, 12), chg(acct, 12), z7(top_pos), z7(acct), taker[j], tk12[j], z7(taker)]
    F = np.column_stack(F)
    F[~ok] = np.nan
    return F


def dataset(syms, btc):
    X, Y, S, T = [], [], [], []
    for s in syms:
        a = load(s)
        mp = MET / f"{s}.npy"
        if a is None or not mp.exists():
            continue
        m = np.load(mp)
        Fc, _ = features(a, btc)
        t = a[:, 0]
        i = np.arange(1440, len(a) - max(HS) - 1, STEP)
        i = i[(t[i] >= START) & ((t[i + max(HS)] - t[i]) == 60 * max(HS))]
        F = np.column_stack([Fc[i], met_features(t[i], m)])
        keep = np.isfinite(F).all(1)
        X.append(F[keep])
        Y.append(labels(a, i[keep]))
        S += [s] * int(keep.sum())
        T.append(t[i][keep])
    return np.concatenate(X), np.concatenate(Y), np.array(S), np.concatenate(T)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "design"
    btc = btc_ref()
    Xp, Yp, Sp, Tp = dataset(SYMBOLS, btc)
    Xn, Yn, Sn, Tn = dataset(NEWER, btc)
    X, Y, S, T = (np.concatenate([Xp, Xn]), np.concatenate([Yp, Yn]), np.concatenate([Sp, Sn]),
                  np.concatenate([Tp, Tn]))
    newer = np.isin(S, NEWER)
    fm, vm, tm = T < VAL_FROM, (T >= VAL_FROM) & (T < TEST_FROM), T >= TEST_FROM
    print(f"points fit {fm.sum()}, val {vm.sum()}, test prominent {(tm & ~newer).sum()}, test newer {(tm & newer).sum()}")
    if cmd == "design":
        ms = fit(X[fm], Y[fm])
        P = np.column_stack([m.predict(X[vm]) for m in ms])
        res = []
        for k, (d, H) in enumerate(LABELS):
            for thr in THRS:
                rows = trade(P, Y[vm], S[vm], T[vm], k, thr)
                if len(rows) >= 200:
                    res.append((day_t(rows), statistics.mean(r[2] for r in rows), (k, thr), rows))
        res = [r for r in res if r[1] >= 0.001]
        res.sort(key=lambda r: -r[0])
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
        for lbl, sel in (("prominent", tm & ~newer), ("newer", tm & newer)):
            P = np.column_stack([m.predict(X[sel]) for m in ms])
            rows = trade(P, Y[sel], S[sel], T[sel], k, thr)
            print(f"TEST {LABELS[k]} thr {thr} {lbl} 2026-03..10:", summary(rows, ""))


if __name__ == "__main__":
    main()
