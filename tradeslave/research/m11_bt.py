"""M11 backtest: follow profitable leader wallets, filter the entries with what the wallet study found, exit fast.
Vault "2026-10-24 M11 - leader wave rider (M11)", pre-registered before the run.

Protocol (no look-ahead): feed split at Oct 23 06:00 (design before, holdout after); the design is split again at its middle.
  D1 (first design half): rank leaders = wallets with >= 8 clean round trips (sold 90-110% of what they bought), after-fee
      profit > 0, median hold >= 10 s, < 50% of entries in a coin's first 2 s.
  D2 (second design half): pick ONE variant (leader set x entry filter x consensus x exit) by mean return at +1 s fills,
      among variants with >= 30 trades on D2.
  Holdout: leaders re-ranked on the whole design period with the same rule; the chosen variant judged once.
  PASS: n >= 60, mean > 0, both holdout halves > 0. Fills +1 s (entry and exit), 1.25% fee a side + tx cost.

    uv run python -u -m tradeslave.research.m11_bt
"""

from __future__ import annotations

import bisect
import itertools
import json
import sqlite3
import statistics
from collections import defaultdict

from ..paths import DATA_DIR
from . import br_test as B
from .dc_style import exit_ret
from .hs_test import coins

FEE = 0.0125
LEADERS = ("all", "top20")
FILTERS = ("none", "accel<=4", "accel<=4,age>=15")
CONSENSUS = (1, 2)
EXITS = (("TP50/SL20", ("tpsl", 0.5, 0.2)), ("TP30/SL15", ("tpsl", 0.3, 0.15)), ("TP100/SL25", ("tpsl", 1.0, 0.25)),
         ("trail15", ("trail", 0.15)), ("hold30", ("hold", 30)), ("leader sell, SL20", ("hisstop", 0.2)))


def clean_trips(allc):
    out = defaultdict(list)
    for m, rows in allc:
        t0, cr = rows[0][0], rows[0][4]
        per = {}
        for i, (t, p, b, sol, w, tok, h) in enumerate(rows):
            x = per.setdefault(w, dict(si=0.0, so=0.0, ti=0.0, to=0.0, k=None, ts=None))
            if b:
                x["si"] += sol
                x["ti"] += tok
                if x["k"] is None:
                    x["k"] = i
            elif x["k"] is not None:
                x["so"] += sol
                x["to"] += tok
                if x["ts"] is None:
                    x["ts"] = t
        for w, x in per.items():
            if x["k"] is None or w == cr or x["si"] <= 0 or not 0.9 * x["ti"] <= x["to"] <= 1.1 * x["ti"]:
                continue
            te = rows[x["k"]][0]
            out[w].append(dict(m=m, t=te, hold=x["ts"] - te, age=te - t0, net=x["so"] * (1 - FEE) - x["si"] * (1 + FEE)))
    return out


def leaders(tr, a, b):
    sc = {}
    for w, v in tr.items():
        d = [x for x in v if a <= x["t"] < b]
        if len(d) < 8:
            continue
        net = sum(x["net"] for x in d)
        if net > 0 and statistics.median(x["hold"] for x in d) >= 10 and sum(x["age"] <= 2 for x in d) / len(d) < 0.5:
            sc[w] = net
    top = set(sorted(sc, key=lambda w: -sc[w])[:20])
    return {"all": set(sc), "top20": top}


def signals(allc, L, a, b, k_needed, filt):
    """One entry per coin: the moment the k-th distinct leader makes a first buy (within 60 s of the first)."""
    out = []
    for m, rows in allc:
        if not a <= rows[0][0] < b:
            continue
        firsts, seen = [], set()
        for i, r in enumerate(rows):
            if r[2] and r[4] in L and r[4] not in seen:
                seen.add(r[4])
                firsts.append(i)
        if len(firsts) < k_needed or rows[firsts[k_needed - 1]][0] - rows[firsts[0]][0] > 60:
            continue
        kb = firsts[k_needed - 1]
        if kb < 3:
            continue
        if filt != "none":
            f = B.state(rows, kb - 1)
            if f["accel"] > 4 or ("age" in filt and rows[kb][0] - rows[0][0] < 15):
                continue
        lw = rows[kb][4]
        ls = next((r[0] for r in rows[kb + 1:] if r[4] == lw and not r[2]), rows[kb][0] + 600)
        out.append((m, rows, kb, ls))
    return out


