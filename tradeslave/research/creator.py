"""Creator economics on the unbiased launch sample (vault: "2026-10-17 creator economics", strategy J).

    uv run python -m tradeslave.research.creator

J1 mechanical replay: we launch, buy X in the creation slot, then the launch's OUTSIDE trades (wallets
not in the creation slot) are replayed in order on a fresh curve (buys by SOL into the curve, sells by
token amount), and we sell our whole bag right before the first outside trade at or after T seconds.
Plus the 0.30% creator fee on outside volume (first 15 min, the mined window).
"""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict

from .backfill_trades import HISTORY_OUT, OUT

VS0, VT0 = 30e9, 1073e12          # fresh curve (lamports, raw token units)
FEE = 0.0125                       # curve fee per side (fees.png)
CREATOR_FEE = 0.003
LAUNCH_COST, TX_COST = 0.03, 0.0015   # assumption: create rent + fees; per tx fee + priority tip
TS = (5, 15, 30, 60, 120, 300)
BUCKETS = ((0.5, 2), (2, 5), (5, 99))


def launches():
    samp, tr = {}, defaultdict(list)
    for p in (OUT, HISTORY_OUT):
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                if '"sample"' in line:
                    r = json.loads(line)
                    samp[r["m"]] = r["t0"]
                elif '"trade"' in line:
                    r = json.loads(line)
                    tr[r["m"]].append(r)
    out = []
    for m, t0 in samp.items():
        ts = sorted(tr.get(m, []), key=lambda r: r.get("i", 0))
        if not ts:
            continue
        s0 = ts[0]["slot"]
        grp = {r["w"] for r in ts if r["slot"] == s0}
        x = sum(r["sol"] for r in ts if r["slot"] == s0 and r["b"] and r["w"] in grp)
        outside = [r for r in ts if r["w"] not in grp]
        out.append({"m": m, "t0": t0, "tc": ts[0]["t"], "x": x, "outside": outside})
    return sorted(out, key=lambda d: d["t0"])


def replay(lau: dict, x: float, t_exit: float) -> float:
    vs, vt = VS0, VT0
    net = x * (1 - FEE) * 1e9
    bag = vt * net / (vs + net)
    vs, vt = vs + net, vt - bag
    proceeds, sold, vol = None, False, 0.0
    for r in lau["outside"]:
        if not sold and r["t"] - lau["tc"] >= t_exit:
            proceeds = vs * bag / (vt + bag) / 1e9 * (1 - FEE)
            vs, vt, sold = vs - proceeds / (1 - FEE) * 1e9, vt + bag, True
        vol += r["sol"]
        if r["b"]:
            dv = max(r["dv"], 0)
            got = vt * dv / (vs + dv)
            vs, vt = vs + dv, vt - got
        else:
            tok = r["tok"] * 1e6
            out = vs * tok / (vt + tok)
            vs, vt = vs - out, vt + tok
    if not sold:
        proceeds = vs * bag / (vt + bag) / 1e9 * (1 - FEE)
    return proceeds - x + CREATOR_FEE * vol - LAUNCH_COST - 3 * TX_COST


def stats(xs) -> str:
    if len(xs) < 5:
        return f"n={len(xs)}"
    m, s = statistics.mean(xs), statistics.pstdev(xs)
    return (f"n={len(xs):3d} mean {m:+7.3f} med {statistics.median(xs):+7.3f} win {sum(v > 0 for v in xs) / len(xs):4.0%} "
            f"t {m / (s / math.sqrt(len(xs))) if s else 0:+5.2f}")


def main() -> None:
    ls = launches()
    h = len(ls) // 2
    print(f"{len(ls)} mined random launches; first half until {ls[h]['t0']:.0f}\n")
    for lo, hi in BUCKETS:
        a = [d for d in ls[:h] if lo < d["x"] <= hi]
        b = [d for d in ls[h:] if lo < d["x"] <= hi]
        best = None
        for t in TS:
            ra = [replay(d, d["x"], t) for d in a]
            print(f"  dev buy {lo}-{hi} SOL, sell at {t:3d}s: 1st half {stats(ra)}")
            if ra and (best is None or statistics.mean(ra) > best[0]):
                best = (statistics.mean(ra), t)
        if best:
            t = best[1]
            print(f"-> chosen T={t}s; 2nd half (judged) {stats([replay(d, d['x'], t) for d in b])}\n")


if __name__ == "__main__":
    main()
