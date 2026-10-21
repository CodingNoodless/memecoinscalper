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
import hashlib
import json
import logging
import time
from collections import defaultdict

from ..paths import DATA_DIR, PROJECT_ROOT

log = logging.getLogger("launch_watch")
PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
OUT = DATA_DIR / "launch_watch.jsonl"
TRADES = DATA_DIR / "launch_watch_trades.jsonl"
CREATES = DATA_DIR / "c1" / "creates.jsonl"   # every create, unfiltered: C1's mint list (research.c1_curve)
MINE_WINDOW_S = 20.0       # J1 exits at 15 s; creator fees after 20 s are left out (conservative); busy launches
                           # have ~850 txs in their first minute (175 s to mine at 5 req/s, measured)
MINE_AFTER_S = 40.0
MINE_PAGES = 10             # signature pages paged back from NOW: busy launches pile up thousands fast
J2_BAND = (8.0, 15.0)      # J2: slot-0 dev buy band; registered 2026-10-17 14:27 PDT from history + 34 forward launches
J2_FROM = 1792272420.0     # J2 is judged only on launches after its registration
MINE_MIN_SOL = 4.0         # creator's own buy; J1's bucket is the slot-0 group's buy > 5 SOL
J1S_BAND = (0.9, 1.2)      # J-1SOL: the 1 SOL live micro-test size; registered 2026-10-18 (history: n=38, ~break-even)
J1S_FROM = 1792303416.0     # judged only on launches recorded after registration
J1S_SAMPLE = 3             # mine 1 in 3 of them (by mint hash) to spare the free RPC
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
            CREATES.parent.mkdir(parents=True, exist_ok=True)
            with connect(PUMPPORTAL_WS, open_timeout=20, max_size=2 ** 22) as ws, \
                    OUT.open("a", encoding="utf-8") as fh, CREATES.open("a", encoding="utf-8") as fc:
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
                            fc.write(json.dumps({"t": now, "m": msg["mint"], "dev": msg.get("traderPublicKey"),
                                                 "sol": msg.get("solAmount"), "tok": msg.get("initialBuy"),
                                                 "mh": msg.get("is_mayhem_mode")}) + "\n")
                            if float(msg.get("solAmount") or 0) >= WATCH_MIN_SOL:
                                n_kept += 1
                                fh.write(json.dumps({"t": now, **msg}) + "\n")
                                fh.flush()
                    if now - last_note > 600:
                        fh.flush()
                        fc.flush()
                        log.info("%d launches seen, %d recorded (dev buy >= %.1f SOL)", n_seen, n_kept, WATCH_MIN_SOL)
                        last_note = now
                log.info("scalp.py is back: closing our PumpPortal connection")
        except Exception as exc:  # noqa: BLE001 - reconnect
            log.warning("feed: %s; retrying in 30 s", exc)
            time.sleep(30)


def wanted(c: dict) -> bool:
    x = float(c.get("solAmount") or 0)
    if x >= MINE_MIN_SOL:
        return True
    return (c["t"] >= J1S_FROM and J1S_BAND[0] <= x <= J1S_BAND[1]
            and int(hashlib.sha256(c["mint"].encode()).hexdigest(), 16) % J1S_SAMPLE == 0)


