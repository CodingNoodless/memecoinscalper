"""Scalper settings. Every value can be overridden in .env with the SCALP_ prefix.

Percentages are fractions (0.05 = 5%). PnL thresholds are NET: after pump.fun
fees on both sides and network fees, i.e. what actually lands in the wallet.
These are starting points to tune on recorded data, not calibrated values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields

from dotenv import load_dotenv

from ..paths import PROJECT_ROOT

load_dotenv(dotenv_path=PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class ScalpConfig:
    # --- watchlist -------------------------------------------------------
    max_watched: int = 40            # tokens tracked at once (one curve subscription each)
    watch_max_age_s: float = 600.0   # stop watching a flat token after this age
    idle_unwatch_s: float = 60.0     # stop watching if no curve change for this long
    min_initial_buy_sol: float = 0.0  # dev's first buy (skip if below)
    max_initial_buy_sol: float = 5.0  # very large dev buys are often dump setups

    # --- entry mode -------------------------------------------------------
    # second_dip (default, Rohit 2026-10-12): initial incline -> a tiny first dip
    #   -> price goes up a little -> a second dip of 10-45% from that new high:
    #   buy during the second dip once selling stalls.
    # initial_zone: Rohit's method - snipers + dev pump the price, it falls back
    #   to around the opening price, buy there once selling stalls, sell the climb.
    # dip_bounce: earlier rule set (dip from peak + bounce), kept for comparison.
    entry_mode: str = "second_dip"
    min_age_s: float = 5.0           # skip the first seconds of chaos
    max_entry_age_s: float = 300.0   # stop opening new trades on old tokens

    # --- initial_zone rules (prices relative to the opening price after the dev buy)
    min_initial_rise_pct: float = 0.20  # the launch spike must reach +20% first
    zone_above_pct: float = 0.10     # buy zone top: opening price +10%
    zone_below_pct: float = 0.12     # buy zone bottom: opening price -12%
    rug_floor_pct: float = 0.25      # trading below opening -25% = support broken: no entries, exit
    entry_confirm: str = "stall"     # none = buy on touch | stall = sellers paused | bounce = small uptick first
    confirm_window_s: float = 2.0    # stall: no sells for this long and the latest trade is a buy
    confirm_bounce_pct: float = 0.015  # bounce: price this far above the zone low since touch
    max_quiet_s: float = 10.0        # a pause longer than this is a dead token, not a stall
    max_recent_net_sell_sol: float = 1000.0  # skip if more than this much SOL was net-sold in flow_window_s
    activity_window_s: float = 30.0  # the token must still be trading:
    min_activity_sol: float = 1.0    #   at least this much SOL traded (buys + sells) in the window

    # --- second_dip rules (drops measured from the swing high) -----------------
    first_dip_min_move_pct: float = 0.02  # a pullback this big ends the first leg (smaller = noise)
    first_dip_max_pct: float = 0.10       # first dip must be tiny: bigger = not the pattern, skip token
    recover_min_pct: float = 0.03         # "goes up a little": bounce off the first dip low
    second_dip_min_pct: float = 0.10      # buy zone: 10% ...
    second_dip_max_pct: float = 0.45      # ... to 45% below the high after the first dip

    # --- rug / scam filters ----------------------------------------------
    max_dev_supply_pct: float = 0.05     # dev's launch buy as share of the 1B supply (bounces: median 2.4%, failures 9.8%)
    early_window_s: float = 3.0          # "sniper window" after creation
    max_early_supply_pct: float = 0.30   # supply bought in the sniper window (incl. dev)
    dev_dump_match_pct: float = 0.005    # one sell matching the dev's whole bag (+-0.5%) = dev dumped
    watch_creator: bool = True           # subscribe to the creator wallet's transactions
    dev_sell_confirm_s: float = 3.0      # a creator-wallet sell counts only if THIS curve shows a sell
    dev_sell_confirm_fraction: float = 0.5  # of at least this share of the dev bag within +-3 s
    serial_launch_max: int = 2           # creator launched more tokens than this recently = token farm, skip
    serial_window_s: float = 3600.0
    skip_mayhem: bool = False            # pump.fun "mayhem mode" tokens (recorded either way)
    # --- narrative / socials (Rohit: tokens with a Twitter page did better) ---
    require_socials: str = "off"         # off | any | twitter | twitter_profile
    metadata_timeout_s: float = 4.0      # per gateway attempt
    max_twitter_reuse: int = 1000        # skip if this many earlier tokens used the same Twitter link
    first_dip_min_pct: float = 0.20  # price must have fallen this far from the launch peak
    dip_max_pct: float = 0.70        # deeper than this = likely dead/rugged, skip
    # --- dip_bounce rules ------------------------------------------------
    # --- entry: repeat trades = buy pullbacks inside the swing ---------------
    swing_lookback_s: float = 30.0   # recent high is measured over this window
    reentry_pullback_min_pct: float = 0.08
    # --- entry confirmation (both kinds) ---------------------------------
    bounce_min_pct: float = 0.03     # price back up at least this much from the dip low
    bounce_max_pct: float = 0.10     # don't chase: skip if already bounced more than this
    flow_window_s: float = 10.0      # buying-pressure window
    min_net_buy_sol: float = 0.3     # net SOL bought into the curve in the window
    min_updates_in_window: int = 3   # curve must be actively trading
    min_curve_sol: float = 32.0      # virtual SOL reserves (30 at launch): some real buying happened

    # --- exits (net PnL) ----------------------------------------------------
    take_profit_pct: float = 0.05
    stop_loss_pct: float = 0.06      # exit at -6% net
    max_hold_s: float = 60.0         # your winners lasted under a minute
    dump_exit_drop_pct: float = 0.12 # one curve update dropping this much = dev/whale dump, get out
    trail_activate_pct: float = 0.035  # once up this much net...
    trail_giveback_pct: float = 0.025  # ...exit if net PnL falls this far from its peak

    # --- per-token limits ------------------------------------------------
    reentry_cooldown_s: float = 3.0
    max_entries_per_token: int = 6
    max_losses_per_token: int = 2    # stop trading a token after this many losers

    # --- sizing and account risk ----------------------------------------
    paper_start_sol: float = 1.0
    trade_size_sol: float = 0.05
    max_open_positions: int = 4
    daily_loss_limit_sol: float = 0.15   # pause new entries for the UTC day
    max_exposure_sol: float = 0.25

    # --- execution simulation --------------------------------------------
    fee_bps: int = 125               # pump.fun bonding curve fee per side (1.25%, Sep 2026)
    network_fee_sol: float = 0.0003  # base + priority fee per transaction (assumption)
    latency_s: float = 0.8           # decision -> landed transaction
    fail_rate: float = 0.05          # share of transactions that fail (fee paid, no fill)
    sell_retry_s: float = 1.0        # a failed sell is retried after this delay
    seed: int = 7                    # deterministic simulation

    # --- infrastructure --------------------------------------------------
    pumpportal_ws: str = "wss://pumpportal.fun/api/data"
    rpc_ws: str = "wss://api.mainnet-beta.solana.com"
    rpc_subs_per_s: float = 4.0        # pace subscribe requests (public RPC rate-limits bursts)
    rpc_rate_limit_backoff_s: float = 60.0
    rpc_stable_after_s: float = 60.0   # a connection must last this long before backoff resets
    rpc_stall_warn_s: float = 120.0    # warn + reconnect if no curve update arrives while watching tokens
    record_ticks: bool = True
    # --- wallet-level data (who buys/sells) --------------------------------
    # PumpPortal token-trade stream: needs PUMPPORTAL_API_KEY with a funded linked
    # wallet; billed 0.01 SOL per 10,000 messages. Off by default; capped per day.
    trade_feed: bool = False
    trade_feed_max_events_per_day: int = 50_000   # ~0.05 SOL/day at PumpPortal's price
    record_migrations: bool = True                # free: tokens graduating to PumpSwap
    status_every_s: float = 30.0


def _coerce(kind: type, raw: str):
    if kind is bool:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return kind(raw)


def load_config(**overrides) -> ScalpConfig:
    """Defaults <- SCALP_* environment variables <- explicit overrides."""
    values = {}
    for item in fields(ScalpConfig):
        raw = os.getenv(f"SCALP_{item.name.upper()}")
        if raw is not None and raw.strip() != "":
            kind = type(item.default)
            try:
                values[item.name] = _coerce(kind, raw)
            except ValueError as exc:
                raise ValueError(f"SCALP_{item.name.upper()} must be a {kind.__name__}") from exc
    values.update(overrides)
    config = ScalpConfig(**values)
    _validate(config)
    return config


def _validate(c: ScalpConfig) -> None:
    checks = [
        (c.max_watched > 0, "max_watched must be positive"),
        (0 < c.first_dip_min_pct < c.dip_max_pct < 1, "need 0 < first_dip_min_pct < dip_max_pct < 1"),
        (0 < c.reentry_pullback_min_pct < 1, "reentry_pullback_min_pct must be in (0, 1)"),
        (0 <= c.bounce_min_pct < c.bounce_max_pct, "need 0 <= bounce_min_pct < bounce_max_pct"),
        (c.take_profit_pct > 0 and c.stop_loss_pct > 0, "take_profit_pct and stop_loss_pct must be positive"),
        (0 < c.trail_giveback_pct < c.trail_activate_pct <= c.take_profit_pct,
         "need 0 < trail_giveback_pct < trail_activate_pct <= take_profit_pct"),
        (0 <= c.fee_bps < 1000, "fee_bps must be in [0, 1000)"),
        (0 <= c.fail_rate < 1, "fail_rate must be in [0, 1)"),
        (0 < c.trade_size_sol <= c.max_exposure_sol, "need 0 < trade_size_sol <= max_exposure_sol"),
        (c.max_open_positions > 0, "max_open_positions must be positive"),
        (c.min_age_s < c.max_entry_age_s <= c.watch_max_age_s, "need min_age_s < max_entry_age_s <= watch_max_age_s"),
        (c.entry_mode in {"second_dip", "initial_zone", "dip_bounce"},
         "entry_mode must be second_dip, initial_zone or dip_bounce"),
        (0 < c.first_dip_min_move_pct < c.first_dip_max_pct <= c.second_dip_min_pct < c.second_dip_max_pct < 1,
         "need 0 < first_dip_min_move < first_dip_max <= second_dip_min < second_dip_max < 1"),
        (c.entry_confirm in {"none", "stall", "bounce"}, "entry_confirm must be none, stall or bounce"),
        (c.min_initial_rise_pct > c.zone_above_pct >= 0, "need min_initial_rise_pct > zone_above_pct >= 0"),
        (0 <= c.zone_below_pct < c.rug_floor_pct < 1, "need 0 <= zone_below_pct < rug_floor_pct < 1"),
        (0 < c.max_dev_supply_pct <= 1 and 0 < c.max_early_supply_pct <= 1, "supply limits must be in (0, 1]"),
        (c.confirm_window_s < c.max_quiet_s, "need confirm_window_s < max_quiet_s"),
        (c.serial_launch_max >= 1, "serial_launch_max must be at least 1"),
        (c.require_socials in {"off", "any", "twitter", "twitter_profile"},
         "require_socials must be off, any, twitter or twitter_profile"),
    ]
    for ok, message in checks:
        if not ok:
            raise ValueError(f"Scalper config: {message}")
