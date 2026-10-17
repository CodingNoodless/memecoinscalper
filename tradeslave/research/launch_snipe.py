"""Selective launch sniping: is buying a few slots after launch profitable for SOME launches?

    uv run python -m tradeslave.research.launch_snipe

Uses only the unbiased random sample of ALL launches (autopilot backfills it:
`autopilot_launch_sample`), because judging on tokens that later turned out
active flatters every launch-time strategy. Entry = state after slot
launch+k (our feed sees a launch ~0.5 s after its block; a transaction lands
~0.5-1 s later, so k=4..6 is realistic without paid infrastructure), exit
after a fixed hold, 1.25% fee per side, 0.001 SOL per transaction.
Only facts known at launch are used to select (dev buy, mayhem, creator
launch count). A rule counts only if mean AND median are positive in both
the earlier and the later half.
"""

from __future__ import annotations

import bisect
import collections
import statistics

from ..scalper.recorder import TICK_DIR, read_ticks
from .backfill_trades import START_VS as SV, START_VT as ST, sampled_mints
from .dataset import load_backfill

FEE = 0.0125


def snipe_returns(trades, ks=(1, 2, 4, 6, 10), holds=(10, 30, 120), size=0.2, net=0.001):
    if not trades or not trades[0].get("create"):
        return None
    s0 = trades[0]["slot"] or 0
    slots, vs, vt = [], [], []
    a, b = SV, ST
    for r in trades:
        a += int(r["dv"]); b += int(r["dc"])
        slots.append(r["slot"] or 0); vs.append(a); vt.append(b)

    def state(s):
        i = bisect.bisect_right(slots, s) - 1
        return (vs[i], vt[i]) if i >= 0 else (SV, ST)

    out = {}
    for k in ks:
        x, y = state(s0 + k)
        lam = size * 1e9 * (1 - FEE)
        tok = y * lam / (x + lam)
        for h in holds:
            x2, y2 = state(s0 + k + int(h / 0.4))
            x2, y2 = x2 + lam, y2 - tok          # our buy is still in the curve when we sell (own impact)
            got = x2 * tok / (y2 + tok) * (1 - FEE)
            out[(k, h)] = (got - net * 1e9) / (size * 1e9 + net * 1e9) - 1
    return out


def main() -> None:
    bf = load_backfill()
    chosen = sampled_mints("all") & bf.keys()
    if len(chosen) < 100:
        raise SystemExit(f"Only {len(chosen)} unbiased launches fetched so far; the autopilot adds "
                         f"~300 per cycle (needs RESEARCH_RPC_URL). Try again later.")
    info, launches = {}, collections.defaultdict(list)
    for r in read_ticks(sorted(TICK_DIR.glob("*.jsonl.gz"))):
        if r.get("k") == "create":
            launches[r.get("cr")].append(float(r["t"]))
            if r["m"] in chosen:
                info[r["m"]] = dict(ib=float(r.get("ib") or 0), mh=str(r.get("mh")) in ("1", "True", "true"),
                                    cr=r.get("cr"), t0=float(r["t"]))
    rows = []
    for m in chosen:
        ret = snipe_returns(bf[m]["trades"])
        if ret is None or m not in info:
            continue
        i = info[m]
        prior = sum(1 for t in launches[i["cr"]] if i["t0"] - 3600 <= t < i["t0"])
        rows.append(dict(t0=i["t0"], ib=i["ib"], mh=i["mh"], prior=prior, r=ret))
    rows.sort(key=lambda x: x["t0"])
    half = len(rows) // 2
    print(f"{len(rows)} unbiased launches (random draw from all launches). Mean / median per rule, early | late half\n")
    rules = {"all launches": lambda x: True, "not mayhem": lambda x: not x["mh"],
             "dev buy < 0.5 SOL": lambda x: x["ib"] < 0.5, "dev buy 0.5-1.5": lambda x: 0.5 <= x["ib"] < 1.5,
             "dev buy 1.5-3": lambda x: 1.5 <= x["ib"] < 3, "dev buy >= 3": lambda x: x["ib"] >= 3,
             "creator's first launch in 1h": lambda x: x["prior"] == 0, "serial creator": lambda x: x["prior"] > 0}
    winners = []
    # +1/+2 slots need paid fast infrastructure (block-0 feed + staked sending); shown to price that decision.
    for (k, h) in [(1, 10), (1, 30), (2, 30), (4, 10), (4, 30), (6, 30), (6, 120), (10, 30)]:
        print(f"buy at launch slot +{k}, sell after {h}s")
        for name, f in rules.items():
            cells, ok = [], True
            for part in (rows[:half], rows[half:]):
                v = [x["r"][(k, h)] for x in part if f(x)]
                if len(v) < 20:
                    cells.append("   n<20"); ok = False
                    continue
                mean, med = statistics.mean(v), statistics.median(v)
                ok = ok and mean > 0 and med > 0
                cells.append(f"n={len(v):4d} {mean*100:+5.1f}%/{med*100:+5.1f}%")
            print(f"  {name:30s} " + " | ".join(cells) + ("   <== both halves positive" if ok else ""))
            if ok:
                winners.append(f"{name} @ +{k} slots, {h}s")
    print("\nVERDICT:", ("candidates: " + "; ".join(winners)) if winners else
          "no launch-time rule is positive (mean and median) in both halves")


if __name__ == "__main__":
    main()
