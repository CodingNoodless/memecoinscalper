"""How fast do we see pump.fun trades on a plain RPC websocket? (strategy P: speed)

    uv run python -m tradeslave.research.latency_probe --secs 90

One websocket per endpoint: slotSubscribe (the chain tip as this endpoint sees it) + logsSubscribe on
the pump.fun program at `processed` commitment. For every trade notification: tip slot at arrival minus
the trade's slot (0 = we see it while its slot is still the tip), and ms since that slot first appeared.
Only hostnames are printed (keys stay in .env).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import threading
import time
from urllib.parse import urlparse

from .. import config  # noqa: F401  (loads .env)

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def probe(url: str, secs: float, out: dict) -> None:
    from websockets.sync.client import connect
    host = urlparse(url).hostname
    ws_url = url.replace("https://", "wss://", 1).split("#", 1)[0]
    first_seen: dict[int, float] = {}
    tip = [0]
    lags, ms = [], []
    try:
        with connect(ws_url, open_timeout=20, max_size=2 ** 24) as ws:
            ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "slotSubscribe"}))
            ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "logsSubscribe",
                                "params": [{"mentions": [PUMP]}, {"commitment": "processed"}]}))
            end = time.time() + secs
            while time.time() < end:
                try:
                    msg = json.loads(ws.recv(timeout=5))
                except TimeoutError:
                    continue
                now = time.time()
                p = msg.get("params")
                if not p:
                    continue
                if msg.get("method") == "slotNotification":
                    s = p["result"]["slot"]
                    tip[0] = max(tip[0], s)
                    first_seen.setdefault(s, now)
                elif msg.get("method") == "logsNotification":
                    s = p["result"]["context"]["slot"]
                    logs = p["result"]["value"].get("logs") or []
                    if tip[0] and any("Instruction: Buy" in x or "Instruction: Sell" in x for x in logs):
                        lags.append(tip[0] - s)
                        if s in first_seen:
                            ms.append((now - first_seen[s]) * 1000)
    except Exception as exc:  # noqa: BLE001 - report and move on
        out[host] = f"error: {type(exc).__name__}"          # never echo the URL: it carries the key
        return
    if lags:
        out[host] = (f"{len(lags)} trades | tip - trade slot: 0 {lags.count(0) / len(lags):.0%}, 1 {lags.count(1) / len(lags):.0%}, "
                     f"2 {lags.count(2) / len(lags):.0%}, 3+ {sum(x >= 3 for x in lags) / len(lags):.0%} | "
                     f"ms after slot appeared: median {statistics.median(ms) if ms else float('nan'):.0f}, "
                     f"p90 {sorted(ms)[int(.9 * len(ms))] if ms else float('nan'):.0f}")
    else:
        out[host] = "no trade notifications"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=90)
    a = ap.parse_args()
    urls = [os.getenv("RESEARCH_RPC_URL", "")] + [u.strip() for u in os.getenv("HISTORY_RPC_URLS", "").split(",")]
    urls = [u for u in urls if u]
    out: dict = {}
    th = [threading.Thread(target=probe, args=(u, a.secs, out)) for u in urls]
    for t in th:
        t.start()
    for t in th:
        t.join()
    for h, v in out.items():
        print(f"{h}: {v}")


if __name__ == "__main__":
    main()
