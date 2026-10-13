"""Scalper tests on synthetic markets. A Market moves a real constant-product
curve, so prices, SOL flows and fills follow pump.fun math exactly."""

import math

import pytest

from tradeslave.scalper.book import PaperBook
from tradeslave.scalper.config import ScalpConfig, load_config
from tradeslave.scalper.core import Scalper
from tradeslave.scalper.curve import (
    BONDING_CURVE_DISCRIMINATOR, LAMPORTS_PER_SOL as L, TOKEN_UNIT as U, CurveState, buy_tokens_out,
    decode_bonding_curve, encode_bonding_curve, sell_sol_out, state_from_pumpportal,
)
from tradeslave.scalper.recorder import TickRecorder, read_ticks
from tradeslave.scalper.backtest import replay, summarize
from tradeslave.scalper.strategy import entry_signal
from tradeslave.scalper.tracker import TokenTracker

T0 = 1_791_850_000.0  # fixed epoch for reproducible UTC days


class Market:
    def __init__(self):
        self.vs = 30 * L
        self.vt = 1_073_000_000 * U
        self.k = self.vs * self.vt

    def move(self, factor):
        """Move the price by `factor` (price ~ vs^2 / k)."""
        self.vs = int(self.vs * math.sqrt(factor))
        self.vt = self.k // self.vs
        return self.state()

    def state(self):
        return CurveState(self.vt, self.vs)


def cfg(**overrides):
    base = dict(fail_rate=0.0, latency_s=0.8, seed=1)
    base.update(overrides)
    return ScalpConfig(**base)


LAUNCH_DUMP_BOUNCE = (
    [(1, 1.3), (2, 1.3), (3, 1.3), (4, 1.3)]            # snipers pump ~2.9x
    + [(8, 0.8), (10, 0.85), (12, 0.9), (14, 0.98)]     # sniper dump to 0.6 of peak
    + [(20, 0.99), (21, 0.99), (22, 0.99)]              # chop at the low
    + [(31, 1.012), (32, 1.012), (33, 1.012), (34, 1.012), (35, 1.012), (36, 1.012)]  # buyers return
)
RALLY = [(37 + i, 1.015) for i in range(10)]


def drive(scalper, mint, path, market, start=T0):
    for dt, factor in path:
        t = start + dt
        scalper.on_clock(t)
        scalper.on_curve(mint, t, market.move(factor))


def launch(scalper, mint="MINT1", start=T0):
    market = Market()
    scalper.on_create(mint, mint[:4], start, market.state())
    return market


# --- curve math ---------------------------------------------------------------

def test_launch_price_and_known_buy():
    s = CurveState(1_073_000_000 * U, 30 * L)
    assert s.price_sol == pytest.approx(30 / 1_073_000_000)
    assert buy_tokens_out(s, L, 0) / U == pytest.approx(34_612_903.2, rel=1e-6)


