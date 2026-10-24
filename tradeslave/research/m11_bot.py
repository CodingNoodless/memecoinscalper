"""M11 paper bot (leader wave rider): copy decu's first buy in each coin with real detection latency, exit TP +50% / stop -20%.
This is also the DC forward test. Vault "2026-10-24 M11 - leader wave rider (M11)" and "2026-10-24 learning decu's style (DC)".

Detection: logsSubscribe (processed) mentioning his wallet on the public RPC websocket (never Helius: see live_feed.run);
a TradeEvent with user == decu and buy == True in a coin we haven't entered -> subscribe to that coin's logs.
Entry: the first curve price seen at >= detection + 1 s. Exit: price >= entry x 1.5 or <= entry x 0.8 -> fill at the first
price seen >= trigger + 1 s; 15 min timeout at the last price. Paper only: no wallet, no signing.

    uv run python -u -m tradeslave.research.m11_bot
"""

from __future__ import annotations

import json
import logging
import time

from ..paths import DATA_DIR, PROJECT_ROOT
from ..scalper.trade_events import parse_trade_events

DECU = "4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9"
OUT = DATA_DIR / "m11"
RULES = "m11-v1+1s"
FEE, TXR = 0.0125, 2 * 0.0005 / 0.1
TP, SL, DELAY, TIMEOUT, SILENT_S = 1.5, 0.8, 1.0, 900, 120
log = logging.getLogger("m11_bot")


def ret(pe, px):
    return px / pe * (1 - FEE) ** 2 - 1 - TXR


def run() -> None:
    from websockets.sync.client import connect
    from .history_miner import endpoints
    wss = [u.replace("https://", "wss://", 1) for u, _ in endpoints(3.0) if "helius" not in u and "quiknode" not in u
           and "alchemy" not in u and "shyft" not in u]
    wss = [u for u in wss if "rpcfast" not in u] or wss         # RPCFast sends nothing for a wallet mention (2026-10-24)
    OUT.mkdir(parents=True, exist_ok=True)
    seen = {json.loads(x)["m"] for x in (OUT / "trades.jsonl").open()} if (OUT / "trades.jsonl").exists() else set()
    pos: dict[str, dict] = {}                     # mint -> position
    k, req, wait = 0, 10, 3.0

    def close(m, p, now, why):
        x = pos.pop(m)
        r = ret(x["pe"], p)
        row = dict(m=m, name=m[:6], score=None, t=x["t_entry"], t_his=x["t_his"], lag=x["t_detect"] - x["t_his"], his_fill=x["his_fill"],
                   p0=x["pe"], pe=x["pe"], px=p, t_exit=now, exit=why, why=why, ret=r, peak=x["peak"], peak_ret=x["peak"] / x["pe"] - 1,
                   his_sold=x.get("his_sold"), rules=RULES, leader=DECU)
        with (OUT / "trades.jsonl").open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        log.info("closed %s… %s %+.1f%% (lag %.1f s, peak %+.0f%%)", m[:8], why, 100 * r, row["lag"], 100 * row["peak_ret"])

    while True:
        try:
            url = wss[k % len(wss)]
            k += 1
            with connect(url, open_timeout=20, max_size=2 ** 24, ping_interval=20) as ws:
                for a in [DECU, *pos]:
                    req += 1
                    ws.send(json.dumps({"jsonrpc": "2.0", "id": req, "method": "logsSubscribe",
                                        "params": [{"mentions": [a]}, {"commitment": "processed"}]}))
                log.info("m11_bot up on %s, %d open", url.split("//")[1].split("/")[0], len(pos))
                wait = 3.0                                   # connected: reset the backoff
                last_status, last_msg = 0.0, time.time()
                while True:
                    try:
                        msg = json.loads(ws.recv(timeout=5))
                    except TimeoutError:
                        msg = {}
                    now = time.time()
                    if msg.get("params"):
                        last_msg = now
                    elif now - last_msg > SILENT_S:     # copy bots mention his wallet ~45/s: silence = a dead subscription
                        raise ConnectionError(f"no notifications for {SILENT_S} s")
                    if now - last_status > 5:
                        last_status = now
                        st = dict(t=now, watching=len(pos), leaders=[DECU], open=[dict(m=m, name=m[:6], t=x.get("t_entry") or x["t_detect"],
                                  p0=x["pe"], last=x["last"]) for m, x in pos.items()])
                        (OUT / "status.json").write_text(json.dumps(st))
                    for m in [m for m, x in pos.items() if now - x["t_detect"] > TIMEOUT]:
                        x = pos[m]
                        if x["pe"] is None:
                            pos.pop(m)
                        else:
                            close(m, x["last"], now, "timeout")
                    p = msg.get("params")
                    if not p:
                        continue
                    v = p["result"]["value"]
                    if v.get("err") is not None:
                        continue
                    for e in parse_trade_events(v.get("logs") or []):
                        m = e["mint"]
                        if not e["vt"] or not e["vs"]:                  # no curve state (0-SOL events): no price
                            continue
                        price = e["vs"] / e["vt"]
                        if e["user"] == DECU and e["buy"] and e["sol"] >= 1e7 and m not in seen:   # >= 0.01 SOL: his real buys
                            seen.add(m)
                            pos[m] = dict(t_detect=now, t_his=float(e["ts"]), his_fill=e["sol"] / max(1, e["tok"]) * 1e-3,
                                          pe=None, peak=0.0, last=price, trig=None)
                            req += 1
                            ws.send(json.dumps({"jsonrpc": "2.0", "id": req, "method": "logsSubscribe",
                                                "params": [{"mentions": [m]}, {"commitment": "processed"}]}))
                            log.info("decu bought %s… (%.2f SOL), lag %.1f s", m[:8], e["sol"] / 1e9, now - e["ts"])
                        x = pos.get(m)
                        if x is None:
                            continue
                        if e["user"] == DECU and not e["buy"] and x.get("his_sold") is None:
                            x["his_sold"] = now
                        x["last"] = price
                        if x["pe"] is None:
                            if now >= x["t_detect"] + DELAY:
                                x["pe"], x["t_entry"], x["peak"] = price, now, price
                            continue
                        x["peak"] = max(x["peak"], price)
                        if x["trig"] is None and (price >= x["pe"] * TP or price <= x["pe"] * SL):
                            x["trig"] = (now, "TP" if price >= x["pe"] * TP else "SL")
                        if x["trig"] and now >= x["trig"][0] + DELAY:
                            close(m, price, now, x["trig"][1])
        except Exception as exc:  # noqa: BLE001 - reconnect and resubscribe
            log.warning("m11_bot: %s %s; reconnecting in %.0f s", type(exc).__name__, str(exc)[:80], wait)
            time.sleep(wait)
            wait = min(120.0, wait * 2)                      # back off: rapid retries keep the public RPC answering 429


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.FileHandler(PROJECT_ROOT / "logs" / "m11_bot.log"), logging.StreamHandler()])
    run()


if __name__ == "__main__":
    main()