def run(sig, rule):
    v = []
    for m, rows, kb, ls in sig:
        ts = [r[0] for r in rows]
        ke = max(0, bisect.bisect_right(ts, rows[kb][0] + 1) - 1)
        if ke >= len(rows) - 1:
            continue
        v.append((rows[kb][0], exit_ret(rows, ts, ke, ls, rule)))
    return v


def main() -> None:
    t_end = sqlite3.connect(DATA_DIR / "feed" / "trades.sqlite").execute("SELECT max(t) FROM tr").fetchone()[0]
    allc = [(m, r) for m, r in coins() if r[0][0] + 900 <= t_end]
    t0 = min(r[0][0] for _, r in allc)
    dmid = (t0 + B.SPLIT) / 2
    tr = clean_trips(allc)
    L1, L_all = leaders(tr, t0, dmid), leaders(tr, t0, B.SPLIT)
    print(f"{len(allc)} coins; leaders on D1: {len(L1['all'])}; on the whole design: {len(L_all['all'])}", flush=True)
    res = []
    for ls_, filt, k in itertools.product(LEADERS, FILTERS, CONSENSUS):
        sig = signals(allc, L1[ls_], dmid, B.SPLIT, k, filt)
        for name, rule in EXITS:
            v = [r for _, r in run(sig, rule)]
            if len(v) >= 30:
                res.append((statistics.mean(v), len(v), ls_, filt, k, name, rule))
    res.sort(reverse=True)
    print("D2 variants (top 12 of", len(res), "):")
    for r in res[:12]:
        print(f"   {r[0]:+6.1%} n {r[1]:4d} leaders {r[2]:5s} filter {r[3]:17s} consensus {r[4]} exit {r[5]}")
    if not res:
        print("M11 VERDICT: FAIL (no variant with >= 30 D2 trades)")
        return
    _, _, ls_, filt, k, name, rule = res[0]
    hv = run(signals(allc, L_all[ls_], B.SPLIT, t_end, k, filt), rule)
    v = [r for _, r in hv]
    hm = statistics.median(t for t, _ in hv)
    h = [[r for t, r in hv if (t < hm) == q] for q in (True, False)]
    ok = len(v) >= 60 and statistics.mean(v) > 0 and all(hh and statistics.mean(hh) > 0 for hh in h)
    print(f"HOLDOUT ({ls_}, {filt}, consensus {k}, {name}): n {len(v)} mean {statistics.mean(v):+.1%} med {statistics.median(v):+.1%} "
          f"win {sum(r > 0 for r in v) / len(v):.0%} <=-25% {sum(r <= -0.25 for r in v) / len(v):.0%} | halves "
          f"{statistics.mean(h[0]):+.1%} / {statistics.mean(h[1]):+.1%}")
    print("holdout, every variant (report only):")
    for ls2, f2, k2 in itertools.product(LEADERS, FILTERS, CONSENSUS):
        sig = signals(allc, L_all[ls2], B.SPLIT, t_end, k2, f2)
        print(f"   {ls2:5s} {f2:17s} k{k2}: " + " | ".join(
            f"{nm} {statistics.mean([r for _, r in run(sig, ru)]):+.1%}" if sig else f"{nm} n/a" for nm, ru in EXITS) + f"  (n {len(sig)})")
    json.dump(dict(leaders=sorted(L_all[ls_]), variant=dict(leaders=ls_, filter=filt, consensus=k, exit=name)),
              open(DATA_DIR / "m1" / "m11_choice.json", "w"))
    print(f"M11 VERDICT: {'PASS' if ok else 'FAIL'}")



def copy_scores(allc, a, b):
    """{exit name: {wallet: [copy returns]}} for +1 s copies of each wallet's first buy per coin in [a, b)."""
    out = {nm: defaultdict(list) for nm, _ in EXITS}
    for m, rows in allc:
        if not a <= rows[0][0] < b:
            continue
        ts = [r[0] for r in rows]
        cnt = defaultdict(int)
        seen = set()
        for i, r in enumerate(rows):
            w = r[4]
            if not r[2] or w in seen or w == rows[0][4] or i < 3 or r[0] - rows[0][0] <= 2:
                continue
            seen.add(w)
            ke = max(0, bisect.bisect_right(ts, r[0] + 1) - 1)
            if ke >= len(rows) - 1:
                continue
            ls = next((q[0] for q in rows[i + 1:] if q[4] == w and not q[2]), r[0] + 600)
            for nm, rule in EXITS:
                out[nm][w].append(exit_ret(rows, ts, ke, ls, rule))
            cnt[w] += 1
    return out


