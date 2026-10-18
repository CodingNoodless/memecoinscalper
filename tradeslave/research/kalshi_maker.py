"""Strategy M2: are resting (maker) orders profitable on Kalshi 15-minute crypto markets?

    uv run python -m tradeslave.research.kalshi_maker fetch --per-series 300
    uv run python -m tradeslave.research.kalshi_maker

Every Kalshi trade records the taker's side. A trade where the taker SOLD yes (taker_side "no") filled a
resting YES bid at yes_price; one where the taker bought yes filled a resting NO bid at 1 - yes_price.
So the realized P&L of resting bids, adverse selection included, is measurable trade by trade:
outcome - price - maker fee (0.0175 x P x (1-P), Kalshi's maker rate; charged per fill, rounded up per order).
"""

from __future__ import annotations

import json
import math
import random
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime

from .kalshi_data import KALSHI, OUT, SERIES, Http

TICKS = OUT / "trades"


def iso(x: str) -> float:
    x = x.replace("Z", "+00:00")
    m = re.match(r"(.*?)(\.\d+)?(\+00:00)$", x)
    frac = (m.group(2) or ".0")[1:7].ljust(6, "0")
    return datetime.fromisoformat(f"{m.group(1)}.{frac}+00:00").timestamp()


def fetch(per_series: int) -> None:
    TICKS.mkdir(parents=True, exist_ok=True)
    h = Http(8.0)
    rng = random.Random(7)
    for series in ("KXDOGE15M", "KXXRP15M", "KXSOL15M", "KXETH15M"):    # BTC: ~10k+ trades per market, too slow
        p = OUT / f"{series}.jsonl"
        if not p.exists():
            continue
        ms = [json.loads(x) for x in p.open(encoding="utf-8")]
        out = TICKS / f"{series}.jsonl"
        have = {json.loads(x)["ticker"] for x in out.open(encoding="utf-8")} if out.exists() else set()
        pick = [m for m in rng.sample(ms, min(per_series, len(ms))) if m["ticker"] not in have]
        with out.open("a", encoding="utf-8") as fh:
            for m in pick:
                cursor, tr = None, []
                while True:
                    params = {"ticker": m["ticker"], "limit": 1000}
                    if cursor:
                        params["cursor"] = cursor
                    js = h.get(f"{KALSHI}/markets/trades", params) or {}
                    tr += js.get("trades") or []
                    cursor = js.get("cursor")
                    if not cursor or not js.get("trades"):
                        break
                rows = sorted((iso(x["created_time"]), float(x["yes_price_dollars"]), float(x["count_fp"]), x["taker_side"])
                              for x in tr)
                fh.write(json.dumps({"ticker": m["ticker"], "open": m["open"], "close": m["close"], "result": m["result"],
                                     "t": rows}) + "\n")
        print(f"{series}: {len(pick)} markets fetched", flush=True)


def maker_fee(p: float) -> float:
    return 0.0175 * p * (1 - p)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "fetch":
        fetch(int(sys.argv[3]) if len(sys.argv) > 3 else 300)
        return
    for series in SERIES:
        p = TICKS / f"{series}.jsonl"
        if not p.exists():
            continue
        ms = sorted((json.loads(x) for x in p.open(encoding="utf-8")), key=lambda m: m["open"])
        per = defaultdict(lambda: [0.0, 0.0])          # (bucket) -> [pnl $, contracts]
        per_market = []
        for m in ms:
            y = 1.0 if m["result"] == "yes" else 0.0
            pnl_m = vol_m = 0.0
            for t, yp, cnt, taker in m["t"]:
                left = m["close"] - t
                if taker == "no":                     # taker sold YES -> resting YES bid filled at yp
                    price, win = yp, y
                else:                                 # taker bought YES -> resting NO bid filled at 1 - yp
                    price, win = 1 - yp, 1 - y
                if not 0 < price < 1:
                    continue
                v = (win - price - maker_fee(price)) * cnt
                tb = "<1m" if left < 60 else "1-5m" if left < 300 else "5-10m" if left < 600 else ">10m"
                pb = "<20c" if price < 0.2 else "20-80c" if price <= 0.8 else ">80c"
                for key in ((tb, pb), (tb, "all"), ("all", pb), ("all", "all")):
                    per[key][0] += v
                    per[key][1] += cnt
                pnl_m += v
                vol_m += cnt
            if vol_m:
                per_market.append(pnl_m / vol_m)
        print(f"\n{series}: {len(ms)} markets; maker P&L in cents per contract filled (all resting bids, both sides)")
        for tb in ("all", ">10m", "5-10m", "1-5m", "<1m"):
            cells = []
            for pb in ("all", "<20c", "20-80c", ">80c"):
                pnl, n = per.get((tb, pb), (0, 0))
                if n:
                    cells.append(f"{pb}: {pnl / n * 100:+.2f}c ({n / 1e3:.0f}k)")
            print(f"  {tb:6s} " + " | ".join(cells))
        if len(per_market) > 5:
            mm, sd = statistics.mean(per_market), statistics.pstdev(per_market)
            h = len(per_market) // 2
            print(f"  per-market mean {mm * 100:+.2f}c, t {mm / (sd / math.sqrt(len(per_market))):+.2f}; "
                  f"halves {statistics.mean(per_market[:h]) * 100:+.2f}c / {statistics.mean(per_market[h:]) * 100:+.2f}c")


if __name__ == "__main__":
    main()
