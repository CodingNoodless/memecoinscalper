"""M10: gradient-boosted model of the trade's own L5 return at a 1 s fill, at many decision moments per feed coin.
Vault "2026-10-23 M10 flagship model - backtest (M10)", pre-registered 23:41 PDT.

    uv run python -u -m tradeslave.research.m10_bt build    -> data/m1/m10_samples.json (features + labels)
    uv run python -u -m tradeslave.research.m10_bt          -> train on design, judge on holdout
"""

from __future__ import annotations

import bisect
import heapq
import json
import math
import sqlite3
import statistics
import sys
import time
from collections import defaultdict

import numpy as np

from ..paths import DATA_DIR
from .hs_test import L5, coins
from .lw_test import feats
from .m5_bt import ret

SPLIT = time.mktime(time.strptime("2026-10-23 06:00", "%Y-%m-%d %H:%M"))
OUT = DATA_DIR / "m1" / "m10_samples.json"
FE = ["t_to_n", "buyers_30", "accel", "flow_30", "buy_ratio_60", "sell_share_60", "h_vs_max", "from_lo60", "vs_launch",
      "savvy_n", "top10", "creator_hold", "uniq", "trades_60", "avg_buy_60"]
WIN = 1800


def moments(rows):
    """Row indexes of the decision moments (every 15 s from 15 to 180 s, plus the 20/40/60th buyer)."""
    ts = [r[0] for r in rows]
    t0, cr = ts[0], rows[0][4]
    ks = set()
    for d in range(15, 181, 15):
        k = bisect.bisect_right(ts, t0 + d) - 1
        if k > 0:
            ks.add(k)
    seen = set()
    for i, r in enumerate(rows):
        if r[2] and r[4] != cr and r[4] not in seen:
            seen.add(r[4])
            if len(seen) in (20, 40, 60):
                ks.add(i)
    return sorted(k for k in ks if k - bisect.bisect_left(ts, ts[k] - 60) + 1 >= 5)


def build() -> None:
    t_end = sqlite3.connect(DATA_DIR / "feed" / "trades.sqlite").execute("SELECT max(t) FROM tr").fetchone()[0]
    items = []
    for m, rows in coins():
        if rows[0][0] + WIN > t_end:
            continue
        items.append((rows[0][0], m, rows))
    items.sort()
    stats, pending, out = defaultdict(lambda: [0, 0]), [], []
    for n, (t0, m, rows) in enumerate(items):
        while pending and pending[0][0] <= t0:                     # hit-savvy record from coins resolved earlier
            _, early, runner = heapq.heappop(pending)
            for w in early:
                stats[w][0] += 1
                stats[w][1] += runner
        ts = [r[0] for r in rows]
        cr = rows[0][4]
        buyers = []
        for r in rows:
            if r[2] and r[4] != cr and r[4] not in buyers:
                buyers.append(r[4])
            if len(buyers) >= 20:
                break
        co = dict(m=m, rows=rows)
        for k in moments(rows):
            early = [w for w in buyers if any(x[4] == w and x[2] for x in rows[:k + 1])][:20]
            f = feats(rows, k, early, stats)
            pre = rows[:k + 1]
            w60 = [r for r in pre if r[0] > ts[k] - 60]
            f.update(uniq=len({r[4] for r in pre if r[2]}), trades_60=len(w60),
                     avg_buy_60=statistics.mean([r[3] for r in w60 if r[2]] or [0.0]))
            k1 = bisect.bisect_right(ts, ts[k] + 1) - 1
            k0, k2 = k, bisect.bisect_right(ts, ts[k] + 2) - 1
            if k2 >= len(rows) - 2:
                continue
            out.append(dict(m=m, t=ts[k], part="design" if ts[k] < SPLIT else "holdout", f=f,
                            y1=ret(co, k1, L5), y0=ret(co, k0, L5), y2=ret(co, k2, L5)))
        p = rows[0][1]
        mx = max((r[1] for r in rows if r[0] <= t0 + WIN), default=p)
        heapq.heappush(pending, (t0 + WIN, buyers, mx >= 2 * p))
        if n % 500 == 0:
            print(f"{n}/{len(items)} coins, {len(out)} moments", flush=True)
    OUT.write_text(json.dumps(out))
    print(f"{len(out)} moments from {len(items)} coins")


def X(rows):
    return np.array([[math.copysign(math.log1p(abs(r["f"][k])), r["f"][k]) for k in FE] for r in rows])


