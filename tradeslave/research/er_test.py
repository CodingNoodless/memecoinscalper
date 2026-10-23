"""ER: M5 entry redesign (vault "2026-10-23 M5 entry redesign (ER)", pre-registered 09:30 PDT).

    uv run python -u -m tradeslave.research.er_test          # train only: every setting, the train bar, the pick
    uv run python -u -m tradeslave.research.er_test held     # the single pick, judged once on validation

Base: M5 v1 entries (cv_test.m5_entries), E2 exits. Candidates A (deep pullback limit), D (narrative gate),
E (looper veto), B (exits), C (regime), and three pre-listed combos. A setting is eligible only with >= 100 train
trades and a train mean >= +20%.
"""

from __future__ import annotations

import bisect
import gzip
import json
import pickle
import statistics
import sys
import time

import numpy as np

from ..paths import DATA_DIR
from .cv_test import m5_entries
from .m4_bt import OUT as COINS
from .m5_bt import EXITS, HELD, TRAIN, ret

CACHE = DATA_DIR / "m1" / "er_entries.pkl"
E2 = EXITS["E2 M4 exits, 15 s holder grace"]
EXB = {"B1 half at 1.5x": dict(stop=0.70, P=dict(E2["P"], half_tp=1.5, tp=99.0)),
       "B2 half at 2x": dict(stop=0.70, P=dict(E2["P"], half_tp=2.0, tp=99.0)),
       "B3 stall 1.15x / 30 s": dict(stop=0.70, P=dict(E2["P"], pstall=(1.15, 30)))}
MIN_N, BAR = 100, 0.20


def load():
    with gzip.open(COINS, "rt", encoding="utf-8") as fh:
        coins = json.load(fh)
    byid = {co["m"]: co for co in coins}
    if CACHE.exists() and CACHE.stat().st_mtime > COINS.stat().st_mtime:
        ent = pickle.loads(CACHE.read_bytes())
    else:
        t0 = time.time()
        ent = m5_entries(coins)
        CACHE.write_bytes(pickle.dumps(ent))
        print(f"entries built in {time.time() - t0:.0f} s")
    return coins, byid, ent


def looper(rows, k):
    """Share of the last 60 s of buy SOL from wallets that both bought and sold in that window."""
    t = rows[k][0]
    w = [r for r in rows[:k + 1] if r[0] > t - 60]
    buyers = {r[4] for r in w if r[2]}
    sellers = {r[4] for r in w if not r[2]}
    tot = sum(r[3] or 0 for r in w if r[2])
    loop = sum(r[3] or 0 for r in w if r[2] and r[4] in sellers and r[4] in buyers)
    return loop / tot if tot else 0.0


def pullback(rows, k, X, W):
    """Index of the fill for a limit at p_k * (1 - X) resting W s; seen at the next 2 s poll, filled at the last
    price then if it is still <= limit * 1.10. None = no fill."""
    lim, t_end = rows[k][1] * (1 - X), rows[k][0] + W
    ts = [r[0] for r in rows]
    j = k + 1
    while j < len(rows) and rows[j][0] <= t_end:
        if rows[j][1] <= lim:
            f = bisect.bisect_right(ts, rows[j][0] + 2.0) - 1
            if rows[f][1] <= lim * 1.10:
                return f
            j = f + 1
            continue
        j += 1
    return None


def regime_index(coins):
    """Trailing-60-min buy SOL over all B coins' trades (proxy for market activity)."""
    ev = sorted((r[0], r[3] or 0) for co in coins if co["set"] == "B" for r in co["rows"] if r[2])
    ts = np.array([e[0] for e in ev])
    cs = np.concatenate([[0.0], np.cumsum([e[1] for e in ev])])
    return lambda t: float(cs[np.searchsorted(ts, t, "right")] - cs[np.searchsorted(ts, t - 3600, "right")])


def summ(v):
    if not v:
        return "n   0"
    m = statistics.mean(v)
    return f"n {len(v):4d} mean {m:+6.1%} median {statistics.median(v):+6.1%} win {sum(x > 0 for x in v) / len(v):4.0%}"


