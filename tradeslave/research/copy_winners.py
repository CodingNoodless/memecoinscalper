"""Strategy N1: copy wallets that were profitable in the past, with our real latency.

    uv run python -m tradeslave.research.copy_winners

Winners are chosen on launches of the FIRST half only (>= MIN_TOKENS tokens there, ROI > MIN_ROI), then
copied on the SECOND half: when a winner buys a token (their first buy), we buy SIZE SOL at the first
trade >= LAG slots later; when they sell their last tokens we sell at the first trade >= LAG slots
after that sell (if they never sell in the window, we dump at the window end). All real trades are
replayed on the curve with our order inserted (own impact), 1.25% fee per side, PRIO per tx.
One copy per token (the first winner to buy it).
"""

from __future__ import annotations

import statistics

from .creator import stats
from .traders import FEE, VS0, VT0, load, positions

SIZE = 0.5
PRIO = 0.002


def replay(ts, buy_slot, sell_slot, size=SIZE):
    """Net SOL of buying at the first trade >= buy_slot and selling at the first trade >= sell_slot."""
    vs, vt = VS0, VT0
    bag, entered, proceeds = 0.0, False, None
    for r in ts:
        if not entered and r["slot"] >= buy_slot:
            net = size * (1 - FEE) * 1e9
            bag = vt * net / (vs + net)
            vs, vt, entered = vs + net, vt - bag, True
        if entered and proceeds is None and sell_slot is not None and r["slot"] >= sell_slot:
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


def main() -> None:
    data = load()
    t0s = {m: ts[0]["t"] for m, ts in data.items()}
    order = sorted(data, key=lambda m: t0s[m])
    cut = t0s[order[len(order) // 2]]
    pos = {m: positions(data[m]) for m in order}
    hist = {}
    for m in order:
        if t0s[m] < cut:
            for w, p in pos[m].items():
                if p["in"] > 0:
                    h = hist.setdefault(w, [0, 0.0, 0.0])
                    h[0] += 1
                    h[1] += p["pnl"]
                    h[2] += p["in"]
    test = [m for m in order if t0s[m] >= cut]
    print(f"{len(order)} launches; winners chosen on {len(order) - len(test)}, copied on {len(test)}\n")
    for min_tokens, min_roi in ((10, 0.05), (10, 0.20), (20, 0.10), (5, 0.30)):
        winners = {w for w, (n, pnl, inv) in hist.items() if n >= min_tokens and pnl / inv > min_roi}
        for lag in (0, 1, 3, 5, 10):
            xs, theirs = [], []
            for m in test:
                ts = data[m]
                cands = []
                for w in [w for w in winners & pos[m].keys() if pos[m][w]["in"] > 0]:
                    first = next(r for r in ts if r["w"] == w and r["b"])
                    sells = [r for r in ts if r["w"] == w and not r["b"]]
                    last_sell = sells[-1]["slot"] if sells and not pos[m][w]["held_out"] else None
                    cands.append((first["slot"], w, last_sell))
                if not cands:
                    continue
                slot, w, last_sell = min(cands)
                v = replay(ts, slot + lag, last_sell + lag if last_sell is not None else None)
                if v is not None:
                    xs.append(v)
                    theirs.append(pos[m][w]["pnl"] / pos[m][w]["in"])
            if lag == 0:
                print(f"winners: >= {min_tokens} tokens & ROI > {min_roi:.0%} in half 1 -> {len(winners)} wallets; "
                      f"their own half-2 ROI on copied tokens {statistics.mean(theirs):+.1%} (mean per token)")
            print(f"   copy {SIZE} SOL, lag {lag:2d} slots: {stats(xs)} | ROI {sum(xs) / (SIZE * len(xs)) if xs else 0:+.1%}")
        print()


if __name__ == "__main__":
    main()
