"""XA: exit anatomy for M5 entries. How deep losses happen (can -25% be held?) and what winners leave behind.

    uv run python -u -m tradeslave.research.xa_test train      -> anatomy + candidate exits on train only
    uv run python -u -m tradeslave.research.xa_test judge      -> pre-registered candidates on validation + live
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from collections import defaultdict

from ..paths import DATA_DIR
from . import er_test
from .m5_bt import EXITS, ret

E2 = EXITS["E2 M4 exits, 15 s holder grace"]
FEE = 0.0125
HZ = 900


def live_coins():
    """M5 live paper trades as dataset-style coins: (coin, k)."""
    db = sqlite3.connect(DATA_DIR / "feed" / "trades.sqlite")
    out = []
    for line in (DATA_DIR / "m5" / "trades.jsonl").open():
        if not line.strip():
            continue
        x = json.loads(line)
        tr = db.execute("SELECT t, w, b, sol, tok, vs, vt FROM tr WHERE m=? ORDER BY slot, seq", (x["m"],)).fetchall()
        bal, h, rows = defaultdict(float), 0, []
        for t, w, b, sol, tok, vs, vt in tr:
            b0 = bal[w]
            b1 = b0 + (tok if b else -tok)
            bal[w] = b1
            h += (b0 <= 1 < b1) - (b1 <= 1 < b0)
            if vt:
                rows.append([t, vs / vt, bool(b), sol, w, tok, h])
        k = max((i for i, r in enumerate(rows) if r[0] <= x["t"]), default=None)
        if k is not None and k >= 3 and len(rows) > k + 1:
            out.append((dict(m=x["m"], rows=rows), k))
    return out


def samples(parts):
    er_test.resplit()
    _, byid, ent = er_test.load()
    S = []
    for s in ent:
        p = "train" if s["part"] in er_test.TRAIN else "valid"
        if p in parts:
            S.append((p, byid[s["m"]], s["k"]))
    if "live" in parts:
        S += [("live", co, k) for co, k in live_coins()]
    return S


def net(ratio):
    return ratio * (1 - FEE) ** 2 - 1


def anatomy(S):
    """Loss side: for paths that cross -25% (price ratio 0.77 ~ -25% net), the price after the cross at each latency."""
    print("\nA. Loss anatomy (price paths after entry, 15 min)")
    deep, fills = [], defaultdict(list)
    one_s = []
    for part, co, k in S:
        rows, p0, t0 = co["rows"], co["rows"][k][1], co["rows"][k][0]
        post = [r for r in rows[k + 1:] if r[0] <= t0 + HZ]
        cross = next((i for i, r in enumerate(post) if r[1] / p0 <= 0.80), None)
        if cross is None:
            continue
        deep.append(part)
        tc = post[cross][0]
        for lat in (0, 1, 2, 4):
            last = [r for r in post if r[0] <= tc + lat]
            fills[lat].append(last[-1][1] / p0)
        before = [r for r in post[:cross] if r[0] < tc]
        prev = before[-1][1] / p0 if before else 1.0
        one_s.append((prev, post[cross][1] / p0))
    n_all = len(S)
    print(f"   {len(deep)} of {n_all} entries ({len(deep) / n_all:.0%}) touch -20% price within 15 min")
    for lat, v in fills.items():
        ok = sum(net(x) >= -0.25 for x in v)
        print(f"   sell lands {lat} s after the first trade at <= -20%: median fill {statistics.median(v):.2f}x, "
              f"loss held to >= -25% net in {ok}/{len(v)} ({ok / len(v):.0%}); fills <= 0.5x: {sum(x <= 0.5 for x in v)}")
    gap = [b for a, b in one_s if a > 0.85 and b < 0.6]
    print(f"   crossing trades that jump from > 0.85x to < 0.60x in one trade (single-block dumps): {len(gap)}/{len(one_s)}")


def upside(S):
    print("\nB. Upside anatomy (E2 exits)")
    rows_out = []
    for part, co, k in S:
        r, why, t_exit, peak = ret_full(co, k, E2)
        rows = co["rows"]
        p0 = rows[k][1]
        after = [x[1] / p0 for x in rows[k + 1:] if t_exit < x[0] <= t_exit + HZ]
        rows_out.append((r, why, peak, max(after, default=0.0), (after[-1] if after else None)))
    by = defaultdict(list)
    for r, why, *_ in rows_out:
        by[why].append(r)
    print("   exit reasons: " + ", ".join(f"{w} n {len(v)} mean {statistics.mean(v):+.0%}" for w, v in sorted(by.items(), key=lambda x: -len(x[1]))))
    pk = [x[2] for x in rows_out]
    for m in (1.3, 1.5, 2, 3, 5):
        print(f"   peak while held >= {m}x: {sum(p >= m for p in pk)}/{len(pk)}")
    tp = [x for x in rows_out if x[1] == "TP"]
    if tp:
        print(f"   after a 2x TP sell, max in the next 15 min: median {statistics.median(x[3] for x in tp):.1f}x, "
              f">= 3x {sum(x[3] >= 3 for x in tp)}/{len(tp)}, >= 5x {sum(x[3] >= 5 for x in tp)}/{len(tp)}; "
              f"price 15 min later below 2x in {sum((x[4] or 0) < 2 for x in tp)}/{len(tp)}")
    gb = [x for x in rows_out if x[2] >= 1.5]
    if gb:
        print(f"   trades that peaked >= 1.5x: {len(gb)}, mean exit {statistics.mean(x[0] for x in gb):+.0%}, "
              f"mean peak {statistics.mean(x[2] for x in gb):.2f}x (capture {statistics.mean((1 + x[0]) / x[2] for x in gb):.0%} of peak)")


def ret_full(co, k, ex):
    from .m5_bt import K, SIZE
    from .tf_replay import BASE, sim
    import math
    rows = co["rows"]
    p0 = rows[k][1]
    v = math.sqrt(p0 * K)
    q = K / v - K / (v + SIZE * (1 - 0.0125))
    x = dict(v="m1", t=rows[k][0], p0=p0, q=q, stop=ex["stop"], high=False)
    return sim(x, rows[:k + 1], rows[k + 1:], dict(BASE, **ex["P"]))


def V(stop=None, **kw):
    return dict(stop=E2["stop"] if stop is None else stop, P=dict(E2["P"], **kw))


CANDS = {
    "E2 (now: stop -30%, 2 s poll, sell all at 2x)": V(),
    "L1 stop -20%": V(0.80),
    "L2 stop -15%": V(0.85),
    "L3 stop -20%, 1 s poll": V(0.80, poll=1.0),
    "L4 stop -15%, 1 s poll": V(0.85, poll=1.0),
    "U1 no 2x TP, trail 20% from peak": V(tp=99.0),
    "U2 half at 2x, rest trails": V(tp=99.0, half_tp=2.0),
    "U3 TP 3x": V(tp=3.0),
    "U4 half at 2x, rest trails 30%": V(tp=99.0, half_tp=2.0, trend_give=0.7),
    "U5 sell all at 1.5x": V(tp=1.5),
    "L5 stop -10%, 1 s poll": V(0.90, poll=1.0),
    "L6 stop -25%, 1 s poll": V(0.75, poll=1.0),
    "L7 stop -30%, 1 s poll (speed only)": V(poll=1.0),
    "C1 stop -15%, 1 s poll, TP 3x": V(0.85, poll=1.0, tp=3.0),
    "C2 stop -15%, 1 s poll, half at 2x": V(0.85, poll=1.0, tp=99.0, half_tp=2.0),
}


def summ(v):
    return (f"n {len(v):3d} mean {statistics.mean(v):+6.1%} med {statistics.median(v):+6.1%} "
            f"<=-25% {sum(x <= -0.25 for x in v) / len(v):4.0%} worst {min(v):+5.0%} best {max(v):+6.0%}")


def run(S, parts, names):
    print("\nC. Exit candidates")
    for name in names:
        ex = CANDS[name]
        for p in parts:
            v = [ret(co, k, ex) for q, co, k in S if q == p]
            if v:
                print(f"   {p:5s} {name:45s} {summ(v)}")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "train"
    parts = ("train",) if mode == "train" else ("valid", "live")
    S = samples(parts)
    print(f"{mode}: {len(S)} entries ({', '.join(f'{p} {sum(q == p for q, *_ in S)}' for p in parts)})")
    anatomy(S)
    upside(S)
    run(S, parts, list(CANDS) if mode == "train" else json.loads((DATA_DIR / "m1" / "xa_picks.json").read_text()))


if __name__ == "__main__":
    main()
