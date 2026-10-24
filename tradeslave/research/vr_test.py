"""VR: VWAP reclaim after a dip (small-cap 'dip and rip' translated to memecoins), filled 1 s late.
Vault "2026-10-23 VWAP reclaim on memecoins (VR)", pre-registered 22:43 PDT.

    uv run python -u -m tradeslave.research.vr_test
"""

from __future__ import annotations

import bisect
import sqlite3
import statistics
import time

from ..paths import DATA_DIR
from .hs_test import E2_1S, L5, coins
from .m5_bt import ret

SPLIT = time.mktime(time.strptime("2026-10-23 06:00", "%Y-%m-%d %H:%M"))
# (name, dip below VWAP, needs positive 15 s flow)
RULES = [("MAIN dip >= 15%, flow > 0", 0.15, True), ("base any dip, no flow", 0.0, False),
         ("var dip >= 25%, flow > 0", 0.25, True), ("var dip >= 40%, flow > 0", 0.40, True), ("var dip >= 15%, no flow", 0.15, False)]


def entry(rows, dip, need_flow):
    """Index of the first reclaim trade (price back above running VWAP after a dip of `dip` within 120 s)."""
    from collections import deque
    pv = vol = 0.0
    t0 = rows[0][0]
    rel = deque()                                               # (t, price / vwap) over the last 120 s (monotone min queue)
    flow, fq = 0.0, deque()                                     # net SOL over the last 15 s
    below = False                                               # previous trade was below its VWAP
    for i, r in enumerate(rows):
        t, p, sol = r[0], r[1], r[3] or 0.0
        prev_vwap = pv / vol if vol else p
        pv += p * sol
        vol += sol
        vwap = pv / vol if vol else p
        x = p / prev_vwap if prev_vwap else 1.0
        while rel and rel[-1][1] >= x:
            rel.pop()
        rel.append((t, x))
        while rel and rel[0][0] < t - 120:
            rel.popleft()
        f = sol if r[2] else -sol
        fq.append((t, f))
        flow += f
        while fq and fq[0][0] <= t - 15:
            flow -= fq.popleft()[1]
        crossed = below and p > vwap
        below = p <= vwap
        if t - t0 < 120 or not crossed:
            continue
        if dip > 0 and rel[0][1] > 1 - dip:
            continue
        if need_flow and flow <= 0:
            continue
        return i
    return None


def main() -> None:
    t_end = sqlite3.connect(DATA_DIR / "feed" / "trades.sqlite").execute("SELECT max(t) FROM tr").fetchone()[0]
    res = {r[0]: [] for r in RULES}
    for m, rows in coins():
        ts = [r[0] for r in rows]
        co = dict(m=m, rows=rows)
        for name, dip, flow in RULES:
            i = entry(rows, dip, flow)
            if i is None or ts[i] + 1800 > t_end:
                continue
            k0 = i
            k1 = bisect.bisect_right(ts, ts[i] + 1) - 1
            k2 = bisect.bisect_right(ts, ts[i] + 2) - 1
            if k2 >= len(rows) - 2:
                continue
            p = rows[k1][1]
            mx = max((r[1] for r in rows[k1 + 1:] if r[0] <= ts[k1] + 1800), default=p)
            res[name].append(dict(part="design" if ts[i] < SPLIT else "holdout", L5=ret(co, k1, L5), L5_0=ret(co, k0, L5),
                                  L5_2=ret(co, k2, L5), E2=ret(co, k1, E2_1S), runner=mx >= 2 * p))
    base = {p: statistics.mean(x["L5"] for x in res[RULES[1][0]] if x["part"] == p) for p in ("design", "holdout")}
    for name, _, _ in RULES:
        for p in ("design", "holdout"):
            g = [x for x in res[name] if x["part"] == p]
            if g:
                print(f"{name:28s} {p:7s} n {len(g):4d} L5 +1s {statistics.mean(x['L5'] for x in g):+6.1%} (+0s "
                      f"{statistics.mean(x['L5_0'] for x in g):+6.1%}, +2s {statistics.mean(x['L5_2'] for x in g):+6.1%}) "
                      f"E2 {statistics.mean(x['E2'] for x in g):+6.1%} <=-25% {sum(x['L5'] <= -0.25 for x in g) / len(g):4.0%} "
                      f"runner {sum(x['runner'] for x in g) / len(g):5.1%}")
    g = {p: [x["L5"] for x in res[RULES[0][0]] if x["part"] == p] for p in ("design", "holdout")}
    ok = (len(g["holdout"]) >= 40 and statistics.mean(g["holdout"]) > 0 and statistics.mean(g["holdout"]) >= base["holdout"] + 0.03
          and statistics.mean(g["design"]) > base["design"])
    print(f"\nbase L5 +1s: design {base['design']:+.1%}, holdout {base['holdout']:+.1%}\nVR VERDICT: {'PASS' if ok else 'FAIL'}")


if __name__ == "__main__":
    main()
