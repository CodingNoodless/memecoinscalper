"""Forward PAPER test of strategy M1 on Kalshi 15-minute crypto markets (no orders are sent).

    uv run python -m tradeslave.research.kalshi_live            # run until stopped
    uv run python -m tradeslave.research.kalshi_live --score

Spot: Coinbase + Kraken + Bitstamp public websockets (CF Benchmarks' BRTI uses these venues); the
composite is the median of the venues' last trade prices that are < 10 s old. Volatility: std of
1-minute log returns over the last 60 minutes (Coinbase REST candles, refreshed each minute).
Kalshi: the public order book of each series' open market, polled every POLL_S seconds.
Rule M1 (frozen 2026-10-17, edge chosen on the design period): p_fair = Phi(ln(S/K) / sqrt(var)) with
var = sigma^2 * (tau - 60 + 20) / 60; if p_fair - yes_ask >= EDGE buy YES at the ask, if
yes_bid - p_fair >= EDGE buy NO at 1 - yes_bid; first signal per market, size = min(CAP, size shown at
the best price); taker fee round-up(0.07 * C * P * (1 - P)); held to settlement.
Every signal (not only the first) is also logged, for decay analysis.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
import threading
import time
from datetime import datetime

import requests

from ..paths import DATA_DIR, PROJECT_ROOT

log = logging.getLogger("kalshi_live")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = {"KXDOGE15M": "DOGE", "KXXRP15M": "XRP", "KXSOL15M": "SOL", "KXETH15M": "ETH", "KXBTC15M": "BTC"}
EDGE = 0.12
CAP = 100              # contracts per trade
MIN_SIZE = 5           # a quote smaller than this is dust: not a tradable signal
POLL_S = 1.0
TRADES = DATA_DIR / "kalshi" / "live_trades.jsonl"
SIGNALS = DATA_DIR / "kalshi" / "live_signals.jsonl"


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def fee(p: float, n: float) -> float:
    return math.ceil(0.07 * n * p * (1 - p) * 100 - 1e-9) / 100


def phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


class Spot:
    """Last trade price per venue per coin, from public websockets."""

    def __init__(self, coins: list[str]):
        self.coins = coins
        self.px: dict[tuple[str, str], tuple[float, float]] = {}
        self.sigma: dict[str, float] = {}
        for target in (self._coinbase, self._kraken, self._bitstamp, self._vol):
            threading.Thread(target=target, daemon=True).start()

    def price(self, coin: str):
        now = time.time()
        xs = [p for (c, _), (p, t) in self.px.items() if c == coin and now - t < 10]
        return statistics.median(xs) if xs else None

    def _run(self, name, url, sub, parse):
        from websockets.sync.client import connect
        while True:
            try:
                with connect(url, open_timeout=20) as ws:
                    for msg in sub:
                        ws.send(json.dumps(msg))
                    log.info("%s websocket up", name)
                    while True:
                        for coin, p in parse(json.loads(ws.recv(timeout=60))):
                            self.px[(coin, name)] = (p, time.time())
            except Exception as exc:  # noqa: BLE001 - reconnect
                log.warning("%s: %s; reconnecting", name, exc)
                time.sleep(5)

    def _coinbase(self):
        def parse(m):
            if m.get("type") == "ticker" and m.get("price"):
                yield m["product_id"].split("-")[0], float(m["price"])
        self._run("coinbase", "wss://ws-feed.exchange.coinbase.com",
                  [{"type": "subscribe", "product_ids": [f"{c}-USD" for c in self.coins], "channels": ["ticker"]}], parse)

    def _kraken(self):
        def parse(m):
            if m.get("channel") == "ticker":
                for d in m.get("data") or []:
                    if d.get("last"):
                        yield d["symbol"].split("/")[0].replace("XBT", "BTC"), float(d["last"])
        self._run("kraken", "wss://ws.kraken.com/v2",
                  [{"method": "subscribe", "params": {"channel": "ticker", "symbol": [f"{c}/USD" for c in self.coins]}}], parse)

    def _bitstamp(self):
        def parse(m):
            if m.get("event") == "trade":
                yield m["channel"].split("_")[-1][:-3].upper(), float(m["data"]["price"])
        self._run("bitstamp", "wss://ws.bitstamp.net",
                  [{"event": "bts:subscribe", "data": {"channel": f"live_trades_{c.lower()}usd"}} for c in self.coins], parse)

    def _vol(self):
        s = requests.Session()
        while True:
            for c in self.coins:
                try:
                    rows = s.get(f"https://api.exchange.coinbase.com/products/{c}-USD/candles",
                                 params={"granularity": 60}, timeout=20).json()
                    cl = [r[4] for r in sorted(rows)[-61:]]
                    xs = [math.log(b / a) for a, b in zip(cl, cl[1:]) if a > 0]
                    if len(xs) > 30:
                        self.sigma[c] = statistics.pstdev(xs)
                except Exception as exc:  # noqa: BLE001 - next minute
                    log.warning("vol %s: %s", c, exc)
            time.sleep(60)


def best(book: dict):
    ob = book.get("orderbook_fp") or {}
    yes = [(float(p), float(q)) for p, q in ob.get("yes_dollars") or []]
    no = [(float(p), float(q)) for p, q in ob.get("no_dollars") or []]
    yb = max(yes) if yes else None              # best YES bid (price, size)
    nb = max(no) if no else None                # best NO bid -> YES ask = 1 - price
    return yb, ((round(1 - nb[0], 4), nb[1]) if nb else None)


def record() -> None:
    s = requests.Session()
    spot = Spot(sorted(set(SERIES.values())))
    markets: dict[str, dict] = {}               # series -> open market
    traded: set[str] = set()
    pending: list[dict] = []
    last_refresh = 0.0
    while True:
        now = time.time()
        if now - last_refresh > 20:
            for series in SERIES:
                try:
                    ms = s.get(f"{KALSHI}/markets", params={"series_ticker": series, "status": "open"}, timeout=20).json()["markets"]
                    if ms:
                        m = ms[0]
                        markets[series] = {"ticker": m["ticker"], "close": _ts(m["close_time"]), "open": _ts(m["open_time"]),
                                           "strike": m.get("floor_strike")}
                except Exception as exc:  # noqa: BLE001
                    log.warning("markets %s: %s", series, exc)
            last_refresh = now
            settle(s, pending)
        for series, m in list(markets.items()):
            coin = SERIES[series]
            tau = m["close"] - now
            if not m["strike"] or tau < 60 or now - m["open"] < 60:
                continue
            S, sig = spot.price(coin), spot.sigma.get(coin)
            if not S or not sig:
                continue
            try:
                book = s.get(f"{KALSHI}/markets/{m['ticker']}/orderbook", timeout=10).json()
            except Exception:  # noqa: BLE001
                continue
            (yb, ya) = best(book)
            var = sig ** 2 * max(tau - 60 + 20, 20) / 60
            p = phi(math.log(S / m["strike"]) / math.sqrt(var))
            sig_side = None
            if ya and 0 < ya[0] < 1 and p - ya[0] >= EDGE:
                sig_side, price, size = "yes", ya[0], ya[1]
            elif yb and 0 < yb[0] < 1 and yb[0] - p >= EDGE:
                sig_side, price, size = "no", round(1 - yb[0], 4), yb[1]
            if not sig_side or size < MIN_SIZE:
                continue
            row = {"t": time.time(), "series": series, "ticker": m["ticker"], "side": sig_side, "price": price,
                   "size_shown": size, "fair": round(p, 4), "spot": S, "strike": m["strike"], "tau": round(tau, 1)}
            with SIGNALS.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            if m["ticker"] not in traded:
                traded.add(m["ticker"])
                n = min(CAP, size)
                trade = {**row, "n": n, "fee": fee(price, n), "close": m["close"]}
                pending.append(trade)
                log.info("PAPER %s %s %s x%.0f @ %.3f (fair %.3f, %.0fs left)", series, m["ticker"], sig_side, n, price, p, tau)
        time.sleep(max(0.0, POLL_S - (time.time() - now)))


def settle(s: requests.Session, pending: list[dict]) -> None:
    for tr in [x for x in pending if time.time() > x["close"] + 90]:
        try:
            m = s.get(f"{KALSHI}/markets/{tr['ticker']}", timeout=20).json()["market"]
        except Exception:  # noqa: BLE001
            continue
        if m.get("result") not in ("yes", "no"):
            continue
        win = m["result"] == tr["side"]
        tr["result"] = m["result"]
        tr["pnl"] = round((1.0 if win else 0.0) * tr["n"] - tr["price"] * tr["n"] - tr["fee"], 4)
        with TRADES.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(tr) + "\n")
        log.info("SETTLED %s %s %s pnl $%+.2f", tr["ticker"], tr["side"], m["result"], tr["pnl"])
        pending.remove(tr)


def score() -> None:
    rows = [json.loads(x) for x in TRADES.open(encoding="utf-8")] if TRADES.exists() else []
    by = {}
    for r in rows:
        by.setdefault(r["series"], []).append(r)
    for series, rs in sorted(by.items()):
        per = [r["pnl"] / r["n"] for r in rs if r["n"]]
        m = statistics.mean(per) if per else 0
        sd = statistics.pstdev(per) if len(per) > 1 else 0
        print(f"{series:10s} n={len(rs):4d} mean {m * 100:+6.2f}c/contract t {m / (sd / math.sqrt(len(per))) if sd else 0:+5.2f} "
              f"win {sum(r['pnl'] > 0 for r in rs) / len(rs):4.0%} total ${sum(r['pnl'] for r in rs):+8.2f}")
    allp = [r["pnl"] / r["n"] for r in rows if r["n"]]
    if allp:
        print(f"ALL        n={len(allp)} mean {statistics.mean(allp) * 100:+.2f}c/contract total ${sum(r['pnl'] for r in rows):+.2f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    if a.score:
        score()
        return
    TRADES.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.FileHandler(PROJECT_ROOT / "logs" / "kalshi_live.log"), logging.StreamHandler()])
    record()


if __name__ == "__main__":
    main()
