"""M9 design test: M1's live coins under M9 skips (cluster, serial funder) and exits (E2, -10% stop, 1 s polls).
Vault "2026-10-23 M9 design test - M1 + M5 entries with loss controls (M9)", pre-registered 16:45 PDT.

    uv run python -u -m tradeslave.research.m9_test fetch   -> Helius funder lookups for M1 coins (cached in funding.json)
    uv run python -u -m tradeslave.research.m9_test         -> the test
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests

from ..paths import DATA_DIR
from .fl_test import CACHE, NONSYSTEM, PUBLIC, features, funding, helius_url, holders_at
from .m5_bt import EXITS, ret
from .xa_test import live_coins

E2 = EXITS["E2 M4 exits, 15 s holder grace"]
L5 = dict(stop=0.90, P=dict(E2["P"], poll=1.0))
E2_1S = dict(stop=E2["stop"], P=dict(E2["P"], poll=1.0))
SEED = DATA_DIR / "m1" / "serial_seed.json"


def m1_coins():
    """M1 live trades over M5's hours as (trade, coin, k), with rows from the feed."""
    t5 = min(json.loads(line)["t"] for line in (DATA_DIR / "m5" / "trades.jsonl").open() if line.strip())
    db = sqlite3.connect(DATA_DIR / "feed" / "trades.sqlite")
    out = []
    for line in (DATA_DIR / "m1" / "trades.jsonl").open():
        if not line.strip():
            continue
        x = json.loads(line)
        if x["t"] < t5:
            continue
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
        if k is not None and k >= 3 and len(rows) > k + 3:
            out.append((x, dict(m=x["m"], rows=rows), k))
    return out


def fetch() -> None:
    cache = json.loads(CACHE.read_text())
    want = set()
    for _, co, k in m1_coins():
        top, cr = holders_at(co["rows"], k)
        want |= {cr} | {w for w, _ in top}
    need = sorted(w for w in want if w not in cache or "err" in cache[w])
    print(f"{len(want)} wallets wanted, {len(need)} to look up (~{10 * len(need)} Helius credits)", flush=True)
    if "--dry" in sys.argv:
        return
    url, lock, last = helius_url(), threading.Lock(), [0.0]

    def one(w):
        with lock:
            wait = last[0] + 1 / 6.0 - time.time()
            last[0] = max(time.time(), last[0] + 1 / 6.0)
        if wait > 0:
            time.sleep(wait)
        return w, funding(url, w, requests.Session())

    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(one, need))
    cache = json.loads(CACHE.read_text())                    # merge with lookups the live bots made meanwhile
    for w, r in res:
        cache[w] = r
    tmp = CACHE.with_name("funding.m9.tmp")
    tmp.write_text(json.dumps(cache))
    tmp.replace(CACHE)
    print(f"done: errors {sum(1 for _, r in res if 'err' in r)} of {len(res)}", flush=True)


def summ(v, pk=None):
    if not v:
        return "n   0"
    s = (f"n {len(v):3d} mean {statistics.mean(v):+6.1%} med {statistics.median(v):+6.1%} "
         f"<=-25% {sum(x <= -0.25 for x in v) / len(v):4.0%}")
    if pk is not None:
        s += f" peak>=2x {sum(p >= 2 for p in pk) / len(pk):5.1%}"
    return s


def main() -> None:
    if "fetch" in sys.argv:
        fetch()
        return
    from .xa_test import ret_full
    fund = json.loads(CACHE.read_text())
    public = set(json.loads(PUBLIC.read_text())) if PUBLIC.exists() else set()
    seen = defaultdict(int, json.loads(SEED.read_text()))     # serial counts as the live M7 starts from
    S1 = m1_coins()
    rows = []
    for x, co, k in sorted(S1, key=lambda z: z[0]["t"]):
        top, cr = holders_at(co["rows"], k)
        f = features(top, cr, co["rows"][k][0], fund, public)
        fu = (fund.get(cr) or {}).get("funder")
        serial = None if not fu else (fu not in NONSYSTEM and seen[fu] >= 1)
        res = {name: ret_full(co, k, ex) for name, ex in (("E2", E2), ("E2 1s", E2_1S), ("L5", L5))}
        rows.append(dict(actual=x["ret"], actual_peak=x.get("peak", 0) / x["p0"], cluster=f["cluster_share"],
                         known=f["known"], serial=serial, **{n: r[0] for n, r in res.items()},
                         **{n + " peak": r[3] for n, r in res.items()}))
    n = len(rows)
    print(f"M1 live trades over M5's hours with feed rows: {n}; creator funder known for "
          f"{sum(r['serial'] is not None for r in rows)}; median known-funder share of top-20 "
          f"{statistics.median(r['known'] for r in rows):.0%}")
    print("\nM1 coins, no skips:")
    print(f"   actual M1 exits      {summ([r['actual'] for r in rows], [r['actual_peak'] for r in rows])}")
    for e in ("E2", "E2 1s", "L5"):
        print(f"   {e:20s} {summ([r[e] for r in rows], [r[e + ' peak'] for r in rows])}")
    keeps = {"M9a (cluster < 15%)": lambda r: r["cluster"] < 0.15,
             "M9b (+ no serial funder)": lambda r: r["cluster"] < 0.15 and r["serial"] is False}
    print("\nM1 coins under M9 skips:")
    for name, keep in keeps.items():
        kept = [r for r in rows if keep(r)]
        sk = [r for r in rows if not keep(r)]
        for e in ("actual", "E2", "L5"):
            pk = "actual_peak" if e == "actual" else e + " peak"
            print(f"   {name:26s} {e:6s} kept {summ([r[e] for r in kept], [r[pk] for r in kept])} | "
                  f"skipped {summ([r[e] for r in sk])}")
    base = statistics.mean(r["actual"] for r in rows)
    print("\nVerdict (M9 exits = L5):")
    ok_any = False
    for name, keep in keeps.items():
        kept = [r for r in rows if keep(r)]
        if not kept:
            continue
        m = statistics.mean(r["L5"] for r in kept)
        p2 = sum(r["L5 peak"] >= 2 for r in kept) / len(kept)
        ok = m > 0 and m >= base + 0.05 and p2 >= 0.05
        ok_any |= ok
        print(f"   {name}: mean {m:+.1%} (> 0: {m > 0}), vs M1 actual {base:+.1%} + 5 pts: {m >= base + 0.05}, "
              f"2x rate {p2:.1%} (>= 5%: {p2 >= 0.05}) -> {'PASS' if ok else 'FAIL'}")
    print(f"M9 verdict: {'PASS' if ok_any else 'FAIL'}")
    # report only: the M5 side under the same exits (XA live replay) for the combined pool
    m5 = [ret(co, k, L5) for co, k in live_coins()]
    print(f"\nReport: M5 live coins under L5 exits {summ(m5)}")


if __name__ == "__main__":
    main()
