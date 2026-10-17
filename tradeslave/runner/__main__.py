"""Live paper trading of the graduated-runner rules (Strategy H) on DexScreener prices.

    uv run python -m tradeslave.runner            # needs scalp.py running (it records the graduations)

Every minute: read new graduations from the scalper's tick files, poll DexScreener for every
graduated token younger than 48 h (30 tokens per call, free API), and run the same rules as
research.runners (S4 survivor, S5 momentum) on a paper wallet per rule. Fills use the pool's
real SOL reserve (DexScreener liquidity.quote), the official PumpSwap fee tier for the market
cap, our own buy kept in the pool, and the network fee.
Files: runner_trades.jsonl, runner_wallet.json, logs/runner.log (STATUS lines).
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests

from ..paths import PROJECT_ROOT
from ..research.runners import NETWORK_FEE_SOL, SUPPLY, fee_for
from ..scalper.recorder import TICK_DIR, read_ticks

log = logging.getLogger("runner")
API = "https://api.dexscreener.com/tokens/v1/solana/"
POLL_S = 60.0
FAST_S = 10.0          # open positions are re-priced this often (stops)
WATCH_H = 48.0
SIZE_SOL = 0.5
START_SOL = 10.0
MAX_OPEN = 20
TRADES = PROJECT_ROOT / "runner_trades.jsonl"
WALLET = PROJECT_ROOT / "runner_wallet.json"

# Same entry facts and exits as research.runners (pre-registered 2026-10-17).
RULES = {
    "S4": dict(tp=None, sl=0.50, hold_min=1440, trail=None),
    "S5": dict(tp=None, sl=None, hold_min=60, trail=0.15),
    "S6": dict(tp=None, sl=None, hold_min=60, trail=0.15),   # S5 at market cap >= 5,000 SOL
}


@dataclass
class Snap:
    t: float
    price: float           # SOL per token
    pool_sol: float        # SOL in the pool
    vol5_usd: float        # volume of the last 5 minutes
    txns5: int             # trades in the last 5 minutes


@dataclass
class Watched:
    mint: str
    t_grad: float
    snaps: deque = field(default_factory=lambda: deque(maxlen=400))
    s4_checked: bool = False

    def price_ago(self, now: float, minutes: float) -> float | None:
        target = now - minutes * 60
        best = None
        for s in self.snaps:
            if s.t <= target + 30:
                best = s
        return best.price if best and abs(best.t - target) <= 120 else None

    def vol15(self, now: float, offset_min: float) -> float | None:
        """Volume of the 15 min ending offset_min ago, from three 5-min readings."""
        vals = []
        for k in (0, 5, 10):
            target = now - (offset_min + k) * 60
            cands = [s for s in self.snaps if abs(s.t - target) <= 90]
            if not cands:
                return None
            vals.append(min(cands, key=lambda s: abs(s.t - target)).vol5_usd)
        return sum(vals)


@dataclass
class Position:
    rule: str
    mint: str
    entry_t: float
    entry_price: float
    dx: float              # SOL that went into the pool (after fee)
    tokens: float
    cost: float
    peak: float


def buy(price: float, pool_sol: float, size: float = SIZE_SOL) -> tuple[float, float]:
    dx = size * (1 - fee_for(price))
    x = max(pool_sol, 1e-9)
    return dx, (x / price) * dx / (x + dx)


def sell_value(pos: Position, price: float, pool_sol: float) -> float:
    x = max(pool_sol, 1e-9) + pos.dx                  # the observed pool with our buy still in it
    y = max(pool_sol, 1e-9) / price - pos.tokens
    gross = x * pos.tokens / (y + pos.tokens) if y > 0 else 0.0
    return gross * (1 - fee_for(price)) - NETWORK_FEE_SOL


class Runner:
    def __init__(self):
        self.watched: dict[str, Watched] = {}
        self.open: dict[tuple[str, str], Position] = {}
        self.balance = {r: START_SOL for r in RULES}
        self.closed = {r: [] for r in RULES}
        self.http = requests.Session()
        self.scan_from = 0.0
        self._load_wallet()

    # --- universe --------------------------------------------------------
    def refresh_universe(self, now: float) -> None:
        files = sorted(TICK_DIR.glob("*.jsonl.gz"))[-3:]
        for r in read_ticks(files):
            if r.get("k") == "migr" and r.get("m") and r["m"] not in self.watched:
                t = float(r["t"])
                if now - t <= WATCH_H * 3600:
                    self.watched[r["m"]] = Watched(r["m"], t)
        for m in [m for m, w in self.watched.items() if now - w.t_grad > WATCH_H * 3600
                  and not any(k[1] == m for k in self.open)]:
            del self.watched[m]

    # --- prices ----------------------------------------------------------
    def poll(self, now: float, only: list[str] | None = None) -> None:
        mints = only if only is not None else list(self.watched)
        for i in range(0, len(mints), 30):
            batch = mints[i:i + 30]
            try:
                r = self.http.get(API + ",".join(batch), timeout=20)
                pairs = r.json() if r.ok else []
            except (requests.RequestException, ValueError):
                continue
            best: dict[str, dict] = {}
            for p in pairs or []:
                if p.get("dexId") != "pumpswap" or (p.get("quoteToken") or {}).get("symbol") != "SOL":
                    continue
                m = (p.get("baseToken") or {}).get("address")
                if m in self.watched and float((p.get("liquidity") or {}).get("quote") or 0) > \
                        float(((best.get(m) or {}).get("liquidity") or {}).get("quote") or -1):
                    best[m] = p
            for m, p in best.items():
                tx = (p.get("txns") or {}).get("m5") or {}
                try:
                    snap = Snap(now, float(p["priceNative"]), float(p["liquidity"]["quote"]),
                                float((p.get("volume") or {}).get("m5") or 0), int(tx.get("buys", 0)) + int(tx.get("sells", 0)))
                except (KeyError, TypeError, ValueError):
                    continue
                if snap.price > 0:
                    self.watched[m].snaps.append(snap)
            time.sleep(0.25)                           # 300 req/min limit; we use far less

    # --- rules -----------------------------------------------------------
    def entries(self, now: float) -> None:
        for m, w in self.watched.items():
            if not w.snaps or w.snaps[-1].t != now:
                continue
            s = w.snaps[-1]
            age_min = (now - w.t_grad) / 60
            if not w.s4_checked and 360 <= age_min <= 370:
                w.s4_checked = True
                if s.txns5 > 0 and s.price * SUPPLY >= 1500:
                    self.open_position("S4", w, s, now)
            if age_min >= 30 and ("S5", m) not in self.open:
                p15 = w.price_ago(now, 15)
                v_now, v_prev = w.vol15(now, 0), w.vol15(now, 15)
                if p15 and v_now is not None and v_prev is not None and s.price >= 1.25 * p15 and v_now > v_prev:
                    self.open_position("S5", w, s, now)
                    if s.price * SUPPLY >= 5000 and ("S6", m) not in self.open:
                        self.open_position("S6", w, s, now)

    def open_position(self, rule: str, w: Watched, s: Snap, now: float) -> None:
        if len(self.open) >= MAX_OPEN or self.balance[rule] < SIZE_SOL + NETWORK_FEE_SOL:
            return
        dx, tokens = buy(s.price, s.pool_sol)
        cost = SIZE_SOL + NETWORK_FEE_SOL
        self.balance[rule] -= cost
        self.open[(rule, w.mint)] = Position(rule, w.mint, now, s.price, dx, tokens, cost, s.price)
        log.info("BUY %s %s at %.3g SOL (pool %.1f SOL, mcap %.0f SOL)", rule, w.mint[:8], s.price, s.pool_sol,
                 s.price * SUPPLY)

    def exits(self, now: float) -> None:
        for key, pos in list(self.open.items()):
            w = self.watched.get(pos.mint)
            if w is None or not w.snaps:
                continue
            s = w.snaps[-1]
            rule = RULES[pos.rule]
            pos.peak = max(pos.peak, s.price)
            why = None
            if rule["sl"] and s.price <= pos.entry_price * (1 - rule["sl"]):
                why = "stop"
            elif rule["trail"] and s.price <= pos.peak * (1 - rule["trail"]):
                why = "trail"
            elif rule["tp"] and s.price >= pos.entry_price * (1 + rule["tp"]):
                why = "target"
            elif now - pos.entry_t >= rule["hold_min"] * 60:
                why = "time"
            if why:
                proceeds = sell_value(pos, s.price, s.pool_sol)
                self.balance[pos.rule] += proceeds
                rec = {"rule": pos.rule, "mint": pos.mint, "entry_t": pos.entry_t, "exit_t": now,
                       "entry_price": pos.entry_price, "exit_price": s.price, "cost_sol": pos.cost,
                       "proceeds_sol": round(proceeds, 9), "pnl_pct": round(proceeds / pos.cost - 1, 5), "exit": why}
                self.closed[pos.rule].append(rec)
                with TRADES.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec) + "\n")
                log.info("SELL %s %s %s %+.1f%%", pos.rule, pos.mint[:8], why, rec["pnl_pct"] * 100)
                del self.open[key]

    # --- state -----------------------------------------------------------
    def _load_wallet(self) -> None:
        try:
            d = json.loads(WALLET.read_text())
            self.balance.update(d.get("balance") or {})
            for p in d.get("open") or []:
                self.open[(p["rule"], p["mint"])] = Position(**p)
        except (OSError, ValueError, TypeError):
            pass
        if TRADES.exists():
            for line in TRADES.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                    self.closed.setdefault(r["rule"], []).append(r)
                except (ValueError, KeyError):
                    continue

    def save(self) -> None:
        tmp = WALLET.with_suffix(".tmp")
        tmp.write_text(json.dumps({"balance": self.balance, "open": [asdict(p) for p in self.open.values()],
                                   "updated": time.time()}))
        tmp.replace(WALLET)

    def status(self) -> None:
        for rule in RULES:
            c = self.closed.get(rule) or []
            pnl = [r["pnl_pct"] for r in c]
            n = len(pnl)
            mean = sum(pnl) / n if n else 0.0
            sd = math.sqrt(sum((x - mean) ** 2 for x in pnl) / n) if n > 1 else 0.0
            t = mean / (sd / math.sqrt(n)) if sd else 0.0
            log.info("STATUS %s: %d closed, mean %+.2f%%, t %+.1f, open %d, balance %.3f SOL | watching %d graduated tokens",
                     rule, n, mean * 100, t, sum(1 for k in self.open if k[0] == rule), self.balance[rule], len(self.watched))

    def fast_exits(self, now: float) -> None:
        """Between full polls: re-price only the open positions (stops fire within ~10 s, not 60 s)."""
        mints = sorted({m for _, m in self.open if m in self.watched})
        if mints:
            self.poll(now, mints)
            self.exits(now)
            self.save()

    def step(self, now: float) -> None:
        self.refresh_universe(now)
        self.poll(now)
        self.exits(now)
        self.entries(now)
        self.save()


def main() -> None:
    (PROJECT_ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        handlers=[logging.FileHandler(PROJECT_ROOT / "logs" / "runner.log", encoding="utf-8"),
                                  logging.StreamHandler()])
    r = Runner()
    log.info("runner paper trader started: rules %s, %.2f SOL per trade", ", ".join(RULES), SIZE_SOL)
    k = 0
    while True:
        started = time.time()
        try:
            r.step(started)
        except Exception:  # noqa: BLE001 - keep running; the next minute retries
            log.exception("step failed")
        if k % 10 == 0:
            r.status()
        k += 1
        while time.time() - started < POLL_S - FAST_S:
            time.sleep(FAST_S)
            try:
                r.fast_exits(time.time())
            except Exception:  # noqa: BLE001
                log.exception("fast exits failed")
        time.sleep(max(1.0, POLL_S - (time.time() - started)))


if __name__ == "__main__":
    main()
