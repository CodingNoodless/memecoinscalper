"""SL: second-leg runners on graduated coins (1-min PumpSwap candles): leg 1 >= 2x, pullback to <= 0.4x the peak, 60-min base,
then a 15-min relative-volume spike (>= 5x the base) with a 60-min range breakout. Entry next candle open; hours horizon.
Vault "2026-10-23 second-leg runners (SL)", pre-registered 23:21 PDT.

    uv run python -u -m tradeslave.research.sl_test
"""

from __future__ import annotations

import glob
import json
import statistics
import time

from ..paths import DATA_DIR

COST = 0.015
DESIGN_DAYS = ("10-13", "10-14")
# (name, pullback level, volume multiple, needs volume)
RULES = [("MAIN pb 0.4, vol 5x", 0.4, 5.0, True), ("base pb 0.4, breakout only", 0.4, 0.0, False),
         ("var pb 0.5, vol 5x", 0.5, 5.0, True), ("var pb 0.4, vol 3x", 0.4, 3.0, True), ("var pb 0.4, vol 10x", 0.4, 10.0, True)]


def minute_series(c):
    """{minute t: (o, h, l, c, vol)} and sorted minutes."""
    return {int(x[0]): (x[1], x[2], x[3], x[4], x[5] or 0.0) for x in c}


def trade(c, t_grad, pb, vmult, need_vol):
    if not c:
        return None
    s = minute_series(c)
    ts = sorted(s)
    p0 = s[ts[0]][0]
    if not p0:
        return None
    lead = [t for t in ts if t <= t_grad + 12 * 3600]
    if not lead:
        return None
    t_p1 = max(lead, key=lambda t: s[t][1])
    p1 = s[t_p1][1]
    if p1 < 2 * p0:
        return None
    t_pb = next((t for t in ts if t > t_p1 and s[t][3] <= pb * p1), None)
    if t_pb is None:
        return None
    vol15 = lambda t: sum(s.get(t - 60 * k, (0, 0, 0, 0, 0.0))[4] for k in range(15))  # noqa: E731
    base_end = t_pb + 3600
    base_vals = [vol15(t) for t in range(t_pb, base_end, 60)]
    baseline = statistics.median(base_vals) if base_vals else 0.0
    for t in ts:
        if t < base_end or t > t_grad + 48 * 3600:
            continue
        prev = [s[u][3] for u in ts if t - 3600 <= u < t]
        if not prev or s[t][3] <= max(prev):
            continue
        if need_vol and vol15(t) < vmult * max(baseline, 1.0):
            continue
        nxt = [u for u in ts if u > t]
        if not nxt:
            return None
        te = nxt[0]
        entry = s[te][0]
        if not entry:
            return None
        peak, exit_p, why, t_exit = entry, None, None, None
        for u in [u for u in ts if u >= te]:
            cl = s[u][3]
            peak = max(peak, cl)
            if cl <= 0.7 * entry:
                exit_p, why, t_exit = cl, "STOP", u
            elif peak >= 1.5 * entry and cl <= 0.7 * peak:
                exit_p, why, t_exit = cl, "TRAIL", u
            elif u - te >= 24 * 3600:
                exit_p, why, t_exit = cl, "TIME", u
            if exit_p is not None:
                break
        if exit_p is None:
            last = [u for u in ts if u >= te][-1]
            exit_p, why, t_exit = s[last][3], "END", last
        r = exit_p / entry * (1 - COST) ** 2 - 1
        return dict(ret=r, why=why, hold_h=(t_exit - te) / 3600, max_x=peak / entry, t=te)
    return None


def main() -> None:
    res = {r[0]: [] for r in RULES}
    n = 0
    for f in glob.glob(str(DATA_DIR / "candles" / "*.json")):
        d = json.loads(open(f).read())
        if not d.get("c"):
            continue
        n += 1
        part = "design" if time.strftime("%m-%d", time.localtime(d["t_grad"])) in DESIGN_DAYS else "holdout"
        for name, pb, vm, nv in RULES:
            x = trade(d["c"], d["t_grad"], pb, vm, nv)
            if x:
                x["part"] = part
                res[name].append(x)
    print(f"{n} coins with candles")
    base = {}
    for name, *_ in RULES:
        for p in ("design", "holdout"):
            g = [x for x in res[name] if x["part"] == p]
            if not g:
                print(f"{name:28s} {p:7s} n 0")
                continue
            m = statistics.mean(x["ret"] for x in g)
            if name.startswith("base"):
                base[p] = m
            print(f"{name:28s} {p:7s} n {len(g):4d} mean {m:+6.1%} med {statistics.median(x['ret'] for x in g):+6.1%} "
                  f"win {sum(x['ret'] > 0 for x in g) / len(g):4.0%} >=2x {sum(x['max_x'] >= 2 for x in g) / len(g):4.0%} "
                  f"hold med {statistics.median(x['hold_h'] for x in g):5.1f} h")
    g = {p: [x["ret"] for x in res[RULES[0][0]] if x["part"] == p] for p in ("design", "holdout")}
    ok = (len(g["holdout"]) >= 30 and g["design"] and statistics.mean(g["holdout"]) > 0
          and statistics.mean(g["holdout"]) >= base.get("holdout", 0) + 0.03 and statistics.mean(g["design"]) > base.get("design", 0))
    print(f"\nbase: design {base.get('design', float('nan')):+.1%}, holdout {base.get('holdout', float('nan')):+.1%}\nSL VERDICT: {'PASS' if ok else 'FAIL'}")


if __name__ == "__main__":
    main()
