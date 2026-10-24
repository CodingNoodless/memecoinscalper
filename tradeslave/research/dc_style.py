"""DC: learn decu's style from every one of his trades (coin histories from dc_fetch), then trade it ourselves.
Vault "2026-10-24 learning decu's style (DC)", pre-registered before the run.

Parts
  A. anatomy (report): coin state at his entries vs a 15 s grid of the same coins; what happened just before his sells.
  B. "trade like decu" without watching him: GBM of "decu buys here" (his design-half entries vs the grid of his design coins);
     policy on the live feed after Oct 23 06:00 (coins he never had to touch): first 15 s grid state per coin scoring >= the design
     95th-percentile grid score, fill +1 s, exit = his style exit (sell all after his design median hold) with a -25% stop.
     PASS: n >= 60, mean > 0, both halves > 0.
  C. "better than decu": copy his entries (his first buy + 1 s) and pick the exit on his design half from a fixed menu;
     judge that exit on his holdout half (his later trades). PASS: n >= 60, mean > 0, both halves of the holdout > 0.

    uv run python -u -m tradeslave.research.dc_style
"""

from __future__ import annotations

import bisect
import json
import statistics
from collections import defaultdict

import numpy as np

from ..paths import DATA_DIR
from . import br_test as B
from .hs_test import coins

W = "4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9"
DIR = DATA_DIR / "m1" / "decu"
FEE, TXR = 0.0125, 2 * 0.0005 / 0.1
MENU = [("his first sell", None), ("hold 30 s", ("hold", 30)), ("hold 60 s", ("hold", 60)), ("hold 120 s", ("hold", 120)),
        ("hold 300 s", ("hold", 300)), ("trail 15%", ("trail", 0.15)), ("trail 25%", ("trail", 0.25)),
        ("trail 40%", ("trail", 0.40)), ("TP 50% / stop 20%", ("tpsl", 0.5, 0.2)), ("TP 100% / stop 25%", ("tpsl", 1.0, 0.25)),
        ("his sell, stop 15%", ("hisstop", 0.15)), ("his sell, stop 25%", ("hisstop", 0.25))]


def rows_of(j):
    """Fetched coin -> hs_test row format [t, p, b, sol, w, tok, holders]."""
    out, bal, h = [], {}, 0
    for slot, ts, w, b, sol, tok, vs, vt in j["rows"]:
        if not vs or not vt:
            continue
        b0 = bal.get(w, 0.0)
        b1 = b0 + (tok if b else -tok)
        bal[w] = b1
        h += (b0 <= 1 < b1) - (b1 <= 1 < b0)
        out.append([ts, vs / vt, bool(b), sol, w, tok, h])
    return out


def at(ts, t):
    return max(0, bisect.bisect_right(ts, t) - 1)


def exit_ret(rows, ts, ke, t_his_sell, rule, delay=1):
    """Return of a position bought at rows[ke] price, managed by rule; sells fill at +delay s."""
    pe = rows[ke][1]
    t0 = rows[ke][0]
    if rule is None:
        return rows[at(ts, t_his_sell + delay)][1] / pe * (1 - FEE) ** 2 - 1 - TXR
    kind = rule[0]
    peak = pe
    for i in range(ke + 1, len(rows)):
        t, p = rows[i][0], rows[i][1]
        peak = max(peak, p)
        hit = (kind == "hold" and t >= t0 + rule[1]) or (kind == "trail" and p <= peak * (1 - rule[1])) or \
              (kind == "tpsl" and (p >= pe * (1 + rule[1]) or p <= pe * (1 - rule[2]))) or \
              (kind == "hisstop" and (t >= t_his_sell or p <= pe * (1 - rule[1])))
        if hit:
            return rows[at(ts, t + delay)][1] / pe * (1 - FEE) ** 2 - 1 - TXR
    return rows[-1][1] / pe * (1 - FEE) ** 2 - 1 - TXR