def evaluate(coins, byid, ent):
    """Every pre-registered setting on every entry. Returns {setting: [(entry, ret) ...]} (ret None = no trade)."""
    reg = regime_index(coins)
    for s in ent:
        co = byid[s["m"]]
        s["score"] = co["c"].get("score") or 0
        s["loop"] = looper(co["rows"], s["k"])
        s["reg"] = reg(co["rows"][s["k"]][0]) if co["set"] == "B" else None
    tr = [s for s in ent if s["part"] in TRAIN]
    out = {"BASE M5 v1 (E2)": {id(s): s["r"] for s in ent}}
    for X in (0.20, 0.35, 0.50):
        for W in (60, 180, 300):
            res = {}
            for s in ent:
                rows = byid[s["m"]]["rows"]
                f = pullback(rows, s["k"], X, W)
                res[id(s)] = None if f is None else ret(byid[s["m"]], f, E2)
            out[f"A dip {X:.0%} / {W} s"] = res
    for N in (3, 4, 5, 6, 7):
        out[f"D score >= {N}"] = {id(s): (s["r"] if s["score"] >= N else None) for s in ent}
    qs = {q: float(np.quantile([s["loop"] for s in tr], q)) for q in (0.5, 0.7, 0.8, 0.9)}
    for q, T in qs.items():
        out[f"E looper <= p{int(q * 100)} ({T:.2f})"] = {id(s): (s["r"] if s["loop"] <= T else None) for s in ent}
    for name, ex in EXB.items():
        out[name] = {id(s): ret(byid[s["m"]], s["k"], ex) for s in ent}
    med = statistics.median([s["reg"] for s in tr if s["reg"] is not None])
    out[f"C regime >= train median ({med:.0f} SOL/h)"] = {
        id(s): (s["r"] if s["reg"] is not None and s["reg"] >= med else None) for s in ent}
    return out, tr


def mean_n(res, group):
    v = [res[id(s)] for s in group if res[id(s)] is not None]
    return (statistics.mean(v) if v else float("-inf")), len(v)


def combos(out, ent, tr):
    def best(prefix):
        c = [(mean_n(out[k], tr), k) for k in out if k.startswith(prefix)]
        c = [x for x in c if x[0][1] >= MIN_N] or c
        return max(c)[1]
    bd, ba, be = best("D "), best("A "), best("E ")
    d, a, e = out[bd], out[ba], out[be]
    out[f"COMBO {bd} x {ba}"] = {i: (a[i] if d[i] is not None else None) for i in d}
    out[f"COMBO {bd} x {be}"] = {i: (d[i] if e[i] is not None else None) for i in d}
    out[f"COMBO {bd} x {be} x {ba}"] = {i: (a[i] if d[i] is not None and e[i] is not None else None) for i in d}


def resplit():
    """ER2 (pre-registered 10:15): train = t0 before Oct 23 00:00 PDT, validation = B coins after it."""
    global TRAIN, HELD, CACHE
    from . import cv_test, m5_bt
    cut = time.mktime(time.strptime("2026-10-23 00:00", "%Y-%m-%d %H:%M"))
    m5_bt.part = lambda co: "T-" + co["set"] if co["t0"] < cut else "V-" + co["set"]       # noqa: E731
    TRAIN, HELD = ("T-A", "T-B"), ("V-A", "V-B")
    cv_test.TRAIN = TRAIN
    CACHE = DATA_DIR / "m1" / "er2_entries.pkl"


def main() -> None:
    if "er2" in sys.argv:
        resplit()
    coins, byid, ent = load()
    out, tr = evaluate(coins, byid, ent)
    combos(out, ent, tr)
    held = [s for s in ent if s["part"] in HELD]
    print(f"{len(ent)} M5 v1 entries: train {len(tr)}, validation {len(held)}\n")
    print("TRAIN (A-design + B-train):")
    elig = []
    for k, res in out.items():
        v = [res[id(s)] for s in tr if res[id(s)] is not None]
        m, n = mean_n(res, tr)
        ok = n >= MIN_N and m >= BAR
        print(f"   {k:52s} {summ(v)}{'   ELIGIBLE' if ok else ''}")
        if ok:
            elig.append((m, k))
    pick = max(elig)[1] if elig else None
    print(f"\nPick for validation: {pick or 'none (no setting reaches >= 100 trades and >= +20% on train)'}")
    (DATA_DIR / "m1" / ("er2_pick.json" if "er2" in sys.argv else "er_pick.json")).write_text(json.dumps({"pick": pick}))
    if "held" in sys.argv and pick:
        print("\nVALIDATION (A-check + B-test), judged once:")
        res = out[pick]
        for p in sorted({s["part"] for s in held}):
            print(f"   {p:9s} {summ([res[id(s)] for s in held if s['part'] == p and res[id(s)] is not None])}")
        print(f"   {'all':9s} {summ([res[id(s)] for s in held if res[id(s)] is not None])}")
        print(f"   base     {summ([s['r'] for s in held])}")


if __name__ == "__main__":
    main()
