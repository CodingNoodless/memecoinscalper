"""MV: M4 replayed on every coin with curve trades from create, and its variants (vault "2026-10-22 M4 variants
backtest (MV)", pre-registered 22:25 PDT).

    uv run python -m tradeslave.research.m4_bt data      -> data/m1/mv_coins.json.gz (A: NC + Helius ticks; B: live feed)
    uv run python -m tradeslave.research.m4_bt run [runscores.json]

Entry: M4's checks every 5 s from the moment the coin was scored, for 20 min; fill 1 s after the decision through the
curve. Exits: tf_replay.sim (M1 base, 2 s polls) with the holder exit, optionally gated on net selling (idea 2).
"""

from __future__ import annotations

import gzip
import json
import math
import sqlite3
import statistics
import sys
import time
from collections import defaultdict

from ..paths import DATA_DIR
from .live_feed import DB
from .tf_replay import BASE, K, path, sim

SOL_USD, SIZE = 105.0, 0.1
OUT = DATA_DIR / "m1" / "mv_coins.json.gz"
B_FROM = time.mktime(time.strptime("2026-10-22 16:55", "%Y-%m-%d %H:%M"))
A_SPLIT = 1792623600                                            # 2026-10-21 15:00 PDT
B_SPLIT = time.mktime(time.strptime("2026-10-22 20:00", "%Y-%m-%d %H:%M"))


def _cands():
    """mint -> best score row (any bot) and the earliest scoring age; M4's rows carry the author's followers."""
    best, age = {}, {}
    for v in ("m1", "m2", "m3", "m4"):
        for line in (DATA_DIR / v / "cands.jsonl").open(encoding="utf-8"):
            c = json.loads(line)
            if c.get("score") is None or "age" not in c:
                continue
            age[c["m"]] = min(age.get(c["m"], 9e9), c["age"])
            if c["m"] not in best or v == "m4" or c["score"] > best[c["m"]]["score"]:
                best[c["m"]] = c
    return best, age


def build() -> None:
    best, age = _cands()
    coins = []
    nc = json.loads((DATA_DIR / "m1" / "nc.json").read_text())
    for r in nc:                                                # A
        p = DATA_DIR / "m1" / "ticks" / f"{r['m']}.json.gz"
        if not p.exists() or r["m"] not in best:
            continue
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            tr = sorted(json.load(fh)["trades"], key=lambda x: (x[0], x[1]))
        pre, post = path({"t": -1.0, "p0": 1.0}, tr, r["t0"])
        rows = [[x[0], x[1], x[2], x[3], x[4], x[5], x[6]] for x in pre + post]
        if rows and rows[0][6] is not None:
            coins.append(dict(set="A", m=r["m"], t0=r["t0"], age=age.get(r["m"], 3), c=best[r["m"]], rows=rows))
    db = sqlite3.connect(DB)                                    # B
    for m, c in best.items():
        if c["t"] < B_FROM:
            continue
        cc = db.execute("SELECT complete FROM coin WHERE m=?", (m,)).fetchone()
        if not cc or not cc[0]:
            continue
        tr = db.execute("SELECT t, w, b, sol, tok, vs, vt FROM tr WHERE m=? ORDER BY slot, seq", (m,)).fetchall()
        bal, h, rows = defaultdict(float), 0, []
        for t, w, b, sol, tok, vs, vt in tr:
            b0 = bal[w]
            b1 = b0 + (tok if b else -tok)
            bal[w] = b1
            h += (b0 <= 1 < b1) - (b1 <= 1 < b0)
            if vt:
                rows.append([t, vs / vt, bool(b), sol, w, tok, h])
        if rows:
            coins.append(dict(set="B", m=m, t0=rows[0][0], age=age.get(m, 3), c=c, rows=rows))
    with gzip.open(OUT, "wt", encoding="utf-8") as fh:
        json.dump(coins, fh)
    print(len(coins), "coins:", sum(x["set"] == "A" for x in coins), "A,", sum(x["set"] == "B" for x in coins), "B")


