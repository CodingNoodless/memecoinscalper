"""Forward (paper) test of strategy J1 on live launches: no money, no transactions.

    uv run python -m tradeslave.research.launch_watch            # record + score until stopped
    uv run python -m tradeslave.research.launch_watch --score    # score what's recorded so far

Records every new pump.fun launch whose creator buys >= WATCH_MIN_SOL in the create transaction (free
PumpPortal launch feed) to data/launch_watch.jsonl. Per-token trade streams cost money on PumpPortal,
so a second thread pulls each recorded launch's first MINE_WINDOW_S seconds of trades from free RPC
(the history miner's exact parser) into data/launch_watch_trades.jsonl, a few minutes after launch.
Scoring replays the FROZEN J1 rule (vault: "2026-10-17 creator economics") with research.creator's
replay: same slot-0 grouping, same costs, T = 15 s, launches with a slot-0 buy > 5 SOL.
PumpPortal allows one connection per IP: this yields while scalp.py is running.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections import defaultdict

from ..paths import DATA_DIR, PROJECT_ROOT

log = logging.getLogger("launch_watch")
PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
OUT = DATA_DIR / "launch_watch.jsonl"
TRADES = DATA_DIR / "launch_watch_trades.jsonl"
MINE_WINDOW_S = 120.0
MINE_AFTER_S = 180.0
SCALPER_LOG = PROJECT_ROOT / "logs" / "scalper.log"
WATCH_MIN_SOL = 1.0
T_EXIT = 15.0                 # frozen (J1, chosen on the first half of the history sample)
FROZEN_AT = 1792267800.0      # 2026-10-17 13:10 PDT (rule frozen; any launch recorded after this is forward): launches after this are the forward test


def scalper_running() -> bool:
    try:
        return time.time() - SCALPER_LOG.stat().st_mtime < 180
    except OSError:
        return False


def record() -> None:
    from websockets.sync.client import connect
    while True:
        if scalper_running():
            time.sleep(60)
            continue
        try:
            with connect(PUMPPORTAL_WS, open_timeout=20, max_size=2 ** 22) as ws, \
                    OUT.open("a", encoding="utf-8") as fh:
                ws.send(json.dumps({"method": "subscribeNewToken"}))
                log.info("listening to PumpPortal launches (dev buy >= %.1f SOL)", WATCH_MIN_SOL)
                n_seen = n_kept = 0
                last_note = time.time()
                while not scalper_running():
                    try:
                        msg = json.loads(ws.recv(timeout=10))
                    except TimeoutError:
                        msg = None
                    now = time.time()
                    if isinstance(msg, dict) and isinstance(msg.get("mint"), str):
                        kind = msg.get("txType")
                        if kind == "create":
                            n_seen += 1
                            if float(msg.get("solAmount") or 0) >= WATCH_MIN_SOL:
                                n_kept += 1
                                fh.write(json.dumps({"t": now, **msg}) + "\n")
                                fh.flush()
                    if now - last_note > 600:
                        fh.flush()
                        log.info("%d launches seen, %d recorded (dev buy >= %.1f SOL)", n_seen, n_kept, WATCH_MIN_SOL)
                        last_note = now
                log.info("scalp.py is back: closing our PumpPortal connection")
        except Exception as exc:  # noqa: BLE001 - reconnect
            log.warning("feed: %s; retrying in 30 s", exc)
            time.sleep(30)


def miner(log=log.info) -> None:
    """Pull trades of recorded launches from free RPC once they are MINE_AFTER_S old."""
    from .chain import Rpc, host_of
    from .history_miner import _fetch, endpoints
    rpcs = []
    for url, rps in reversed(endpoints(2.0)):     # configured endpoints first, the public RPC last
        rpc = Rpc(url, min(rps, 2.0))
        rpc.gtfa = False
        rpcs.append(rpc)
    log("trade miner on %s", ", ".join(host_of(r.url) for r in rpcs))
    done = set()
    if TRADES.exists():
        done = {json.loads(x)["m"] for x in TRADES.open(encoding="utf-8") if '"done"' in x}
    while True:
        todo = []
        if OUT.exists():
            for line in OUT.open(encoding="utf-8"):
                try:
                    c = json.loads(line)
                except ValueError:
                    continue
                if c.get("txType") == "create" and c["mint"] not in done and time.time() - c["t"] > MINE_AFTER_S:
                    todo.append(c)
        for c in todo:
            tok = {"m": c["mint"], "bc": c.get("bondingCurveKey"), "t0": c["t"]}
            if not tok["bc"]:
                done.add(c["mint"])
                continue
            try:
                for rpc in rpcs:                      # first endpoint that serves the whole chart
                    status, rows, _ = _fetch(rpc, tok, MINE_WINDOW_S, 2)
                    if status != "retry":
                        break
            except Exception as exc:  # noqa: BLE001 - next pass
                log("mine %s: %s", c["mint"][:8], exc)
                continue
            if status == "retry":
                continue
            with TRADES.open("a", encoding="utf-8") as fh:
                for k, r in enumerate(rows or []):
                    fh.write(json.dumps({**r, "i": k}, separators=(",", ":")) + "\n")
                fh.write(json.dumps({"k": "done", "m": c["mint"], "status": status}) + "\n")
            done.add(c["mint"])
        time.sleep(30)


def score() -> None:
    from .creator import launch_rows, replay, stats
    creates = {}
    for line in OUT.open(encoding="utf-8"):
        r = json.loads(line)
        if r.get("txType") == "create" and r["t"] >= FROZEN_AT:
            creates[r["mint"]] = r
    tr = defaultdict(list)
    if TRADES.exists():
        for line in TRADES.open(encoding="utf-8"):
            r = json.loads(line)
            if r.get("k") == "trade" and r["m"] in creates:
                tr[r["m"]].append(r)
    ls = [x for x in (launch_rows(m, creates[m]["t"], tr[m]) for m in tr) if x]
    big = [d for d in ls if d["x"] > 5]
    print(f"forward launches recorded {len(creates)}, mined with trades {len(ls)}, slot-0 buy > 5 SOL: {len(big)}")
    print(f"J1 own dev buy : {stats([replay(d, d['x'], T_EXIT) for d in big])}")
    print(f"J1 fixed 5 SOL : {stats([replay(d, 5.0, T_EXIT) for d in big])}")
    xs = [replay(d, 5.0, T_EXIT) for d in big]
    if xs:
        print(f"total (5 SOL each): {sum(xs):+.2f} SOL over {len(xs)} launches")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    if a.score:
        score()
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.FileHandler(PROJECT_ROOT / "logs" / "launch_watch.log"), logging.StreamHandler()])
    import threading
    threading.Thread(target=miner, daemon=True, name="miner").start()
    record()


if __name__ == "__main__":
    main()
