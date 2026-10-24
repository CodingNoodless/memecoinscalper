"""LW: runner prediction and trading after a longer look (20th / 50th / 100th buyer) with realistic fills.
Vault "2026-10-23 longer look before buying (LW)", pre-registered 20:47 PDT.

    uv run python -u -m tradeslave.research.lw_test
"""

from __future__ import annotations

import heapq
import math
import sqlite3
import statistics
import time
from collections import defaultdict

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

from ..paths import DATA_DIR
from .hs_test import E2_1S, L5, coins
from .m5_bt import ret

NS, WIN, SPLIT = (20, 50, 100), 1800, time.mktime(time.strptime("2026-10-23 06:00", "%Y-%m-%d %H:%M"))
FE = ["t_to_n", "buyers_30", "accel", "flow_30", "buy_ratio_60", "sell_share_60", "h_vs_max", "from_lo60", "vs_launch",
      "savvy_n", "top10", "creator_hold"]


def at(rows, n):
    cr, seen = rows[0][4], set()
    for i, r in enumerate(rows):
        if r[2] and r[4] != cr and r[4] not in seen:
            seen.add(r[4])
            if len(seen) == n:
                return i, seen
    return None, seen


def feats(rows, k, early, stats):
    t, p = rows[k][0], rows[k][1]
    pre = rows[:k + 1]
    w = lambda a, b=None: [r for r in pre if t - a < r[0] <= (t - b if b else t)]  # noqa: E731
    w30, w60 = w(30), w(60)
    buy = lambda ws: sum(r[3] for r in ws if r[2])  # noqa: E731
    sell = lambda ws: sum(r[3] for r in ws if not r[2])  # noqa: E731
    last = len({r[4] for r in w(15) if r[2]})
    prev = len({r[4] for r in w(30, 15) if r[2]})
    bal = defaultdict(float)
    for r in pre:
        bal[r[4]] += (r[5] or 0) if r[2] else -(r[5] or 0)
    top = sorted((v for v in bal.values() if v > 1), reverse=True)
    hmax = max(r[6] or 0 for r in pre)
    st = [stats[x] for x in early]
    return dict(t_to_n=t - pre[0][0], buyers_30=len({r[4] for r in w30 if r[2]}), accel=(last + 1) / (prev + 1),
                flow_30=buy(w30) - sell(w30), buy_ratio_60=buy(w60) / sell(w60) if sell(w60) else 9.9,
                sell_share_60=sell(w60) / max(1e-9, buy(w60) + sell(w60)), h_vs_max=(rows[k][6] or 0) / max(1, hmax),
                from_lo60=p / min(r[1] for r in w60) if w60 else 1.0, vs_launch=p / pre[0][1],
                savvy_n=sum(1 for nn, h in st if nn >= 2 and h / nn >= 0.30), top10=sum(top[:10]) / 1e9,
                creator_hold=max(0.0, bal.get(pre[0][4], 0.0)) / 1e9)


def X(rows):
    return np.array([[math.copysign(math.log1p(abs(r[f])), r[f]) for f in FE] for r in rows])


def main() -> None:
    t_end = sqlite3.connect(DATA_DIR / "feed" / "trades.sqlite").execute("SELECT max(t) FROM tr").fetchone()[0]
    allc = list(coins())
    print(f"{len(allc)} complete coins", flush=True)
    for n in NS:
        items = []
        for m, rows in allc:
            k, early = at(rows, n)
            if k is None or rows[k][0] + WIN > t_end:
                continue
            p = rows[k][1]
            mx = max((r[1] for r in rows[k + 1:] if r[0] <= rows[k][0] + WIN), default=p)
            items.append(dict(m=m, rows=rows, k=k, t=rows[k][0], early=list(early)[:20], runner=mx >= 2 * p))
        items.sort(key=lambda x: x["t"])
        stats, pending = defaultdict(lambda: [0, 0]), []
        for i, x in enumerate(items):                           # hit-savvy from resolved earlier coins only
            while pending and pending[0][0] <= x["t"]:
                _, j = heapq.heappop(pending)
                for wlt in items[j]["early"]:
                    stats[wlt][0] += 1
                    stats[wlt][1] += items[j]["runner"]
            x.update(feats(x["rows"], x["k"], x["early"], stats))
            heapq.heappush(pending, (x["t"] + WIN, i))
        for x in items:
            co = dict(m=x["m"], rows=x["rows"])
            for d in (0, 1, 2):
                kk = max(i for i, r in enumerate(x["rows"]) if r[0] <= x["t"] + d) if d else x["k"]
                x[f"L5_{d}"] = ret(co, kk, L5)
            kk = max(i for i, r in enumerate(x["rows"]) if r[0] <= x["t"] + 1)
            x["E2_1"] = ret(co, kk, E2_1S)
        D = [x for x in items if x["t"] < SPLIT]
        H = [x for x in items if x["t"] >= SPLIT]
        if len(D) < 100 or len(H) < 100:
            print(f"N={n}: too few coins ({len(D)}/{len(H)})")
            continue
        Xd, Xh = X(D), X(H)
        mu, sd = Xd.mean(0), Xd.std(0) + 1e-9
        mdl = LogisticRegression(C=0.3, max_iter=2000).fit((Xd - mu) / sd, [x["runner"] for x in D])
        pd_, ph = mdl.predict_proba((Xd - mu) / sd)[:, 1], mdl.predict_proba((Xh - mu) / sd)[:, 1]
        auc_d = roc_auc_score([x["runner"] for x in D], pd_)
        auc_h = roc_auc_score([x["runner"] for x in H], ph)
        cut = float(np.quantile(pd_, 0.8))
        kd = [x for x, q in zip(D, pd_) if q >= cut]
        kh = [x for x, q in zip(H, ph) if q >= cut]
        base = statistics.mean(x["L5_1"] for x in H)
        mk = statistics.mean(x["L5_1"] for x in kh) if kh else float("nan")
        ok = auc_h >= 0.65 and len(kh) >= 40 and mk > 0 and mk >= base + 0.03
        print(f"\nN={n}: design {len(D)} (runner {statistics.mean(x['runner'] for x in D):.1%}), holdout {len(H)} "
              f"(runner {statistics.mean(x['runner'] for x in H):.1%})")
        print(f"   AUC design {auc_d:.3f} holdout {auc_h:.3f}")
        print(f"   holdout base: L5 +0s {statistics.mean(x['L5_0'] for x in H):+.1%} +1s {base:+.1%} +2s {statistics.mean(x['L5_2'] for x in H):+.1%}")
        for lab, g in (("design top-20%", kd), ("holdout kept", kh)):
            if g:
                print(f"   {lab:15s} n {len(g):4d} runner {statistics.mean(x['runner'] for x in g):.1%} | L5 +0s "
                      f"{statistics.mean(x['L5_0'] for x in g):+.1%} +1s {statistics.mean(x['L5_1'] for x in g):+.1%} "
                      f"+2s {statistics.mean(x['L5_2'] for x in g):+.1%} | E2 +1s {statistics.mean(x['E2_1'] for x in g):+.1%}")
        coef = sorted(zip(FE, mdl.coef_[0]), key=lambda z: -abs(z[1]))[:6]
        print("   top coefficients:", ", ".join(f"{f} {c:+.2f}" for f, c in coef))
        print(f"   VERDICT N={n}: {'PASS' if ok else 'FAIL'}")


if __name__ == "__main__":
    main()
