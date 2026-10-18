"""Strategy K: ride the bot wave after a big dev buy and get out before the dev sells.

    uv run python -m tradeslave.research.front_run

We see a launch on the free PumpPortal feed ~0.5-1 s after its block (measured 2026-10-17), so with a
priority fee our buy lands about slot +3 to +5. Replay: ALL of the launch's real trades (creator
included) in order on a fresh curve, buys by SOL into the curve and sells by token amount; our buy goes
in right before the first trade at slot >= s0 + k, our sell right before the first trade at or after
T seconds. Costs: 1.25% curve fee per side, PRIO SOL per transaction (priority fee + tip).

Design = history sample (research.creator.launches, random launches, first 15 min).
Test   = forward launches recorded live by research.launch_watch (first 20 s, so T <= 15 s).
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict

from .creator import FEE, VS0, VT0, launches, stats

PRIO = 0.003
KS = (3, 5, 8)
TS = (3, 5, 8, 10, 15)
BANDS = ((0.5, 2), (2, 5), (5, 8), (8, 15), (15, 1e9))



def replay(trades: list[dict], s0: int, tc: float, k: int, t_exit: float, size: float):
    """Net SOL of buying `size` at slot s0+k and selling at T; None if no trade reaches slot s0+k."""
    vs, vt = VS0, VT0
    bag, entered, proceeds = 0.0, False, None
    for r in trades:
        if not entered and r["slot"] >= s0 + k:
            if r["t"] - tc >= t_exit:
                return None                       # nothing to ride: first trade at our slot is past T
            net = size * (1 - FEE) * 1e9
            bag = vt * net / (vs + net)
            vs, vt, entered = vs + net, vt - bag, True
        if entered and proceeds is None and r["t"] - tc >= t_exit:
            proceeds = vs * bag / (vt + bag) / 1e9 * (1 - FEE)
            vs, vt = vs - proceeds / (1 - FEE) * 1e9, vt + bag
        if r["b"]:
            dv = max(r["dv"], 0)
            got = vt * dv / (vs + dv)
            vs, vt = vs + dv, vt - got
        else:
            tok = r["tok"] * 1e6
            out = vs * tok / (vt + tok)
            vs, vt = vs - out, vt + tok
    if not entered:
        return None
    if proceeds is None:
        proceeds = vs * bag / (vt + bag) / 1e9 * (1 - FEE)
    return proceeds - size - 2 * PRIO


def history() -> list[dict]:
    out = []
    for d in launches():
        ts = d["trades"]
        out.append({"x": d["x"], "s0": ts[0]["slot"], "tc": ts[0]["t"], "t0": d["t0"], "all": ts})
    return out


def forward() -> list[dict]:
    from .launch_watch import FROZEN_AT, OUT, TRADES
    creates = {}
    for line in OUT.open(encoding="utf-8"):
        r = json.loads(line)
        if r.get("txType") == "create" and r["t"] >= FROZEN_AT:
            creates[r["mint"]] = r
    tr = defaultdict(list)
    for line in TRADES.open(encoding="utf-8"):
        r = json.loads(line)
        if r.get("k") == "trade" and r["m"] in creates:
            tr[r["m"]].append(r)
    out = []
    for m, ts in tr.items():
        ts.sort(key=lambda r: r.get("i", 0))
        s0 = ts[0]["slot"]
        x = sum(r["sol"] for r in ts if r["slot"] == s0 and r["b"])
        out.append({"x": x, "s0": s0, "tc": ts[0]["t"], "t0": creates[m]["t"], "all": ts})
    return out


def run(ls, k, t, size, lo, hi):
    return [v for v in (replay(d["all"], d["s0"], d["tc"], k, t, size) for d in ls if lo < d["x"] <= hi)
            if v is not None]


def main() -> None:
    hist, fwd = history(), forward()
    print(f"design (history) {len(hist)} launches, test (forward, 20 s) {len(fwd)}\n")
    for lo, hi in BANDS:
        best = None
        for k in KS:
            for t in TS:
                xs = run(hist, k, t, 1.0, lo, hi)
                if len(xs) >= 10 and (best is None or statistics.mean(xs) > best[0]):
                    best = (statistics.mean(xs), k, t)
        if not best:
            continue
        _, k, t = best
        print(f"dev buy {lo}-{hi:g}: best on design k=+{k} slots, T={t}s: {stats(run(hist, k, t, 1.0, lo, hi))}")
        print(f"{'':16s} TEST (forward):               {stats(run(fwd, k, t, 1.0, lo, hi))}")
        for kk in KS:
            print(f"{'':16s}   k=+{kk}: design {stats(run(hist, kk, t, 1.0, lo, hi))[:52]} | test {stats(run(fwd, kk, t, 1.0, lo, hi))[:52]}")
        print()


if __name__ == "__main__":
    main()
