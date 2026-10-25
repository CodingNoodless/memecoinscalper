"""HT: does market heat (share of recent coins already doubling) separate good from bad M-bot trades? For M12.
Heat at time t = among feed coins whose first trade was 5-60 min before t, the share whose highest price seen BEFORE t is
>= 2x their first price (causal: nothing after t). Design: M5/M6 trades before M8 started (Oct 23 16:17). Check: M8 trades
after. Pre-registered in vault "2026-10-24 M12 - M8 with hype context (M12)".

    uv run python -u -m tradeslave.research.heat_test
"""

from __future__ import annotations

import bisect
import json
import sqlite3
import statistics
import time

from ..paths import DATA_DIR

M8_FROM = time.mktime(time.strptime("2026-10-23 16:17", "%Y-%m-%d %H:%M"))


def coin_paths():
    """mint -> (t0, p0, [(t, running max price)]) from the feed."""
    db = sqlite3.connect(DATA_DIR / "feed" / "trades.sqlite")
    out, cur, rows = {}, None, []
    for m, t, vs, vt in db.execute("SELECT m, t, vs, vt FROM tr WHERE vs > 0 AND vt > 0 ORDER BY m, slot, seq"):
        if m != cur:
            if rows:
                out[cur] = rows
            cur, rows = m, []
        rows.append((t, vs / vt))
    if rows:
        out[cur] = rows
    res = {}
    for m, rows in out.items():
        t0, p0 = rows[0]
        mx, run = 0.0, []
        for t, p in rows:
            if p > mx:
                mx = p
                run.append((t, mx / p0))
        res[m] = (t0, run)
    return res


def heat_at(paths, starts, t):
    lo, hi = bisect.bisect_left(starts, (t - 3600,)), bisect.bisect_left(starts, (t - 300,))
    n = hit = 0
    for t0, m in starts[lo:hi]:
        run = paths[m][1]
        k = bisect.bisect_right([a for a, _ in run], t) - 1
        n += 1
        hit += k >= 0 and run[k][1] >= 2.0
    return (hit / n if n else None), n


def trades(v, rule=None):
    try:
        r = [json.loads(x) for x in open(DATA_DIR / v / "trades.jsonl")]
    except OSError:
        return []
    return [x for x in r if rule is None or x.get("rules") == rule]


def report(label, xs):
    xs = [x for x in xs if x["heat"] is not None]
    if len(xs) < 12:
        print(f"{label}: too few ({len(xs)})")
        return None
    hs = sorted(x["heat"] for x in xs)
    cut = [hs[len(hs) // 3], hs[2 * len(hs) // 3]]
    print(f"{label}: n {len(xs)}, heat terciles at {cut[0]:.1%} / {cut[1]:.1%}")
    for name, f in (("low", lambda h: h < cut[0]), ("mid", lambda h: cut[0] <= h < cut[1]), ("high", lambda h: h >= cut[1])):
        q = [x["ret"] for x in xs if f(x["heat"])]
        if q:
            print(f"   {name:4s} heat: n {len(q):3d} mean {statistics.mean(q):+6.1%} med {statistics.median(q):+6.1%} win {sum(a > 0 for a in q) / len(q):.0%}")
    return cut


def main() -> None:
    paths = coin_paths()
    starts = sorted((t0, m) for m, (t0, _) in paths.items())
    print(f"{len(paths)} feed coins")
    D = [x for v in ("m5", "m6") for x in trades(v) if x["t"] < M8_FROM]
    H = [x for x in trades("m8", "m8-v2+1s")]
    for x in D + H:
        x["heat"], x["heat_n"] = heat_at(paths, starts, x["t"])
    cut = report("DESIGN: M5+M6 trades before Oct 23 16:17", D)
    report("CHECK: M8 trades (m8-v2+1s)", H)
    if cut:
        thr = cut[0]
        keep = [x["ret"] for x in H if x["heat"] is not None and x["heat"] >= thr]
        drop = [x["ret"] for x in H if x["heat"] is not None and x["heat"] < thr]
        print(f"\nPRE-REGISTERED RULE (skip when heat < design lower tercile {thr:.1%}) on M8: kept n {len(keep)} mean "
              f"{statistics.mean(keep) if keep else float('nan'):+.1%} | skipped n {len(drop)} mean {statistics.mean(drop) if drop else float('nan'):+.1%}")
        json.dump(dict(threshold=thr, design_cut=cut), open(DATA_DIR / "m1" / "heat_threshold.json", "w"))
    by_h = {}
    for x in H:
        if x["heat"] is not None:
            by_h.setdefault(time.localtime(x["t"]).tm_hour // 6, []).append(x["heat"])
    print("M8 trade-time heat by 6 h block (PT):", {f"{k * 6:02d}-{k * 6 + 6:02d}": f"{statistics.median(v):.1%}" for k, v in sorted(by_h.items())})


if __name__ == "__main__":
    main()
