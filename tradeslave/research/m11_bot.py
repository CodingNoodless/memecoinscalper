"""M11 paper bot (leader wave rider): copy decu's first buy in each coin with real detection latency, exit TP +50% / stop -20%.
This is also the DC forward test. Vault "2026-10-24 M11 - leader wave rider (M11)" and "2026-10-24 learning decu's style (DC)".

Feed-driven (2026-10-24 v1.1): the live feed (live_feed.py) already sees every pump.fun trade on its one websocket and writes
the leader wallets' trades to data/feed/leader_trades.jsonl. M11 tails that file (no websocket of its own: its own connection
kept getting HTTP 429 from the public RPC), asks the feed for the coin by logging it in data/m11/cands.jsonl, and reads
prices from the feed database like the other bots.
Entry: the first feed price at >= detection + 1 s. Exit: price >= entry x 1.5 or <= entry x 0.8 -> fill at the first feed
price seen >= trigger + 1 s; 15 min timeout at the last price. Paper only: no wallet, no signing.

    uv run python -u -m tradeslave.research.m11_bot
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time

from ..paths import DATA_DIR, PROJECT_ROOT
from .live_feed import DB, LEADER_TRADES

DECU = "4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9"
OUT = DATA_DIR / "m11"
RULES = "m11-v1+1s"
FEE, TXR = 0.0125, 2 * 0.0005 / 0.1
TP, SL, DELAY, TIMEOUT, POLL = 1.5, 0.8, 1.0, 900, 0.25
log = logging.getLogger("m11_bot")


def ret(pe, px):
    return px / pe * (1 - FEE) ** 2 - 1 - TXR


def last_price(db, m):
    r = db.execute("SELECT vs, vt FROM tr WHERE m=? AND vs > 0 AND vt > 0 ORDER BY slot DESC, seq DESC LIMIT 1", (m,)).fetchone()
    return r[0] / r[1] if r else None


def feed_up(db):
    b = db.execute("SELECT t FROM beat WHERE k='feed'").fetchone()
    return bool(b) and time.time() - b[0] < 5


def run() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    seen = {json.loads(x)["m"] for x in (OUT / "trades.jsonl").open()} if (OUT / "trades.jsonl").exists() else set()
    pos: dict[str, dict] = {}
    db = sqlite3.connect(DB, timeout=10)
    LEADER_TRADES.touch()
    fh = LEADER_TRADES.open()
    fh.seek(0, 2)                                          # only trades from now on
    last_status, down_since = 0.0, None
    log.info("m11_bot up (feed-driven), watching %s…", DECU[:8])

    def close(m, p, now, why):
        x = pos.pop(m)
        r = ret(x["pe"], p)
        row = dict(m=m, name=m[:6], score=None, t=x["t_entry"], t_his=x["t_his"], lag=x["t_detect"] - x["t_his"], his_fill=x["his_fill"],
                   p0=x["pe"], pe=x["pe"], px=p, t_exit=now, exit=why, why=why, ret=r, peak=x["peak"], peak_ret=x["peak"] / x["pe"] - 1,
                   his_sold=x.get("his_sold"), rules=RULES, leader=DECU, src="feed")
        with (OUT / "trades.jsonl").open("a") as f:
            f.write(json.dumps(row) + "\n")
        log.info("closed %s… %s %+.1f%% (lag %.1f s, peak %+.0f%%)", m[:8], why, 100 * r, row["lag"], 100 * row["peak_ret"])

    while True:
        now = time.time()
        while True:                                        # new leader trades from the feed
            pos_before = fh.tell()
            line = fh.readline()
            if not line.endswith("\n"):                    # nothing new, or a line still being written
                fh.seek(pos_before)
                break
            e = json.loads(line)
            m = e["m"]
            if e["w"] != DECU:
                continue
            if e["buy"] and e["sol"] >= 0.01 and e["vs"] and e["vt"] and m not in seen:
                seen.add(m)
                pos[m] = dict(t_detect=now, t_his=float(e["ts"]), his_fill=e["sol"] / max(1e-12, e["tok"]), pe=None, peak=0.0,
                              last=e["vs"] / e["vt"], trig=None)
                with (OUT / "cands.jsonl").open("a") as f:       # asks the feed to write this coin's trades
                    f.write(json.dumps(dict(t=now, m=m, stage="m11", leader=DECU)) + "\n")
                log.info("decu bought %s… (%.2f SOL), lag %.1f s", m[:8], e["sol"], now - e["ts"])
            elif not e["buy"] and m in pos and pos[m].get("his_sold") is None:
                pos[m]["his_sold"] = now
        up = feed_up(db)
        if not up and down_since is None:
            down_since = now
            log.warning("feed heartbeat stale: prices paused")
        elif up and down_since is not None:
            log.info("feed back after %.0f s", now - down_since)
            down_since = None
        for m in list(pos):
            x = pos[m]
            p = last_price(db, m) if up else None
            if p is not None:
                x["last"] = p
            if now - x["t_detect"] > TIMEOUT:
                if x["pe"] is None:
                    pos.pop(m)
                else:
                    close(m, x["last"], now, "timeout")
                continue
            if p is None:
                continue
            if x["pe"] is None:
                if now >= x["t_detect"] + DELAY:
                    x["pe"], x["t_entry"], x["peak"] = p, now, p
                continue
            x["peak"] = max(x["peak"], p)
            if x["trig"] is None and (p >= x["pe"] * TP or p <= x["pe"] * SL):
                x["trig"] = (now, "TP" if p >= x["pe"] * TP else "SL")
            if x["trig"] and now >= x["trig"][0] + DELAY:
                close(m, p, now, x["trig"][1])
        if now - last_status > 5:
            last_status = now
            st = dict(t=now, watching=len(pos), leaders=[DECU], feed_up=up, open=[dict(m=m, name=m[:6], t=x.get("t_entry") or x["t_detect"],
                      p0=x["pe"], last=x["last"]) for m, x in pos.items()])
            (OUT / "status.json").write_text(json.dumps(st))
        time.sleep(POLL)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.FileHandler(PROJECT_ROOT / "logs" / "m11_bot.log"), logging.StreamHandler()])
    run()


if __name__ == "__main__":
    main()
