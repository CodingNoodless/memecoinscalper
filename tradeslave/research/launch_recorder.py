"""All-launches recorder: every pump.fun curve trade / create / completion and every pump-token PumpSwap trade, forward.

    uv run python -u -m tradeslave.research.launch_recorder        # runs until stopped (start_all table)

Why: research data must be an unbiased sample of ALL launches, rugs included (CLAUDE.md), and the pump.fun candle API
is gone (410). Two public-RPC websockets (logsSubscribe at `processed`, never Helius: its firehose burns credits):
  - pump.fun program: every TradeEvent (scalper.trade_events, verified 400/400) with exact virtual reserves; `c=1` if
    the transaction is a create; CompleteEvents (curve finished -> migration follows).
  - PumpSwap program: Buy/SellEvents decoded (layout verified 2026-10-30: 6,237/6,237 consecutive same-pool events
    chain exactly, i.e. event reserves are PRE-trade), kept only for pools whose quote is WSOL and base mint is a
    pump.fun token (pool accounts resolved in batches of 100 with getMultipleAccounts on the public RPC; others are
    dropped and counted). Other PumpSwap events (create pool, deposit, withdraw, ...) are kept raw.
Output, rotated hourly (multi-member gzip, safe to append after a crash):
  data/recorder/pump-YYYYMMDD-HH.jsonl.gz      [slot, i, ts, lag, mint#, user#, buy, sol_lamports, tok_raw, vs, vt, c]
  data/recorder/complete-YYYYMMDD-HH.jsonl.gz  [recv, slot, sig, user, mint, curve, ts]
  data/recorder/swap-YYYYMMDD-HH.jsonl.gz      [slot, ts, lag, buy, pool#, user#, base_amt, quote_lamports,
                                                pool_base_pre, pool_quote_pre]
  data/recorder/swapx-YYYYMMDD-HH.jsonl.gz     [recv, slot, sig, [instruction names], [base64 payloads]] pool lifecycle
Each stream listens on every free websocket at once (public RPC + RPCFast) and keeps the first copy of each
transaction (dedupe by signature), so one stalled connection doesn't open a gap. Signatures are not stored for trade
rows (disk budget 100-300 MB/day, Rohit 2026-10-30).
  data/recorder/pools.json                     pool -> [base_mint, quote_mint, kept]
  data/recorder/ids.jsonl                      [id, address]: `#` fields are ids into this append-only table
  lag = arrival time - chain block time (s, rounded)
  data/recorder/status.jsonl                   up / down / 10-min counts per stream (gaps are known, not guessed)
Paper only: read-only subscriptions, nothing is signed or sent.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import struct
import threading
import time
from collections import defaultdict, deque

import requests

from ..paths import DATA_DIR
from ..scalper.trade_events import b58encode, parse_trade_events

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
OUT = DATA_DIR / "recorder"
COMPLETE_EVENT = hashlib.sha256(b"event:CompleteEvent").digest()[:8]
BUY_EVENT = hashlib.sha256(b"event:BuyEvent").digest()[:8]
SELL_EVENT = hashlib.sha256(b"event:SellEvent").digest()[:8]
PUMP_TRADE_EVENT = hashlib.sha256(b"event:TradeEvent").digest()[:8]
WSOL = "So11111111111111111111111111111111111111112"
PUBLIC_HTTP = "https://api.mainnet-beta.solana.com"
LIFECYCLE = {"CreatePool", "Deposit", "Withdraw", "Migrate", "CollectCoinCreatorFee", "CreateConfig", "UpdateFeeConfig"}


class Ids:
    """Address -> small integer, persisted append-only in ids.jsonl (survives restarts; addresses are the bulk of disk)."""

    def __init__(self):
        self.path = OUT / "ids.jsonl"
        self.map, self.lock = {}, threading.Lock()
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    i, a = json.loads(line)
                except ValueError:
                    continue
                self.map[a] = i
        OUT.mkdir(parents=True, exist_ok=True)
        self.fh = self.path.open("a", encoding="utf-8")

    def __call__(self, addr: str) -> int:
        with self.lock:
            i = self.map.get(addr)
            if i is None:
                i = self.map[addr] = len(self.map)
                self.fh.write(json.dumps([i, addr]) + chr(10))
            return i

    def flush(self) -> None:
        with self.lock:
            self.fh.flush()


class Seen:
    """Rolling set of recent signatures per stream (duplicate copies from redundant websockets are dropped)."""

    def __init__(self, keep: int = 400_000):
        self.set, self.q, self.keep, self.lock = set(), deque(), keep, threading.Lock()

    def first(self, sig: str) -> bool:
        with self.lock:
            if sig in self.set:
                return False
            self.set.add(sig)
            self.q.append(sig)
            if len(self.q) > self.keep:
                self.set.discard(self.q.popleft())
            return True
log = logging.getLogger("recorder")


class Sink:
    """Hourly gzip files per stream; flushed every few seconds by the main loop."""

    def __init__(self, stream: str):
        self.stream, self.hour, self.fh, self.lock, self.n = stream, None, None, threading.Lock(), 0

    def write(self, row) -> None:
        line = json.dumps(row, separators=(",", ":")) + "\n"
        with self.lock:
            h = time.strftime("%Y%m%d-%H", time.gmtime())
            if h != self.hour:
                if self.fh:
                    self.fh.close()
                OUT.mkdir(parents=True, exist_ok=True)
                self.fh = gzip.open(OUT / f"{self.stream}-{h}.jsonl.gz", "at", encoding="utf-8")
                self.hour = h
            self.fh.write(line)
            self.n += 1

    def flush(self) -> None:
        with self.lock:
            if self.fh:
                self.fh.flush()


def status(**kw) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "status.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": round(time.time(), 1), **kw}) + "\n")


def parse_complete(logs) -> list[list]:
    out = []
    for line in logs or []:
        if isinstance(line, str) and line.startswith("Program data: "):
            try:
                raw = base64.b64decode(line[14:])
            except ValueError:
                continue
            if raw[:8] == COMPLETE_EVENT and len(raw) >= 8 + 96 + 8:
                user, mint, curve = (b58encode(raw[8 + 32 * k:8 + 32 * (k + 1)]) for k in range(3))
                ts = int.from_bytes(raw[104:112], "little", signed=True)
                out.append([user, mint, curve, ts])
    return out


def parse_swap(raw: bytes) -> list | None:
    """[buy, ts, pool, user, base_amt, quote_amt, pool_base_pre, pool_quote_pre] for a PumpSwap Buy/SellEvent."""
    if raw[:8] not in (BUY_EVENT, SELL_EVENT) or len(raw) < 184:
        return None
    ts, base_amt, _limit, _ub, _uq, pb, pq, quote_amt = struct.unpack_from("<q7Q", raw, 8)
    return [int(raw[:8] == BUY_EVENT), ts, b58encode(raw[120:152]), b58encode(raw[152:184]), base_amt, quote_amt, pb, pq]


class Pools:
    """pool -> [base_mint, quote_mint, kept]; events of unresolved pools wait in memory (bounded) until resolved."""

    def __init__(self, sink: Sink, fmt):
        self.fmt = fmt                       # (pool, row) -> output row
        self.path = OUT / "pools.json"
        self.map = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.wait: dict[str, deque] = defaultdict(lambda: deque(maxlen=5000))
        self.lock, self.sink, self.dropped = threading.Lock(), sink, 0

    def add(self, row) -> None:
        pool = row[0]
        with self.lock:
            m = self.map.get(pool)
            if m is None:
                self.wait[pool].append(row)
                return
        if m[2]:
            self.sink.write(self.fmt(row))
        else:
            self.dropped += 1

    def resolve_loop(self) -> None:
        s = requests.Session()
        while True:
            time.sleep(2)
            with self.lock:
                todo = list(self.wait)[:100]
            if not todo:
                continue
            try:
                r = s.post(PUBLIC_HTTP, json={"jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
                                              "params": [todo, {"encoding": "base64",
                                                                "dataSlice": {"offset": 43, "length": 64}}]},
                           timeout=30).json()
                vals = r["result"]["value"]
            except Exception:  # noqa: BLE001 - rate limited or down: try again next round
                time.sleep(5)
                continue
            with self.lock:
                for pool, v in zip(todo, vals):
                    if not v:
                        continue
                    raw = base64.b64decode(v["data"][0])
                    base, quote = b58encode(raw[:32]), b58encode(raw[32:64])
                    keep = quote == WSOL and base.endswith("pump")
                    self.map[pool] = [base, quote, int(keep)]
                    rows = self.wait.pop(pool, ())
                    if keep:
                        for row in rows:
                            self.sink.write(self.fmt(row))
                    else:
                        self.dropped += len(rows)
                self.path.write_text(json.dumps(self.map))


def listen(program: str, name: str, handle, url: str, seen: Seen) -> None:
    from websockets.sync.client import connect
    host = url.split("//")[1].split("/")[0]
    while True:
        try:
            with connect(url, open_timeout=20, max_size=2 ** 24, ping_interval=20) as ws:
                ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                    "params": [{"mentions": [program]}, {"commitment": "processed"}]}))
                status(stream=name, ev="up", host=host)
                log.info("%s up on %s", name, host)
                while True:
                    msg = json.loads(ws.recv(timeout=60))
                    p = msg.get("params")
                    if not p:
                        continue
                    v = p["result"]["value"]
                    if v.get("err") is not None or not seen.first(v.get("signature") or ""):
                        continue
                    handle(time.time(), p["result"]["context"]["slot"], v.get("signature"), v.get("logs") or [])
        except Exception as exc:  # noqa: BLE001 - reconnect; the gap is logged
            status(stream=name, ev="down", host=host, why=type(exc).__name__)
            log.warning("%s down (%s); reconnecting in 3 s", name, type(exc).__name__)
            time.sleep(3)


def run() -> None:
    from .history_miner import endpoints
    wss = [u.replace("https://", "wss://", 1) for u, _ in endpoints(3.0)
           if not any(x in u for x in ("helius", "quiknode", "alchemy", "shyft"))]
    pump, comp, swap, swapx = Sink("pump"), Sink("complete"), Sink("swap"), Sink("swapx")
    ids = Ids()
    # swap rows wait for pool resolution as (pool, slot, ts, lag, buy, user, base, quote, pb, pq)
    pools = Pools(swap, lambda r: [r[1], r[2], r[3], r[4], ids(r[0]), ids(r[5]), *r[6:]])
    threading.Thread(target=pools.resolve_loop, daemon=True).start()

    def on_pump(now, slot, sig, logs):
        evs = parse_trade_events(logs)
        if evs:
            c = int(any("Instruction: Create" in x for x in logs))
            for i, e in enumerate(evs):
                pump.write([slot, i, e["ts"], round(now - e["ts"]), ids(e["mint"]), ids(e["user"]), int(e["buy"]),
                            e["sol"], e["tok"], e["vs"], e["vt"], c])
        for row in parse_complete(logs):
            comp.write([round(now, 3), slot, sig, *row])

    def on_swap(now, slot, sig, logs):
        other = []
        for x in logs:
            if not x.startswith("Program data: "):
                continue
            try:
                raw = base64.b64decode(x[14:])
            except ValueError:
                continue
            ev = parse_swap(raw)
            if ev:
                buy, ts, pool, user, *amts = ev
                pools.add([pool, slot, ts, round(now - ts), buy, user, *amts])
            elif raw[:8] != PUMP_TRADE_EVENT:
                other.append(x[14:])
        if other:
            ins = [x[len("Program log: Instruction: "):] for x in logs if x.startswith("Program log: Instruction: ")]
            if set(ins) & LIFECYCLE:
                swapx.write([round(now, 3), slot, sig, ins, other])

    for program, name, handle in ((PUMP, "pump", on_pump), (PUMPSWAP, "swap", on_swap)):
        seen = Seen()
        for url in wss:
            threading.Thread(target=listen, args=(program, name, handle, url, seen), daemon=True).start()
    sinks = (pump, comp, swap, swapx)
    last = {s.stream: 0 for s in sinks}
    t_note = time.time()
    while True:
        time.sleep(30)
        for s in sinks:
            s.flush()
        ids.flush()
        if time.time() - t_note >= 600:
            t_note = time.time()
            counts = {s.stream: s.n - last[s.stream] for s in sinks}
            last = {s.stream: s.n for s in sinks}
            status(ev="count", dropped_pool_events=pools.dropped, pools=len(pools.map), **counts)
            log.info("last 10 min: %s, dropped %d, pools %d", counts, pools.dropped, len(pools.map))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    run()


if __name__ == "__main__":
    main()
