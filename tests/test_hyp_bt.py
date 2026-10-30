import pytest

from tradeslave.research import hyp_bt as hb


def cand(t, o, h, l, c, v=1.0, ph="curve"):
    return [t, o, h, l, c, v, hb.reserve(c, ph), ph]


def test_static_price_fill_when_nobody_trades_next_minute():
    c = [cand(0, 1, 1, 1, 1.0), cand(600, 1.3, 1.3, 1.3, 1.3)]
    p, t, j, first_stop_only = hb.entry_fill(c, 0)
    assert p == 1.0 and j == 1 and not first_stop_only


def test_next_open_fill_and_entry_candle_checks_only_stop():
    # next minute trades: fill at its open; its high hits the target but that must not count
    c = [cand(0, 1, 1, 1, 1.0), cand(60, 1.0, 1.5, 0.99, 1.0), cand(120, 1.0, 1.0, 1.0, 1.0)]
    tr, _ = hb.simulate(c, 0, tgt=0.2, stop=0.15, hold_s=3600)
    assert tr["why"] != "target"


def test_stop_inside_candle_uses_gap():
    c = [cand(0, 1, 1, 1, 1.0), cand(600, 1.0, 1.0, 0.5, 0.6)]
    tr, _ = hb.simulate(c, 0, tgt=0.5, stop=0.15, hold_s=3600)
    assert tr["why"] == "stop" and tr["stop_fill"] and tr["ret"] < -0.15 - 0.10


def test_dead_token_kept_and_exits_at_last_close():
    c = [cand(0, 1, 1, 1, 1.0)]
    tr, _ = hb.simulate(c, 0, tgt=0.5, stop=0.15, hold_s=3600)
    assert tr["gross"] == 0 and tr["ret"] == pytest.approx(hb.trade_return(0.0, hb.SIZE, tr["sol_reserve_in"], tr["sol_reserve_out"]))


def test_holdout_refused_by_default():
    with pytest.raises(SystemExit, match="REFUSED"):
        hb.load("holdout")
