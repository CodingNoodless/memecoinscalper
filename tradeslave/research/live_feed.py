"""Real-time pump.fun trades for the coins the M bots watch (infra v4, 2026-10-22).

    uv run python -m tradeslave.research.live_feed

Why: the swap-api trade list the bots used for holders, volume and bundle checks runs 20 s to several minutes
behind the chain (measured 2026-10-22 15:15: 22 s, 97 s, 156 s on three coins), so M4 bought "Aura Final Boss" on
a holder count from the past. This listens to every pump.fun transaction on one public-RPC websocket (never Helius: see run()) (logsSubscribe on
the program, `processed`), decodes each TradeEvent (scalper.trade_events, verified 400/400 against curve deltas),
keeps the last 30 min of trades per coin in memory, and writes every trade of the coins the bots are watching
(any mint in data/m*/cands.jsonl from the last 30 min) to data/feed/trades.sqlite, including their history from
create. Bots read it with feed_trades(); `complete` = the feed saw the coin's create and had no gap since.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from ..paths import DATA_DIR, PROJECT_ROOT
from ..scalper.trade_events import parse_trade_events

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
DB = DATA_DIR / "feed" / "trades.sqlite"
KEEP_S, WANT_S = 1800, 1800
LEADERS = DATA_DIR / "feed" / "leaders.json"            # wallets whose trades go to leader_trades.jsonl (M11)
LEADER_TRADES = DATA_DIR / "feed" / "leader_trades.jsonl"


def leaders() -> set[str]:
    try:
        return set(json.loads(LEADERS.read_text()))
    except (OSError, ValueError):
        return set()
log = logging.getLogger("live_feed")


def connect_db(path: Path = DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS tr (m TEXT, sig TEXT, i INTEGER, slot INTEGER, t REAL, w TEXT, b INTEGER, "
               "sol REAL, tok REAL, vs REAL, vt REAL, seq INTEGER, PRIMARY KEY (sig, i))")
    db.execute("CREATE INDEX IF NOT EXISTS tr_m ON tr (m, slot, seq)")
    db.execute("CREATE TABLE IF NOT EXISTS coin (m TEXT PRIMARY KEY, created REAL, complete INTEGER)")
    db.execute("CREATE TABLE IF NOT EXISTS beat (k TEXT PRIMARY KEY, t REAL, up_since REAL)")
    return db


def feed_trades(m: str, db: sqlite3.Connection | None = None, max_age: float = 5.0):
    """(rows, complete) for a coin: rows = [(slot, t, wallet, is_buy, sol, tok)] oldest first, or (None, False) when
    the feed is down (no heartbeat within max_age s) or doesn't know the coin."""
    own = db is None
    db = db or sqlite3.connect(DB, timeout=5)
    try:
        beat = db.execute("SELECT t FROM beat WHERE k='feed'").fetchone()
        if not beat or time.time() - beat[0] > max_age:
            return None, False
        c = db.execute("SELECT complete FROM coin WHERE m=?", (m,)).fetchone()
        if not c:
            return None, False
        rows = db.execute("SELECT slot, t, w, b, sol, tok FROM tr WHERE m=? ORDER BY slot, seq", (m,)).fetchall()
        return [(s, t, w, bool(b), sol, tok) for s, t, w, b, sol, tok in rows], bool(c[0])
    except sqlite3.Error:
        return None, False
    finally:
        if own:
            db.close()


class Wanted:
    """Mints any M bot logged in the last 30 min (tails data/m*/cands.jsonl)."""

    def __init__(self):
        self.pos, self.when = {}, {}

    def scan(self) -> set[str]:
        now = time.time()
        for p in DATA_DIR.glob("m*/cands.jsonl"):                 # every bot folder (m10, m11 too)
            try:
                with p.open(encoding="utf-8") as fh:
                    fh.seek(self.pos.get(p, max(0, p.stat().st_size - 2_000_000)))
                    for line in fh:
                        if not line.endswith("\n"):
                            break
                        try:
                            c = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(c.get("m"), str):
                            self.when[c["m"]] = now
                    self.pos[p] = fh.tell()
            except OSError:
                continue
        for m in [m for m, t in self.when.items() if now - t > WANT_S]:
            del self.when[m]
        return set(self.when)


