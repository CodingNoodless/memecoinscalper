"""Strategy Y: aged pump.fun graduates (1-10 days old) and Setuh-style meta-following launches (pre-registered;
vault: "2026-10-20 aged memecoin survivors (Y)").

    uv run python -m tradeslave.research.survivors fetch     # 15-min candles (SOL) for every logged graduation
    uv run python -m tradeslave.research.survivors design    # Y1-Y4 grids on design graduations, freeze
    uv run python -m tradeslave.research.survivors test      # judge each frozen family once

Candles: pump.fun's candle API, interval 15m, newest 1000 (10.4 days), so every graduation from Oct 13-17 is covered
from graduation to the fetch. Signals use closed candles; entry at the next candle's open.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from itertools import product

import requests

from ..paths import DATA_DIR
from ..scalper.recorder import TICK_DIR, read_ticks
from .long_paths import fetch as pf_fetch

OUT = DATA_DIR / "survivors"
SPLIT = 1792065600                      # 2026-10-15 12:00 UTC: graduations before are design, after are test
Q = 900                                 # candle seconds
COST, COST2 = 0.0125, 0.025             # per side; robustness
FROZEN = OUT / "rule.json"
H = 3600


def grads():
    g = {}
    for r in read_ticks(sorted(TICK_DIR.glob("*.jsonl.gz"))):
        if r.get("k") == "migr" and r.get("m"):
            g.setdefault(r["m"], float(r["t"]))
    return g


def fetch():
    OUT.mkdir(parents=True, exist_ok=True)
    g = grads()
    todo = [m for m in g if not (OUT / f"{m}.json").exists()]
    print(f"{len(g)} graduations; fetching {len(todo)}", flush=True)
    s = requests.Session()
    s.headers.update({"Origin": "https://pump.fun", "Accept": "application/json"})

    def one(m):
        try:
            c = pf_fetch(s, m, g[m] - 6 * H, "15m")
        except (TypeError, ValueError):                 # a candle with a null price: skip the coin
            return False
        if c is not None:
            (OUT / f"{m}.json").write_text(json.dumps({"m": m, "tg": g[m], "c": c}))
        return c is not None

    with ThreadPoolExecutor(6) as ex:
        for i, ok in enumerate(ex.map(one, todo), 1):
            if i % 500 == 0:
                print(f"  {i}/{len(todo)}", flush=True)
    print("fetch done", flush=True)


# ---------------------------------------------------------------- simulation

def load():
    out = []
    for f in sorted(OUT.glob("*.json")):
        if f.name == "rule.json":
            continue
        d = json.loads(f.read_text())
        c = [x for x in d["c"] if x[0] >= d["tg"] - Q]
        if c:
            out.append(dict(m=d["m"], tg=d["tg"], c=c))
    return out


def trade(c, i, tp=None, sl=None, trail=None, max_h=72, cost=COST):
    """Enter at candle i's open; exit on TP/SL (stop first if both in one candle), trailing stop on closes, or time."""
    if i >= len(c):
        return None
    e = c[i][1]
    if e <= 0:
        return None
    end_t = c[i][0] + max_h * H
    peak = e
    for k in range(i, len(c)):
        t, o, hi, lo, cl = c[k][:5]
        if t > end_t:
            x = c[k - 1][4]
            break
        if sl is not None and lo <= e * (1 + sl):
            x = e * (1 + sl)
            break
        if tp is not None and hi >= e * (1 + tp):
            x = e * (1 + tp)
            break
        peak = max(peak, cl)
        if trail is not None and cl <= peak * (1 - trail):
            x = cl
            break
    else:
        x = c[-1][4]                    # no more trades: last price (a dead coin keeps its last print)
    return (x / e) * (1 - cost) / (1 + cost) - 1


def vol_since(c, k, secs):
    t, v = c[k][0], 0.0
    for j in range(k, -1, -1):
        if c[j][0] <= t - secs:
            break
        v += c[j][5]
    return v


def first_after(c, t):
    for k, x in enumerate(c):
        if x[0] >= t:
            return k
    return None


def y1(d, D, V, M, exit_, cost=COST):
    c = d["c"]
    k = first_after(c, d["tg"] + D * H)          # the first candle at/after graduation + D (closed before entry)
    if k is None or k == 0:
        return None
    k -= 1
    if c[k][0] < d["tg"] + D * H - 6 * H:        # no trades in the 6 h before the check
        return None
    if vol_since(c, k, 6 * H) < V or c[k][4] * 1e9 < M:
        return None
    return trade(c, k + 1, cost=cost, **exit_)


def highs(d):
    """Post-graduation high BEFORE each candle (running max of earlier highs), cached per token."""
    if "_hb" not in d:
        hb, run = [], None
        for x in d["c"]:
            hb.append(run)
            if x[0] >= d["tg"]:
                run = x[2] if run is None else max(run, x[2])
        d["_hb"] = hb
    return d["_hb"]


