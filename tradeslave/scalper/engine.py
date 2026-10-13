"""Live paper engine: discover fresh pump.fun tokens and scalp many at once.

Data (all free):
  * PumpPortal websocket `subscribeNewToken` - every new pump.fun token, with
    its bonding-curve address and starting reserves.
  * Solana RPC websocket `accountSubscribe` on each token's bonding curve -
    a message on every change, which is every buy or sell.
One asyncio process handles all tokens concurrently; the bottleneck is the
network, not the CPU, so threads or extra processes would not add speed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler

from ..paths import LOG_DIR, PROJECT_ROOT
from ..state import append_jsonl, save_json
from .config import ScalpConfig, load_config
from .core import Scalper
from .curve import decode_bonding_curve, state_from_pumpportal
from .recorder import TickRecorder

log = logging.getLogger("scalper")
RAW_SAMPLE_LIMIT = 25


class LiveEngine:
    def __init__(self, cfg: ScalpConfig):
        self.cfg = cfg
        self.scalper = Scalper(cfg, on_event=self._on_event)
        self.recorder = TickRecorder() if cfg.record_ticks else None
        self.rpc_ws = None
        self.rpc_ready = asyncio.Event()
        self.next_id = 1
        self.pending_subs: dict[int, str] = {}   # request id -> mint
        self.sub_by_mint: dict[str, int] = {}
        self.mint_by_sub: dict[int, str] = {}
        self.watch: dict[str, str] = {}           # mint -> curve account
        self.stats = {"created_seen": 0, "skipped_full": 0, "skipped_filter": 0, "curve_msgs": 0,
                      "decode_fail": 0, "pp_reconnects": 0, "rpc_reconnects": 0}
        self.raw_samples = 0
        self.started = time.time()
        self._restore_balance()

    # --- persistence --------------------------------------------------------
    def _restore_balance(self) -> None:
        path = PROJECT_ROOT / "scalp_wallet.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            balance = float(data["balance_sol"])
            carried = 0.0
            for p in data.get("open_positions", []):
                carried += float(p["cost_sol"]) * (1.0 + float(p.get("last_pnl_pct", 0.0)))
            self.scalper.book.balance_sol = balance + carried
            if carried:
                log.warning("Previous run left %d open paper positions; credited at last mark (%.4f SOL)",
                            len(data.get("open_positions", [])), carried)
            log.info("Restored paper balance %.4f SOL", self.scalper.book.balance_sol)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("Could not restore scalp_wallet.json (%s); starting at %.4f SOL", exc, self.cfg.paper_start_sol)

    def _save_wallet(self) -> None:
        snap = self.scalper.book.snapshot()
        snap["equity_sol"] = self.scalper.book.equity_sol(self.scalper.trackers)
        snap["updated_at"] = time.time()
        snap["watched"] = len(self.watch)
        save_json("scalp_wallet.json", snap)

    def _on_event(self, kind: str, data: dict) -> None:
        record = {"event": kind, **data}
        if kind == "trade_closed":
            append_jsonl("scalp_trades.jsonl", record)
            log.info("CLOSED %s %s %+.2f%% (%+.5f SOL) held %.1fs reason=%s balance=%.4f",
                     data["symbol"], data["kind"], data["pnl_pct"] * 100, data["pnl_sol"], data["held_s"],
                     data["exit_reason"], data["balance_sol"])
        elif kind == "buy_filled":
            log.info("BUY %s at %.3e SOL cost=%.4f", data["symbol"], data["price"], data["cost_sol"])
        append_jsonl("scalp_events.jsonl", record)

    # --- PumpPortal: new tokens ---------------------------------------------
    async def pumpportal_loop(self, websockets) -> None:
        url = self.cfg.pumpportal_ws
        key = os.getenv("PUMPPORTAL_API_KEY", "").strip()
        if key:
            url = f"{url}?api-key={key}"
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**20) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    log.info("PumpPortal connected; subscribed to new tokens")
                    backoff = 1.0
                    async for raw in ws:
                        self._on_pumpportal(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network errors: reconnect
                self.stats["pp_reconnects"] += 1
                log.warning("PumpPortal connection lost (%s); reconnecting in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _sample(self, kind: str, raw) -> None:
        """Keep the first few raw messages so field formats can be checked by hand."""
        if self.raw_samples >= RAW_SAMPLE_LIMIT:
            return
        self.raw_samples += 1
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with (LOG_DIR / "scalper_raw_samples.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"kind": kind, "raw": str(raw)[:4000]}) + "\n")
        except OSError as exc:
            log.debug("Could not save raw sample: %s", exc)

    def _on_pumpportal(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(msg, dict) or msg.get("txType") != "create":
            return
        self._sample("pumpportal_create", raw)
        if msg.get("pool") not in (None, "pump"):
            return  # other launchpads relayed by PumpPortal
        mint, curve_key = msg.get("mint"), msg.get("bondingCurveKey")
        if not isinstance(mint, str) or not isinstance(curve_key, str):
            return
        self.stats["created_seen"] += 1
        state = state_from_pumpportal(msg)
        initial_buy_sol = max(0.0, state.virtual_sol - 30.0) if state else 0.0
        if not (self.cfg.min_initial_buy_sol <= initial_buy_sol <= self.cfg.max_initial_buy_sol):
            self.stats["skipped_filter"] += 1
            return
        if len(self.watch) >= self.cfg.max_watched:
            self.stats["skipped_full"] += 1
            return
        now = time.time()
        symbol = str(msg.get("symbol") or "?")[:20]
        creator = str(msg.get("traderPublicKey") or "")
        self.scalper.on_create(mint, symbol, now, state, creator, curve_key, initial_buy_sol)
        if self.recorder:
            self.recorder.create(now, mint, symbol, state, creator, curve_key, initial_buy_sol)
        self.watch[mint] = curve_key
        if self.rpc_ready.is_set():
            asyncio.get_running_loop().create_task(self._subscribe(mint, curve_key))

    # --- RPC: bonding curve updates -----------------------------------------
    async def _send(self, payload: dict) -> None:
        if self.rpc_ws is not None:
            await self.rpc_ws.send(json.dumps(payload))

    async def _subscribe(self, mint: str, curve_key: str) -> None:
        request_id = self.next_id
        self.next_id += 1
        self.pending_subs[request_id] = mint
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": "accountSubscribe",
                          "params": [curve_key, {"encoding": "base64", "commitment": "processed"}]})

    async def _unsubscribe(self, mint: str) -> None:
        self.watch.pop(mint, None)
        sub = self.sub_by_mint.pop(mint, None)
        if sub is not None:
            self.mint_by_sub.pop(sub, None)
            request_id = self.next_id
            self.next_id += 1
            try:
                await self._send({"jsonrpc": "2.0", "id": request_id, "method": "accountUnsubscribe", "params": [sub]})
            except Exception:
                pass

    async def rpc_loop(self, websockets) -> None:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.cfg.rpc_ws, ping_interval=20, ping_timeout=20,
                                              max_size=2**22) as ws:
                    self.rpc_ws = ws
                    self.pending_subs.clear()
                    self.sub_by_mint.clear()
                    self.mint_by_sub.clear()
                    self.rpc_ready.set()
                    log.info("Solana RPC connected (%s); resubscribing %d curves", self.cfg.rpc_ws, len(self.watch))
                    for mint, curve_key in list(self.watch.items()):
                        await self._subscribe(mint, curve_key)
                    backoff = 1.0
                    async for raw in ws:
                        self._on_rpc(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats["rpc_reconnects"] += 1
                log.warning("RPC connection lost (%s); reconnecting in %.0fs", exc, backoff)
            finally:
                self.rpc_ready.clear()
                self.rpc_ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def _on_rpc(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if "id" in msg and msg.get("id") in self.pending_subs:
            mint = self.pending_subs.pop(msg["id"])
            if "result" in msg and mint in self.watch:
                self.sub_by_mint[mint] = msg["result"]
                self.mint_by_sub[msg["result"]] = mint
            elif "result" in msg:
                # Token was dropped while the subscription was in flight: release it.
                asyncio.get_running_loop().create_task(self._send(
                    {"jsonrpc": "2.0", "id": 0, "method": "accountUnsubscribe", "params": [msg["result"]]}))
            elif "error" in msg:
                log.warning("accountSubscribe failed for %s: %s", mint, msg["error"])
                self.watch.pop(mint, None)
                self.scalper.forget(mint)
            return
        if msg.get("method") != "accountNotification":
            return
        params = msg.get("params") or {}
        mint = self.mint_by_sub.get(params.get("subscription"))
        if mint is None:
            return
        self.stats["curve_msgs"] += 1
        if self.stats["curve_msgs"] <= 5:
            self._sample("rpc_account_notification", raw)
        result = params.get("result") or {}
        value = result.get("value") or {}
        data = value.get("data")
        state = decode_bonding_curve(data[0]) if isinstance(data, list) and data else None
        if state is None:
            self.stats["decode_fail"] += 1
            return
        now = time.time()
        if self.recorder:
            self.recorder.curve(now, mint, state, (result.get("context") or {}).get("slot"))
        self.scalper.on_curve(mint, now, state)

    # --- clock, housekeeping, status ----------------------------------------
    async def clock_loop(self) -> None:
        last_status = last_save = time.time()
        while True:
            await asyncio.sleep(0.2)
            now = time.time()
            self.scalper.on_clock(now)
            for mint in list(self.watch):
                tr = self.scalper.trackers.get(mint)
                if tr is None:
                    await self._unsubscribe(mint)
                    continue
                idle = now - (tr.last_update_t or tr.created_at)
                if tr.age(now) > self.cfg.watch_max_age_s or idle > self.cfg.idle_unwatch_s or \
                        (tr.state is not None and tr.state.complete):
                    if self.scalper.forget(mint):
                        await self._unsubscribe(mint)
            if now - last_save >= 5:
                self._save_wallet()
                last_save = now
            if now - last_status >= self.cfg.status_every_s:
                self._status(now)
                last_status = now

    def _status(self, now: float) -> None:
        book = self.scalper.book
        closed = book.closed
        wins = sum(1 for c in closed if c["pnl_sol"] > 0)
        pnl = sum(c["pnl_sol"] for c in closed)
        avg = (sum(c["pnl_pct"] for c in closed) / len(closed)) if closed else 0.0
        blocks = sorted(self.scalper.entry_block_reasons.items(), key=lambda kv: -kv[1])[:5]
        log.info(
            "STATUS up=%.0fm watched=%d open=%d balance=%.4f equity=%.4f | trades=%d wins=%d (%.0f%%) "
            "avg=%+.2f%% pnl=%+.4f SOL fees=%.4f failed_tx=%d | new_tokens=%d curve_msgs=%d decode_fail=%d "
            "skipped_full=%d | top_entry_blocks=%s",
            (now - self.started) / 60, len(self.watch), len(book.positions), book.balance_sol,
            book.equity_sol(self.scalper.trackers), len(closed), wins, 100 * wins / len(closed) if closed else 0,
            avg * 100, pnl, book.fees_paid_sol, book.failed_tx, self.stats["created_seen"],
            self.stats["curve_msgs"], self.stats["decode_fail"], self.stats["skipped_full"], blocks,
        )
        if self.stats["curve_msgs"] > 50 and self.stats["decode_fail"] > self.stats["curve_msgs"] * 0.5:
            log.error("Most curve updates fail to decode - pump.fun may have changed its account layout. "
                      "See logs/scalper_raw_samples.jsonl")

    async def run(self) -> None:
        try:
            import websockets
        except ImportError as exc:
            raise SystemExit("The scalper needs the 'websockets' package: run `uv sync`") from exc
        tasks = [
            asyncio.create_task(self.rpc_loop(websockets), name="rpc"),
            asyncio.create_task(self.pumpportal_loop(websockets), name="pumpportal"),
            asyncio.create_task(self.clock_loop(), name="clock"),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            self._save_wallet()
            if self.recorder:
                self.recorder.close()


def _lock(name: str):
    handle = (PROJECT_ROOT / name).open("a+b")
    if handle.seek(0, os.SEEK_END) == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(),
                  RotatingFileHandler(LOG_DIR / "scalper.log", maxBytes=5 * 1024 * 1024, backupCount=5,
                                      encoding="utf-8")],
    )
    cfg = load_config()
    lock = _lock(".scalper.lock")
    if lock is None:
        log.error("Another scalper instance is already running; exiting")
        return
    log.info("Scalper (PAPER) starting: size=%.3f SOL, max_open=%d, watch=%d, TP=%+.1f%% SL=-%.1f%% "
             "max_hold=%.0fs, fee=%.2f%%/side, latency=%.1fs, fail_rate=%.0f%%",
             cfg.trade_size_sol, cfg.max_open_positions, cfg.max_watched, cfg.take_profit_pct * 100,
             cfg.stop_loss_pct * 100, cfg.max_hold_s, cfg.fee_bps / 100, cfg.latency_s, cfg.fail_rate * 100)
    try:
        asyncio.run(LiveEngine(cfg).run())
    except KeyboardInterrupt:
        log.info("Scalper stopped")