def run() -> None:
    from websockets.sync.client import connect
    from .history_miner import endpoints
    # infra v5 (15:58): never Helius. Its websocket firehose used up the plan ("max usage reached") within 35 min and
    # blinded every bot. The free public RPC websocket first, then RPCFast; switch on each reconnect.
    wss = [u.replace("https://", "wss://", 1) for u, _ in endpoints(3.0) if "helius" not in u and "quiknode" not in u
           and "alchemy" not in u and "shyft" not in u]
    k = 0
    db = connect_db()
    lock = threading.Lock()
    hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=20000))
    first: dict[str, float] = {}               # mint -> time we first saw it (its create, if created while up)
    created: dict[str, float] = {}
    last: dict[str, float] = {}
    written: set[str] = set()
    wanted = Wanted()
    state = {"up_since": 0.0, "n": 0, "seq": int(time.time() * 1e6)}   # seq keeps rising across restarts
    lead = {"w": leaders(), "t": time.time()}

    def beat():
        while True:
            want = wanted.scan()
            if time.time() - lead["t"] > 30:
                lead["w"], lead["t"] = leaders(), time.time()
            now = time.time()
            with lock:
                for m in want - written:
                    if m in hist or m in created:
                        rows = list(hist.get(m, ()))
                        db.executemany("INSERT OR IGNORE INTO tr VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [(m, *r) for r in rows])
                        full = m in created and created[m] >= state["up_since"] > 0
                        db.execute("INSERT OR REPLACE INTO coin VALUES (?,?,?)", (m, created.get(m), int(full)))
                        written.add(m)
                for m in [m for m, t in last.items() if now - t > KEEP_S]:
                    last.pop(m, None)
                    hist.pop(m, None)
                    created.pop(m, None)
                    written.discard(m)
                db.execute("INSERT OR REPLACE INTO beat VALUES ('feed', ?, ?)", (now if state["up_since"] else 0.0,
                                                                                state["up_since"]))
                db.commit()
            time.sleep(1.0)

    threading.Thread(target=beat, daemon=True, name="beat").start()
    last_note = time.time()
    while True:
        try:
            ws_url = wss[k % len(wss)]
            k += 1
            with connect(ws_url, open_timeout=20, max_size=2 ** 24, ping_interval=20) as ws:
                ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                    "params": [{"mentions": [PUMP]}, {"commitment": "processed"}]}))
                with lock:
                    state["up_since"] = time.time()
                    # only coins still trading when the gap began have a gap; ones that went quiet before keep their flag
                    db.execute("UPDATE coin SET complete=0 WHERE m IN (SELECT DISTINCT m FROM tr WHERE t > ?)",
                               (time.time() - KEEP_S,))
                    db.commit()
                log.info("live feed up on %s (logsSubscribe on the pump.fun program)", ws_url.split("//")[1].split("/")[0])
                while True:
                    msg = json.loads(ws.recv(timeout=30))
                    p = msg.get("params")
                    if not p:
                        continue
                    v = p["result"]["value"]
                    if v.get("err") is not None:
                        continue
                    logs = v.get("logs") or []
                    evs = parse_trade_events(logs)
                    if not evs:
                        continue
                    slot, sig, now = p["result"]["context"]["slot"], v.get("signature"), time.time()
                    is_create = any("Instruction: Create" in x for x in logs)
                    with lock:
                        state["n"] += len(evs)
                        for i, e in enumerate(evs):
                            m = e["mint"]
                            if is_create and m not in created:
                                created[m] = now
                            state["seq"] += 1                             # arrival order = execution order in a slot
                            row = (sig, i, slot, float(e["ts"] or now), e["user"], int(e["buy"]), e["sol"] / 1e9,
                                   e["tok"] / 1e6, e["vs"] / 1e9, e["vt"] / 1e6, state["seq"])
                            hist[m].append(row)
                            last[m] = now
                            if e["user"] in lead["w"]:
                                with LEADER_TRADES.open("a") as fh:
                                    fh.write(json.dumps(dict(recv=now, ts=e["ts"], slot=slot, sig=sig, w=e["user"], m=m, buy=e["buy"],
                                                             sol=e["sol"] / 1e9, tok=e["tok"] / 1e6, vs=e["vs"] / 1e9, vt=e["vt"] / 1e6)) + "\n")
                            if m in written:
                                db.execute("INSERT OR IGNORE INTO tr VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (m, *row))
                    if now - last_note > 600:
                        last_note = now
                        log.info("%d trades decoded, %d coins in memory, %d written", state["n"], len(hist), len(written))
        except Exception as exc:  # noqa: BLE001 - reconnect; coins seen so far are marked incomplete
            with lock:
                state["up_since"] = 0.0
            log.warning("live feed: %s; reconnecting in 3 s", type(exc).__name__)
            time.sleep(3)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.FileHandler(PROJECT_ROOT / "logs" / "live_feed.log"), logging.StreamHandler()])
    run()


if __name__ == "__main__":
    main()


def feed_rows(m: str, db: sqlite3.Connection | None = None, max_age: float = 5.0):
    """(rows, complete) for M5: rows = [t, spot SOL/token, is_buy, sol, wallet, tokens, holders] oldest first (holders =
    wallets holding > 1 token, dev included, counted from create); (None, False) when the feed is down or lacks it."""
    own = db is None
    db = db or sqlite3.connect(DB, timeout=5)
    try:
        beat = db.execute("SELECT t FROM beat WHERE k='feed'").fetchone()
        c = db.execute("SELECT complete FROM coin WHERE m=?", (m,)).fetchone()
        if not beat or time.time() - beat[0] > max_age or not c:
            return None, False
        rows = db.execute("SELECT t, w, b, sol, tok, vs, vt FROM tr WHERE m=? ORDER BY slot, seq", (m,)).fetchall()
    except sqlite3.Error:
        return None, False
    finally:
        if own:
            db.close()
    bal, h, out = {}, 0, []
    for t, w, b, sol, tok, vs, vt in rows:
        b0 = bal.get(w, 0.0)
        b1 = b0 + (tok if b else -tok)
        bal[w] = b1
        h += (b0 <= 1 < b1) - (b1 <= 1 < b0)
        if vt:
            out.append([t, vs / vt, bool(b), sol, w, tok, h])
    return out, bool(c[0])
