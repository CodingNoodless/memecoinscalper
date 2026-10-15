"""The scalper's decision loop, shared by the live engine and the backtester.

Feed it token creations, curve states and clock ticks; it keeps one tracker
per token, asks the strategy what to do and routes orders to the paper book.
"""

from __future__ import annotations

import bisect
from typing import Callable

from .book import PaperBook
from .config import ScalpConfig
from .curve import CurveState
from .strategy import Signal, entry_signal, exit_policy, exit_signal, update_launch_state
from .tracker import TokenTracker


class Scalper:
    def __init__(self, cfg: ScalpConfig, on_event: Callable[[str, dict], None] | None = None):
        self.cfg = cfg
        self.emit = on_event or (lambda kind, data: None)
        self.book = PaperBook(cfg, on_event=self.emit)
        self.trackers: dict[str, TokenTracker] = {}
        self.creator_history: dict[str, list[tuple[float, str]]] = {}   # creator -> [(t, mint)]
        self.twitter_links: dict[str, int] = {}                          # normalised link -> uses
        self.entry_block_reasons: dict[str, int] = {}
        self.feed_ok = True                                # curve feed connected (no entries while it's down)
        self.create_times: list[float] = []                # market heat: recent launches (time order)
        self.migration_times: list[float] = []             # market heat: recent graduations
        self.model = None                                  # autopilot champion (entry_mode="model")
        self.position_policy: dict[str, dict] = {}         # mint -> exit policy of a model trade

    # --- inputs -----------------------------------------------------------
    def on_create(self, mint: str, symbol: str, t: float, state: CurveState | None,
                  creator: str = "", curve_key: str = "", initial_buy_sol: float = 0.0,
                  dev_tokens: float = 0.0, mayhem: bool = False) -> TokenTracker:
        self._remember(self.create_times, t)
        tr = TokenTracker(mint=mint, symbol=symbol, created_at=t, creator=creator,
                          curve_key=curve_key, initial_buy_sol=initial_buy_sol,
                          dev_tokens=dev_tokens, mayhem=mayhem)
        if state is not None:
            tr.update(t, state)
        self.trackers[mint] = tr
        if creator:
            history = [(ht, hm) for ht, hm in self.creator_history.get(creator, [])
                       if t - ht <= self.cfg.serial_window_s]
            history.append((t, mint))
            self.creator_history[creator] = history
            for _, other in history:   # a new launch also flags this creator's earlier tokens
                other_tr = self.trackers.get(other)
                if other_tr is not None:
                    other_tr.creator_launches = len(history)
        return tr

    @staticmethod
    def _remember(times: list[float], t: float, keep_s: float = 3600.0) -> None:
        if times and t < times[-1]:
            bisect.insort(times, t)
        else:
            times.append(t)
        if len(times) > 4096 and times[0] < t - keep_s:
            del times[:bisect.bisect_left(times, t - keep_s)]

    def on_migration(self, t: float) -> None:
        self._remember(self.migration_times, t)

    def heat(self, now: float) -> tuple[float, float]:
        """(launches in the last 10 min, graduations in the last hour): how hot the market is."""
        def count(times, window):
            return bisect.bisect_right(times, now) - bisect.bisect_left(times, now - window)
        return float(count(self.create_times, 600)), float(count(self.migration_times, 3600))

    def on_metadata(self, mint: str, t: float, socials: dict) -> None:
        """Socials arrived for a token. Copycats re-using an earlier token's
        Twitter link are counted (narrative copies are common)."""
        tr = self.trackers.get(mint)
        if tr is None:
            return
        tr.socials = socials
        link = (socials or {}).get("twitter")
        if link:
            key = link.lower().split("?")[0].rstrip("/")
            tr.twitter_reuse = self.twitter_links.get(key, 0)
            self.twitter_links[key] = tr.twitter_reuse + 1
        self._decide(tr, t)

    def on_creator_sell(self, mint: str, t: float) -> None:
        """The creator wallet sold something. It counts as this dev dumping only
        once the token's own curve confirms a dev-sized sell (see strategy)."""
        tr = self.trackers.get(mint)
        if tr is None or tr.dev_sold:
            return
        tr.creator_sell_times.append(t)
        update_launch_state(tr, self.cfg)
        if tr.dev_sold:
            self.emit("dev_sold", {"mint": mint, "symbol": tr.symbol, "t": t, "how": tr.dev_sold_reason})
            self._decide(tr, t)

    def on_curve(self, mint: str, t: float, state: CurveState) -> None:
        tr = self.trackers.get(mint)
        if tr is None:
            return
        # Fill any order that came due BEFORE this new state existed.
        self.book.fill_due(tr, t)
        if tr.update(t, state) is None:
            return
        was_flagged = tr.dev_sold
        update_launch_state(tr, self.cfg)
        if tr.dev_sold and not was_flagged:
            self.emit("dev_sold", {"mint": mint, "symbol": tr.symbol, "t": t, "how": tr.dev_sold_reason})
        self._decide(tr, t)

    def on_dev_sell(self, mint: str, t: float, how: str = "creator_wallet_sell") -> None:
        """The creator wallet sold: never trade this token again; exit any position."""
        tr = self.trackers.get(mint)
        if tr is None or tr.dev_sold:
            return
        tr.dev_sold, tr.dev_sold_reason = True, how
        self.emit("dev_sold", {"mint": mint, "symbol": tr.symbol, "t": t, "how": how})
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
            policy = self.position_policy.get(tr.mint)
            if policy is not None:
                signal = exit_policy(pnl, position.peak_pnl_pct, now - position.entry_t, tr, policy)
            else:
                signal = exit_signal(pnl, position.peak_pnl_pct, now - position.entry_t, tr, self.cfg)
            if signal.ok:
                self.book.request_sell(tr, now, signal.reason, signal.details)
            return
        self.position_policy.pop(tr.mint, None)           # flat: forget the last trade's exits
        if not self.feed_ok:
            signal = Signal(False, "feed_down", {})
        elif self.cfg.entry_mode == "model":
            signal = self.model.entry(tr, now) if self.model is not None else Signal(False, "no_model_yet", {})
        else:
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
        if signal.details.get("policy_rules"):
            self.position_policy[tr.mint] = signal.details["policy_rules"]
        self.book.request_buy(tr, now, signal.reason, {**signal.details, "signal_price": tr.price})
