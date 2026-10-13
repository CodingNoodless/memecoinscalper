import time

import pytest

from tradeslave.trader import _entry_pullback_reclaim_status


def chart(fast=1.00, slow=0.95, age_seconds=60, reasons=None):
    return {
        "fast_ema_5": fast,
        "slow_ema_13": slow,
        "latest_candle_at": time.time() - age_seconds,
        "rejection_reasons": reasons or [],
    }


def samples(*prices):
    return {"price_samples": [{"price_usd": p} for p in prices]}


@pytest.mark.parametrize(
    "analysis, price, reason",
    [
        (chart(), None, "live_market_price_unavailable"),
        (chart(), 0.0, "live_market_price_unavailable"),
        (chart(fast=0.9, slow=0.95), 1.0, "closed_chart_trend_not_bullish"),
        (chart(age_seconds=1_000), 1.0, "closed_chart_stale"),
        (chart(reasons=["chart_fast_ema_not_rising"]), 1.0, "closed_chart_gate:chart_fast_ema_not_rising"),
        (chart(), 0.94, "live_price_below_slow_ema"),
        (chart(), 0.95 * 1.21, "live_price_overextended_from_slow_ema"),
        (chart(), 1.05, "waiting_for_pullback_near_fast_ema"),
    ],
)
def test_entry_blockers(analysis, price, reason):
    ok, why = _entry_pullback_reclaim_status(samples(price or 1.0), analysis, price)
    assert not ok
    assert why == reason


def test_close_below_fast_ema_alone_does_not_block():
    pending = samples(1.05)
    ok, why = _entry_pullback_reclaim_status(pending, chart(reasons=["chart_close_below_fast_ema"]), 1.05)
    assert why == "waiting_for_pullback_near_fast_ema"


def test_pullback_then_reclaim_triggers_entry():
    pending = samples(1.004, 1.006)
    pending["pullback_seen"] = True
    ok, why = _entry_pullback_reclaim_status(pending, chart(), 1.006)
    assert ok
    assert why == "fast_ema_pullback_reclaimed"


def test_first_touch_marks_pullback_but_waits_for_reclaim():
    pending = samples(1.001)
    ok, why = _entry_pullback_reclaim_status(pending, chart(), 1.001)
    assert not ok
    assert pending["pullback_seen"] is True
    assert why == "waiting_for_fast_ema_reclaim"


def test_reclaim_needs_previous_sample_near_ema():
    pending = samples(1.05, 1.006)
    pending["pullback_seen"] = True
    ok, why = _entry_pullback_reclaim_status(pending, chart(), 1.006)
    assert not ok
    assert why == "waiting_for_fast_ema_reclaim"


def test_documents_overlapping_pullback_and_reclaim_bands():
    """Known weakness (see vault Known issues): the 1% pullback band contains the
    0.2% reclaim level, so two samples anywhere in (EMA+0.2%, EMA+1%] count as a
    'pullback and reclaim' with no actual dip. Kept as-is in Phase 0; Phase 3
    tests better entry timing on recorded data."""
    pending = samples(1.008, 1.009)
    ok, _ = _entry_pullback_reclaim_status(pending, chart(), 1.009)
    assert ok
