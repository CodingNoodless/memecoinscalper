"""Y6: Claude-read narrative conviction (Setuh's PBANK play), pre-registered; vault: "2026-10-20 aged memecoin
survivors (Y)", section Y6.

    uv run python -m tradeslave.research.narr_llm cands     # sample candidates + metadata, NO outcomes written
    (Claude scores data/narr/cands.jsonl blind into data/narr/scores.jsonl)
    uv run python -m tradeslave.research.narr_llm design
    uv run python -m tradeslave.research.narr_llm test
"""

from __future__ import annotations

import json
import math
import random
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

from ..paths import DATA_DIR
from ..scalper.recorder import TICK_DIR, read_ticks
from .meta_follow import PATHS, SPLIT

OUT = DATA_DIR / "narr"
ENTRY_SOL, ENTRY_WIN = 100, 2 * 3600
PER = 450
COST = 0.02


def entry(d):
    for k, x in enumerate(d["curve"]):
        if x[0] > d["t0"] + ENTRY_WIN:
            return None
        if x[4] * 1e9 >= ENTRY_SOL:
            return k
    return None


def cands():
    OUT.mkdir(parents=True, exist_ok=True)
    uri = {}
    for r in read_ticks(sorted(TICK_DIR.glob("*.jsonl.gz"))):
        if r.get("k") == "create" and r.get("m"):
            uri[r["m"]] = (r.get("uri"), r.get("nm"), r.get("s"))
    pool = {"design": [], "test": []}
    for f in sorted(PATHS.glob("*.json")):
        d = json.loads(f.read_text())
        if d.get("curve") and d["m"] in uri and entry(d) is not None:
            pool["design" if d["t0"] < SPLIT else "test"].append(d["m"])
    s = requests.Session()

    def meta(m):
        u, nm, sy = uri[m]
        u = u or ""
        if u.startswith("ipfs://"):
            u = "https://ipfs.io/ipfs/" + u[7:]
        for url in (u, u.replace("https://ipfs.io/ipfs/", "https://gateway.pinata.cloud/ipfs/")):
            try:
                j = s.get(url, timeout=15).json()
                return dict(m=m, name=nm, symbol=sy, desc=(j.get("description") or "")[:400],
                            twitter=bool(j.get("twitter")), telegram=bool(j.get("telegram")),
                            website=bool(j.get("website")))
            except Exception:
                continue
        return None

    rows = []
    for lab, ms in pool.items():
        ms.sort()
        random.Random(6).shuffle(ms)
        with ThreadPoolExecutor(8) as ex:
            got = [x for x in ex.map(meta, ms[: int(PER * 1.3)]) if x]
        rows += [dict(x, period=lab) for x in got[:PER]]
        print(f"{lab}: {len(ms)} eligible, {len(got[:PER])} with metadata", flush=True)
    random.Random(7).shuffle(rows)                      # mix periods so scoring can't drift by period
    with (OUT / "cands.jsonl").open("w", encoding="utf-8") as f:
        for i, r in enumerate(rows):
            f.write(json.dumps(dict(r, i=i), ensure_ascii=False) + "\n")


def outcome(d, k, stop):
    c, e = d["curve"], d["curve"][k][4]
    end = c[k][0] + 24 * 3600
    x = None
    for j in range(k + 1, len(c)):
        t, o, hi, lo, cl = c[j][:5]
        if t > end:
            x = c[j - 1][4]
            break
        if stop and lo <= e * 0.5:
            x = e * 0.5
            break
        if hi >= 2 * e:
            x = 2 * e
            break
    if x is None:
        x = c[-1][4]
    return (x / e) * (1 - COST) / (1 + COST) - 1


def rows(period):
    sc = {}
    for line in (OUT / "scores.jsonl").open(encoding="utf-8"):
        r = json.loads(line)
        sc[r["i"]] = r["score"]
    out = []
    for line in (OUT / "cands.jsonl").open(encoding="utf-8"):
        c = json.loads(line)
        if c["period"] != period or c["i"] not in sc:
            continue
        d = json.loads((PATHS / f"{c['m']}.json").read_text())
        k = entry(d)
        out.append(dict(score=sc[c["i"]], t0=d["t0"], r1=outcome(d, k, False), r2=outcome(d, k, True)))
    return out


def stats(v):
    if len(v) < 3:
        return len(v), 0.0, 0.0, 0.0
    m, s = statistics.mean(v), statistics.stdev(v)
    k = max(1, int(len(v) * 0.02))
    return len(v), m, (m / s * math.sqrt(len(v))) if s else 0.0, statistics.mean(sorted(v)[:-k])


def fmt(v):
    n, m, t, trim = stats(v)
    return f"n {n:3d} mean {m:+.2%} t {t:+.2f} w/o top 2% {trim:+.2%}"


def design():
    xs = rows("design")
    print("score distribution:", {s: sum(1 for x in xs if x["score"] == s) for s in range(11)})
    best = None
    for T in (6, 7, 8):
        for ex in ("r1", "r2"):
            hi = [x[ex] for x in xs if x["score"] >= T]
            lo = [x[ex] for x in xs if x["score"] < T]
            print(f"T {T} {ex}: high {fmt(hi)} | low {fmt(lo)}")
            if len(hi) >= 15 and (best is None or statistics.mean(hi) > best[0]):
                best = (statistics.mean(hi), T, ex)
    (OUT / "rule.json").write_text(json.dumps({"T": best[1], "exit": best[2]}))
    print("FROZEN", best)


def test():
    xs = sorted(rows("test"), key=lambda x: x["t0"])
    fr = json.loads((OUT / "rule.json").read_text())
    T, ex = fr["T"], fr["exit"]
    hi = [x for x in xs if x["score"] >= T]
    mid = hi[len(hi) // 2]["t0"] if hi else 0
    print(f"TEST T {T} {ex}: high {fmt([x[ex] for x in hi])} | low {fmt([x[ex] for x in xs if x['score'] < T])}")
    print(f"   halves {fmt([x[ex] for x in hi if x['t0'] < mid])} / {fmt([x[ex] for x in hi if x['t0'] >= mid])}")


def main() -> None:
    {"cands": cands, "design": design, "test": test}[sys.argv[1]]()


if __name__ == "__main__":
    main()
