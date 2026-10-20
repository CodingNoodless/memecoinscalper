"""Strategy Y7: buy the rebound after a big creator dumps into the bot wave (pre-registered; vault:
"2026-10-20 post-dump rebound (Y7)").

    uv run python -m tradeslave.research.dump_rebound fetch
    uv run python -m tradeslave.research.dump_rebound design
    uv run python -m tradeslave.research.dump_rebound test
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from itertools import product

import requests

from ..paths import DATA_DIR
from . import launch_watch as LW
from .long_paths import fetch as pf_fetch

OUT = DATA_DIR / "dump_rebound"
FROM = 1792267800.0                     # launch_watch forward start (2026-10-17)
COST, COST2 = 0.0225, 0.035


def events():
    cr = {}
    for line in LW.OUT.open(encoding="utf-8"):
        r = json.loads(line)
        if r.get("txType") == "create" and r["t"] >= FROM:
            cr[r["mint"]] = r
    tr = defaultdict(list)
    for line in LW.TRADES.open(encoding="utf-8"):
        r = json.loads(line)
        if r.get("k") == "trade" and r["m"] in cr:
            tr[r["m"]].append(r)
    out = []
    for m, ts in tr.items():
        c = cr[m]
        if c["solAmount"] < 4:
            continue
        sells = [t["t"] for t in ts if not t["b"] and t["w"] == c["traderPublicKey"]]
        if sells:
            out.append(dict(m=m, t0=c["t"], x=c["solAmount"], dump=min(sells)))
    return sorted(out, key=lambda e: e["t0"])


def fetch():
    OUT.mkdir(parents=True, exist_ok=True)
    ev = [e for e in events() if not (OUT / f"{e['m']}.json").exists()]
    print(f"fetching {len(ev)}", flush=True)
    s = requests.Session()
    s.headers.update({"Origin": "https://pump.fun", "Accept": "application/json"})

    def one(e):
        try:
            c = pf_fetch(s, e["m"], e["t0"], "1m")
        except (TypeError, ValueError):
            return
        if c is not None:
            (OUT / f"{e['m']}.json").write_text(json.dumps(dict(e, c=c)))

    with ThreadPoolExecutor(6) as ex:
        list(ex.map(one, ev))
    print("fetch done", flush=True)


def load():
    out = []
    for f in sorted(OUT.glob("*.json")):
        if f.name == "rule.json":
            continue
        d = json.loads(f.read_text())
        c = d["c"]
        if c and c[0][0] <= d["t0"] + 120:                     # history reaches back to creation
            out.append(d)
    return sorted(out, key=lambda d: d["t0"])


def trade(d, L, tp, sl, M, cost):
    c = d["c"]
    k = next((i for i, x in enumerate(c) if x[0] >= d["dump"] + L), None)
    if k is None or c[k][0] > d["dump"] + L + 600:                # nothing trades within 10 min: no entry
        return None
    e = c[k][1]
    end = c[k][0] + M * 60
    x = None
    for j in range(k, len(c)):
        t, o, hi, lo, cl = c[j][:5]
        if t >= end:
            x = c[j - 1][4] if j > k else o
            break
        if lo <= e * (1 + sl):
            x = e * (1 + sl)
            break
        if hi >= e * (1 + tp):
            x = e * (1 + tp)
            break
    if x is None:
        x = c[-1][4]
    return (x / e) * (1 - cost) / (1 + cost) - 1


BANDS = {"4-8": (4, 8), "8-15": (8, 15), "15+": (15, 1e9), "all": (4, 1e9)}
GRID = list(product((0, 60), (0.3, 0.6, 1.0), (-0.3, -0.5), (10, 60), BANDS))


def run(data, L, tp, sl, M, band, cost=COST):
    lo, hi = BANDS[band]
    out = []
    for d in data:
        if lo <= d["x"] < hi:
            r = trade(d, L, tp, sl, M, cost)
            if r is not None:
                out.append((d["t0"], r))
    return out


def stats(xs):
    v = [r for _, r in xs]
    if len(v) < 3:
        return len(v), 0.0, 0.0, 0.0, 0.0
    m, s = statistics.mean(v), statistics.stdev(v)
    k = max(1, int(len(v) * 0.02))
    return len(v), m, (m / s * math.sqrt(len(v))) if s else 0.0, statistics.median(v), statistics.mean(sorted(v)[:-k])


def fmt(xs):
    n, m, t, med, trim = stats(xs)
    return f"n {n:4d} mean {m:+.2%} t {t:+.2f} median {med:+.2%} w/o top 2% {trim:+.2%}"


def split(data):
    k = int(len(data) * 0.6)
    return data[:k], data[k:]


def design():
    des, _ = split(load())
    print(f"design launches {len(des)}")
    best = None
    for p in GRID:
        xs = run(des, *p)
        print(p, fmt(xs), flush=True)
        if stats(xs)[0] >= 40 and (best is None or stats(xs)[1] > best[0]):
            best = (stats(xs)[1], p)
    (OUT / "rule.json").write_text(json.dumps(best[1]))
    print("FROZEN", best)


def test():
    _, tst = split(load())
    p = json.loads((OUT / "rule.json").read_text())
    xs = run(tst, *p)
    mid = xs[len(xs) // 2][0] if xs else 0
    print(f"TEST {p}: {fmt(xs)}\n   halves {fmt([x for x in xs if x[0] < mid])} / {fmt([x for x in xs if x[0] >= mid])}"
          f"\n   cost 3.5%/side {fmt(run(tst, *p, cost=COST2))}")




# ---------------------------------------------------------------- Y7b: fresh launches, dump time from pump.fun's trade API

Y7B_FROM = 1792377660.0                  # 2026-10-19 02:41 UTC: after the last launch in the Y7 data
Y7B = OUT / "y7b"
Y7B_RULE = (0, 1.0, -0.3, 10, "15+")


def fetch_b():
    import time
    from datetime import datetime
    from .full_trades import API as TAPI
    Y7B.mkdir(parents=True, exist_ok=True)
    cr = [json.loads(x) for x in LW.OUT.open(encoding="utf-8")]
    cr = [r for r in cr if r.get("txType") == "create" and r["solAmount"] >= 15 and Y7B_FROM < r["t"] < time.time() - 7200]
    todo = [r for r in cr if not (Y7B / f"{r['mint']}.json").exists()]
    print(f"{len(cr)} fresh 15+ launches; fetching {len(todo)}", flush=True)
    s = requests.Session()
    s.headers.update({"Origin": "https://pump.fun", "Accept": "application/json"})
    for i, r in enumerate(todo):
        m, t0 = r["mint"], r["t"]
        dump, cur, pages = None, f"9999999999990000000000-{int((t0 + 60) * 1000)}", 0
        while pages < 4:
            try:
                q = s.get(TAPI.format(m), params={"limit": 100, "cursor": cur}, timeout=20)
            except requests.RequestException:
                time.sleep(5)
                continue
            if q.status_code == 429:
                time.sleep(30)
                continue
            if not q.ok:
                break
            j = q.json()
            tr = j.get("trades") or []
            pages += 1
            for x in tr:
                if x["type"] == "sell" and x["userAddress"] == r["traderPublicKey"]:
                    t = datetime.fromisoformat(x["timestamp"][:19] + "+00:00").timestamp()
                    dump = t if dump is None else min(dump, t)
            cur = (j.get("pagination") or {}).get("nextCursor")
            if not tr or not cur or min(datetime.fromisoformat(x["timestamp"][:19] + "+00:00").timestamp()
                                         for x in tr) <= t0:
                break
            time.sleep(3)
        time.sleep(3)
        if dump is None:
            (Y7B / f"{m}.json").write_text(json.dumps(dict(m=m, t0=t0, x=r["solAmount"], dump=None, c=[])))
            continue
        try:
            c = pf_fetch(s, m, t0, "1m")
        except (TypeError, ValueError):
            c = None
        (Y7B / f"{m}.json").write_text(json.dumps(dict(m=m, t0=t0, x=r["solAmount"], dump=dump, c=c or [])))
        if i % 25 == 0:
            print(f"  {i}/{len(todo)}", flush=True)
    print("fetch done", flush=True)


def test_b():
    data = []
    for f in sorted(Y7B.glob("*.json")):
        d = json.loads(f.read_text())
        if d["dump"] and d["dump"] - d["t0"] <= 60 and d["c"] and d["c"][0][0] <= d["t0"] + 120:
            data.append(d)
    data.sort(key=lambda d: d["t0"])
    xs = run(data, *Y7B_RULE)
    print(f"Y7b {Y7B_RULE}: {len(data)} usable launches\n   {fmt(xs)}\n   cost 3.5%/side {fmt(run(data, *Y7B_RULE, cost=COST2))}")


def main() -> None:
    {"fetch": fetch, "design": design, "test": test, "fetch_b": fetch_b, "test_b": test_b}[sys.argv[1]]()


if __name__ == "__main__":
    main()