def trips():
    d = json.loads((DATA_DIR / "m1" / "st_decu_trades.json").read_text())
    out = []
    for p in sorted(DIR.glob("*.json")):
        j = json.loads(p.read_text())
        rows = rows_of(j)
        if len(rows) < 20:
            continue
        ev = sorted(d[p.stem])
        b = [e for e in ev if e[2] < 0 and e[1] > 0]
        s = [e for e in ev if e[2] > 0 and e[1] < 0]
        if not b or not s or s[0][0] < b[0][0]:
            continue
        ts = [r[0] for r in rows]
        mine = [i for i, r in enumerate(rows) if r[4] == W and r[2]]
        kb = mine[0] if mine else at(ts, b[0][0])
        out.append(dict(m=p.stem, rows=rows, ts=ts, kb=kb, tb=b[0][0], tsell=s[0][0], created=j["created"],
                        own=sum(e[2] for e in s) / -sum(e[2] for e in b) - 1, capped=j["capped"]))
    return out


def grid(rows, horizon=B.HORIZON):
    ts = [r[0] for r in rows]
    for dd in range(B.GRID, horizon + 1, B.GRID):
        k = bisect.bisect_right(ts, ts[0] + dd) - 1
        if k >= 4 and k - bisect.bisect_left(ts, ts[k] - 60) + 1 >= 5 and ts[-1] > ts[k] + 2:
            yield dd, k