def y2(d, dd, V, exit_, cost=COST):
    c, tg = d["c"], d["tg"]
    hb = highs(d)
    for k, x in enumerate(c):
        if x[0] < tg + 24 * H:
            continue
        hi = max(hb[k], x[2]) if hb[k] is not None else x[2]
        if x[4] <= (1 - dd) * hi and vol_since(c, k, 6 * H) >= V:
            return trade(c, k + 1, cost=cost, **exit_)
    return None


def y3(d, V, trail, cost=COST):
    c, tg = d["c"], d["tg"]
    hb = highs(d)
    g0 = next((x[1] for x in c if x[0] >= tg - Q), None)
    k24 = first_after(c, tg + 24 * H)
    if not g0 or k24 is None or hb[k24] is None or hb[k24] < 2 * g0:
        return None
    for k in range(k24, len(c)):
        x = c[k]
        if hb[k] is not None and x[4] > hb[k] and x[5] >= V:
            return trade(c, k + 1, trail=trail, max_h=48, cost=cost)
    return None


def y4(d, kx, exit_, cost=COST):
    c, tg = d["c"], d["tg"]
    lo = 0
    for k, x in enumerate(c):
        while c[lo][0] <= x[0] - 24 * H:
            lo += 1
        if x[0] < tg + 24 * H:
            continue
        prev = [y[5] for y in c[lo:k] if y[5] > 0]
        if len(prev) >= 4 and x[5] >= 5 and x[5] >= kx * statistics.median(prev):
            return trade(c, k + 1, cost=cost, **exit_)
    return None


def grid():
    for D, V, M, ex in product((6, 24, 48), (10, 50, 200), (300, 1000, 3000),
                               (dict(max_h=6), dict(max_h=24), dict(max_h=72), dict(tp=0.5, sl=-0.3), dict(tp=1.0, sl=-0.5))):
        yield "Y1", dict(D=D, V=V, M=M, exit_=ex)
    for dd, V, tp, sl in product((0.5, 0.7), (10, 50), (0.3, 0.6, 1.0), (-0.3, -0.5)):
        yield "Y2", dict(dd=dd, V=V, exit_=dict(tp=tp, sl=sl, max_h=48))
    for V, tr in product((5, 20), (0.2, 0.35)):
        yield "Y3", dict(V=V, trail=tr)
    for kx, tp in product((10, 30), (0.5, 1.0)):
        yield "Y4", dict(kx=kx, exit_=dict(tp=tp, sl=-0.4, max_h=24))


FN = {"Y1": y1, "Y2": y2, "Y3": y3, "Y4": y4}


def run(data, fam, p, cost=COST):
    out = []
    for d in data:
        r = FN[fam](d, cost=cost, **p)
        if r is not None:
            out.append((d["tg"], r))
    return out


def stats(xs):
    v = [r for _, r in xs]
    if len(v) < 3:
        return len(v), 0.0, 0.0, 0.0, 0.0
    m, s = statistics.mean(v), statistics.stdev(v)
    k = max(1, int(len(v) * 0.02))
    trim = statistics.mean(sorted(v)[:-k])
    return len(v), m, (m / s * math.sqrt(len(v))) if s else 0.0, statistics.median(v), trim


def fmt(xs):
    n, m, t, med, trim = stats(xs)
    return f"n {n:4d} mean {m:+.2%} t {t:+.2f} median {med:+.2%} w/o top 2% {trim:+.2%}"


def design():
    data = [d for d in load() if d["tg"] < SPLIT]
    print(f"design graduations {len(data)}")
    best = {}
    for fam, p in grid():
        xs = run(data, fam, p)
        n, m = stats(xs)[:2]
        print(f"{fam} {p}: {fmt(xs)}", flush=True)
        if n >= 30 and (fam not in best or m > best[fam][0]):
            best[fam] = (m, p)
    FROZEN.write_text(json.dumps({f: p for f, (m, p) in best.items()}))
    for f, (m, p) in best.items():
        print(f"FROZEN {f}: {p} (design mean {m:+.2%})")


def test():
    data = [d for d in load() if d["tg"] >= SPLIT]
    print(f"test graduations {len(data)}")
    fr = json.loads(FROZEN.read_text())
    for fam, p in fr.items():
        xs = sorted(run(data, fam, p))
        mid = xs[len(xs) // 2][0] if xs else 0
        print(f"TEST {fam} {p}: {fmt(xs)}\n   halves: {fmt([x for x in xs if x[0] < mid])} / "
              f"{fmt([x for x in xs if x[0] >= mid])}\n   cost 2.5%/side: {fmt(run(data, fam, p, COST2))}")


def main() -> None:
    {"fetch": fetch, "design": design, "test": test}[sys.argv[1]]()


if __name__ == "__main__":
    main()
