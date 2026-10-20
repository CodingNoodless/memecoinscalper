"""Strategy Y5: Setuh-style "meta" following on fresh pump.fun launches (pre-registered; vault:
"2026-10-20 aged memecoin survivors (Y)", section Y5).

    uv run python -m tradeslave.research.meta_follow design
    uv run python -m tradeslave.research.meta_follow test

A word (name + symbol) is hot at time t when >= K graduations in [t - W, t) contain it. Candidates are launches from
the random long_paths sample (1-min candles from launch) with a hot word; the same rule on launches without one is
the control. Exits follow his video: 2x targets, "initials" at 2x, wide stops.
"""

from __future__ import annotations

import bisect
import json
import math
import re
import statistics
import sys
from collections import defaultdict
from itertools import product

from ..paths import DATA_DIR
from ..scalper.recorder import TICK_DIR, read_ticks

PATHS = DATA_DIR / "long_paths"
OUT = DATA_DIR / "meta_follow"
SPLIT = 1792065600                                  # 2026-10-15 12:00 UTC
COST, COST2 = 0.02, 0.03
H = 3600
STOP = set("the and for with you your this that from coin token sol solana pump fun inu official new meme memes "
           "just like get not but are was its his her who what when all one has have our out can will".split())


def words(*xs):
    out = set()
    for x in xs:
        for w in re.split(r"[^a-z0-9]+", (x or "").lower()):
            if len(w) >= 3 and w not in STOP and not w.isdigit():
                out.add(w)
    return out


def index():
    names, gr = {}, {}
    for r in read_ticks(sorted(TICK_DIR.glob("*.jsonl.gz"))):
        if r.get("k") == "create" and r.get("m"):
            names[r["m"]] = words(r.get("nm"), r.get("s"))
        elif r.get("k") == "migr" and r.get("m"):
            gr.setdefault(r["m"], float(r["t"]))
    by_word = defaultdict(list)
    for m, t in gr.items():
        for w in names.get(m, ()):
            by_word[w].append(t)
    for v in by_word.values():
        v.sort()
    return names, by_word


def hot(ws, t, by_word, K, W):
    for w in ws:
        ts = by_word.get(w)
        if ts and bisect.bisect_left(ts, t) - bisect.bisect_left(ts, t - W * H) >= K:
            return True
    return False


def entry(c, t0, E):
    if E == "m1":
        return (0, c[0][4]) if c and c[0][0] < t0 + 120 else None
    for k, x in enumerate(c):
        if x[0] > t0 + 60 * 60:
            return None
        if x[4] * 1e9 >= 60:
            return k, x[4]
    return None


def exit_ret(c, k, e, X, cost):
    """Return of one unit bought at e after candle k (exits from candle k+1 on), max hold 24 h."""
    end = c[k][0] + 24 * H
    half = None                                      # X2: price at which half was sold
    peak = e
    x = None
    for j in range(k + 1, len(c)):
        t, o, hi, lo, cl = c[j][:5]
        if t > end:
            x = c[j - 1][4]
            break
        if X == "X3":
            if lo <= e * 0.7:
                x = e * 0.7
                break
            if hi >= e * 2:
                x = e * 2
                break
        elif X == "X1" or half is None:
            if lo <= e * 0.5:
                x = e * 0.5
                break
            if hi >= e * 2:
                if X == "X1":
                    x = e * 2
                    break
                half, peak = e * 2, max(peak, hi)
                continue
        else:                                        # X2 after selling initials: 50% trailing stop on the rest
            peak = max(peak, hi)
            if lo <= peak * 0.5:
                x = peak * 0.5
                break
    if x is None:
        x = c[-1][4]
    gross = (0.5 * half + 0.5 * x) / e if half is not None else x / e
    return gross * (1 - cost) / (1 + cost) - 1


def run(K, W, E, X, lo, hi, meta=True, cost=COST):
    names, by_word = CACHE["idx"]
    out = []
    for d in CACHE["paths"]:
        t0 = d["t0"]
        if not (lo <= t0 < hi):
            continue
        ws = names.get(d["m"])
        if ws is None or hot(ws, t0, by_word, K, W) != meta:
            continue
        c = d["curve"]
        en = entry(c, t0, E)
        if not en:
            continue
        out.append((t0, exit_ret(c, en[0], en[1], X, cost)))
    return out


CACHE = {}


def prep():
    CACHE["idx"] = index()
    ps = []
    for f in PATHS.glob("*.json"):
        d = json.loads(f.read_text())
        if d.get("curve"):
            ps.append(d)
    CACHE["paths"] = ps
    print(f"{len(ps)} random launches with candles", flush=True)


def stats(xs):
    v = [r for _, r in xs]
    if len(v) < 3:
        return len(v), 0.0, 0.0, 0.0, 0.0
    m, s = statistics.mean(v), statistics.stdev(v)
    k = max(1, int(len(v) * 0.02))
    return len(v), m, (m / s * math.sqrt(len(v))) if s else 0.0, statistics.median(v), statistics.mean(sorted(v)[:-k])


def fmt(xs):
    n, m, t, med, trim = stats(xs)
    return f"n {n:5d} mean {m:+.2%} t {t:+.2f} median {med:+.2%} w/o top 2% {trim:+.2%}"


GRID = list(product((2, 3), (6, 24), ("m1", "60"), ("X1", "X2", "X3")))


def design():
    prep()
    best = None
    for K, W, E, X in GRID:
        a = run(K, W, E, X, 0, SPLIT)
        b = run(K, W, E, X, 0, SPLIT, meta=False)
        print(f"K {K} W {W:2d} E {E:3s} {X}: meta {fmt(a)} | control {fmt(b)}", flush=True)
        if stats(a)[0] >= 30 and (best is None or stats(a)[1] > best[0]):
            best = (stats(a)[1], dict(K=K, W=W, E=E, X=X))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "rule.json").write_text(json.dumps(best[1]))
    print("FROZEN", best[1], f"design mean {best[0]:+.2%}")


def test():
    prep()
    p = json.loads((OUT / "rule.json").read_text())
    a = sorted(run(lo=SPLIT, hi=9e9, **p))
    mid = a[len(a) // 2][0] if a else 0
    print(f"TEST {p}: meta {fmt(a)}")
    print(f"   halves {fmt([x for x in a if x[0] < mid])} / {fmt([x for x in a if x[0] >= mid])}")
    print(f"   cost 3%/side {fmt(run(lo=SPLIT, hi=9e9, cost=COST2, **p))}")
    print(f"   control (no hot word) {fmt(run(lo=SPLIT, hi=9e9, meta=False, **p))}")


def main() -> None:
    {"design": design, "test": test}[sys.argv[1]]()


if __name__ == "__main__":
    main()
