"""Strategy M3: the settlement-average edge in the last 90 s of Kalshi 15-minute crypto markets.

    uv run python -m tradeslave.research.kalshi_final fetch     # Kraken trades around each market's close
    uv run python -m tradeslave.research.kalshi_final

Settlement A = mean of the index over the final 60 s. At time t inside that window part of A is already
realized: with e seconds elapsed (partial mean M_e) and r = 60 - e left, A = (e * M_e + r * avg_rest) / 60,
and avg_rest ~ S_t with variance sigma_s^2 * r / 3, so sd(A) = (r / 60) * sigma_s * sqrt(r / 3).
Before the window (60 < tau <= 90 s): sd(A)^2 = sigma_s^2 * (tau - 60 + 20).
p = Phi((E[A] - K) / sd(A)). Spot = Kraken trades (a CF Benchmarks constituent), as a 1-second series.

Fills: a real Kalshi trade where the taker bought side X at price q at time t means X was buyable at q at t;
we "take" it if p_X - q >= EDGE (only such taker-buy trades are used, so the price was a real ask), with
LATENCY seconds of spot staleness (our p uses spot up to t - LATENCY). Taker fee; held to settlement.
"""

from __future__ import annotations

import bisect
import json
import math
import statistics
import sys
import time

import requests

from .kalshi_fv import fee, phi
from .kalshi_maker import TICKS

KRAKEN = {"KXBTC15M": "XBTUSD", "KXETH15M": "ETHUSD", "KXDOGE15M": "XDGUSD", "KXXRP15M": "XRPUSD", "KXSOL15M": "SOLUSD"}
SPOT = TICKS.parent / "kraken"
EDGES = (0.05, 0.10, 0.20)
LATENCY = 1.0


def fetch() -> None:
    SPOT.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    for series, pair in KRAKEN.items():
        p = TICKS / f"{series}.jsonl"
        if not p.exists():
            continue
        out = SPOT / f"{series}.json"
        have = json.loads(out.read_text()) if out.exists() else {}
        for line in p.open(encoding="utf-8"):
            m = json.loads(line)
            if m["ticker"] in have:
                continue
            since, rows = int((m["close"] - 3600 - 180) * 1e9), []     # 1 h of history for sigma + the final window
            while True:
                try:
                    js = s.get("https://api.kraken.com/0/public/Trades", params={"pair": pair, "since": since}, timeout=30).json()
                except requests.RequestException:
                    time.sleep(3)
                    continue
                if js.get("error"):
                    time.sleep(3)
                    continue
                res = js["result"]
                key = next(k for k in res if k != "last")
                batch = [(float(x[2]), float(x[0])) for x in res[key]]
                rows += [b for b in batch if b[0] <= m["close"] + 5]
                since = int(res["last"])
                time.sleep(1.1)
                if not batch or batch[-1][0] > m["close"] + 5 or len(batch) < 1000:
                    break
            have[m["ticker"]] = rows
            out.write_text(json.dumps(have))
        print(f"{series}: kraken windows for {len(have)} markets", flush=True)


class Px:
    def __init__(self, rows):
        rows = sorted(rows)
        self.t = [r[0] for r in rows]
        self.p = [r[1] for r in rows]

    def at(self, t):
        i = bisect.bisect_right(self.t, t) - 1
        return self.p[i] if i >= 0 else None

    def mean(self, a, b):
        xs = [self.at(a + k + 0.5) for k in range(int(b - a))]
        xs = [x for x in xs if x]
        return statistics.mean(xs) if xs else None

    def sigma_s(self, t):
        ms = [self.at(t - 60 * k) for k in range(61)]
        ms = [x for x in ms if x]
        rs = [math.log(a / b) for a, b in zip(ms, ms[1:])]
        return statistics.pstdev(rs) / math.sqrt(60) if len(rs) > 30 else None


def prob_yes(px: Px, k: float, close: float, t: float):
    st, sig = px.at(t), px.sigma_s(t)
    if not st or not sig:
        return None
    tau = close - t
    if tau > 60:
        mu, sd = st, st * sig * math.sqrt(tau - 60 + 20)
    else:
        e, r = 60 - tau, tau
        me = px.mean(close - 60, t) if e >= 1 else st
        mu = (e * me + r * st) / 60
        sd = st * (r / 60) * sig * math.sqrt(max(r, 0.5) / 3)
    return phi((mu - k) / max(sd, 1e-12))


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "fetch":
        fetch()
        return
    strikes = {}
    for series in KRAKEN:
        p = TICKS.parent / f"{series}.jsonl"
        if p.exists():
            for line in p.open(encoding="utf-8"):
                m = json.loads(line)
                strikes[m["ticker"]] = m["strike"]
    res = {e: [] for e in EDGES}
    for series in KRAKEN:
        sp, tp = SPOT / f"{series}.json", TICKS / f"{series}.jsonl"
        if not sp.exists():
            continue
        spot = json.loads(sp.read_text())
        for line in tp.open(encoding="utf-8"):
            m = json.loads(line)
            if m["ticker"] not in spot or not strikes.get(m["ticker"]):
                continue
            px, k = Px(spot[m["ticker"]]), strikes[m["ticker"]]
            done = set()
            for t, yp, cnt, taker in m["t"]:
                tau = m["close"] - t
                if not 1 <= tau <= 90:
                    continue
                side = "yes" if taker == "yes" else "no"
                q = yp if side == "yes" else 1 - yp
                if not 0 < q < 1:
                    continue
                py = prob_yes(px, k, m["close"], t - LATENCY)
                if py is None:
                    continue
                p_side = py if side == "yes" else 1 - py
                win = 1.0 if m["result"] == side else 0.0
                for e in EDGES:
                    if e not in done and p_side - q >= e:
                        res[e].append((series, m["open"], win - q - fee(q)))
                        done.add(e)                  # first opportunity per market per edge
    for e in EDGES:
        xs = [r[2] for r in res[e]]
        if len(xs) < 5:
            print(f"edge {e}: n={len(xs)}")
            continue
        mm, sd = statistics.mean(xs), statistics.pstdev(xs)
        print(f"edge {e:.2f}: n={len(xs):4d} mean {mm * 100:+6.2f}c/contract win {sum(x > 0 for x in xs) / len(xs):4.0%} "
              f"t {mm / (sd / math.sqrt(len(xs))) if sd else 0:+5.2f}")
        for series in KRAKEN:
            ys = [r[2] for r in res[e] if r[0] == series]
            if ys:
                print(f"     {series:10s} n={len(ys):4d} mean {statistics.mean(ys) * 100:+6.2f}c win {sum(y > 0 for y in ys) / len(ys):4.0%}")


if __name__ == "__main__":
    main()
