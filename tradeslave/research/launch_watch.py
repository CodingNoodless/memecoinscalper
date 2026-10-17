"""Forward (paper) test of strategy J1 on live launches: no money, no transactions.

    uv run python -m tradeslave.research.launch_watch            # record + score until stopped
    uv run python -m tradeslave.research.launch_watch --score    # score what's recorded so far

Records every new pump.fun launch whose creator buys >= WATCH_MIN_SOL in the create transaction, plus
all its trades for the next WATCH_S seconds (PumpPortal websocket), to data/launch_watch.jsonl.
Scoring replays the FROZEN J1 rule (vault: "2026-10-17 creator economics"): we launch with a dev buy
of X SOL, the launch's outside trades (everyone but the creator) are replayed on a fresh curve, and
we sell the whole bag right before the first outside trade at or after T = 15 s.
PumpPortal allows one connection per IP: this yields while scalp.py is running.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
import time
from collections import defaultdict

from ..paths import DATA_DIR, PROJECT_ROOT
from .creator import CREATOR_FEE, FEE, LAUNCH_COST, TX_COST, VS0, VT0

log = logging.getLogger("launch_watch")
PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
OUT = DATA_DIR / "launch_watch.jsonl"
SCALPER_LOG = PROJECT_ROOT / "logs" / "scalper.log"
WATCH_MIN_SOL = 2.0
WATCH_S = 90.0
T_EXIT = 15.0                 # frozen (J1, chosen on the first half of the history sample)
FROZEN_AT = 1792267800.0      # 2026-10-17 13:10 PDT: launches after this are the forward test


def scalper_running() -> bool:
    try:
        return time.time() - SCALPER_LOG.stat().st_mtime < 180
    except OSError:
        return False


def record() -> None:
    from websockets.sync.client import connect
    watching: dict[str, float] = {}
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
                        m, kind = msg["mint"], msg.get("txType")
                        if kind == "create":
                            n_seen += 1
                            if float(msg.get("solAmount") or 0) >= WATCH_MIN_SOL:
                                n_kept += 1
                                watching[m] = now
                                ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [m]}))
                                fh.write(json.dumps({"t": now, **msg}) + "\n")
                        elif kind in ("buy", "sell") and m in watching:
                            fh.write(json.dumps({"t": now, **msg}) + "\n")
                    done = [m for m, t in watching.items() if now - t > WATCH_S]
                    if done:
                        ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": done}))
                        for m in done:
                            del watching[m]
                    if now - last_note > 600:
                        fh.flush()
                        log.info("%d launches seen, %d recorded (dev buy >= %.1f SOL)", n_seen, n_kept, WATCH_MIN_SOL)
                        last_note = now
                log.info("scalp.py is back: closing our PumpPortal connection")
        except Exception as exc:  # noqa: BLE001 - reconnect
            log.warning("feed: %s; retrying in 30 s", exc)
            time.sleep(30)


def replay(create: dict, trades: list[dict], x: float, t_exit: float = T_EXIT) -> float:
    vs, vt = VS0, VT0
    net = x * (1 - FEE) * 1e9
    bag = vt * net / (vs + net)
    vs, vt = vs + net, vt - bag
    proceeds, vol = None, 0.0
    for r in trades:
        if r.get("traderPublicKey") == create.get("traderPublicKey"):
            continue
        if proceeds is None and r["t"] - create["t"] >= t_exit:
            proceeds = vs * bag / (vt + bag) / 1e9 * (1 - FEE)
            vs, vt = vs - proceeds / (1 - FEE) * 1e9, vt + bag
        sol, tok = float(r.get("solAmount") or 0), float(r.get("tokenAmount") or 0) * 1e6
        vol += sol
        if r["txType"] == "buy":
            dv = sol * (1 - FEE) * 1e9
            got = vt * dv / (vs + dv)
            vs, vt = vs + dv, vt - got
        else:
            out = vs * tok / (vt + tok)
            vs, vt = vs - out, vt + tok
    if proceeds is None:
        proceeds = vs * bag / (vt + bag) / 1e9 * (1 - FEE)
    return proceeds - x + CREATOR_FEE * vol - LAUNCH_COST - 3 * TX_COST


def score() -> None:
    creates, trades = {}, defaultdict(list)
    with OUT.open(encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            if r.get("txType") == "create":
                creates[r["mint"]] = r
            else:
                trades[r["mint"]].append(r)
    now = time.time()
    done = [c for m, c in creates.items() if c["t"] >= FROZEN_AT and now - c["t"] > WATCH_S]
    for lo, hi in ((2, 5), (5, 1e9)):
        cs = [c for c in done if lo <= float(c["solAmount"]) < hi]
        for lbl, xf in (("own dev buy", None), ("fixed 5 SOL", 5.0)):
            xs = [replay(c, trades[c["mint"]], xf or float(c["solAmount"])) for c in cs]
            if len(xs) < 5:
                print(f"dev buy {lo}-{hi:g} SOL, {lbl}: n={len(xs)}")
                continue
            m, s = statistics.mean(xs), statistics.pstdev(xs)
            print(f"dev buy {lo}-{hi:g} SOL, {lbl:11s}: n={len(xs):4d} mean {m:+.3f} SOL med {statistics.median(xs):+.3f} "
                  f"win {sum(v > 0 for v in xs) / len(xs):.0%} t {m / (s / math.sqrt(len(xs))) if s else 0:+.2f} "
                  f"total {sum(xs):+.2f} SOL")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    if a.score:
        score()
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.FileHandler(PROJECT_ROOT / "logs" / "launch_watch.log"), logging.StreamHandler()])
    record()


if __name__ == "__main__":
    main()
