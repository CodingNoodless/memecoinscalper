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
    max_watched: int = 25            # tokens tracked at once (one curve subscription each)
    watch_max_age_s: float = 600.0   # stop watching a flat token after this age
    idle_unwatch_s: float = 60.0     # stop watching if no curve change for this long
    min_initial_buy_sol: float = 0.0  # dev's first buy (skip if below)
    max_initial_buy_sol: float = 5.0  # very large dev buys are often dump setups

    # --- entry: first trade on a token = buy the sniper dump ----------------
    min_age_s: float = 30.0          # don't trade the first seconds of chaos
    max_entry_age_s: float = 480.0   # stop opening new trades on old tokens
    first_dip_min_pct: float = 0.20  # price must have fallen this far from the launch peak
    dip_max_pct: float = 0.70        # deeper than this = likely dead/rugged, skip
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
    record_ticks: bool = True
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
    ]
    for ok, message in checks:
        if not ok:
            raise ValueError(f"Scalper config: {message}")