def miner(log=log.info) -> None:
    """Pull trades of recorded forward launches (creator buy >= MINE_MIN_SOL) from free RPC once they are
    MINE_AFTER_S old: one worker per endpoint; a launch an endpoint can't serve goes to the next one."""
    import queue
    import threading
    from .chain import Rpc, host_of
    from .history_miner import _fetch, endpoints
    rpcs = []
    urls = endpoints(3.0)
    for url, rps in urls[1:] or urls:         # measured: the public RPC needs ~55 s per launch; keyed ones ~1.5 s
        rpc = Rpc(url, rps)
        rpc.gtfa = False
        rpcs.append(rpc)
    log("trade miner on %s", ", ".join(host_of(r.url) for r in rpcs))
    done, queued, lock = set(), set(), threading.Lock()
    if TRADES.exists():
        done = {json.loads(x)["m"] for x in TRADES.open(encoding="utf-8") if '"done"' in x}
    work: queue.Queue = queue.Queue()
    fails = [0] * len(rpcs)

    def worker(k: int) -> None:
        while True:
            if fails[k] >= 3:                     # rate-limited / out of quota: rest it, others carry on
                log("resting %s for 10 min after 3 failures in a row", host_of(rpcs[k].url))
                time.sleep(600)
                fails[k] = 0
            c, tried = work.get()
            tok = {"m": c["mint"], "bc": c.get("bondingCurveKey"), "t0": c["t"]}
            try:
                status, rows, _ = _fetch(rpcs[k], tok, MINE_WINDOW_S, MINE_PAGES)
            except Exception as exc:  # noqa: BLE001 - hand it on
                log("mine %s on %s: %s", c["mint"][:8], host_of(rpcs[k].url), exc)
                status, rows = "retry", None
            fails[k] = fails[k] + 1 if status == "retry" else 0
            with lock:
                if status == "retry":
                    tried = tried | {k}
                    if len(tried) < len(rpcs):
                        work.put((c, tried))
                    else:
                        queued.discard(c["mint"])     # all endpoints failed: next scan tries again
                    continue
                with TRADES.open("a", encoding="utf-8") as fh:
                    for n, r in enumerate(rows or []):
                        fh.write(json.dumps({**r, "i": n}, separators=(",", ":")) + "\n")
                    fh.write(json.dumps({"k": "done", "m": c["mint"], "status": status}) + "\n")
                done.add(c["mint"])

    for k in range(len(rpcs)):
        threading.Thread(target=worker, args=(k,), daemon=True, name=f"mine{k}").start()
    while True:
        if OUT.exists():
            for line in OUT.open(encoding="utf-8"):
                try:
                    c = json.loads(line)
                except ValueError:
                    continue
                if (c.get("txType") == "create" and c["t"] >= FROZEN_AT and c.get("bondingCurveKey")
                        and wanted(c) and time.time() - c["t"] > MINE_AFTER_S):
                    with lock:
                        if c["mint"] not in done and c["mint"] not in queued:
                            queued.add(c["mint"])
                            work.put((c, frozenset()))
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
    for d in ls:                                  # same window for every launch (early ones were mined longer)
        d["outside"] = [r for r in d["outside"] if r["t"] - d["tc"] <= MINE_WINDOW_S]
    big = [d for d in ls if d["x"] > 5]
    skips = sum(1 for line in TRADES.open(encoding="utf-8") if '"status": "skip"' in line) if TRADES.exists() else 0
    print(f"forward launches recorded {len(creates)}, mined with trades {len(ls)} (skipped as too busy: {skips}), "
          f"slot-0 buy > 5 SOL: {len(big)}")
    print(f"J1 own dev buy : {stats([replay(d, d['x'], T_EXIT) for d in big])}")
    print(f"J1 fixed 5 SOL : {stats([replay(d, 5.0, T_EXIT) for d in big])}")
    xs = [replay(d, 5.0, T_EXIT) for d in big]
    if xs:
        print(f"total (5 SOL each): {sum(xs):+.2f} SOL over {len(xs)} launches")
    j2 = [d for d in ls if J2_BAND[0] < d["x"] <= J2_BAND[1] and d["t0"] >= J2_FROM]
    ys = [replay(d, d["x"], T_EXIT) for d in j2]
    print(f"J2 (dev buy 8-15 SOL, launches after its registration): {stats(ys)}"
          + (f" | ROI {sum(ys) / sum(d['x'] for d in j2):+.1%}" if j2 else ""))
    j1s = [d for d in ls if 0.8 <= d["x"] <= 1.5 and d["t0"] >= J1S_FROM]
    zs = [replay(d, d["x"], T_EXIT) for d in j1s]
    print(f"J-1SOL (dev buy ~1 SOL, sell at 15 s, launches after its registration): {stats(zs)}"
          + (f" | total {sum(zs):+.3f} SOL" if zs else ""))


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