def entry(coin, gate):
    """M4's entry replay. gate(coin) -> True if the narrative gate passes. Returns the row index to fill at, or None."""
    c, rows = coin["c"], coin["rows"]
    if not gate(coin):
        return None
    f = c.get("followers")
    if f is not None and not 100 <= f < 10000:
        return None
    if c.get("after") and not c.get("improves"):
        return None                                             # a copy that doesn't improve: no buy (M1 rule)
    top = 10000.0 if (c.get("score") or 0) >= 9 else 7000.0
    dev = rows[0][4]
    t = coin["t0"] + coin["age"]
    j, bal, hmax, vol, buys = 0, defaultdict(float), 0, 0.0, defaultdict(float)
    while t <= coin["t0"] + coin["age"] + 1200:
        while j < len(rows) and rows[j][0] <= t:
            r = rows[j]
            bal[r[4]] += r[5] if r[2] else -r[5]
            vol += r[3] or 0.0
            if r[2] and r[4] != dev:
                buys[r[4]] += r[3] or 0.0
            hmax = max(hmax, r[6] or 0)
            j += 1
        if j == 0:
            t += 5
            continue
        p, h = rows[j - 1][1], rows[j - 1][6]
        mc = p * 1e9 * SOL_USD
        if 4000 <= mc <= top and vol * SOL_USD >= 1500:
            px = [(r[0], r[1]) for r in rows[:j]]
            lo60 = min((q for tt, q in px if tt >= t - 60), default=p)
            flow10 = sum((r[3] or 0) if r[2] else -(r[3] or 0) for r in rows[:j] if r[0] >= t - 10)
            if p >= 1.3 * lo60 and flow10 < 0:
                return None                                     # top skip (done)
            if p <= 1.15 * px[0][1] and p <= 0.8 * max(q for _, q in px):
                return None                                     # back at launch price (done)
            top5 = sorted((v for w, v in bal.items() if v > 0), reverse=True)[:5]
            if any(abs(a - b) / max(a, b) < 0.005 for i, a in enumerate(top5) for b in top5[i + 1:]):
                return None                                     # identical balances (done)
            if buys and max(buys.values()) > 0.4 * sum(buys.values()):
                return None                                     # one buy dominates (done)
            if h >= 20 and h >= 0.9 * hmax:
                k = j - 1
                while k + 1 < len(rows) and rows[k + 1][0] <= t + 1:
                    k += 1
                return k
        t += 5
    return None


def trade(coin, k, over):
    rows, c = coin["rows"], coin["c"]
    p0 = rows[k][1]
    v = math.sqrt(p0 * K)
    q = K / v - K / (v + SIZE * (1 - 0.0125))
    x = dict(v="m1", t=rows[k][0], p0=p0, q=q, stop=0.7, high=(c.get("score") or 0) >= 9, score=c.get("score"))
    pre, post = rows[:k + 1], rows[k + 1:]
    P = dict(BASE, **{"hdecl": 0.10, **over})
    return sim(x, pre, post, P)[0]


def stats(v):
    if not v:
        return "n   0"
    m = statistics.mean(v)
    t = m / (statistics.stdev(v) / len(v) ** 0.5) if len(v) > 2 and statistics.stdev(v) else 0.0
    return (f"n {len(v):3d} mean {m:+6.1%} t {t:+5.2f} win {sum(x > 0 for x in v) / len(v):4.0%} "
            f"SOL {sum(v) * SIZE:+.3f} ex-best-2 {sum(sorted(v)[:-2]) * SIZE:+.3f}")


def part(coin):
    if coin["set"] == "A":
        return "A-design" if coin["t0"] < A_SPLIT else "A-check"
    return "B-train" if coin["t0"] < B_SPLIT else "B-test"


def run(runscores=None) -> None:
    with gzip.open(OUT, "rt", encoding="utf-8") as fh:
        coins = json.load(fh)
    rs = json.loads(open(runscores).read()) if runscores else {}
    gate7 = lambda co: (co["c"].get("score") or 0) >= 7                     # noqa: E731
    variants = {"V0 M4 as is": (gate7, {}),
                "V1 holder exit needs net selling (20 s)": (gate7, {"hflow": 20}),
                "V1b holder exit needs net selling (40 s)": (gate7, {"hflow": 40})}
    for R in (5, 6, 7):
        if rs:
            g = lambda co, R=R: rs.get(co["m"]) is not None and rs[co["m"]] >= R    # noqa: E731
            variants[f"V2 run score >= {R}"] = (g, {})
            variants[f"V3 run score >= {R} + V1 (20 s)"] = (g, {"hflow": 20})
    parts = ("A-design", "A-check", "B-train", "B-test")
    print(f"coins: " + ", ".join(f"{p} {sum(part(c) == p for c in coins)}" for p in parts)
          + (f"; run scores for {len(rs)}" if rs else ""))
    res = {}
    for name, (gate, over) in variants.items():
        out = defaultdict(list)
        for co in coins:
            k = entry(co, gate)
            if k is not None and k + 1 < len(co["rows"]):
                out[part(co)].append(trade(co, k, over))
        res[name] = out
        print(f"\n{name}")
        for p in parts:
            print(f"   {p:8s} {stats(out[p])}")
        print(f"   {'ALL':8s} {stats([x for p in parts for x in out[p]])}")
    json.dump({k: dict(v) for k, v in res.items()}, open(DATA_DIR / "m1" / "mv_results.json", "w"))


if __name__ == "__main__":
    if sys.argv[1:2] == ["data"]:
        build()
    else:
        run(sys.argv[2] if len(sys.argv) > 2 else None)
