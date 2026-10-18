"""Strategy M1: trade Kalshi 15-minute crypto markets against a spot-based fair value (pre-registered;
vault: "2026-10-17 Kalshi 15-minute fair value").

    uv run python -m tradeslave.research.kalshi_fv
"""

from __future__ import annotations

import bisect
import json
import math
import statistics

from .kalshi_data import OUT, SERIES

EDGES = (0.03, 0.05, 0.08, 0.12)
DESIGN_FRAC = 0.6


def fee(p: float, mult: float = 1.0) -> float:
    return math.ceil(0.07 * mult * p * (1 - p) * 100 - 1e-9) / 100     # per contract, rounded up to a cent


def phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


class Spot:
    def __init__(self, coin: str):
        rows = json.loads((OUT / f"spot_{coin}.json").read_text())
        self.t = [r[0] + 60 for r in rows]             # candle START + 60 = the minute its close belongs to
        self.c = [r[4] for r in rows]

    def at(self, t: float):
        i = bisect.bisect_right(self.t, t) - 1
        return self.c[i] if i >= 0 and t - self.t[i] <= 120 else None

    def sigma(self, t: float, n: int = 60):
        i = bisect.bisect_right(self.t, t) - 1
        if i < n:
            return None
        xs = [math.log(self.c[k] / self.c[k - 1]) for k in range(i - n + 1, i + 1) if self.c[k - 1] > 0]
        return statistics.pstdev(xs) if len(xs) > 30 else None


def fair(spot: Spot, k: float, t: float, close: float):
    s, sig = spot.at(t), spot.sigma(t)
    if not s or not sig or not k:
        return None
    tau = close - t
    var = sig ** 2 * max(tau - 60 + 20, 20) / 60
    return phi(math.log(s / k) / math.sqrt(var))


def trades(ms: list[dict], spot: Spot, edge: float, delay: int = 0, fee_mult: float = 1.0):
    """[(open time, net $ per contract)] for each market's first signal."""
    out = []
    for m in ms:
        cs = [c for c in m["c"] if c[1] is not None and c[2] is not None]
        for j, c in enumerate(cs):
            t = c[0]
            if t < m["open"] + 60 or t > m["close"] - 60:
                continue
            p = fair(spot, m["strike"], t, m["close"])
            if p is None:
                continue
            q = cs[j + delay] if j + delay < len(cs) else None
            if q is None or q[0] > m["close"] - 30:
                break
            bid, ask = q[1], q[2]
            win = m["result"] == "yes"
            if 0 < ask < 1 and p - ask >= edge:
                out.append((m["open"], (1.0 if win else 0.0) - ask - fee(ask, fee_mult)))
                break
            if 0 < bid < 1 and bid - p >= edge:
                price = 1 - bid
                out.append((m["open"], (0.0 if win else 1.0) - price - fee(price, fee_mult)))
                break
    return out


def stats(xs) -> str:
    if len(xs) < 5:
        return f"n={len(xs)}"
    m, s = statistics.mean(xs), statistics.pstdev(xs)
    return (f"n={len(xs):5d} mean {m * 100:+6.2f}c/contract win {sum(x > 0 for x in xs) / len(xs):4.0%} "
            f"t {m / (s / math.sqrt(len(xs))) if s else 0:+5.2f} total ${sum(xs):+8.2f}")


def load(series: str) -> list[dict]:
    p = OUT / f"{series}.jsonl"
    if not p.exists():
        return []
    ms = [json.loads(x) for x in p.open(encoding="utf-8")]
    return sorted((m for m in ms if m.get("strike") and m["c"]), key=lambda m: m["open"])


def main() -> None:
    pooled = {"design": {e: [] for e in EDGES}}
    data = {}
    for series, coin in SERIES.items():
        ms = load(series)
        if not ms or not (OUT / f"spot_{coin}.json").exists():
            continue
        spot = Spot(coin)
        cut = ms[0]["open"] + DESIGN_FRAC * (ms[-1]["open"] - ms[0]["open"])
        data[series] = (ms, spot, cut)
        for e in EDGES:
            pooled["design"][e] += [r[1] for r in trades([m for m in ms if m["open"] < cut], spot, e)]
    for e in EDGES:
        print(f"pooled DESIGN edge {e:.2f}: {stats(pooled['design'][e])}")
    best = max(EDGES, key=lambda e: statistics.mean(pooled["design"][e]) if pooled["design"][e] else -9)
    print(f"\n-> edge chosen on design: {best}\n")
    allt, all_delay, all_fee2 = [], [], []
    for series, (ms, spot, cut) in data.items():
        test = [m for m in ms if m["open"] >= cut]
        rows = trades(test, spot, best)
        d1 = trades(test, spot, best, delay=1)
        f2 = trades(test, spot, best, fee_mult=2.0)
        allt += rows
        all_delay += d1
        all_fee2 += f2
        print(f"{series:10s} TEST {stats([r[1] for r in rows])} | 60 s delay {stats([r[1] for r in d1])[:40]} | 2x fee {stats([r[1] for r in f2])[:40]}")
    allt.sort()
    h = len(allt) // 2
    print(f"\nPOOLED TEST  {stats([r[1] for r in allt])}")
    print(f"  halves     {stats([r[1] for r in allt[:h]])} | {stats([r[1] for r in allt[h:]])}")
    print(f"  60 s delay {stats([r[1] for r in all_delay])}")
    print(f"  2x fees    {stats([r[1] for r in all_fee2])}")


if __name__ == "__main__":
    main()
