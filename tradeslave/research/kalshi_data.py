"""Kalshi 15-minute crypto up/down markets + Coinbase 1-minute spot, for strategy M (fair-value trading).

    uv run python -m tradeslave.research.kalshi_data --days 45

Kalshi (public API, no key): every settled market of each series (strike = the 60 s BRTI average before
the open, result yes/no) and its per-minute candles (yes bid / yes ask / last price closes, volume).
Coinbase Exchange (public, US): 1-minute candles of the underlying, as a stand-in for CF Benchmarks' BRTI.
Output: data/kalshi/<SERIES>.jsonl (one market per line), data/kalshi/spot_<COIN>.json.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

from ..paths import DATA_DIR

OUT = DATA_DIR / "kalshi"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
COINBASE = "https://api.exchange.coinbase.com"
SERIES = {"KXBTC15M": "BTC", "KXETH15M": "ETH", "KXSOL15M": "SOL", "KXXRP15M": "XRP", "KXDOGE15M": "DOGE"}


class Http:
    def __init__(self, rps: float):
        self.gap, self.last, self.lock = 1.0 / rps, 0.0, threading.Lock()
        self.s = requests.Session()
        self.s.headers["Accept"] = "application/json"

    def get(self, url: str, params: dict, tries: int = 6):
        for k in range(tries):
            with self.lock:                                # shared pacing across threads
                wait = self.last + self.gap - time.time()
                self.last = max(self.last + self.gap, time.time())
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.s.get(url, params=params, timeout=30)
            except requests.RequestException:
                time.sleep(2 * (k + 1))
                continue
            if r.status_code == 429:
                time.sleep(5 * (k + 1))
                continue
            if r.ok:
                return r.json()
            time.sleep(2 * (k + 1))
        return None


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def markets(h: Http, series: str, since: float) -> list[dict]:
    out, cursor = [], None
    while True:
        params = {"series_ticker": series, "status": "settled", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        js = h.get(f"{KALSHI}/markets", params)
        if not js:
            break
        ms = js.get("markets") or []
        out += [m for m in ms if _ts(m["open_time"]) >= since]
        cursor = js.get("cursor")
        if not cursor or not ms or _ts(ms[-1]["open_time"]) < since:
            break
    return out


def candles(h: Http, series: str, m: dict) -> list[list]:
    o, c = _ts(m["open_time"]), _ts(m["close_time"])
    js = h.get(f"{KALSHI}/series/{series}/markets/{m['ticker']}/candlesticks",
               {"start_ts": int(o), "end_ts": int(c) + 60, "period_interval": 1})
    rows = []
    for x in (js or {}).get("candlesticks") or []:
        f = lambda d, k: float(d[k]) if d and d.get(k) not in (None, "") else None   # noqa: E731
        rows.append([x["end_period_ts"], f(x.get("yes_bid"), "close_dollars"), f(x.get("yes_ask"), "close_dollars"),
                     f(x.get("price"), "close_dollars"), float(x.get("volume_fp") or 0)])
    return rows


def spot(h: Http, coin: str, since: float, until: float) -> list[list]:
    rows, end = {}, until
    while end > since:
        start = max(since, end - 300 * 60)
        js = h.get(f"{COINBASE}/products/{coin}-USD/candles",
                   {"granularity": 60, "start": datetime.utcfromtimestamp(start).isoformat(),
                    "end": datetime.utcfromtimestamp(end).isoformat()})
        for t, lo, hi, op, cl, v in js or []:
            rows[int(t)] = [int(t), op, hi, lo, cl, v]
        end = start
    return [rows[t] for t in sorted(rows)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=45)
    ap.add_argument("--series", default=",".join(SERIES), help="comma-separated, in fetch order")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    since = time.time() - a.days * 86400
    kh, ch = Http(12.0), Http(6.0)
    for series in a.series.split(","):
        coin = SERIES[series]
        p = OUT / f"{series}.jsonl"
        have = set()
        if p.exists():
            have = {json.loads(x)["ticker"] for x in p.open(encoding="utf-8")}
        ms = [m for m in markets(kh, series, since) if m["ticker"] not in have and m.get("result") in ("yes", "no")]
        print(f"{series}: {len(ms)} new settled markets", flush=True)
        with p.open("a", encoding="utf-8") as fh:
            def one(m):
                return {"ticker": m["ticker"], "open": _ts(m["open_time"]), "close": _ts(m["close_time"]),
                        "strike": m.get("floor_strike"), "result": m["result"],
                        "exp": float(m["expiration_value"]) if m.get("expiration_value") else None,
                        "c": candles(kh, series, m)}

            with ThreadPoolExecutor(8) as ex:
                for i, row in enumerate(ex.map(one, ms), 1):
                    fh.write(json.dumps(row) + "\n")
                    if i % 500 == 0:
                        print(f"  {i}/{len(ms)}", flush=True)
        sp = spot(ch, coin, since - 7200, time.time())
        (OUT / f"spot_{coin}.json").write_text(json.dumps(sp))
        print(f"  spot {coin}: {len(sp)} minutes", flush=True)


if __name__ == "__main__":
    main()
