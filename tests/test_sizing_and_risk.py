from datetime import datetime, timedelta, timezone

import pytest

from tradeslave import state
from tradeslave.risk import daily_realized_risk
from tradeslave.trader import _paper_haircut, _sized_trade_amount


@pytest.mark.parametrize(
    "balance, expected",
    [
        (100.0, 5.0),      # 1% risk at a 20% stop = $5; 5% exposure cap = $5
        (500.0, 25.0),
        (10_000.0, 50.0),  # TRADE_SIZE_USD cap binds
        (0.0, 0.0),
    ],
)
def test_position_size(balance, expected):
    assert _sized_trade_amount(balance) == pytest.approx(expected)


def test_paper_haircut_applies_slippage_bps():
    assert _paper_haircut(100.0) == pytest.approx(99.5)


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def test_daily_realized_risk_counts_today_for_this_account_once(state_dir):
    now = datetime.now(timezone.utc)
    history = [
        {"account_id": "acc", "trade_id": "t1", "closed_at": _iso(now), "profit_usd": -2.0},
        {"account_id": "acc", "trade_id": "t2", "closed_at": _iso(now), "profit_usd": -1.0},
        {"account_id": "acc", "trade_id": "t2", "closed_at": _iso(now), "profit_usd": -1.0},  # duplicate close
        {"account_id": "acc", "trade_id": "t3", "closed_at": _iso(now - timedelta(days=2)), "profit_usd": -50.0},
        {"account_id": "other", "trade_id": "t4", "closed_at": _iso(now), "profit_usd": -50.0},
        {"trade_id": "legacy", "closed_at": _iso(now), "profit_usd": -50.0},  # no account id
    ]
    state.save_json("trade_history.json", history)
    day, pnl, limit, day_start = daily_realized_risk({"account_id": "acc", "balance": 97.0})
    assert day == now.date().isoformat()
    assert pnl == pytest.approx(-3.0)
    assert day_start == pytest.approx(100.0)
    assert limit == pytest.approx(3.0)


def test_daily_realized_risk_with_no_history(state_dir):
    _, pnl, limit, day_start = daily_realized_risk({"account_id": "acc", "balance": 100.0})
    assert pnl == 0.0
    assert day_start == pytest.approx(100.0)
    assert limit == pytest.approx(3.0)