def main() -> None:
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.inspection import permutation_importance
    S = json.loads(OUT.read_text())
    D = [s for s in S if s["part"] == "design"]
    H = [s for s in S if s["part"] == "holdout"]
    mdl = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, random_state=0).fit(X(D), [s["y1"] for s in D])
    pd_, ph = mdl.predict(X(D)), mdl.predict(X(H))
    cut = float(np.quantile(pd_, 0.95))

    def trades(rows, pred):
        by = defaultdict(list)
        for s, q in zip(rows, pred):
            by[s["m"]].append((s["t"], q, s))
        out = []
        for m, v in by.items():
            for t, q, s in sorted(v, key=lambda z: z[0]):
                if q >= cut:
                    out.append(s)
                    break
        return out

    td, th = trades(D, pd_), trades(H, ph)
    print(f"moments: design {len(D)}, holdout {len(H)}; base L5 +1 s design {statistics.mean(s['y1'] for s in D):+.1%}, "
          f"holdout {statistics.mean(s['y1'] for s in H):+.1%}")
    for lab, g in (("design (in-sample)", td), ("HOLDOUT", th)):
        if not g:
            print(lab, "no trades")
            continue
        mid = statistics.median(s["t"] for s in g)
        print(f"{lab:18s} trades {len(g):4d} | L5 +1 s mean {statistics.mean(s['y1'] for s in g):+6.1%} med {statistics.median(s['y1'] for s in g):+6.1%} "
              f"| +0 s {statistics.mean(s['y0'] for s in g):+6.1%} +2 s {statistics.mean(s['y2'] for s in g):+6.1%} "
              f"| halves {statistics.mean(s['y1'] for s in g if s['t'] < mid):+.1%} / {statistics.mean(s['y1'] for s in g if s['t'] >= mid):+.1%} "
              f"| <=-25% {sum(s['y1'] <= -0.25 for s in g) / len(g):.0%}")
    ok = len(th) >= 60 and statistics.mean(s["y1"] for s in th) > 0 and all(
        statistics.mean([s["y1"] for s in th if (s["t"] < statistics.median(x["t"] for x in th)) == h] or [-1]) > 0 for h in (True, False))
    imp = permutation_importance(mdl, X(H[:20000]), [s["y1"] for s in H[:20000]], n_repeats=3, random_state=0)
    order = np.argsort(-imp.importances_mean)
    print("holdout permutation importance:", ", ".join(f"{FE[i]} {imp.importances_mean[i]:+.4f}" for i in order[:8]))
    W = [s for s in H if s["y1"] >= 0.5]
    Lz = [s for s in H if s["y1"] <= -0.25]
    print(f"winners (>= +50%, {len(W)}) vs deep losers (<= -25%, {len(Lz)}) on holdout, medians:")
    for i in order[:8]:
        k = FE[i]
        print(f"   {k:14s} winners {statistics.median(s['f'][k] for s in W):9.3f}  losers {statistics.median(s['f'][k] for s in Lz):9.3f}")
    print(f"M10 VERDICT: {'PASS' if ok else 'FAIL'}")




def v2() -> None:
    """M10 v2: a 4-condition calm-entry rule whose features and thresholds come from DESIGN only."""
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.inspection import permutation_importance
    S = json.loads(OUT.read_text())
    D = [s for s in S if s["part"] == "design"]
    H = [s for s in S if s["part"] == "holdout"]
    mdl = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, random_state=0).fit(X(D), [s["y1"] for s in D])
    imp = permutation_importance(mdl, X(D), [s["y1"] for s in D], n_repeats=3, random_state=0)
    top = [FE[i] for i in np.argsort(-imp.importances_mean)[:4]]
    W = [s for s in D if s["y1"] >= 0.5]
    Lz = [s for s in D if s["y1"] <= -0.25]
    rule = []
    for k in top:
        mw, ml = statistics.median(s["f"][k] for s in W), statistics.median(s["f"][k] for s in Lz)
        rule.append((k, ">=" if mw > ml else "<=", (mw + ml) / 2))
    print(f"design winners {len(W)}, deep losers {len(Lz)}; rule: " + " & ".join(f"{k} {op} {c:.3f}" for k, op, c in rule))

    def ok(s):
        return all((s["f"][k] >= c) if op == ">=" else (s["f"][k] <= c) for k, op, c in rule)

    for lab, rows in (("design", D), ("HOLDOUT", H)):
        by = defaultdict(list)
        for s in rows:
            by[s["m"]].append(s)
        tr = []
        for v in by.values():
            for s in sorted(v, key=lambda z: z["t"]):
                if ok(s):
                    tr.append(s)
                    break
        if not tr:
            print(lab, "no trades")
            continue
        mid = statistics.median(s["t"] for s in tr)
        h1 = [s["y1"] for s in tr if s["t"] < mid]
        h2 = [s["y1"] for s in tr if s["t"] >= mid]
        m = statistics.mean(s["y1"] for s in tr)
        print(f"{lab:8s} trades {len(tr):4d} | L5 +1 s mean {m:+6.1%} med {statistics.median(s['y1'] for s in tr):+6.1%} | "
              f"+0 s {statistics.mean(s['y0'] for s in tr):+6.1%} +2 s {statistics.mean(s['y2'] for s in tr):+6.1%} | halves "
              f"{statistics.mean(h1):+.1%} / {statistics.mean(h2):+.1%} | <=-25% {sum(s['y1'] <= -0.25 for s in tr) / len(tr):.0%}")
        if lab == "HOLDOUT":
            print(f"M10 v2 VERDICT: {'PASS' if len(tr) >= 60 and m > 0 and statistics.mean(h1) > 0 and statistics.mean(h2) > 0 else 'FAIL'}")




