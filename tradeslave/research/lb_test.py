"""LB: late busy coins (still >= 60 trades/min 15+ min after creation), filled 1 s late.
Vault "2026-10-23 late busy coins (LB)", pre-registered 20:49 PDT.

    uv run python -u -m tradeslave.research.lb_test
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
RULES = [("MAIN age>=15m & >=60 trades/min", 900, 60), ("base age>=15m & >=10 trades/min", 900, 10),
         ("var age>=10m & >=60", 600, 60), ("var age>=20m & >=60", 1200, 60),
         ("var age>=15m & >=30", 900, 30), ("var age>=15m & >=120", 900, 120)]


def entry(rows, ts, min_age, min_act):
    t0 = ts[0]
    t = t0 + min_age
    while t <= ts[-1]:
        i = bisect.bisect_right(ts, t) - 1
        if i - bisect.bisect_left(ts, t - 60) + 1 >= min_act:
            k = bisect.bisect_right(ts, t + 1) - 1                # fill at the state 1 s later
            return k if k < len(rows) - 2 else None
        t += 10
    return None


def main() -> None:
    t_end = sqlite3.connect(DATA_DIR / "feed" / "trades.sqlite").execute("SELECT max(t) FROM tr").fetchone()[0]
    res = {r[0]: [] for r in RULES}
    for m, rows in coins():
        ts = [r[0] for r in rows]
        for name, age, act in RULES:
            k = entry(rows, ts, age, act)
            if k is None or ts[k] + 1800 > t_end:
                continue
            p = rows[k][1]
            mx = max((r[1] for r in rows[k + 1:] if r[0] <= ts[k] + 1800), default=p)
            co = dict(m=m, rows=rows)
            res[name].append(dict(part="design" if ts[k] < SPLIT else "holdout", L5=ret(co, k, L5), E2=ret(co, k, E2_1S),
                                  runner=mx >= 2 * p))
    base = {p: statistics.mean(x["L5"] for x in res[RULES[1][0]] if x["part"] == p) for p in ("design", "holdout")}
    for name, _, _ in RULES:
        for p in ("design", "holdout"):
            g = [x for x in res[name] if x["part"] == p]
            if g:
                print(f"{name:34s} {p:7s} n {len(g):4d} L5 {statistics.mean(x['L5'] for x in g):+6.1%} med "
                      f"{statistics.median(x['L5'] for x in g):+6.1%} E2 {statistics.mean(x['E2'] for x in g):+6.1%} "
                      f"<=-25% {sum(x['L5'] <= -0.25 for x in g) / len(g):4.0%} runner {sum(x['runner'] for x in g) / len(g):5.1%}")
    g = {p: [x["L5"] for x in res[RULES[0][0]] if x["part"] == p] for p in ("design", "holdout")}
    ok = (len(g["holdout"]) >= 40 and statistics.mean(g["holdout"]) > 0 and statistics.mean(g["holdout"]) >= base["holdout"] + 0.03
          and statistics.mean(g["design"]) > base["design"])
    print(f"\nbase L5: design {base['design']:+.1%}, holdout {base['holdout']:+.1%}\nLB VERDICT: {'PASS' if ok else 'FAIL'}")


if __name__ == "__main__":
    main()
