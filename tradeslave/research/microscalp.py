"""Strategy P: next-slot micro-scalping, learned from outcomes.

    uv run python -m tradeslave.research.microscalp

Measured 2026-10-18 (research.latency_probe): a plain Helius websocket (logsSubscribe, processed) shows
pump.fun trades while their slot is still the tip, median 100 ms into the slot. So the bot can decide at
the end of slot s and (with a priority fee / Jito tip) land at the FRONT of slot s+1.

Decision point = the end of a slot with at least one trade, token age 3 s..15 min. Action: buy SIZE at
the front of slot s+1 (state after every trade in slots <= s), sell at the front of slot s+1+H (state
after every trade in slots <= s+H, our bag still in the curve). Costs: 1.25% curve fee per side,
TIP per transaction (priority + Jito tip, assumption). Model: HistGradientBoosting regressor on the
net return, fit on the first half of the 5,140-launch exact-trade set (time order), scored on the
second half, then on the unbiased random launches (research.whale_wave.unbiased) that it never saw.
"""

from __future__ import annotations

import math
import random
import statistics
from bisect import bisect_right

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor

from .imitate import FEATS, State, norm_training
from .whale_wave import unbiased

VS0, VT0 = 30e9, 1073e12
K = VS0 * VT0
FEE = 0.0125
TIP = 0.0003                 # SOL per transaction (priority fee + Jito tip), assumption
SIZE = 0.5
HS = (3, 8, 25)              # hold in slots (~1.2 s, 3 s, 10 s)
SLOT_FEATS = ["slot_buys", "slot_sells", "slot_buy_sol", "slot_sell_sol", "slot_max_buy", "slot_n"]


def points(tr: list[tuple], rng: random.Random, keep: float):
    """Yield (features, {H: net return}) at slot ends."""
    st = State(tr[0])
    slots = [r[1] for r in tr]
    vts, vt = [], VT0
    for r in tr:
        vt += -r[5] * 1e6 if r[3] else r[5] * 1e6
        vts.append(max(vt, 1e12))
    n = len(tr)
    i = 0
    while i < n:
        s = tr[i][1]
        j = bisect_right(slots, s)            # trades i..j-1 are slot s
        for r in tr[i:j]:
            st.add(r)
        age = tr[j - 1][0] - st.tc
        if 3 <= age <= 900 and j < n and st.vs < 100e9 and rng.random() < keep:
            grp = tr[i:j]
            f = st.features(tr[j - 1][0]) + [
                sum(1 for r in grp if r[3]), sum(1 for r in grp if not r[3]), sum(r[4] for r in grp if r[3]),
                sum(r[4] for r in grp if not r[3]), max((r[4] for r in grp if r[3]), default=0.0), len(grp)]
            vs_e = K / vts[j - 1]
            net = SIZE * (1 - FEE) * 1e9
            tokens = (K / vs_e) * net / (vs_e + net)
            y = {}
            for h in HS:
                k = bisect_right(slots, s + h) - 1         # last trade in slots <= s+h
                vs2 = K / vts[k] + net
                vt2 = K / vs2
                out = vs2 * tokens / (vt2 + tokens) / 1e9 * (1 - FEE)
                y[h] = (out - SIZE - 2 * TIP) / SIZE
            yield f, y
        i = j


def build(toks, keep, seed):
    rng = random.Random(seed)
    X, Y, T = [], {h: [] for h in HS}, []
    for t0, m, tr in toks:
        for f, y in points(tr, rng, keep):
            X.append(f)
            T.append(m)
            for h in HS:
                Y[h].append(y[h])
    return np.array(X), {h: np.array(v) for h, v in Y.items()}, T


def report(lbl, pred, y, toks_of):
    order = np.argsort(-pred)
    print(f"  {lbl}: all points mean {y.mean():+.2%}")
    for frac in (0.0005, 0.001, 0.005, 0.01, 0.05):
        k = max(5, int(frac * len(order)))
        sel = order[:k]
        xs = y[sel]
        sd = xs.std()
        uniq = len({toks_of[i] for i in sel})
        print(f"    top {frac:6.2%} (n {k:6d}, tokens {uniq:5d}): mean {xs.mean():+6.2%} med {np.median(xs):+6.2%} "
              f"win {np.mean(xs > 0):4.0%} t {xs.mean() / (sd / math.sqrt(k)) if sd else 0:+5.2f}")


def main() -> None:
    busy = sorted((ts[0][0], m, ts) for m, ts in norm_training().items())
    h = len(busy) // 2
    Xa, Ya, _ = build(busy[:h], 0.25, 1)
    Xb, Yb, Tb = build(busy[h:], 0.25, 2)
    ub = unbiased()
    seen = {m for _, m, _ in busy}
    ub = [x for x in ub if x[1] not in seen]
    Xu, Yu, Tu = build(ub, 1.0, 3)
    print(f"points: train {len(Xa)}, busy test {len(Xb)}, unbiased {len(Xu)} ({len(ub)} launches)")
    for hh in HS:
        m = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=200,
                                          l2_regularization=1.0, random_state=0).fit(Xa, Ya[hh])
        print(f"\nhold {hh} slots:")
        report("busy 2nd half", m.predict(Xb), Yb[hh], Tb)
        if len(Xu):
            report("unbiased", m.predict(Xu), Yu[hh], Tu)
    _ = (FEATS, SLOT_FEATS, statistics)


if __name__ == "__main__":
    main()
