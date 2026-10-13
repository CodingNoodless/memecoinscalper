"""Live paper engine: discover fresh pump.fun tokens and scalp many at once.

Data (all free):
  * PumpPortal websocket `subscribeNewToken` - every new pump.fun token, with
    its bonding-curve address, starting reserves and the dev's buy.
  * Solana RPC websocket on one connection:
      - `accountSubscribe` on each token's bonding curve: a message on every
        buy or sell, giving the exact curve state;
      - `logsSubscribe` on each creator wallet: flags the dev selling.
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
from ..state import append_jsonl, save_json, state_path
from .config import ScalpConfig, load_config
from .core import Scalper
import dataclasses

from .curve import GRADUATED, decode_bonding_curve, state_from_pumpportal
from .recorder import TickRecorder
from .socials import fetch_metadata, parse_socials
from .strategy import entry_signal, safety_block

log = logging.getLogger("scalper")
RAW_SAMPLE_LIMIT = 25


class LiveEngine:
    def __init__(self, cfg: ScalpConfig):
        self.cfg = cfg
        self.scalper = Scalper(cfg, on_event=self._on_event)
        self.recorder = TickRecorder() if cfg.record_ticks else None
        self.rpc_ws = None
        self.rpc_ready = asyncio.Event()
        self.stopping = False
        self.next_id = 1
        self.pending: dict[int, tuple[str, str]] = {}      # request id -> (kind, key)
        self.curve_sub: dict[str, int] = {}                # mint -> subscription
        self.mint_by_curve_sub: dict[int, str] = {}
        self.creator_sub: dict[str, int] = {}              # creator -> subscription
        self.creator_by_sub: dict[int, str] = {}
        self.creator_mints: dict[str, set[str]] = {}       # creator -> watched mints
        self.watch: dict[str, str] = {}                    # mint -> curve account
        self.stats = {"created_seen": 0, "skipped_full": 0, "skipped_filter": 0, "curve_msgs": 0,
                      "decode_fail": 0, "graduated": 0, "meta_ok": 0, "meta_fail": 0, "with_twitter": 0, "handler_errors": 0, "creator_msgs": 0, "creator_sell_logs": 0, "dev_sells": 0, "pp_reconnects": 0,
                      "rpc_reconnects": 0, "sub_errors": 0}
        self.raw_samples = 0
        self.meta_slots = asyncio.Semaphore(6)
        self.started = time.time()
        self._restore_balance()

    # --- persistence --------------------------------------------------------
    def _restore_balance(self) -> None:
        path = state_path("scalp_wallet.json")
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            balance = float(data["balance_sol"])
            carried = sum(float(p["cost_sol"]) * (1.0 + float(p.get("last_pnl_pct", 0.0)))
                          for p in data.get("open_positions", []))
            self.scalper.book.balance_sol = balance + carried
            if carried:
                log.warning("Previous run left %d open paper positions; credited at last mark (%.4f SOL)",
                            len(data.get("open_positions", [])), carried)
            log.info("Restored paper balance %.4f SOL", self.scalper.book.balance_sol)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("Could not restore scalp_wallet.json (%s); starting at %.4f SOL", exc, self.cfg.paper_start_sol)

    def _watchlist(self, now: float) -> list[dict]:
        rows = []
        for mint in self.watch:
            tr = self.scalper.trackers.get(mint)
            if tr is None or not tr.launch_price:
                continue
            signal = entry_signal(tr, now, self.cfg) if mint not in self.scalper.book.positions else None
            early = tr.early_supply_pct(self.cfg.early_window_s)
            rows.append({
                "mint": mint, "symbol": tr.symbol, "age_s": round(tr.age(now), 1),
                "vs_opening": round(tr.price / tr.launch_price - 1.0, 4),
                "rise": round(tr.peak_price / tr.launch_price - 1.0, 4),
                "dev_supply_pct": round(tr.dev_supply_pct, 4),
                "early_supply_pct": None if early is None else round(early, 4),
                "safety": safety_block(tr, now, self.cfg),
                "status": "in position" if mint in self.scalper.book.positions else (signal.reason if signal else ""),
                "entries": tr.entries, "updates": len(tr.ticks), "creator_launches": tr.creator_launches,
                "socials": None if tr.socials is None else (tr.socials.get("twitter_kind") or
                                                            ("telegram/web" if tr.socials.get("has_any") else "none")),
            })
        rows.sort(key=lambda r: r["age_s"])
        return rows

    def _save_wallet(self) -> None:
        now = time.time()
        book = self.scalper.book
        snap = book.snapshot()
        snap.update({
            "equity_sol": book.equity_sol(self.scalper.trackers), "updated_at": now, "started_at": self.started,
            "paper_start_sol": self.cfg.paper_start_sol, "watched": len(self.watch), "stats": self.stats,
            "entry_block_reasons": dict(sorted(self.scalper.entry_block_reasons.items(), key=lambda kv: -kv[1])[:12]),
            "watchlist": self._watchlist(now),
            "settings": {k: getattr(self.cfg, k) for k in (
                "entry_mode", "entry_confirm", "min_initial_rise_pct", "zone_above_pct", "zone_below_pct",
                "rug_floor_pct", "max_dev_supply_pct", "max_early_supply_pct", "require_socials", "take_profit_pct",
                "stop_loss_pct", "max_hold_s", "trade_size_sol", "max_open_positions", "fee_bps", "latency_s")},
        })
        for p in snap["open_positions"]:
            tr = self.scalper.trackers.get(p["mint"])
            if tr is not None and tr.launch_price:
                p["vs_opening"] = round(tr.price / tr.launch_price - 1.0, 4)
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
        elif kind == "dev_sold":
            self.stats["dev_sells"] += 1
            log.info("DEV SOLD %s (%s): token blocked", data["symbol"], data["how"])
        append_jsonl("scalp_events.jsonl", record)

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

    # --- PumpPortal: new tokens ---------------------------------------------
    async def pumpportal_loop(self, websockets) -> None:
        url = self.cfg.pumpportal_ws
        key = os.getenv("PUMPPORTAL_API_KEY", "").strip()
        if key:
            url = f"{url}?api-key={key}"
        backoff = 1.0
        while not self.stopping:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**20) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    log.info("PumpPortal connected; subscribed to new tokens")
                    backoff = 1.0
                    async for raw in ws:
                        self._guard(self._on_pumpportal, raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network errors: reconnect
                self.stats["pp_reconnects"] += 1
                log.warning("PumpPortal connection lost (%s); reconnecting in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

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
        if not isinstance(mint, str) or not isinstance(curve_key, str) or mint in self.watch:
            return
        self.stats["created_seen"] += 1
        state = state_from_pumpportal(msg)
        try:
            initial_buy_sol = float(msg["solAmount"])
        except (KeyError, TypeError, ValueError):
            initial_buy_sol = max(0.0, state.virtual_sol - 30.0) if state else 0.0
        try:
            dev_tokens = float(msg.get("initialBuy") or 0.0)
        except (TypeError, ValueError):
            dev_tokens = 0.0
        if not (self.cfg.min_initial_buy_sol <= initial_buy_sol <= self.cfg.max_initial_buy_sol):
            self.stats["skipped_filter"] += 1
            return
        if len(self.watch) >= self.cfg.max_watched:
            self.stats["skipped_full"] += 1
            return
        now = time.time()
        symbol = str(msg.get("symbol") or "?")[:20]
        creator = str(msg.get("traderPublicKey") or "")
        mayhem = msg.get("is_mayhem_mode") is True
        self.scalper.on_create(mint, symbol, now, state, creator, curve_key, initial_buy_sol, dev_tokens, mayhem)
        if self.recorder:
            self.recorder.create(now, mint, symbol, state, creator, curve_key, initial_buy_sol, dev_tokens, mayhem)
        self.watch[mint] = curve_key
        uri = msg.get("uri")
        try:
            asyncio.get_running_loop().create_task(self._load_metadata(mint, uri))
        except RuntimeError:
            pass   # no running loop (offline tests)
        if self.rpc_ready.is_set():
            loop = asyncio.get_running_loop()
            loop.create_task(self._subscribe_curve(mint, curve_key))
            if creator and self.cfg.watch_creator:
                loop.create_task(self._subscribe_creator(creator, mint))
        elif creator:
            self.creator_mints.setdefault(creator, set()).add(mint)

    async def _load_metadata(self, mint: str, uri) -> None:
        """Fetch the token's metadata (socials, description) without blocking the loop."""
        async with self.meta_slots:
            meta = await asyncio.to_thread(fetch_metadata, uri, self.cfg.metadata_timeout_s) if uri else None
        socials = parse_socials(meta)
        self.stats["meta_ok" if socials["fetched"] else "meta_fail"] += 1
        if socials["twitter"]:
            self.stats["with_twitter"] += 1
        now = time.time()
        if self.recorder:
            self.recorder.metadata(now, mint, socials)
        try:
            self.scalper.on_metadata(mint, now, socials)
        except Exception:
            self.stats["handler_errors"] += 1
            log.exception("Error applying metadata for %s", mint)

    # --- RPC: bonding curves + creator wallets --------------------------------
    async def _send(self, payload: dict) -> None:
        if self.rpc_ws is not None:
            await self.rpc_ws.send(json.dumps(payload))

    def _request_id(self) -> int:
        request_id = self.next_id
        self.next_id += 1
        return request_id

    async def _subscribe_curve(self, mint: str, curve_key: str) -> None:
        request_id = self._request_id()
        self.pending[request_id] = ("curve", mint)
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": "accountSubscribe",
                          "params": [curve_key, {"encoding": "base64", "commitment": "processed"}]})

    async def _subscribe_creator(self, creator: str, mint: str) -> None:
        mints = self.creator_mints.setdefault(creator, set())
        mints.add(mint)
        if creator in self.creator_sub or any(k == ("creator", creator) for k in self.pending.values()):
            return
        request_id = self._request_id()
        self.pending[request_id] = ("creator", creator)
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": "logsSubscribe",
                          "params": [{"mentions": [creator]}, {"commitment": "processed"}]})

    async def _unsubscribe(self, mint: str) -> None:
        self.watch.pop(mint, None)
        sub = self.curve_sub.pop(mint, None)
        if sub is not None:
            self.mint_by_curve_sub.pop(sub, None)
            await self._safe_send({"jsonrpc": "2.0", "id": self._request_id(), "method": "accountUnsubscribe",
                                   "params": [sub]})
        for creator, mints in list(self.creator_mints.items()):
            if mint in mints:
                mints.discard(mint)
                if not mints:
                    del self.creator_mints[creator]
                    csub = self.creator_sub.pop(creator, None)
                    if csub is not None:
                        self.creator_by_sub.pop(csub, None)
                        await self._safe_send({"jsonrpc": "2.0", "id": self._request_id(),
                                               "method": "logsUnsubscribe", "params": [csub]})

    async def _safe_send(self, payload: dict) -> None:
        try:
            await self._send(payload)
        except Exception:
            pass

    async def rpc_loop(self, websockets) -> None:
        backoff = 1.0
        while not self.stopping:
            try:
                async with websockets.connect(self.cfg.rpc_ws, ping_interval=20, ping_timeout=20,
                                              max_size=2**22) as ws:
                    self.rpc_ws = ws
                    self.pending.clear()
                    self.curve_sub.clear()
                    self.mint_by_curve_sub.clear()
                    self.creator_sub.clear()
                    self.creator_by_sub.clear()
                    self.rpc_ready.set()
                    log.info("Solana RPC connected (%s); resubscribing %d curves", self.cfg.rpc_ws, len(self.watch))
                    for mint, curve_key in list(self.watch.items()):
                        await self._subscribe_curve(mint, curve_key)
                    if self.cfg.watch_creator:
                        for creator, mints in list(self.creator_mints.items()):
                            for mint in list(mints):
                                await self._subscribe_creator(creator, mint)
                    backoff = 1.0
                    async for raw in ws:
                        self._guard(self._on_rpc, raw)
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
        if "id" in msg and msg.get("id") in self.pending:
            self._on_subscribe_reply(msg)
            return
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "accountNotification":
            self._on_curve_notification(params, raw)
        elif method == "logsNotification":
            self._on_creator_logs(params)

    def _on_subscribe_reply(self, msg: dict) -> None:
        kind, key = self.pending.pop(msg["id"])
        if "error" in msg:
            self.stats["sub_errors"] += 1
            log.warning("%s subscription failed for %s: %s", kind, key, msg["error"])
            if kind == "curve":
                self.watch.pop(key, None)
                self.scalper.forget(key)
            return
        sub = msg.get("result")
        if kind == "curve":
            if key in self.watch:
                self.curve_sub[key] = sub
                self.mint_by_curve_sub[sub] = key
            else:  # dropped while the request was in flight
                asyncio.get_running_loop().create_task(self._safe_send(
                    {"jsonrpc": "2.0", "id": 0, "method": "accountUnsubscribe", "params": [sub]}))
        else:
            if self.creator_mints.get(key):
                self.creator_sub[key] = sub
                self.creator_by_sub[sub] = key
            else:
                asyncio.get_running_loop().create_task(self._safe_send(
                    {"jsonrpc": "2.0", "id": 0, "method": "logsUnsubscribe", "params": [sub]}))

    def _on_curve_notification(self, params: dict, raw: str) -> None:
        mint = self.mint_by_curve_sub.get(params.get("subscription"))
        if mint is None:
            return
        self.stats["curve_msgs"] += 1
        if self.stats["curve_msgs"] <= 5:
            self._sample("rpc_account_notification", raw)
        result = params.get("result") or {}
        value = result.get("value") or {}
        data = value.get("data")
        state = decode_bonding_curve(data[0]) if isinstance(data, list) and data else None
        if state is GRADUATED:
            # Curve finished and migrated to an AMM pool: stop trading it here.
            self.stats["graduated"] += 1
            tr = self.scalper.trackers.get(mint)
            if tr is not None and tr.state is not None and not tr.state.complete:
                tr.state = dataclasses.replace(tr.state, complete=True)
            return
        if state is None:
            self.stats["decode_fail"] += 1
            if self.stats["decode_fail"] <= 5:
                self._sample("decode_fail", raw)
            return
        now = time.time()
        if self.recorder:
            self.recorder.curve(now, mint, state, (result.get("context") or {}).get("slot"))
        self.scalper.on_curve(mint, now, state)

    def _on_creator_logs(self, params: dict) -> None:
        creator = self.creator_by_sub.get(params.get("subscription"))
        if creator is None:
            return
        self.stats["creator_msgs"] += 1
        value = (params.get("result") or {}).get("value") or {}
        if value.get("err") is not None:
            return
        logs = value.get("logs") or []
        if not any("Instruction: Sell" in line for line in logs if isinstance(line, str)):
            return
        now = time.time()
        self.stats["creator_sell_logs"] += 1
        if self.stats["creator_sell_logs"] <= 10:
            self._sample("creator_sell_logs", json.dumps(value)[:4000])
        for mint in list(self.creator_mints.get(creator, ())):
            if self.recorder:
                self.recorder.creator_sell(now, mint)
            self.scalper.on_creator_sell(mint, now)

    def _guard(self, handler, raw) -> None:
        """One bad message must never take the engine down."""
        try:
            handler(raw)
        except Exception:
            self.stats["handler_errors"] += 1
            log.exception("Error handling a %s message; continuing", handler.__name__)

    # --- clock, housekeeping, status ----------------------------------------
    async def clock_loop(self) -> None:
        last_status = last_save = time.time()
        while True:
            await asyncio.sleep(0.2)
            try:
                last_status, last_save = await self._clock_tick(last_status, last_save)
            except Exception:
                self.stats["handler_errors"] += 1
                log.exception("Error in the clock loop; continuing")

    async def _clock_tick(self, last_status: float, last_save: float) -> tuple[float, float]:
        now = time.time()
        self.scalper.on_clock(now)
        for mint in list(self.watch):
            tr = self.scalper.trackers.get(mint)
            if tr is None:
                await self._unsubscribe(mint)
                continue
            idle = now - (tr.last_update_t or tr.created_at)
            done = tr.age(now) > self.cfg.watch_max_age_s or idle > self.cfg.idle_unwatch_s or \
                (tr.state is not None and tr.state.complete) or tr.dev_sold or tr.floor_broken or \
                tr.creator_launches > self.cfg.serial_launch_max
            if done and self.scalper.forget(mint):
                await self._unsubscribe(mint)
        if now - last_save >= 2:
            self._save_wallet()
            last_save = now
        if now - last_status >= self.cfg.status_every_s:
            self._status(now)
            last_status = now
        return last_status, last_save

    def _status(self, now: float) -> None:
        book = self.scalper.book
        closed = book.closed
        wins = sum(1 for c in closed if c["pnl_sol"] > 0)
        pnl = sum(c["pnl_sol"] for c in closed)
        avg = (sum(c["pnl_pct"] for c in closed) / len(closed)) if closed else 0.0
        blocks = sorted(self.scalper.entry_block_reasons.items(), key=lambda kv: -kv[1])[:6]
        log.info(
            "STATUS up=%.0fm watched=%d open=%d balance=%.4f equity=%.4f | trades=%d wins=%d (%.0f%%) "
            "avg=%+.2f%% pnl=%+.4f SOL fees=%.4f failed_tx=%d | new_tokens=%d curve_msgs=%d decode_fail=%d "
            "dev_sells=%d (creator sell logs %d) graduated=%d errors=%d skipped_full=%d | metadata ok=%d fail=%d "
            "twitter=%d | top_entry_blocks=%s",
            (now - self.started) / 60, len(self.watch), len(book.positions), book.balance_sol,
            book.equity_sol(self.scalper.trackers), len(closed), wins, 100 * wins / len(closed) if closed else 0,
            avg * 100, pnl, book.fees_paid_sol, book.failed_tx, self.stats["created_seen"],
            self.stats["curve_msgs"], self.stats["decode_fail"], self.stats["dev_sells"],
            self.stats["creator_sell_logs"], self.stats["graduated"], self.stats["handler_errors"],
            self.stats["skipped_full"], self.stats["meta_ok"], self.stats["meta_fail"], self.stats["with_twitter"], blocks,
        )
        if self.stats["curve_msgs"] > 50 and self.stats["decode_fail"] > self.stats["curve_msgs"] * 0.5:
            log.error("Most curve updates fail to decode - pump.fun may have changed its account layout. "
                      "See logs/scalper_raw_samples.jsonl")

    def _loop_exception(self, loop, context) -> None:
        # Windows closes SSL sockets noisily during Ctrl+C shutdown; that is harmless.
        if self.stopping:
            log.debug("Ignored during shutdown: %s", context.get("message"))
            return
        loop.default_exception_handler(context)

    async def run(self) -> None:
        try:
            import websockets
        except ImportError as exc:
            raise SystemExit("The scalper needs the 'websockets' package: run `uv sync`") from exc
        asyncio.get_running_loop().set_exception_handler(self._loop_exception)
        tasks = [
            asyncio.create_task(self.rpc_loop(websockets), name="rpc"),
            asyncio.create_task(self.pumpportal_loop(websockets), name="pumpportal"),
            asyncio.create_task(self.clock_loop(), name="clock"),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            self.stopping = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
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
    log.info("Scalper (PAPER) starting: mode=%s confirm=%s | rise>=%.0f%% zone=[-%.0f%%, +%.0f%%] of opening "
             "| filters: dev<=%.0f%% snipers<=%.0f%% dev-sell watch=%s socials=%s | TP=%+.1f%% SL=-%.1f%% max_hold=%.0fs "
             "| size=%.3f SOL max_open=%d watch=%d | fee=%.2f%%/side latency=%.1fs fail=%.0f%%",
             cfg.entry_mode, cfg.entry_confirm, cfg.min_initial_rise_pct * 100, cfg.zone_below_pct * 100,
             cfg.zone_above_pct * 100, cfg.max_dev_supply_pct * 100, cfg.max_early_supply_pct * 100,
             cfg.watch_creator, cfg.require_socials, cfg.take_profit_pct * 100, cfg.stop_loss_pct * 100, cfg.max_hold_s,
             cfg.trade_size_sol, cfg.max_open_positions, cfg.max_watched, cfg.fee_bps / 100, cfg.latency_s,
             cfg.fail_rate * 100)
    engine = LiveEngine(cfg)
    try:
        asyncio.run(engine.run())
    except KeyboardInterrupt:
        engine.stopping = True
        log.info("Scalper stopped")
    except Exception:
        engine.stopping = True
        log.exception("Scalper crashed")   # the traceback lands in logs/scalper.log, not only the terminal
        raise
