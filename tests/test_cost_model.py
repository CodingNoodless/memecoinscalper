import pytest

from tradeslave import cost_model as cm
from tradeslave.scalper.curve import CurveState, buy_tokens_out

ZERO = cm.with_overrides(mev_side=0.0, min_slip_side=0.0, network_fee_sol=0.0, fail_rate=0.0)


def test_locked_numbers():
    assert cm.COSTS.fee_side == 0.0125
    assert cm.COSTS.network_fee_sol >= 0.0003
    assert cm.COSTS.min_slip_side >= 0.01  # never better fills than the paper bots


def test_buy_impact_matches_curve_math():
    vs, vt = 30 * 10**9, 1_073_000_000 * 10**6
    st = CurveState(vt, vs)
    size = 1.0
    tokens = buy_tokens_out(st, int(size * 10**9), 0) / 10**6
    avg_price = size / tokens
    assert avg_price / st.price_sol - 1 == pytest.approx(cm.buy_impact(size, 30), rel=1e-6)


def test_fee_only_round_trip():
    # tiny size in a huge pool, only the 1.25% fee each side
    r = cm.trade_return(0.0, 0.001, 1e9, 1e9, costs=ZERO)
    assert r == pytest.approx((1 - 0.0125) ** 2 - 1, abs=1e-6)


def test_default_round_trip_is_at_least_fees_plus_paper_slippage():
    r = cm.round_trip_cost(0.1, 85)
    assert r < (1 - 0.0125) ** 2 * (1 - 0.01) ** 2 - 1


def test_slippage_scales_with_size_vs_liquidity():
    small = cm.trade_return(0.2, 0.1, 30)
    big = cm.trade_return(0.2, 5.0, 30)
    deep = cm.trade_return(0.2, 5.0, 3000)
    assert big < small and big < deep


def test_stop_fill_applies_gap():
    at_level = cm.trade_return(-0.2, 0.1, 85, stop_fill=False)
    gapped = cm.trade_return(-0.2, 0.1, 85, stop_fill=True)
    assert gapped < at_level
    assert cm.COSTS.stop_gap < 0


def test_total_loss_bounded():
    assert cm.trade_return(-1.0, 0.1, 85) < -1.0
    assert cm.trade_return(-1.0, 0.1, 85) > -1.1


def test_breakeven_and_capacity():
    be = cm.breakeven_gross(0.1, 85)
    assert cm.trade_return(be, 0.1, 85) == pytest.approx(0, abs=1e-6)
    gross = [0.30] * 50 + [-0.10] * 50          # +10% gross mean
    cap = cm.capacity(gross, [30.0] * 100)
    assert 0.1 < cap < 1000
    assert cm.mean_net(gross, cap * 1.5, [30.0] * 100) < 0 < cm.mean_net(gross, cap * 0.5, [30.0] * 100)
    assert cm.capacity([-0.05] * 10, [85.0] * 10) == 0.0


def test_entry_latency_and_gap_sampler():
    assert cm.entry_time(100.0) == 100.0 + cm.COSTS.entry_latency_s
    assert cm.sample_stop_gap(0.5) == pytest.approx(cm.STOP_GAP_MEDIAN)
    assert cm.sample_stop_gap(0.0) <= cm.sample_stop_gap(0.5) <= cm.sample_stop_gap(1.0)
