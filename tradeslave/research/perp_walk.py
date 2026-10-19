"""Strategy N3: walk-forward chart reading on memecoin perps (refit every 7 days on the previous 60 days).

    uv run --no-sync python -m tradeslave.research.perp_walk

Features / labels / trades: research.perp_chart (24 chart features, 15-minute decision points, long / short 60 min,
0.07%/side). Every STEP_D days from WALK_FROM, fit GBM regressors on samples whose 60-min label completed before the
refit time within the last FIT_D days, and trade the next STEP_D days: the better of long / short when its prediction
is >= THR, non-overlapping per coin. Fixed in advance; vault note "2026-10-19 neural chart prediction (N)", N3.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone

import numpy as np

from .perp_chart import LABELS, NEWER, btc_ref, dataset, day_t, fit, summary
from .perps_data import SYMBOLS

WALK_FROM = datetime(2025, 10, 1, tzinfo=timezone.utc).timestamp()
WALK_TO = datetime(2026, 10, 16, tzinfo=timezone.utc).timestamp()
STEP_D, FIT_D, THR = 7, 60, 0.004
K_LONG, K_SHORT = LABELS.index((1, 60)), LABELS.index((-1, 60))


def main() -> None:
    btc = btc_ref()
    Xp, Yp, Sp, Tp = dataset(SYMBOLS, btc)
    Xn, Yn, Sn, Tn = dataset(NEWER, btc)
    X, Y, S, T = np.concatenate([Xp, Xn]), np.concatenate([Yp, Yn]), np.concatenate([Sp, Sn]), np.concatenate([Tp, Tn])
    rows, busy = [], {}
    t = WALK_FROM
    while t < WALK_TO:
        tr = (T >= t - FIT_D * 86400) & (T + 3600 + 60 <= t)
        te = (T >= t) & (T < t + STEP_D * 86400)
        if tr.sum() < 5000 or not te.any():
            t += STEP_D * 86400
            continue
        ms = fit(X[tr][:, :], Y[tr][:, [K_LONG, K_SHORT]])
        idx = np.flatnonzero(te)
        P = np.column_stack([m.predict(X[idx]) for m in ms])
        for j in idx[np.argsort(T[idx], kind="stable")]:
            p = P[np.searchsorted(idx, j)]
            k = int(np.argmax(p))
            if p[k] < THR or T[j] < busy.get(S[j], -1):
                continue
            busy[S[j]] = T[j] + 3600
            rows.append((T[j], S[j], float(Y[j, (K_LONG, K_SHORT)[k]]), k))
        print(f"  {datetime.fromtimestamp(t, timezone.utc):%Y-%m-%d}: fit {tr.sum()} trades so far {len(rows)}", flush=True)
        t += STEP_D * 86400
    rows.sort()
    h = len(rows) // 2
    r3 = [r[:3] for r in rows]
    print("WALK all:", summary(r3, ""))
    print("  prominent:", summary([r for r in r3 if r[1] not in NEWER], ""))
    print("  newer:", summary([r for r in r3 if r[1] in NEWER], ""))
    print("  1st half:", summary(r3[:h], ""), "| 2nd half:", summary(r3[h:], ""))
    print(f"  longs {sum(r[3] == 0 for r in rows)}, shorts {sum(r[3] == 1 for r in rows)}; day-t {day_t(r3):+.2f}; "
          f"mean {statistics.mean(r[2] for r in rows):+.3%}")


if __name__ == "__main__":
    main()
