"""The scalper's decision loop, shared by the live engine and the backtester.

Feed it token creations, curve states and clock ticks; it keeps one tracker
per token, asks the strategy what to do and routes orders to the paper book.
"""

from __future__ import annotations

import bisect
import math
import statistics
from typing import Callable

from .book import PaperBook
from .config import ScalpConfig
from .curve import CurveState
from .strategy import Signal, entry_signal, exit_policy, exit_signal, update_launch_state
from .tracker import TokenTracker


# Market regime: how did recently launched tokens actually do? Each watched token
# contributes log(price at ~REGIME_TO_S of age / price at ~REGIME_FROM_S), logged on
# its first curve update past REGIME_TO_S (if it was still being watched, i.e. it
# traded within the idle window). Known by then, so no lookahead; identical live
# and in replay.
REGIME_FROM_S, REGIME_TO_S, REGIME_MAX_AGE_S = 15.0, 120.0, 600.0


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
        self.outcome_times: list[float] = []               # market regime: when each outcome became known
        self.outcomes: list[float] = []                    # ... and the outcome (log return, 15 s -> 120 s of age)
        self.model = None                                  # autopilot champion (entry_mode="model")
        self.position_policy: dict[str, dict] = {}         # mint -> exit policy of a model trade
        self.shadows: list[ShadowLane] = []                # forward-test models, each with its own paper wallet

    @property
    def shadow(self) -> "ShadowLane | None":
        return self.shadows[0] if self.shadows else None

    def add_shadow(self, gate, on_event: Callable[[str, dict], None] | None = None) -> "ShadowLane":
        """Run another model (an autopilot shadow) side by side in its own paper wallet."""
        book = PaperBook(self.cfg, on_event=on_event)
        book.mutate_tracker = False                         # never touch the main lane's per-token state
        book.daily_loss_limit_sol = math.inf                # a forward test must not stop after one bad hour
        lane = ShadowLane(gate, book)
        self.shadows.append(lane)
        return lane

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

    def on_trade(self, mint: str, t: float, wallet: str, is_buy: bool, sol: float, tokens_raw: float) -> None:
        """A trade with its wallet (live trade-event logs / exact chain data). Curve updates still drive decisions."""
        tr = self.trackers.get(mint)
        if tr is not None:
            tr.on_trade(t, wallet, is_buy, sol, tokens_raw)

    def on_migration(self, t: float) -> None:
        self._remember(self.migration_times, t)

    def heat(self, now: float) -> tuple[float, float]:
        """(launches in the last 10 min, graduations in the last hour): how hot the market is."""
        def count(times, window):
            return bisect.bisect_right(times, now) - bisect.bisect_left(times, now - window)
        return float(count(self.create_times, 600)), float(count(self.migration_times, 3600))

    def _note_outcome(self, tr: TokenTracker, t: float, prev_update_t: float) -> None:
        if tr.regime_logged or tr.price <= 0:
            return
        age = t - tr.created_at
        if not tr.p_ref:
            if age >= REGIME_FROM_S:
                tr.p_ref = tr.price
            return
        if age >= REGIME_TO_S:
            tr.regime_logged = True
            if age <= REGIME_MAX_AGE_S and t - prev_update_t <= self.cfg.idle_unwatch_s:
                i = bisect.bisect_right(self.outcome_times, t)
                self.outcome_times.insert(i, t)
                self.outcomes.insert(i, math.log(tr.price / tr.p_ref))
                if len(self.outcome_times) > 8192 and self.outcome_times[0] < t - 7200:
                    cut = bisect.bisect_left(self.outcome_times, t - 7200)
                    del self.outcome_times[:cut], self.outcomes[:cut]

    def regime(self, now: float) -> tuple[float, float, float, float]:
        """(median outcome 30 min, share of outcomes > 0 in 30 min, outcomes in 30 min, median outcome 10 min).
        Outcome = log return of a recently launched token from ~15 s to ~120 s of age."""
        def window(seconds):
            lo = bisect.bisect_left(self.outcome_times, now - seconds)
            return self.outcomes[lo:bisect.bisect_right(self.outcome_times, now)]
        w30, w10 = window(1800), window(600)
        med30 = statistics.median(w30) if len(w30) >= 5 else math.nan
        up30 = sum(v > 0 for v in w30) / len(w30) if len(w30) >= 5 else math.nan
        med10 = statistics.median(w10) if len(w10) >= 5 else math.nan
        return med30, up30, float(len(w30)), med10

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
        for lane in self.shadows:
            lane.book.fill_due(tr, t)
        prev_update_t = tr.last_update_t or tr.created_at
        if tr.update(t, state) is None:
            return
        self._note_outcome(tr, t, prev_update_t)
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
                self._decide_main(tr, now)
        for lane in self.shadows:
            sb = lane.book
            for mint in list(sb.orders):
                tr = self.trackers.get(mint)
                if tr is not None:
                    sb.fill_due(tr, now)
            for mint in list(sb.positions):
                tr = self.trackers.get(mint)
                if tr is not None and not sb.busy(mint):
                    self._decide_shadow(lane, tr, now)

    def forget(self, mint: str) -> bool:
        """Drop a tracker unless it still has a position or order (in any lane)."""
        if mint in self.book.positions or mint in self.book.orders:
            return False
        if any(mint in lane.book.positions or mint in lane.book.orders for lane in self.shadows):
            return False
        self.trackers.pop(mint, None)
        return True

    # --- decisions --------------------------------------------------------
    def _decide(self, tr: TokenTracker, now: float) -> None:
        self._decide_main(tr, now)
        for lane in self.shadows:
            self._decide_shadow(lane, tr, now)

    @staticmethod
    def _manage(book: PaperBook, policies: dict, gate, tr: TokenTracker, now: float, cfg: ScalpConfig) -> None:
        """Exit decisions for an open position: the trained policy (or learned exit), else the config's rules."""
        position = book.positions[tr.mint]
        pnl = book.mark(tr)
        if pnl is None:
            return
        policy = policies.get(tr.mint)
        if policy is not None:
            signal = exit_policy(pnl, position.peak_pnl_pct, now - position.entry_t, tr, policy)
            if not signal.ok and policy.get("learned") and gate is not None:
                signal = gate.should_exit(tr, now)
        else:
            signal = exit_signal(pnl, position.peak_pnl_pct, now - position.entry_t, tr, cfg)
        if signal.ok:
            book.request_sell(tr, now, signal.reason, {**signal.details, "pnl_at_signal": round(pnl, 4)})

    def _decide_shadow(self, lane: "ShadowLane", tr: TokenTracker, now: float) -> None:
        if lane.book.busy(tr.mint):
            return
        if tr.mint in lane.book.positions:
            self._manage(lane.book, lane.position_policy, lane.gate, tr, now, self.cfg)
            return
        lane.position_policy.pop(tr.mint, None)
        if not self.feed_ok or lane.gate is None:
            return
        signal = lane.gate.entry(tr, now)
        if not signal.ok:
            return
        lane.top_up()
        allowed, _ = lane.book.can_open(now)
        if not allowed:
            return
        lane.position_policy[tr.mint] = signal.details["policy_rules"]
        lane.book.request_buy(tr, now, signal.reason, {**signal.details, "signal_price": tr.price})

    def _decide_main(self, tr: TokenTracker, now: float) -> None:
        if self.book.busy(tr.mint):
            return
        if tr.mint in self.book.positions:
            self._manage(self.book, self.position_policy, self.model, tr, now, self.cfg)
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


class ShadowLane:
    """A second model trading its own paper wallet on the same live feed (forward test)."""

    def __init__(self, gate, book: PaperBook):
        self.gate, self.book = gate, book
        self.position_policy: dict[str, dict] = {}
        self.refills = 0

    def top_up(self) -> None:
        """Keep the test running: a drained wallet is refilled (results are judged per trade, not by balance)."""
        b = self.book
        if not b.positions and not b.orders and b.balance_sol < b.cfg.trade_size_sol + 2 * b.cfg.network_fee_sol:
            b.balance_sol = b.cfg.paper_start_sol
            self.refills += 1

    def reset(self) -> None:
        """A new model takes over this lane: fresh wallet (open positions finish under their own rules)."""
        b = self.book
        if not b.positions and not b.orders:
            b.balance_sol = b.cfg.paper_start_sol
        b.day_pnl_sol, b.closed = 0.0, []