def good(scores, nm):
    L = set()
    for w, v in scores[nm].items():
        if len(v) >= 10:
            mu, sd = statistics.mean(v), statistics.pstdev(v)
            if mu > 0 and sd > 0 and mu / (sd / len(v) ** 0.5) >= 2:
                L.add(w)
    return L


def v2() -> None:
    t_end = sqlite3.connect(DATA_DIR / "feed" / "trades.sqlite").execute("SELECT max(t) FROM tr").fetchone()[0]
    allc = [(m, r) for m, r in coins() if r[0][0] + 900 <= t_end]
    t0 = min(r[0][0] for _, r in allc)
    dmid = (t0 + B.SPLIT) / 2
    S1 = copy_scores(allc, t0, dmid)
    res = []
    for nm, rule in EXITS:
        L = good(S1, nm)
        print(f"{nm}: copy-profitable leaders on D1 (n >= 10, t >= 2): {len(L)}", flush=True)
        for filt, k in itertools.product(FILTERS, CONSENSUS):
            v = [r for _, r in run(signals(allc, L, dmid, B.SPLIT, k, filt), rule)]
            if len(v) >= 30:
                res.append((statistics.mean(v), len(v), filt, k, nm, rule))
    res.sort(reverse=True)
    print("D2 variants (top 10 of", len(res), "):")
    for r in res[:10]:
        print(f"   {r[0]:+6.1%} n {r[1]:4d} filter {r[2]:17s} consensus {r[3]} exit {r[4]}")
    if not res:
        print("M11 v2 VERDICT: FAIL (no variant with >= 30 D2 trades)")
        return
    _, _, filt, k, nm, rule = res[0]
    S = copy_scores(allc, t0, B.SPLIT)
    L = good(S, nm)
    hv = run(signals(allc, L, B.SPLIT, t_end, k, filt), rule)
    v = [r for _, r in hv]
    if not v:
        print("M11 v2 VERDICT: FAIL (no holdout trades)")
        return
    hm = statistics.median(t for t, _ in hv)
    h = [[r for t, r in hv if (t < hm) == q] for q in (True, False)]
    ok = len(v) >= 60 and statistics.mean(v) > 0 and all(hh and statistics.mean(hh) > 0 for hh in h)
    print(f"HOLDOUT ({len(L)} leaders, {filt}, consensus {k}, {nm}): n {len(v)} mean {statistics.mean(v):+.1%} med {statistics.median(v):+.1%} "
          f"win {sum(r > 0 for r in v) / len(v):.0%} <=-25% {sum(r <= -0.25 for r in v) / len(v):.0%} | halves "
          f"{statistics.mean(h[0]) if h[0] else float('nan'):+.1%} / {statistics.mean(h[1]) if h[1] else float('nan'):+.1%}")
    print("holdout, every exit x filter x consensus with leaders re-scored on the whole design (report only):")
    for nm2, ru in EXITS:
        L2 = good(S, nm2)
        print(f"   {nm2:18s} leaders {len(L2):4d}: " + " | ".join(
            f"{f2} k{k2} " + (f"{statistics.mean(q):+.1%} (n {len(q)})" if (q := [r for _, r in run(signals(allc, L2, B.SPLIT, t_end, k2, f2), ru)]) else "n/a")
            for f2, k2 in itertools.product(FILTERS, CONSENSUS)))
    json.dump(dict(leaders=sorted(L), variant=dict(filter=filt, consensus=k, exit=nm)), open(DATA_DIR / "m1" / "m11_choice_v2.json", "w"))
    print(f"M11 v2 VERDICT: {'PASS' if ok else 'FAIL'}")


if __name__ == "__main__":
    import sys
    {"v2": v2}.get(sys.argv[1] if len(sys.argv) > 1 else "", main)()