def main() -> None:
    T = trips()
    T.sort(key=lambda x: x["tb"])
    mid = statistics.median(x["tb"] for x in T)
    D, H = [x for x in T if x["tb"] < mid], [x for x in T if x["tb"] >= mid]
    print(f"decu round trips with curve histories: {len(T)} (design {len(D)}, holdout {len(H)}); "
          f"his own mean per coin {statistics.mean(x['own'] for x in T):+.1%}", flush=True)

    # A. anatomy
    pos, ctl = [], []
    for x in T:
        rows = x["rows"]
        if x["kb"] >= 3:
            f = B.state(rows, x["kb"] - 1)
            f["age_s"] = rows[x["kb"]][0] - x["created"]
            f["mcap_sol"] = rows[x["kb"] - 1][1] * 1e9
            pos.append((x, f))
        for _, k in grid(rows):
            g = B.state(rows, k)
            g["age_s"] = rows[k][0] - x["created"]
            g["mcap_sol"] = rows[k][1] * 1e9
            ctl.append((x, g))
    keys = B.FE + ["age_s", "mcap_sol"]
    print("\nA. coin state at his entries vs the 15 s grid of the same coins (medians), and his winners vs losers:")
    for k in keys:
        a = [f[k] for _, f in pos]
        wv = [f[k] for x, f in pos if x["own"] > 0]
        lv = [f[k] for x, f in pos if x["own"] <= 0]
        print(f"   {k:14s} entry {np.median(a):9.3f} | grid {np.median([g[k] for _, g in ctl]):9.3f} | winners {np.median(wv):9.3f} losers {np.median(lv):9.3f}")
    pre = defaultdict(list)
    for x in T:
        rows, ts = x["rows"], x["ts"]
        ks = at(ts, x["tsell"] - 0.5)
        kb = x["kb"]
        seg = rows[kb:ks + 1]
        if not seg:
            continue
        pk = max(r[1] for r in seg)
        pre["ret at his sell (curve)"].append(rows[ks][1] / rows[kb][1] - 1)
        pre["peak since entry"].append(pk / rows[kb][1] - 1)
        pre["off peak at his sell"].append(rows[ks][1] / pk - 1)
        tp = next(r[0] for r in seg if r[1] == pk)
        pre["s since peak at sell"].append(x["tsell"] - tp)
        w5 = [r for r in rows[:ks + 1] if r[0] > x["tsell"] - 5]
        pre["sell SOL share, 5 s before"].append(sum(r[3] for r in w5 if not r[2]) / max(1e-9, sum(r[3] for r in w5)))
        cr = rows[0][4]
        pre["creator sold before his sell"].append(any(r[4] == cr and not r[2] for r in rows[kb:ks + 1]))
    print("\nA. just before his first sell (medians; booleans as shares):")
    for k, v in pre.items():
        v = [float(z) for z in v]
        print(f"   {k:30s} median {np.median(v):+8.3f}  mean {np.mean(v):+8.3f}")

    # C. better than decu: copy entries +1 s, exit chosen on design
    print("\nC. copy his entries at +1 s, exit menu (design -> holdout):")
    res = {}
    for name, rule in MENU:
        for part, xs in (("D", D), ("H", H)):
            v = []
            for x in xs:
                ke = at(x["ts"], x["tb"] + 1)
                if ke >= len(x["rows"]) - 1:
                    continue
                v.append((x["tb"], exit_ret(x["rows"], x["ts"], ke, x["tsell"], rule)))
            res[(name, part)] = v
        dv, hv = [r for _, r in res[(name, "D")]], [r for _, r in res[(name, "H")]]
        print(f"   {name:20s} design n {len(dv)} mean {statistics.mean(dv):+6.1%} | holdout n {len(hv)} mean {statistics.mean(hv):+6.1%} med {statistics.median(hv):+6.1%}")
    best = max(MENU, key=lambda nr: statistics.mean([r for _, r in res[(nr[0], "D")]]))[0]
    hv = res[(best, "H")]
    hm = statistics.median(t for t, _ in hv)
    halves = [[r for t, r in hv if (t < hm) == q] for q in (True, False)]
    okC = len(hv) >= 60 and statistics.mean(r for _, r in hv) > 0 and all(statistics.mean(h) > 0 for h in halves)
    print(f"   design pick: {best}; holdout mean {statistics.mean(r for _, r in hv):+.1%}, halves {statistics.mean(halves[0]):+.1%} / "
          f"{statistics.mean(halves[1]):+.1%} -> C VERDICT: {'PASS' if okC else 'FAIL'}")

    # B. trade like decu on the live feed, without watching him
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score
    Pd = [f for x, f in pos if x["tb"] < mid]
    Cd = [g for x, g in ctl if x["tb"] < mid]
    Ph = [f for x, f in pos if x["tb"] >= mid]
    Ch = [g for x, g in ctl if x["tb"] >= mid]
    clf = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_depth=4, random_state=7).fit(
        np.vstack([B.X(Pd), B.X(Cd)]), [1] * len(Pd) + [0] * len(Cd))
    if Ph and Ch:
        print(f"\nB. 'decu buys here' model: AUC on his holdout-half coins {roc_auc_score([1] * len(Ph) + [0] * len(Ch), clf.predict_proba(np.vstack([B.X(Ph), B.X(Ch)]))[:, 1]):.3f}")
    thr = float(np.percentile(clf.predict_proba(B.X(Cd))[:, 1], 95))
    hold = statistics.median(x["tsell"] - x["tb"] for x in D)
    import sqlite3
    t_end = sqlite3.connect(DATA_DIR / "feed" / "trades.sqlite").execute("SELECT max(t) FROM tr").fetchone()[0]
    feed = [(m, r) for m, r in coins() if r[0][0] >= B.SPLIT and r[0][0] + B.WIN <= t_end]
    fm = statistics.median(r[0][0] for _, r in feed)
    out = []
    for m, rows in feed:
        g = list(grid(rows))
        if not g:
            continue
        sc = clf.predict_proba(B.X([B.state(rows, k) for _, k in g]))[:, 1]
        hit = next((g[i] for i in range(len(g)) if sc[i] >= thr), None)
        if hit is None:
            continue
        ts = [r[0] for r in rows]
        ke = at(ts, rows[hit[1]][0] + 1)
        r = exit_ret(rows, ts, ke, rows[ke][0] + hold, ("hisstop", 0.25))
        out.append((rows[0][0] < fm, r))
    v = [r for _, r in out]
    if v:
        h1, h2 = [r for q, r in out if q], [r for q, r in out if not q]
        okB = len(v) >= 60 and statistics.mean(v) > 0 and h1 and h2 and statistics.mean(h1) > 0 and statistics.mean(h2) > 0
        print(f"B. feed policy (hold {hold:.0f} s, -25% stop, +1 s fills): n {len(v)} mean {statistics.mean(v):+.1%} med {statistics.median(v):+.1%} "
              f"win {sum(r > 0 for r in v) / len(v):.0%} | halves {statistics.mean(h1) if h1 else float('nan'):+.1%} / "
              f"{statistics.mean(h2) if h2 else float('nan'):+.1%} -> B VERDICT: {'PASS' if okB else 'FAIL'}")
    else:
        print("B. no feed trades -> B VERDICT: FAIL")


if __name__ == "__main__":
    main()
