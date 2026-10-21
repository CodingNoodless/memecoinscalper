"""PGD0: Phase-0 proxy of SPEC.md's post-graduation dip bot on 1-min PumpSwap candles (pre-registered; vault:
"2026-10-21 post-graduation dip spec (PGD0)").

    uv run python -m tradeslave.research.postgrad_dip

Price-structure entry rules E1-E7, E10 (1-min proxies) and exits X2-X7 with the spec's default parameters.
Wallet / forensic rules (E8, E9, E11, Tier A/B) and E12 can't be replayed from candles. No tuning.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter

from .shock import K_POOL, fee, load

FIX_SOL, SIZE_SOL = 0.0001, 0.1
SIDE_FALLBACK = 0.015


def signal(c, tg):
    """Index of the first candle where E1-E7, E10 hold, or (None, reason histogram of the binding rule)."""
    H, Lo, kLo = 0.0, math.inf, -1
    vols = []
    fails = Counter()
    for i, (t, o, h, l, cl, v) in enumerate(c):
        age = t + 60 - tg                                   # at candle close
        if h > H:
            H, Lo, kLo = h, math.inf, -1
        if l < Lo:
            Lo, kLo = l, i
        vols.append((t, v))
        if age < 300:
            continue
        if age > 90 * 60:
            return None, fails
        dd = 1 - Lo / H
        if dd > 0.75:
            fails["E_DEAD"] += 1
            return None, fails
        if age < 360:
            fails["E1_TIME"] += 1
            continue
        if dd < 0.35:
            fails["E2_DD"] += 1
            continue
        if i - kLo < 2:
            fails["E3_BASE_AGE"] += 1
            continue
        b = cl / Lo - 1
        if not 0.08 <= b <= 0.35:
            fails["E4_BOUNCE"] += 1
            continue
        if l < Lo * 1.03:
            fails["E5_HL"] += 1
            continue
        post = [(tt, vv) for tt, vv in vols if tt + 60 - tg > 300]
        roll = [sum(vv for tt, vv in post if t2 - 120 <= tt <= t2) for t2, _ in post]
        if roll and roll[-1] > 0.40 * max(roll):
            fails["E6_SELLERS"] += 1
            continue
        if cl <= o:
            fails["E7_FLOW"] += 1
            continue
        if math.sqrt(K_POOL * cl) < 25:
            fails["E10_LIQ"] += 1
            continue
        if i + 1 >= len(c) or c[i + 1][0] != t + 60:
            fails["NO_NEXT"] += 1
            continue
        return (i, Lo), fails
    return None, fails


def side_cost(p, mode):
    if mode == "fallback":
        return SIDE_FALLBACK
    reserve = math.sqrt(K_POOL * p)
    return fee(p * 1e9) + SIZE_SOL / reserve


def trade(c, i, Lo, mode):
    e = c[i + 1][1]
    t0 = c[i + 1][0]
    rt = 2 * side_cost(e, mode) + 2 * FIX_SOL / SIZE_SOL
    stop = max(e * 0.75, Lo * 0.92)
    left, got, peak, tp1, tp2, why = 1.0, 0.0, e, False, False, "X7_MAXHOLD"
    maxgain = 0.0

    def sell(frac, px):
        nonlocal left, got
        got += frac * (px / e) * (1 - side_cost(px, mode)) - frac * 0 - FIX_SOL / SIZE_SOL
        left -= frac

    for t, o, h, l, cl, v in c[i + 1:]:
        if t > t0 + 24 * 3600:
            break
        if l <= stop:
            sell(left, min(stop, o) * 0.95)
            why = "X2_STOP"
            break
        if not tp1 and h >= e * 2:
            sell(0.5, e * 2)
            tp1 = True
            stop = max(stop, e * (1 + rt))
        if tp1 and not tp2 and h >= e * 4:
            sell(0.3, e * 4)
            tp2 = True
        peak = max(peak, h)
        maxgain = max(maxgain, h / e - 1)
        if tp2 and cl <= peak * 0.65:
            sell(left, cl)
            why = "X5_TRAIL"
            break
        if t - t0 >= 3600 - 60 and maxgain < 0.25:
            sell(left, cl)
            why = "X6_TIME"
            break
        last = cl
    else:
        why = "END"
    if left > 1e-9:
        sell(left, last)
    gross_in = 1 + side_cost(e, mode)                       # paid fee on the way in
    return got / gross_in - 1, why


def stats(v):
    if len(v) < 3:
        return f"n {len(v)}"
    m, sd = statistics.mean(v), statistics.stdev(v)
    w, ls = [x for x in v if x > 0], [x for x in v if x <= 0]
    pf = sum(w) / -sum(ls) if ls and sum(ls) else float("inf")
    top2 = sorted(v)[:-2]
    return (f"n {len(v):4d} mean {m:+.2%} median {statistics.median(v):+.2%} t {m / sd * math.sqrt(len(v)):+.2f} "
            f"win {len(w) / len(v):.0%} PF {pf:.2f} w/o top2 {statistics.mean(top2):+.2%} "
            f"avg win {statistics.mean(w) if w else 0:+.1%} avg loss {statistics.mean(ls) if ls else 0:+.1%}")


def main() -> None:
    toks = load()
    cut = toks[int(len(toks) * 0.6)][0]
    fails, out = Counter(), {"tune": [], "hold": []}
    for tg, m, c in toks:
        s, f = signal(c, tg)
        fails.update(f)
        if s:
            i, Lo = s
            split = "tune" if tg < cut else "hold"
            out[split].append((m, trade(c, i, Lo, "fallback"), trade(c, i, Lo, "tiers")))
    print(f"graduations {len(toks)}; signals tune {len(out['tune'])} hold {len(out['hold'])}")
    print("binding-rule counts (candle evaluations failing at that rule):", dict(fails.most_common()))
    for split in ("tune", "hold"):
        xs = out[split]
        print(f"\n== {split.upper()}")
        print(f"  spec fallback 1.5%/side: {stats([a[0] for _, a, _ in xs])}")
        print(f"  official tiers + impact @0.1 SOL: {stats([b[0] for _, _, b in xs])}")
        print("  exits:", dict(Counter(a[1] for _, a, _ in xs)))
    v = [a[0] for _, a, _ in out["hold"]]
    if v:
        w, ls = [x for x in v if x > 0], [x for x in v if x <= 0]
        gate = dict(n=len(v) >= 60, mean=statistics.mean(v) >= 0.05,
                    pf=(sum(w) / -sum(ls) if ls else 9) >= 1.3, top2=statistics.mean(sorted(v)[:-2]) > 0 if len(v) > 2 else False)
        print("\nspec gate on holdout:", gate, "| worth-building bar (mean >= 0):", statistics.mean(v) >= 0)


if __name__ == "__main__":
    main()
