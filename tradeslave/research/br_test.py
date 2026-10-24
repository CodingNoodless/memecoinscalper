"""BR: reverse-engineer profitable trading bots from their trades, then trade the learned rule ourselves at a 1 s fill.
Vault "2026-10-24 reverse-engineering profitable bots (BR)", pre-registered 00:54 PDT.

1. Find bots on the design period (feed before Oct 23 06:00): >= 30 closed round trips, profit > 0 and ROI >= 5%, profitable in both
   design halves, < 50% of entries in a coin's first 2 s (not creation snipers), consistent buy size (CV <= 0.30).
2. Learn "a bot buys here": gradient boosting, the bots' entry states (just before their buy) vs a 15 s grid of states in the same coins.
3. Holdout (after the split): score the 15 s grid, buy the first state per coin above the design 95th-percentile control score,
   fill 1 s later, L5 exits. PASS: n >= 60, mean > 0, both holdout halves > 0.

    uv run python -u -m tradeslave.research.br_test
"""

from __future__ import annotations

import bisect
import math
import sqlite3
import statistics
from collections import defaultdict

import numpy as np

from ..paths import DATA_DIR
from .hs_test import L5, coins
from .lw_test import feats
from .m5_bt import ret
from .m10_bt import SPLIT

FE = ["t_to_n", "buyers_30", "accel", "flow_30", "buy_ratio_60", "sell_share_60", "h_vs_max", "from_lo60", "vs_launch",
      "top10", "creator_hold", "uniq", "trades_60", "avg_buy_60"]
GRID, HORIZON, WIN = 15, 900, 1800


def state(rows, k):
    f = feats(rows, k, [], {})
    t = rows[k][0]
    w60 = [r for r in rows[:k + 1] if r[0] > t - 60]
    f.update(uniq=len({r[4] for r in rows[:k + 1] if r[2]}), trades_60=len(w60),
             avg_buy_60=statistics.mean([r[3] for r in w60 if r[2]] or [0.0]))
    return f


def X(fs):
    return np.array([[math.copysign(math.log1p(abs(f[k])), f[k]) for k in FE] for f in fs])


def trips(allc):
    """wallet -> list of round trips (coin, t entry, t first sell, sol in, sol out, sizes, entry row, entry age)."""
    out = defaultdict(list)
    for m, rows in allc:
        t0 = rows[0][0]
        per = {}
        for i, (t, p, b, sol, w, tok, h) in enumerate(rows):
            x = per.setdefault(w, dict(si=0.0, so=0.0, ti=0.0, to=0.0, k=None, ts=None, sizes=[]))
            if b:
                x["si"] += sol
                x["ti"] += tok
                x["sizes"].append(sol)
                if x["k"] is None:
                    x["k"] = i
            elif x["k"] is not None:
                x["so"] += sol
                x["to"] += tok
                if x["ts"] is None:
                    x["ts"] = t
        for w, x in per.items():
            if x["k"] is None or x["si"] <= 0 or x["ti"] <= 0 or x["to"] < 0.9 * x["ti"] or w == rows[0][4]:
                continue
            te = rows[x["k"]][0]
            out[w].append(dict(m=m, t=te, hold=x["ts"] - te, si=x["si"], so=x["so"], size=x["sizes"][0], k=x["k"], age=te - t0))
    return out


def bots(tr):
    found = {}
    mid = None
    for w, v in tr.items():
        d = [x for x in v if x["t"] < SPLIT]
        if len(d) < 30:
            continue
        mid = mid or statistics.median(x["t"] for vv in tr.values() for x in vv if x["t"] < SPLIT)
        si, so = sum(x["si"] for x in d), sum(x["so"] for x in d)
        halves = [sum(x["so"] - x["si"] for x in d if (x["t"] < mid) == h) for h in (True, False)]
        sz = [x["size"] for x in d]
        cv = statistics.pstdev(sz) / statistics.mean(sz)
        early = sum(x["age"] <= 2 for x in d) / len(d)
        if so > si and (so - si) / si >= 0.05 and min(halves) > 0 and early < 0.5 and cv <= 0.30:
            found[w] = dict(n=len(d), roi=(so - si) / si, prof=so - si, cv=cv, early=early, hold=statistics.median(x["hold"] for x in d),
                            win=sum(x["so"] > x["si"] for x in d) / len(d), size=statistics.median(sz))
    return found