def v3() -> None:
    """M10 v3: v2 entries, six exit sets; pick the best on design, judge once on holdout."""
    from .m5_bt import EXITS
    E2 = EXITS["E2 M4 exits, 15 s holder grace"]
    sets = {
        "1 L5": dict(stop=0.90, P=dict(E2["P"], poll=1.0)),
        "2 E2 (-30%)": dict(stop=0.70, P=dict(E2["P"], poll=1.0)),
        "3 -10%, no TP": dict(stop=0.90, P=dict(E2["P"], poll=1.0, tp=99.0)),
        "4 -10%, TP 3x": dict(stop=0.90, P=dict(E2["P"], poll=1.0, tp=3.0)),
        "5 -10%, half at 2x": dict(stop=0.90, P=dict(E2["P"], poll=1.0, tp=99.0, half_tp=2.0)),
        "6 -20%, trend only": dict(stop=0.80, P=dict(E2["P"], poll=1.0, tp=99.0, hdecl=None, stall_s=99999, max_hold=1800)),
    }
    S = json.loads(OUT.read_text())
    rule = (("avg_buy_60", "<=", 0.511), ("top10", "<=", 0.233), ("t_to_n", ">=", 30.25), ("buy_ratio_60", "<=", 1.593))
    first = {}
    for s in sorted(S, key=lambda z: z["t"]):
        if s["m"] not in first and all((s["f"][k] >= c) if op == ">=" else (s["f"][k] <= c) for k, op, c in rule):
            first[s["m"]] = s
    rows_of = {m: rows for m, rows in coins() if m in first}
    res = defaultdict(lambda: defaultdict(list))
    for m, s in first.items():
        rows = rows_of.get(m)
        if not rows:
            continue
        ts = [r[0] for r in rows]
        k1 = bisect.bisect_right(ts, s["t"] + 1) - 1
        for name, ex in sets.items():
            res[name][s["part"]].append((s["t"], ret(dict(m=m, rows=rows), k1, ex)))
    best = max(sets, key=lambda n: statistics.mean(r for _, r in res[n]["design"]))
    for n in sets:
        d, h = [r for _, r in res[n]["design"]], sorted(res[n]["holdout"])
        mid = len(h) // 2
        print(f"{n:22s} design {statistics.mean(d):+6.1%} (n {len(d)}) | holdout {statistics.mean(r for _, r in h):+6.1%} (n {len(h)}) "
              f"halves {statistics.mean(r for _, r in h[:mid]):+.1%} / {statistics.mean(r for _, r in h[mid:]):+.1%} "
              f"<=-25% {sum(r <= -0.25 for _, r in h) / len(h):.0%} >=+50% {sum(r >= 0.5 for _, r in h) / len(h):.1%}")
    h = sorted(res[best]["holdout"])
    mid = len(h) // 2
    m = statistics.mean(r for _, r in h)
    ok = len(h) >= 60 and m > 0 and statistics.mean(r for _, r in h[:mid]) > 0 and statistics.mean(r for _, r in h[mid:]) > 0
    print(f"chosen on design: {best} -> holdout {m:+.1%}\nM10 v3 VERDICT: {'PASS' if ok else 'FAIL'}")


if __name__ == "__main__":
    build() if "build" in sys.argv else v2() if "v2" in sys.argv else v3() if "v3" in sys.argv else main()
