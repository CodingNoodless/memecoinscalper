"""Graduated-runner backtest: fills, costs, stops."""

import pytest

from tradeslave.research import runners as R


def tok(closes, highs=None, lows=None, opens=None, p0=4e-7):
    n = len(closes)
    t = [1000.0 + 60 * i for i in range(n)]
    o = opens or closes
    return R.Tok("A", 1000.0, t, o, highs or closes, lows or closes, closes, [1.0] * n, p0)


def test_fee_tiers_follow_market_cap():
    assert R.fee_for(4e-7) == 0.0125          # 400 SOL mcap: 0-420 SOL tier
    assert R.fee_for(1e-6) == 0.0120          # 1,000 SOL
    assert R.fee_for(1e-4) == 0.0030          # 100k SOL mcap
    assert R.fee_for(5e-6) == 0.0100          # 5,000 SOL


def test_flat_trade_loses_about_the_costs():
    p = 2e-6
    r, _ = R.trade(tok([p] * 10, p0=p), 0, 0.5, tp=0.2, sl=0.2, hold_min=3)
    assert r == pytest.approx(-(1 - (1 - 0.0115) ** 2) - 0.0006 / 0.5003, abs=1e-3)   # fees only: our buy unwinds


def test_gap_through_stop_fills_at_the_open_and_stop_beats_target():
    p = 2e-6
    gap = tok([p, p, p * 0.6, p * 0.6], lows=[p, p, p * 0.5, p * 0.5], opens=[p, p, p * 0.6, p * 0.6], p0=p)
    r, _ = R.trade(gap, 0, 0.5, tp=0.2, sl=0.15, hold_min=10)
    assert r < -0.40                          # filled at the 0.6 open, not at the -15% stop
    both = tok([p, p, p], highs=[p, p, p * 1.5], lows=[p, p, p * 0.7], p0=p)
    r2, _ = R.trade(both, 0, 0.5, tp=0.2, sl=0.15, hold_min=10)
    assert r2 < 0                             # the candle touched both: count the stop


def test_live_runner_round_trip_costs_only_fees_when_price_is_flat():
    from tradeslave.runner.__main__ import Position, buy, sell_value
    price, pool = 2e-6, 120.0
    dx, tokens = buy(price, pool, size=0.5)
    pos = Position("S5", "A", 0.0, price, dx, tokens, 0.5003, price)
    r = sell_value(pos, price, pool) / 0.5003 - 1
    assert r == pytest.approx(-(1 - (1 - 0.0115) ** 2) - 0.0006 / 0.5003, abs=1e-3)
