"""Rohit's second-dip pattern: initial incline -> tiny first dip -> up a little
-> second dip of 10-45% from the new high -> buy."""

from tradeslave.scalper.core import Scalper
from tradeslave.scalper.curve import decode_failure_reason, encode_bonding_curve, CurveState
from tradeslave.scalper.strategy import entry_signal

from test_scalper import T0
from test_scalper_zone import new_token, step

from tradeslave.scalper.config import ScalpConfig


def sd(**overrides):
    base = dict(fail_rate=0.0, latency_s=0.8, seed=1)   # default entry_mode is second_dip
    base.update(overrides)
    return ScalpConfig(**base)


INCLINE = [(4, 1.15), (5, 1.15), (6, 1.10)]              # +45% over opening
FIRST_DIP = [(7, 0.97), (8, 0.98)]                       # ~5% pullback: tiny
UP = [(9, 1.04), (10, 1.04)]                             # up a little: new high H2
SECOND_DIP = [(12, 0.92), (14, 0.90), (16, 0.97)]        # ~20% below H2
STALL_THEN_BUY = [(19, 1.004)]                           # sellers paused for 3 s, a buyer
CLIMB = [(20 + i, 1.025) for i in range(8)]


def run(cfg, path):
    scalper = Scalper(cfg)
    m = new_token(scalper, "S")
    scalper.trackers["S"].creator_launches = 1
    step(scalper, "S", m, path)
    return scalper, scalper.trackers["S"]


def test_full_pattern_buys_the_second_dip_and_sells_the_climb():
    scalper, tr = run(sd(), INCLINE + FIRST_DIP + UP + SECOND_DIP + STALL_THEN_BUY + CLIMB)
    [trade] = scalper.book.closed
    assert trade["kind"] == "second_dip" and trade["exit_reason"] == "take_profit"
    d = trade["entry_details"]
    assert 0.02 <= d["first_dip"] <= 0.10 and 0.10 <= d["second_dip"] <= 0.45


def test_pattern_states_step_by_step():
    cfg = sd()
    scalper, tr = run(cfg, INCLINE)
    assert tr.sd_phase == "first_leg"
    assert entry_signal(tr, T0 + 6, cfg).reason == "waiting_first_dip"
    scalper, tr = run(cfg, INCLINE + FIRST_DIP)
    assert tr.sd_phase == "first_dip"
    assert entry_signal(tr, T0 + 8, cfg).reason == "waiting_recovery"
    scalper, tr = run(cfg, INCLINE + FIRST_DIP + UP)
    assert tr.sd_phase == "second_leg"
    assert entry_signal(tr, T0 + 10, cfg).reason == "second_dip_not_yet"


def test_no_buy_while_the_second_dip_is_still_falling():
    scalper, tr = run(sd(), INCLINE + FIRST_DIP + UP + SECOND_DIP)
    assert entry_signal(tr, T0 + 16.5, scalper.cfg).reason == "still_selling"
    assert not scalper.book.positions and not scalper.book.orders


def test_a_big_first_dip_is_not_the_pattern():
    scalper, tr = run(sd(), INCLINE + [(7, 0.85)] + UP + SECOND_DIP + STALL_THEN_BUY)
    assert tr.sd_phase == "invalid" and tr.sd_invalid_reason == "first_dip_too_big"
    assert not scalper.book.closed and not scalper.book.positions


def test_a_second_dip_deeper_than_45_percent_is_a_collapse():
    scalper, tr = run(sd(), INCLINE + FIRST_DIP + UP + [(12, 0.7), (14, 0.7)])
    assert tr.sd_phase == "invalid" and tr.sd_invalid_reason == "second_dip_too_deep"


def test_new_high_moves_the_reference_for_the_second_dip():
    scalper, tr = run(sd(), INCLINE + FIRST_DIP + UP + [(11, 1.2)])
    h2 = tr.sd_h2
    assert h2 == tr.price


def test_buy_immediately_mode_enters_in_the_zone():
    scalper, tr = run(sd(entry_confirm="none"), INCLINE + FIRST_DIP + UP + SECOND_DIP)
    assert scalper.book.positions or scalper.book.orders


def test_decode_failure_reasons():
    assert decode_failure_reason("") == "empty_account"
    assert decode_failure_reason("AAAA") == "short_3b"
    assert decode_failure_reason("A" * 80).startswith("other_account_")
    zero = encode_bonding_curve(CurveState(0, 0, 0, 0, 0, False))
    assert decode_failure_reason(zero) == "zero_reserves_not_complete"