def test_round_trip_costs_two_fees():
    s = CurveState(1_073_000_000 * U, 30 * L)
    tokens = buy_tokens_out(s, L, 125)
    after = CurveState(s.virtual_token_reserves - tokens, s.virtual_sol_reserves + L * 10_000 // 10_125)
    back = sell_sol_out(after, tokens, 125) / L
    assert back == pytest.approx(1 / 1.0125 * 0.9875, rel=1e-4)  # about -2.47%


def test_account_decode_roundtrip_and_rejects_other_accounts():
    s = CurveState(800_000_000 * U, 40 * L, 500 * U, 10 * L, 1_000_000_000 * U, False)
    assert decode_bonding_curve(encode_bonding_curve(s)) == s
    assert BONDING_CURVE_DISCRIMINATOR.hex() == "17b7f83760d8ac60"
    assert decode_bonding_curve("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA") is None
    assert decode_bonding_curve("not base64!!") is None


def test_pumpportal_state_units():
    s = state_from_pumpportal({"vSolInBondingCurve": 31.5, "vTokensInBondingCurve": 1_021_000_000.5})
    assert s.virtual_sol == pytest.approx(31.5)
    assert s.virtual_token_reserves == 1_021_000_000_500_000
    assert state_from_pumpportal({"vSolInBondingCurve": "x"}) is None


# --- full scenarios -------------------------------------------------------------

def test_buys_the_sniper_dump_bounce_and_takes_profit():
    scalper = Scalper(cfg())
    market = launch(scalper)
    drive(scalper, "MINT1", LAUNCH_DUMP_BOUNCE + RALLY, market)
    [trade] = scalper.book.closed
    assert trade["kind"] == "first_dip"
    assert trade["exit_reason"] == "take_profit"
    assert trade["pnl_pct"] >= 0.05
    assert trade["held_s"] < 60
    assert scalper.book.balance_sol > 1.0


def test_entry_fills_at_state_in_effect_after_latency():
    """A trade landing between the signal and our fill moves our fill price."""
    events = []
    scalper = Scalper(cfg(), on_event=lambda kind, data: events.append((kind, data)))
    market = launch(scalper)
    path = LAUNCH_DUMP_BOUNCE[:-3]  # stop right after the entry signal (t=33)
    drive(scalper, "MINT1", path, market)
    requested = next(d for k, d in events if k == "buy_requested")
    # Someone else buys 0.5 s after our signal, before our transaction lands.
    jumped = market.move(1.03)
    scalper.on_curve("MINT1", requested["t"] + 0.5, jumped)
    scalper.on_clock(requested["t"] + 1.0)
    filled = next(d for k, d in events if k == "buy_filled")
    assert filled["t"] >= requested["t"] + 0.8
    assert filled["price"] == pytest.approx(jumped.price_sol)
    assert filled["price"] > requested["price"]


def test_dump_after_entry_exits_fast_with_a_loss():
    scalper = Scalper(cfg())
    market = launch(scalper)
    drive(scalper, "MINT1", LAUNCH_DUMP_BOUNCE + [(37, 0.75), (38, 0.98), (39, 0.98)], market)
    [trade] = scalper.book.closed
    assert trade["exit_reason"] in {"dump_detected", "stop_loss"}
    assert trade["pnl_sol"] < 0
    assert scalper.trackers["MINT1"].losses == 1


def test_flat_market_hits_time_stop():
    scalper = Scalper(cfg(max_hold_s=20))
    market = launch(scalper)
    flat = [(37 + i, 1.0005 if i % 2 else 0.9995) for i in range(40)]
    drive(scalper, "MINT1", LAUNCH_DUMP_BOUNCE + flat, market)
    assert scalper.book.closed[0]["exit_reason"] == "time_stop"


def test_time_stop_fires_from_the_clock_when_token_goes_quiet():
    scalper = Scalper(cfg(max_hold_s=10))
    market = launch(scalper)
    drive(scalper, "MINT1", LAUNCH_DUMP_BOUNCE, market)
    assert scalper.book.positions
    for i in range(100):
        scalper.on_clock(T0 + 37 + i * 0.2)
    assert scalper.book.closed and scalper.book.closed[0]["exit_reason"] == "time_stop"


def test_repeat_trade_on_same_token_after_pullback():
    scalper = Scalper(cfg())
    market = launch(scalper)
    second_swing = (
        [(50 + i, 0.97) for i in range(4)]                 # pullback ~11% from the swing high
        + [(56 + i, 1.012) for i in range(6)]              # buyers return
        + [(62 + i, 1.015) for i in range(10)]             # rally
    )
    drive(scalper, "MINT1", LAUNCH_DUMP_BOUNCE + RALLY + second_swing, market)
    kinds = [c["kind"] for c in scalper.book.closed]
    assert kinds[0] == "first_dip"
    assert "pullback" in kinds[1:]
    assert scalper.trackers["MINT1"].entries >= 2


def test_trades_two_tokens_at_once():
    scalper = Scalper(cfg())
    m1 = launch(scalper, "MINT1")
    m2 = launch(scalper, "MINT2")
    for dt, factor in LAUNCH_DUMP_BOUNCE:
        t = T0 + dt
        scalper.on_clock(t)
        scalper.on_curve("MINT1", t, m1.move(factor))
        scalper.on_curve("MINT2", t + 0.1, m2.move(factor))
    scalper.on_clock(T0 + 36.5)
    assert set(scalper.book.positions) == {"MINT1", "MINT2"}


def test_max_open_positions_is_enforced():
    scalper = Scalper(cfg(max_open_positions=1))
    m1, m2 = launch(scalper, "MINT1"), launch(scalper, "MINT2")
    for dt, factor in LAUNCH_DUMP_BOUNCE:
        t = T0 + dt
        scalper.on_clock(t)
        scalper.on_curve("MINT1", t, m1.move(factor))
        scalper.on_curve("MINT2", t + 0.1, m2.move(factor))
    assert len(scalper.book.positions) + len(scalper.book.orders) == 1
    assert scalper.entry_block_reasons.get("max_open_positions", 0) >= 1


def test_failed_buy_costs_a_fee_and_opens_nothing():
    scalper = Scalper(cfg(fail_rate=0.999999))
    market = launch(scalper)
    drive(scalper, "MINT1", LAUNCH_DUMP_BOUNCE, market)
    assert not scalper.book.positions
    assert scalper.book.failed_tx >= 1
    assert scalper.book.balance_sol < 1.0


def test_daily_loss_limit_blocks_new_entries():
    book = PaperBook(cfg(daily_loss_limit_sol=0.01))
    book.day, book.day_pnl_sol = "2026-10-12", -0.02
    book._roll_day = lambda now: None
    assert book.can_open(T0) == (False, "daily_loss_limit")


# --- entry guards ---------------------------------------------------------------

def _tracker_after(path):
    market = Market()
    tr = TokenTracker("M", "M", created_at=T0)
    tr.update(T0, market.state())
    for dt, factor in path:
        tr.update(T0 + dt, market.move(factor))
    return tr


def test_no_entry_before_min_age():
    tr = _tracker_after(LAUNCH_DUMP_BOUNCE)
    assert entry_signal(tr, T0 + 10, cfg()).reason == "too_young"


def test_no_entry_without_a_dip():
    tr = _tracker_after([(1, 1.3), (2, 1.3), (31, 1.01), (32, 1.01), (33, 1.01), (34, 1.01)])
    assert entry_signal(tr, T0 + 34, cfg()).reason == "no_dip_yet"


def test_no_entry_when_bounce_already_ran():
    path = LAUNCH_DUMP_BOUNCE + [(37 + i, 1.03) for i in range(4)]
    tr = _tracker_after(path)
    assert entry_signal(tr, T0 + 40, cfg()).reason == "bounce_already_extended"


def test_no_entry_while_still_selling():
    path = LAUNCH_DUMP_BOUNCE[:-6] + [(31, 1.04), (40, 0.995), (41, 0.995), (42, 0.995), (43, 0.995)]
    tr = _tracker_after(path)
    assert entry_signal(tr, T0 + 43, cfg()).reason in {"buying_not_back", "waiting_for_bounce"}


# --- recorder + backtest ----------------------------------------------------------

def test_recorded_ticks_replay_to_the_same_result(tmp_path):
    rec = TickRecorder(tmp_path)
    live = Scalper(cfg())
    market = Market()
    live.on_create("MINT1", "MINT", T0, market.state())
    rec.create(T0, "MINT1", "MINT", market.state())
    for dt, factor in LAUNCH_DUMP_BOUNCE + RALLY:
        t = T0 + dt
        state = market.move(factor)
        live.on_clock(t)
        live.on_curve("MINT1", t, state)
        rec.curve(t, "MINT1", state)
    rec.close()
    replayed = replay(read_ticks(list(tmp_path.glob("*.jsonl.gz"))), cfg())
    summary = summarize(replayed)
    assert summary["trades"] == 1
    assert replayed.book.closed[0]["pnl_sol"] == pytest.approx(live.book.closed[0]["pnl_sol"], rel=1e-6)
    assert list(summary["by_exit_reason"]) == ["take_profit"]
    assert summary["by_exit_reason"]["take_profit"]["n"] == 1


# --- config -----------------------------------------------------------------------

def test_env_override_and_validation(monkeypatch):
    monkeypatch.setenv("SCALP_TAKE_PROFIT_PCT", "0.04")
    assert load_config().take_profit_pct == 0.04
    with pytest.raises(ValueError):
        load_config(stop_loss_pct=-0.05)
    with pytest.raises(ValueError):
        load_config(trail_activate_pct=0.2)
