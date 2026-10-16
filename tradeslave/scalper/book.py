"""Paper execution and account risk for many concurrent positions.

Fills are deliberately pessimistic:
  * an order fills at the curve state in effect `latency_s` after the decision,
    not at the price that triggered it;
  * pump.fun's fee is charged on both sides and every transaction pays a
    network fee;
  * a share of transactions fail (fee paid, nothing filled). A failed sell is
    retried, so it stays exposed to the market meanwhile.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .config import ScalpConfig
from .curve import LAMPORTS_PER_SOL, buy_tokens_out, sell_sol_out
from .tracker import TokenTracker


@dataclass
class Position:
    trade_id: str
    mint: str
    symbol: str
    kind: str
    tokens: int
    cost_sol: float          # SOL spent including fees
    entry_t: float
    entry_price: float
    entry_details: dict = field(default_factory=dict)
    peak_pnl_pct: float = 0.0
    last_pnl_pct: float = 0.0


@dataclass
class Order:
    side: str                # "buy" | "sell"
    mint: str
    due_t: float
    reason: str
    details: dict = field(default_factory=dict)
    attempts: int = 0


def utc_day(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat()


class PaperBook:
    def __init__(self, cfg: ScalpConfig, on_event: Callable[[str, dict], None] | None = None):
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)
        self.balance_sol = cfg.paper_start_sol
        self.positions: dict[str, Position] = {}
        self.orders: dict[str, Order] = {}
        self.closed: list[dict] = []
        self.day = ""
        self.day_pnl_sol = 0.0
        self.fees_paid_sol = 0.0
        self.failed_tx = 0
        self._emit = on_event or (lambda kind, data: None)
        self.mutate_tracker = True     # False for a shadow book: leave per-token cooldown/loss counters alone
        self.daily_loss_limit_sol = cfg.daily_loss_limit_sol

    # --- risk -------------------------------------------------------------
    def exposure_sol(self) -> float:
        pending_buys = sum(1 for o in self.orders.values() if o.side == "buy") * self.cfg.trade_size_sol
        return sum(p.cost_sol for p in self.positions.values()) + pending_buys

    def _roll_day(self, now: float) -> None:
        day = utc_day(now)
        if day != self.day:
            self.day, self.day_pnl_sol = day, 0.0

    def can_open(self, now: float) -> tuple[bool, str]:
        self._roll_day(now)
        open_count = len(self.positions) + sum(1 for o in self.orders.values() if o.side == "buy")
        if open_count >= self.cfg.max_open_positions:
            return False, "max_open_positions"
        if self.day_pnl_sol <= -self.daily_loss_limit_sol:
            return False, "daily_loss_limit"
        if self.exposure_sol() + self.cfg.trade_size_sol > self.cfg.max_exposure_sol + 1e-12:
            return False, "max_exposure"
        if self.balance_sol < self.cfg.trade_size_sol + 2 * self.cfg.network_fee_sol:
            return False, "insufficient_balance"
        return True, "ok"

    # --- orders -----------------------------------------------------------
    def busy(self, mint: str) -> bool:
        return mint in self.orders

    def request_buy(self, tr: TokenTracker, now: float, reason: str, details: dict) -> None:
        self.orders[tr.mint] = Order("buy", tr.mint, now + self.cfg.latency_s, reason, details)
        self._emit("buy_requested", {"mint": tr.mint, "symbol": tr.symbol, "t": now,
                                     "price": tr.price, "reason": reason, **details})

    def request_sell(self, tr: TokenTracker, now: float, reason: str, details: dict) -> None:
        if tr.mint in self.orders:
            return
        self.orders[tr.mint] = Order("sell", tr.mint, now + self.cfg.latency_s, reason, details)
        self._emit("sell_requested", {"mint": tr.mint, "symbol": tr.symbol, "t": now,
                                      "price": tr.price, "reason": reason, **details})

    def due(self, mint: str, now: float) -> bool:
        order = self.orders.get(mint)
        return order is not None and now >= order.due_t

    def fill_due(self, tr: TokenTracker, now: float) -> None:
        """Fill this token's order at the CURRENT curve state if it is due.

        Callers must invoke this before applying a newer curve state, so the
        fill uses the state that was in effect at the order's due time.
        """
        order = self.orders.get(tr.mint)
        if order is None or now < order.due_t or tr.state is None:
            return
        fee = self.cfg.network_fee_sol
        if self.rng.random() < self.cfg.fail_rate:
            self.balance_sol -= fee
            self.fees_paid_sol += fee
            self.failed_tx += 1
            if order.side == "sell":
                order.attempts += 1
                order.due_t = now + self.cfg.sell_retry_s
                self._emit("sell_failed_retrying", {"mint": tr.mint, "t": now, "attempts": order.attempts})
            else:
                del self.orders[tr.mint]
                if self.mutate_tracker:
                    tr.last_exit_t = now
                self._emit("buy_failed", {"mint": tr.mint, "t": now})
            return
        del self.orders[tr.mint]
        if order.side == "buy":
            self._fill_buy(tr, now, order)
        else:
            self._fill_sell(tr, now, order)

    def _fill_buy(self, tr: TokenTracker, now: float, order: Order) -> None:
        size = self.cfg.trade_size_sol
        tokens = buy_tokens_out(tr.state, int(size * LAMPORTS_PER_SOL), self.cfg.fee_bps)
        if tokens <= 0:
            return
        cost = size + self.cfg.network_fee_sol
        self.balance_sol -= cost
        self.fees_paid_sol += self.cfg.network_fee_sol + size * self.cfg.fee_bps / (10_000 + self.cfg.fee_bps)
        position = Position(
            trade_id=uuid.uuid4().hex[:12], mint=tr.mint, symbol=tr.symbol, kind=order.reason,
            tokens=tokens, cost_sol=cost, entry_t=now, entry_price=tr.price, entry_details=order.details,
        )
        self.positions[tr.mint] = position
        self._emit("buy_filled", {"mint": tr.mint, "symbol": tr.symbol, "t": now, "price": tr.price,
                                  "cost_sol": cost, "slippage_vs_signal": order.details.get("signal_price") and
                                  tr.price / order.details["signal_price"] - 1.0, "trade_id": position.trade_id})

    def _fill_sell(self, tr: TokenTracker, now: float, order: Order) -> None:
        position = self.positions.pop(tr.mint, None)
        if position is None:
            return
        gross = sell_sol_out(tr.state, position.tokens, self.cfg.fee_bps) / LAMPORTS_PER_SOL
        proceeds = gross - self.cfg.network_fee_sol
        self.balance_sol += proceeds
        self.fees_paid_sol += self.cfg.network_fee_sol + gross * self.cfg.fee_bps / (10_000 - self.cfg.fee_bps)
        pnl = proceeds - position.cost_sol
        self._roll_day(now)
        self.day_pnl_sol += pnl
        if self.mutate_tracker:
            tr.last_exit_t = now
            if pnl < 0:
                tr.losses += 1
        record = {
            "trade_id": position.trade_id, "mint": tr.mint, "symbol": tr.symbol, "kind": position.kind,
            "entry_t": position.entry_t, "exit_t": now, "held_s": round(now - position.entry_t, 2),
            "entry_price": position.entry_price, "exit_price": tr.price,
            "cost_sol": round(position.cost_sol, 9), "proceeds_sol": round(proceeds, 9),
            "pnl_sol": round(pnl, 9), "pnl_pct": round(pnl / position.cost_sol, 5),
            "peak_pnl_pct": round(position.peak_pnl_pct, 5), "exit_reason": order.reason,
            "exit_details": order.details, "entry_details": position.entry_details,
            "sell_attempts": order.attempts + 1, "balance_sol": round(self.balance_sol, 9),
        }
        self.closed.append(record)
        self._emit("trade_closed", record)

    # --- marking ----------------------------------------------------------
    def mark(self, tr: TokenTracker) -> float | None:
        """Net PnL fraction if the position were sold at the current state."""
        position = self.positions.get(tr.mint)
        if position is None or tr.state is None:
            return None
        value = sell_sol_out(tr.state, position.tokens, self.cfg.fee_bps) / LAMPORTS_PER_SOL - self.cfg.network_fee_sol
        pnl = value / position.cost_sol - 1.0
        position.last_pnl_pct = pnl
        position.peak_pnl_pct = max(position.peak_pnl_pct, pnl)
        return pnl

    def equity_sol(self, trackers: dict[str, TokenTracker]) -> float:
        total = self.balance_sol
        for mint, position in self.positions.items():
            tr = trackers.get(mint)
            if tr is not None and tr.state is not None:
                total += sell_sol_out(tr.state, position.tokens, self.cfg.fee_bps) / LAMPORTS_PER_SOL
        return total

    def snapshot(self) -> dict:
        return {
            "balance_sol": self.balance_sol, "day": self.day, "day_pnl_sol": self.day_pnl_sol,
            "fees_paid_sol": self.fees_paid_sol, "failed_tx": self.failed_tx,
            "open_positions": [asdict(p) for p in self.positions.values()],
            "closed_count": len(self.closed),
        }
