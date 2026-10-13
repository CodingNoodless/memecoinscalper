import pytest

from tradeslave.trader import _ema, _update_quote_chart


def test_ema_needs_enough_values():
    assert _ema([1.0, 2.0], 3) is None
    assert _ema([1.0, 2.0, 3.0], 0) is None


def test_ema_seed_is_simple_average():
    assert _ema([1.0, 2.0, 3.0], 3) == pytest.approx(2.0)


def test_ema_weights_recent_values():
    # alpha = 2/(3+1) = 0.5: seed 2.0, then 0.5*10 + 0.5*2 = 6
    assert _ema([1.0, 2.0, 3.0, 10.0], 3) == pytest.approx(6.0)


def test_flat_ema_of_constant_series():
    assert _ema([5.0] * 20, 8) == pytest.approx(5.0)


def test_quote_chart_exits_after_three_bearish_samples():
    trade = {}
    for value in [10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 13.0, 13.5]:
        result = _update_quote_chart(trade, value, "t")
        assert not result["bearish_exit"]
    # First drop (11.0) leaves EMA3 above EMA8, so it does not count yet;
    # the next three falling samples each confirm, and the third exits.
    expected = [(11.0, 0, False), (10.0, 1, False), (9.0, 2, False), (8.0, 3, True)]
    for value, confirmations, exits in expected:
        result = _update_quote_chart(trade, value, "t")
        assert result["bearish_confirmations"] == confirmations
        assert result["bearish_exit"] is exits


def test_quote_chart_resets_on_a_rising_sample():
    trade = {}
    for value in [10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 13.0, 13.5, 11.0, 10.0]:
        _update_quote_chart(trade, value, "t")
    result = _update_quote_chart(trade, 14.0, "t")
    assert result["bearish_confirmations"] == 0


def test_quote_chart_keeps_last_24_samples():
    trade = {}
    for i in range(40):
        _update_quote_chart(trade, 10.0 + i, "t")
    assert len(trade["quote_chart_history"]) == 24
