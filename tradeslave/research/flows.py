"""Who actually makes money on fresh pump.fun tokens, and can we copy them?

    uv run python -m tradeslave.research.flows

Uses the exact chain trades from research.backfill_trades (replayed curve).
1. Money flow by role: creator wallet, wallets that sold more than they bought
   (tokens handed over by the creator's bundle), and everyone else by how many
   slots after launch they first bought (open bags valued at 0: worst case).
2. Skill persistence: is a wallet's ROI on the earlier half of tokens related
   to its ROI on the later half?
3. Copy test: follow the wallets that were skilled on one half on the other
   half, buying/selling d slots after they do (0.2 SOL, 1.25% fee per side,
   0.001 SOL per transaction), both directions.
"""

from __future__ import annotations

import bisect
import collections
import statistics

from .backfill_trades import START_VS as SV, START_VT as ST
from .dataset import load_backfill

FEE = 0.0125


def positions(backfill: dict) -> tuple[dict, dict]:
    pos, arrays = {}, {}
    for m, d in backfill.items():
        tr = d["trades"]
        if not tr:
            continue
        s0 = tr[0]["slot"] or 0
        dev = tr[0]["w"] if tr[0].get("create") else None
        vs, vt, slots, vss, vts = SV, ST, [], [], []
        for r in tr:
            dv, dc, slot = int(r["dv"]), int(r["dc"]), r["slot"] or 0
            vs += dv; vt += dc
            slots.append(slot); vss.append(vs); vts.append(vt)
            p = pos.setdefault((r["w"], m), dict(fb=None, fs=None, inn=0.0, out=0.0, bought=0, sold=0,
                                                 dev=r["w"] == dev, s0=s0, t0=d["t0"]))
            if dv > 0:
                p["inn"] += dv * (1 + FEE); p["bought"] += -dc
                if p["fb"] is None:
                    p["fb"] = slot
            else:
                p["out"] += -dv * (1 - FEE); p["sold"] += dc
                if p["fs"] is None and p["fb"] is not None:
                    p["fs"] = slot
        arrays[m] = (slots, vss, vts)
    return pos, arrays


def _line(label, ps):
    inn = sum(p["inn"] for p in ps)
    pnl = sum(p["out"] - p["inn"] for p in ps)
    if not ps or inn <= 0:
        return
    win = sum(p["out"] > p["inn"] for p in ps) / len(ps)
    print(f"  {label:42s} positions {len(ps):6d}  pnl {pnl/1e9:+9.1f} SOL  per SOL in {pnl/inn*100:+6.1f}%  win {win:.0%}")


def main() -> None:
    backfill = load_backfill()
    if not backfill:
        raise SystemExit("No backfilled trades. Run: uv run python -m tradeslave.research.backfill_trades")
    pos, arrays = positions(backfill)
    print(f"{len(arrays)} tokens, {len(pos)} wallet positions (realized SOL, fees included, open bags worth 0)\n")
    buyers = [p for p in pos.values() if p["inn"] > 0]
    _line("creator wallet", [p for p in buyers if p["dev"]])
    _line("sold more than bought (bundle wallets)", [p for p in buyers if not p["dev"] and p["sold"] > p["bought"] * 1.01])
    rest = [p for p in buyers if not p["dev"] and p["sold"] <= p["bought"] * 1.01]
    rest_ids = {id(p) for p in rest}
    for lo, hi, label in [(0, 0, "same slot as launch"), (1, 2, "1-2 slots"), (3, 5, "3-5 slots"),
                          (6, 25, "6-25 slots (2-10 s)"), (26, 150, "10-60 s"), (151, 10**12, "> 60 s")]:
        _line(f"others, first buy {label}", [p for p in rest if lo <= p["fb"] - p["s0"] <= hi])

    mints = sorted(arrays, key=lambda m: backfill[m]["t0"])
    halves = {"early": set(mints[: len(mints) // 2]), "late": set(mints[len(mints) // 2:])}

    def roi_by_wallet(half):
        w = collections.defaultdict(lambda: [0.0, 0.0, 0])
        for (wallet, m), p in pos.items():
            if m in halves[half] and id(p) in rest_ids:
                w[wallet][0] += p["out"] - p["inn"]; w[wallet][1] += p["inn"]; w[wallet][2] += 1
        return w

    early, late = roi_by_wallet("early"), roi_by_wallet("late")
    both = [(early[w][0] / early[w][1], late[w][0] / late[w][1]) for w in early
            if w in late and early[w][2] >= 5 and late[w][2] >= 5 and early[w][1] > 0 and late[w][1] > 0]
    if len(both) >= 10:
        ra = {v: i for i, v in enumerate(sorted(x[0] for x in both))}
        rb = {v: i for i, v in enumerate(sorted(x[1] for x in both))}
        n = len(both)
        rho = 1 - 6 * sum((ra[a] - rb[b]) ** 2 for a, b in both) / (n * (n * n - 1))
        print(f"\nskill persistence: {n} wallets with >= 5 trades in each half, rank correlation of ROI = {rho:+.2f}")

    def state(m, s):
        slots, vss, vts = arrays[m]
        i = bisect.bisect_right(slots, s) - 1
        return (vss[i], vts[i]) if i >= 0 else (SV, ST)

    def copy(m, fb, fs, d, size=0.2, net=0.001):
        a, b = state(m, fb + d)
        x = size * 1e9 * (1 - FEE)
        tok = b * x / (a + x)
        a, b = state(m, (fs + d) if fs is not None else arrays[m][0][-1])
        return (a * tok / (b + tok) * (1 - FEE) - net * 1e9) / (size * 1e9 + net * 1e9) - 1

    print("\ncopy test (skilled = >= 5 trades and ROI >= 10% on the training half):")
    for train, test, w_roi in (("early", "late", early), ("late", "early", late)):
        skilled = {w for w, v in w_roi.items() if v[2] >= 5 and v[1] > 0 and v[0] / v[1] >= 0.10}
        trades = [(m, p) for (w, m), p in pos.items() if w in skilled and m in halves[test] and id(p) in rest_ids]
        if not trades:
            continue
        theirs = sum(p["out"] - p["inn"] for _, p in trades) / sum(p["inn"] for _, p in trades)
        cells = []
        for d in (0, 1, 2, 4, 8):
            r = [copy(m, p["fb"], p["fs"], d) for m, p in trades]
            cells.append(f"d={d}: {statistics.mean(r)*100:+5.1f}%")
        print(f"  {train}->{test}: {len(skilled)} wallets, {len(trades)} trades; their ROI {theirs*100:+5.1f}%; "
              f"copying them (mean, delay in slots): " + "  ".join(cells))


if __name__ == "__main__":
    main()
