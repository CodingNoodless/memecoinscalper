"""Entry and exit rules for the scalper. Pure functions: no I/O.

Default entry, `second_dip` (Rohit, 2026-10-12):
  1. Initial incline: price >= opening x (1 + min_initial_rise_pct).
  2. A tiny first dip: a pullback of first_dip_min_move_pct..first_dip_max_pct
     from the incline high (bigger = not the pattern: token skipped).
  3. It goes up a little: >= recover_min_pct off the first-dip low; the high
     after that is H2.
  4. The second dip: price 10-45% below H2 = buy zone; buy once selling stalls.
     Deeper than 45% = collapse: token skipped.

Alternative entry, `initial_zone` (earlier method):
  1. Launch spike: snipers + the dev push the price at least
     min_initial_rise_pct above the opening price (the price right after the
     dev's creation buy).
  2. Return: the price falls back into the zone around the opening price
     [opening x (1 - zone_below_pct), opening x (1 + zone_above_pct)].
     Recorded launches on 2026-10-13 had a median post-spike low of 1.05x
     the opening price.
  3. Buy once selling stalls in the zone (entry_confirm), then sell the climb.
  Repeat whenever the price returns to the zone, within per-token limits.
Safety (never trade): dev holds > max_dev_supply_pct, snipers/bundles bought
> max_early_supply_pct in the first seconds, dev sold, price broke
rug_floor_pct below the opening price, curve complete.

Alternative entry, `dip_bounce`: dip from the launch peak / swing high, then
a bounce with net buying (kept for backtest comparison).

Exits (net of fees): take profit, stop loss, dev sold, support broken, dump
detected, trailing giveback, time stop.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import ScalpConfig
from .socials import socials_ok
from .tracker import TokenTracker


@dataclass(frozen=True)
class Signal:
    ok: bool
    reason: str
    details: dict


def _no(reason: str, **details) -> Signal:
    return Signal(False, reason, details)


def update_launch_state(tr: TokenTracker, c: ScalpConfig) -> None:
    """Per-update bookkeeping for the initial-zone rules and the rug checks."""
    if not tr.launch_price or not tr.ticks:
        return
    price = tr.price
    opening = tr.launch_price
    # A single sell matching the dev's whole launch bag = the dev dumping (often
    # before the creator-wallet subscription is live). Snipers routinely sell
    # more than the dev holds, so only a near-exact match counts.
    if not tr.dev_sold and tr.dev_supply_pct >= 0.005 and tr.last_tokens_sold > 0 \
            and abs(tr.last_tokens_sold - tr.dev_tokens) <= c.dev_dump_match_pct * tr.dev_tokens:
        tr.dev_sold, tr.dev_sold_reason = True, "dev_sized_sell"
    confirm_creator_sells(tr, c)
    update_second_dip(tr, c)
    risen = tr.peak_price >= opening * (1.0 + c.min_initial_rise_pct)
    if risen and price < opening * (1.0 - c.rug_floor_pct):
        tr.floor_broken = True
    in_zone = risen and opening * (1.0 - c.zone_below_pct) <= price <= opening * (1.0 + c.zone_above_pct)
    if in_zone:
        if tr.zone_touch_t is None:
            tr.zone_touch_t, tr.zone_low = tr.ticks[-1].t, price
        tr.zone_low = min(tr.zone_low, price)
    elif tr.zone_touch_t is not None and price > opening * (1.0 + c.zone_above_pct):
        tr.zone_touch_t, tr.zone_low = None, 0.0   # left the zone upward: a new touch is needed


def update_second_dip(tr: TokenTracker, c: ScalpConfig) -> None:
    """Advance the second-dip pattern state with the latest price."""
    if tr.sd_phase == "invalid" or not tr.launch_price or not tr.ticks:
        return
    price, t = tr.price, tr.ticks[-1].t
    if tr.sd_phase == "wait_rise":
        if price >= tr.launch_price * (1.0 + c.min_initial_rise_pct):
            tr.sd_phase, tr.sd_h1 = "first_leg", price
        return
    if tr.sd_phase == "first_leg":
        if price > tr.sd_h1:
            tr.sd_h1 = price
            return
        if price > tr.sd_h1 * (1.0 - c.first_dip_min_move_pct):
            return
        tr.sd_phase, tr.sd_l1 = "first_dip", price          # the first leg just ended
    if tr.sd_phase == "first_dip":
        tr.sd_l1 = min(tr.sd_l1, price)
        if tr.sd_l1 < tr.sd_h1 * (1.0 - c.first_dip_max_pct):
            tr.sd_phase, tr.sd_invalid_reason = "invalid", "first_dip_too_big"
        elif price >= tr.sd_l1 * (1.0 + c.recover_min_pct):
            tr.sd_phase, tr.sd_h2, tr.sd_h2_t = "second_leg", price, t
        return
    # second_leg: H2 follows new highs; the second dip is measured from it
    if price > tr.sd_h2:
        tr.sd_h2, tr.sd_h2_t = price, t
        return
    drawdown = 1.0 - price / tr.sd_h2
    if drawdown > c.second_dip_max_pct:
        tr.sd_phase, tr.sd_invalid_reason = "invalid", "second_dip_too_deep"
    elif drawdown >= c.second_dip_min_pct and tr.sd_setup_t is None:
        tr.sd_setup_t = t


def confirm_creator_sells(tr: TokenTracker, c: ScalpConfig) -> None:
    """A sell by the creator WALLET may be of a different token (serial launchers
    trade many). Count it as this dev dumping only if this curve shows a sell of
    at least dev_sell_confirm_fraction of the dev bag within dev_sell_confirm_s."""
    if tr.dev_sold or not tr.creator_sell_times or tr.dev_tokens <= 0:
        return
    need = c.dev_sell_confirm_fraction * tr.dev_tokens
    latest = tr.ticks[-1].t if tr.ticks else 0.0
    for sell_t in list(tr.creator_sell_times):
        if any(abs(t.t - sell_t) <= c.dev_sell_confirm_s and t.tokens_sold >= need for t in tr.ticks):
            tr.dev_sold, tr.dev_sold_reason = True, "creator_wallet_sell_confirmed"
            tr.creator_sell_times.clear()
            return
        if latest - sell_t > c.dev_sell_confirm_s:
            tr.creator_sell_times.remove(sell_t)   # no matching sell on this token: not this dev dumping


def safety_block(tr: TokenTracker, now: float, c: ScalpConfig) -> str | None:
    """Reason this token must not be traded (scam/rug signals), or None."""
    if tr.state is not None and tr.state.complete:
        return "curve_complete"
    if tr.dev_sold:
        return "dev_sold"
    if tr.floor_broken:
        return "floor_broken"
    if tr.creator_launches > c.serial_launch_max:
        return "serial_launcher"
    if c.require_socials != "off":
        if tr.socials is None:
            return "metadata_pending"
        if not socials_ok(tr.socials, c.require_socials):
            return "no_socials"
    if tr.twitter_reuse >= c.max_twitter_reuse:
        return "copycat_twitter"
    if c.skip_mayhem and tr.mayhem:
        return "mayhem_mode"
    if tr.dev_supply_pct > c.max_dev_supply_pct:
        return "dev_holds_too_much"
    early = tr.early_supply_pct(c.early_window_s)
    if early is None and tr.age(now) <= c.early_window_s:
        return "sniper_window_open"
    if early is not None and early > c.max_early_supply_pct:
        return "snipers_hold_too_much"
    return None


def entry_signal(tr: TokenTracker, now: float, c: ScalpConfig) -> Signal:
    if c.entry_mode == "second_dip":
        return second_dip_entry(tr, now, c)
    if c.entry_mode == "initial_zone":
        return initial_zone_entry(tr, now, c)
    return dip_bounce_entry(tr, now, c)


def _common_guards(tr: TokenTracker, now: float, c: ScalpConfig) -> Signal | None:
    if tr.state is None or not tr.ticks:
        return _no("no_data")
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
    blocked = safety_block(tr, now, c)
    if blocked:
        return _no(blocked)
    return None


def _activity_and_confirm(tr: TokenTracker, now: float, c: ScalpConfig, details: dict, low: float) -> Signal | None:
    """Shared by the zone entries: the token must still be trading, nobody may be
    dumping into it, and the configured confirmation must pass. Fills details."""
    price = tr.price
    net_recent, _ = tr.flow(now, c.flow_window_s)
    if -net_recent > c.max_recent_net_sell_sol:
        return _no("heavy_selling", net_sell_sol=round(-net_recent, 3))
    updates = len(tr.window(now, c.flow_window_s))
    if updates < c.min_updates_in_window:
        return _no("market_inactive", updates=updates)
    volume = sum(abs(t.flow_sol) for t in tr.window(now, c.activity_window_s))
    if volume < c.min_activity_sol:
        return _no("market_inactive", volume_sol=round(volume, 3))
    details.update({"volume_sol": round(volume, 3), "creator_launches": tr.creator_launches})
    if c.entry_confirm == "stall":
        last_flow = tr.ticks[-1].flow_sol
        last_sell_t = next((t.t for t in reversed(tr.ticks) if t.flow_sol < 0), None)
        quiet = now - last_sell_t if last_sell_t is not None else float("inf")
        if last_flow <= 0 or quiet < c.confirm_window_s:
            return _no("still_selling", last_flow_sol=round(last_flow, 3), quiet_s=round(min(quiet, 999.0), 1))
        if quiet > c.max_quiet_s:
            return _no("market_inactive", quiet_s=round(min(quiet, 999.0), 1))
        details["quiet_s"] = round(min(quiet, 999.0), 1)
    elif c.entry_confirm == "bounce":
        bounce = price / low - 1.0 if low > 0 else 0.0
        if bounce < c.confirm_bounce_pct:
            return _no("waiting_for_uptick", bounce=round(bounce, 4))
        details["bounce"] = round(bounce, 4)
    return None


def second_dip_entry(tr: TokenTracker, now: float, c: ScalpConfig) -> Signal:
    guard = _common_guards(tr, now, c)
    if guard:
        return guard
    if tr.sd_phase == "invalid":
        return _no(tr.sd_invalid_reason or "pattern_invalid")
    if tr.sd_phase != "second_leg":
        return _no({"wait_rise": "no_launch_spike_yet", "first_leg": "waiting_first_dip",
                    "first_dip": "waiting_recovery"}[tr.sd_phase])
    price = tr.price
    drawdown = 1.0 - price / tr.sd_h2
    if drawdown < c.second_dip_min_pct:
        return _no("second_dip_not_yet", drawdown=round(drawdown, 4))
    details = {"second_dip": round(drawdown, 4), "first_dip": round(1.0 - tr.sd_l1 / tr.sd_h1, 4),
               "h2_vs_opening": round(tr.sd_h2 / tr.launch_price - 1.0, 4),
               "vs_opening": round(price / tr.launch_price - 1.0, 4), "age": round(tr.age(now), 1),
               "dev_supply_pct": round(tr.dev_supply_pct, 4),
               "early_supply_pct": None if (e := tr.early_supply_pct(c.early_window_s)) is None else round(e, 4)}
    blocked = _activity_and_confirm(tr, now, c, details, low=tr.low_since(tr.sd_h2_t))
    if blocked:
        return blocked
    return Signal(True, "second_dip", details)


def initial_zone_entry(tr: TokenTracker, now: float, c: ScalpConfig) -> Signal:
    guard = _common_guards(tr, now, c)
    if guard:
        return guard
    opening, price = tr.launch_price, tr.price
    rise = tr.peak_price / opening - 1.0
    if rise < c.min_initial_rise_pct:
        return _no("no_launch_spike_yet", rise=round(rise, 4))
    vs_opening = price / opening - 1.0
    if vs_opening > c.zone_above_pct:
        return _no("above_buy_zone", vs_opening=round(vs_opening, 4))
    if vs_opening < -c.zone_below_pct:
        return _no("below_buy_zone", vs_opening=round(vs_opening, 4))
    details = {"rise": round(rise, 4), "vs_opening": round(vs_opening, 4), "age": round(tr.age(now), 1),
               "dev_supply_pct": round(tr.dev_supply_pct, 4),
               "early_supply_pct": None if (e := tr.early_supply_pct(c.early_window_s)) is None else round(e, 4)}
    blocked = _activity_and_confirm(tr, now, c, details, low=tr.zone_low or price)
    if blocked:
        return blocked
    return Signal(True, "initial_zone", details)


def dip_bounce_entry(tr: TokenTracker, now: float, c: ScalpConfig) -> Signal:
    guard = _common_guards(tr, now, c)
    if guard:
        return guard
    age = tr.age(now)
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
    if tr.dev_sold:
        return Signal(True, "dev_sold", {"how": tr.dev_sold_reason, "pnl": round(net_pnl_pct, 4)})
    if tr.floor_broken:
        return Signal(True, "support_broken", {"pnl": round(net_pnl_pct, 4)})
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