def main() -> None:
    t_end = sqlite3.connect(DATA_DIR / "feed" / "trades.sqlite").execute("SELECT max(t) FROM tr").fetchone()[0]
    allc = [(m, rows) for m, rows in coins() if rows[0][0] + WIN <= t_end]
    byc = dict(allc)
    tr = trips(allc)
    B = bots(tr)
    print(f"{len(allc)} coins, {len(tr)} wallets with closed round trips; bots found on design: {len(B)}", flush=True)
    for w, s in sorted(B.items(), key=lambda kv: -kv[1]["prof"])[:25]:
        hv = [x for x in tr[w] if x["t"] >= SPLIT]
        hr = (sum(x["so"] for x in hv) / sum(x["si"] for x in hv) - 1) if hv else None
        print(f"  {w[:8]}… n {s['n']:4d} roi {s['roi']:+6.1%} prof {s['prof']:7.2f} SOL win {s['win']:4.0%} size {s['size']:.3f} "
              f"cv {s['cv']:.2f} hold {s['hold']:6.1f}s early {s['early']:4.0%} | holdout n {len(hv)} roi "
              + (f"{hr:+.1%}" if hr is not None else "n/a"))
    if not B:
        print("BR VERDICT: FAIL (no bots pass the design filter)")
        return
    hv = [x for w in B for x in tr[w] if x["t"] >= SPLIT]
    if hv:
        print(f"bots' own holdout: n {len(hv)} roi {sum(x['so'] for x in hv) / sum(x['si'] for x in hv) - 1:+.1%} "
              f"win {sum(x['so'] > x['si'] for x in hv) / len(hv):.0%}")

    # 2. their entry states vs the 15 s grid in the same coins (design)
    pos, ctl, ages, holds = [], [], [], []
    seen = set()
    for w in B:
        for x in tr[w]:
            if x["t"] >= SPLIT or x["k"] < 3 or x["age"] > HORIZON:
                continue
            pos.append(state(byc[x["m"]], x["k"] - 1))
            ages.append(x["age"])
            holds.append(x["hold"])
            seen.add(x["m"])
    print(f"design bot entries {len(pos)} in {len(seen)} coins; entry age median {statistics.median(ages):.0f}s; "
          f"hold median {statistics.median(holds):.0f}s (p25 {np.percentile(holds, 25):.0f}, p75 {np.percentile(holds, 75):.0f})", flush=True)

    def grid(rows):
        ts = [r[0] for r in rows]
        for d in range(GRID, HORIZON + 1, GRID):
            k = bisect.bisect_right(ts, ts[0] + d) - 1
            if k >= 4 and k - bisect.bisect_left(ts, ts[k] - 60) + 1 >= 5 and ts[-1] > ts[k] + 2:
                yield d, k

    for m in seen:
        for _, k in grid(byc[m]):
            ctl.append(state(byc[m], k))
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score
    Xd, yd = np.vstack([X(pos), X(ctl)]), np.array([1] * len(pos) + [0] * len(ctl))
    idx = np.random.default_rng(7).permutation(len(yd))
    cut = int(0.7 * len(idx))
    clf = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_depth=4, random_state=7).fit(Xd[idx[:cut]], yd[idx[:cut]])
    print(f"'bot buys here' model: design in-sample-split AUC {roc_auc_score(yd[idx[cut:]], clf.predict_proba(Xd[idx[cut:]])[:, 1]):.3f}")
    clf = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_depth=4, random_state=7).fit(Xd, yd)
    thr = float(np.percentile(clf.predict_proba(X(ctl))[:, 1], 95))
    P, C = np.array(pos and [[f[k] for k in FE] for f in pos]), np.array([[f[k] for k in FE] for f in ctl])
    print("feature medians, bot entries vs grid:")
    for j, k in enumerate(FE):
        print(f"   {k:14s} {np.median(P[:, j]):9.3f} vs {np.median(C[:, j]):9.3f}")

    # 3. holdout: first grid state per coin scoring above thr; fill +1 s (and +0 s for the speed cost); L5 exits
    res = []
    hcoins = [(m, rows) for m, rows in allc if rows[0][0] >= SPLIT]
    hmid = statistics.median(rows[0][0] for _, rows in hcoins)
    for m, rows in hcoins:
        g = list(grid(rows))
        if not g:
            continue
        sc = clf.predict_proba(X([state(rows, k) for _, k in g]))[:, 1]
        hit = next((g[i] for i in range(len(g)) if sc[i] >= thr), None)
        if hit is None:
            continue
        ts = [r[0] for r in rows]
        k = hit[1]
        k1 = bisect.bisect_right(ts, ts[k] + 1) - 1
        co = dict(m=m, rows=rows)
        res.append(dict(h="h1" if rows[0][0] < hmid else "h2", y1=ret(co, k1, L5), y0=ret(co, k, L5), age=hit[0]))
    if not res:
        print("BR VERDICT: FAIL (no holdout trades)")
        return
    for key, lab in (("y1", "fill +1 s (pre-registered)"), ("y0", "fill +0 s (report)")):
        v = [r[key] for r in res]
        h = {hh: [r[key] for r in res if r["h"] == hh] for hh in ("h1", "h2")}
        print(f"holdout {lab}: n {len(v)} mean {statistics.mean(v):+.1%} med {statistics.median(v):+.1%} win {sum(x > 0 for x in v) / len(v):.0%} "
              f"<=-25% {sum(x <= -0.25 for x in v) / len(v):.0%} | halves " + " / ".join(f"{statistics.mean(h[hh]):+.1%}" if h[hh] else "n/a" for hh in h))
    v = [r["y1"] for r in res]
    ok = len(v) >= 60 and statistics.mean(v) > 0 and all(
        [r["y1"] for r in res if r["h"] == hh] and statistics.mean([r["y1"] for r in res if r["h"] == hh]) > 0 for hh in ("h1", "h2"))
    print(f"entry age median {statistics.median(r['age'] for r in res):.0f}s")
    print(f"BR VERDICT: {'PASS' if ok else 'FAIL'}")


if __name__ == "__main__":
    main()
