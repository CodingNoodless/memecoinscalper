"""The scalper's decision loop, shared by the live engine and the backtester.

Feed it token creations, curve states and clock ticks; it keeps one tracker
per token, asks the strategy what to do and routes orders to the paper book.
"""

from __future__ import annotations

from typing import Callable

from .book import PaperBook
from .config import ScalpConfig
from .curve import CurveState
from .strategy import entry_signal, exit_signal
from .tracker import TokenTracker


class Scalper:
    def __init__(self, cfg: ScalpConfig, on_event: Callable[[str, dict], None] | None = None):
        self.cfg = cfg
        self.emit = on_event or (lambda kind, data: None)
        self.book = PaperBook(cfg, on_event=self.emit)
        self.trackers: dict[str, TokenTracker] = {}
        self.entry_block_reasons: dict[str, int] = {}

    # --- inputs -----------------------------------------------------------
    def on_create(self, mint: str, symbol: str, t: float, state: CurveState | None,
                  creator: str = "", curve_key: str = "", initial_buy_sol: float = 0.0) -> TokenTracker:
        tr = TokenTracker(mint=mint, symbol=symbol, created_at=t, creator=creator,
                          curve_key=curve_key, initial_buy_sol=initial_buy_sol)
        if state is not None:
            tr.update(t, state)
        self.trackers[mint] = tr
        return tr

    def on_curve(self, mint: str, t: float, state: CurveState) -> None:
        tr = self.trackers.get(mint)
        if tr is None:
            return
        # Fill any order that came due BEFORE this new state existed.
        self.book.fill_due(tr, t)
        if tr.update(t, state) is None:
            return
        self._decide(tr, t)

    def on_clock(self, now: float) -> None:
        """Called periodically: fills due orders and enforces time-based exits
        even when a token stops trading."""
        for mint in list(self.book.orders):
            tr = self.trackers.get(mint)
            if tr is not None:
                self.book.fill_due(tr, now)
        for mint in list(self.book.positions):
            tr = self.trackers.get(mint)
            if tr is not None and not self.book.busy(mint):
                self._decide(tr, now)

    def forget(self, mint: str) -> bool:
        """Drop a tracker unless it still has a position or order."""
        if mint in self.book.positions or mint in self.book.orders:
            return False
        self.trackers.pop(mint, None)
        return True

    # --- decisions --------------------------------------------------------
    def _decide(self, tr: TokenTracker, now: float) -> None:
        if self.book.busy(tr.mint):
            return
        position = self.book.positions.get(tr.mint)
        if position is not None:
            pnl = self.book.mark(tr)
            if pnl is None:
                return
            signal = exit_signal(pnl, position.peak_pnl_pct, now - position.entry_t, tr, self.cfg)
            if signal.ok:
                self.book.request_sell(tr, now, signal.reason, signal.details)
            return
        signal = entry_signal(tr, now, self.cfg)
        if not signal.ok:
            self.entry_block_reasons[signal.reason] = self.entry_block_reasons.get(signal.reason, 0) + 1
            return
        allowed, why = self.book.can_open(now)
        if not allowed:
            self.entry_block_reasons[why] = self.entry_block_reasons.get(why, 0) + 1
            return
        tr.entries += 1
        tr.first_dip_done = True
        self.book.request_buy(tr, now, signal.reason, {**signal.details, "signal_price": tr.price})
