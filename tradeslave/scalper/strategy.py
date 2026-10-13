"""Entry and exit rules for the launch-dip scalper. Pure functions: no I/O.

Entry (Rohit's method):
  1. First trade on a token: wait for the initial sniper dump (price at least
     first_dip_min_pct below the launch peak), then buy once buying returns:
     price bounced bounce_min_pct off the dip low, net SOL flow is positive and
     the curve is actively trading.
  2. Repeat trades: same confirmation, but the dip is a pullback of at least
     reentry_pullback_min_pct from the recent swing high.
Exits (net of fees): take profit, stop loss, trailing giveback once in profit,
time stop, and an immediate exit when one update dumps the price.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import ScalpConfig
from .tracker import TokenTracker


@dataclass(frozen=True)
class Signal:
    ok: bool
    reason: str
    details: dict


def _no(reason: str, **details) -> Signal:
    return Signal(False, reason, details)


def entry_signal(tr: TokenTracker, now: float, c: ScalpConfig) -> Signal:
    if tr.state is None or not tr.ticks:
        return _no("no_data")
    if tr.state.complete:
        return _no("curve_complete")
    age = tr.age(now)
    if age < c.min_age_s:
        return _no("too_young", age=round(age, 1))
    if age > c.max_entry_age_s:
        return _no("too_old", age=round(age, 1))
    if tr.entries >= c.max_entries_per_token:
        return _no("token_entry_limit")
    if tr.losses >= c.max_losses_per_token:
        return _no("token_loss_limit")
    if now - tr.last_exit_t < c.reentry_cooldown_s:
        return _no("reentry_cooldown")
    if tr.state.virtual_sol < c.min_curve_sol:
        return _no("curve_too_small", virtual_sol=round(tr.state.virtual_sol, 2))

    price = tr.price
    if not tr.first_dip_done:
        high, high_t = tr.peak_price, tr.peak_t
        min_dip = c.first_dip_min_pct
        kind = "first_dip"
    else:
        high, high_t = tr.swing_high(now, c.swing_lookback_s)
        min_dip = c.reentry_pullback_min_pct
        kind = "pullback"
    if high <= 0:
        return _no("no_high")
    low = tr.low_since(high_t)
    dip = 1.0 - low / high
    if dip < min_dip:
        return _no("no_dip_yet", kind=kind, dip=round(dip, 4))
    if dip > c.dip_max_pct:
        return _no("dip_too_deep", kind=kind, dip=round(dip, 4))
    bounce = price / low - 1.0 if low > 0 else 0.0
    if bounce < c.bounce_min_pct:
        return _no("waiting_for_bounce", kind=kind, dip=round(dip, 4), bounce=round(bounce, 4))
    if bounce > c.bounce_max_pct:
        return _no("bounce_already_extended", kind=kind, bounce=round(bounce, 4))
    net_buy, updates = tr.flow(now, c.flow_window_s)
    if updates < c.min_updates_in_window:
        return _no("not_enough_activity", updates=updates)
    if net_buy < c.min_net_buy_sol:
        return _no("buying_not_back", net_buy_sol=round(net_buy, 3))
    return Signal(True, kind, {
        "dip": round(dip, 4), "bounce": round(bounce, 4), "net_buy_sol": round(net_buy, 3),
        "updates": updates, "age": round(age, 1), "virtual_sol": round(tr.state.virtual_sol, 2),
    })


def exit_signal(
    net_pnl_pct: float, peak_net_pnl_pct: float, held_s: float, tr: TokenTracker, c: ScalpConfig
) -> Signal:
    """Decide on an open position given its current NET PnL (from a sell quote)."""
    if tr.state is not None and tr.state.complete:
        return Signal(True, "curve_complete", {})
    if net_pnl_pct >= c.take_profit_pct:
        return Signal(True, "take_profit", {"pnl": round(net_pnl_pct, 4)})
    if net_pnl_pct <= -c.stop_loss_pct:
        return Signal(True, "stop_loss", {"pnl": round(net_pnl_pct, 4)})
    drop = tr.last_drop_pct()
    if drop >= c.dump_exit_drop_pct:
        return Signal(True, "dump_detected", {"drop": round(drop, 4), "pnl": round(net_pnl_pct, 4)})
    if peak_net_pnl_pct >= c.trail_activate_pct and peak_net_pnl_pct - net_pnl_pct >= c.trail_giveback_pct:
        return Signal(True, "trailing_giveback", {"peak": round(peak_net_pnl_pct, 4), "pnl": round(net_pnl_pct, 4)})
    if held_s >= c.max_hold_s:
        return Signal(True, "time_stop", {"held_s": round(held_s, 1), "pnl": round(net_pnl_pct, 4)})
    return Signal(False, "hold", {})
