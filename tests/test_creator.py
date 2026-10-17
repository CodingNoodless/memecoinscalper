import pytest

from tradeslave.research import creator, launch_watch


def _lau(outside):
    return {"m": "M", "t0": 0.0, "tc": 0.0, "x": 5.0, "outside": outside}


def test_no_outside_trades_loses_round_trip_fees_and_costs():
    got = creator.replay(_lau([]), 5.0, 15)
    want = 5.0 * (1 - creator.FEE) ** 2 - 5.0 - creator.LAUNCH_COST - 3 * creator.TX_COST
    assert got == pytest.approx(want, rel=1e-6)


def test_outside_buy_before_exit_makes_money_after_exit_does_not():
    buy = {"t": 5.0, "b": 1, "sol": 10.0, "dv": int(10e9 * (1 - creator.FEE)), "tok": 0.0, "w": "x"}
    early = creator.replay(_lau([buy]), 5.0, 15)
    late = creator.replay(_lau([{**buy, "t": 20.0}]), 5.0, 15)
    assert early > 0.5
    assert late < 0                        # we sold before that buyer came: only the creator fee on it
    assert late == pytest.approx(creator.replay(_lau([]), 5.0, 15) + creator.CREATOR_FEE * 10, rel=1e-6)


def test_live_replay_matches_history_replay_and_skips_creator():
    create = {"t": 0.0, "traderPublicKey": "dev", "solAmount": 5.0}
    trades = [{"t": 3.0, "traderPublicKey": "dev", "txType": "sell", "solAmount": 1.0, "tokenAmount": 1e6},
              {"t": 5.0, "traderPublicKey": "x", "txType": "buy", "solAmount": 10.0, "tokenAmount": 0}]
    hist = creator.replay(_lau([{"t": 5.0, "b": 1, "sol": 10.0, "dv": 10e9 * (1 - creator.FEE), "tok": 0, "w": "x"}]), 5.0, 15)
    assert launch_watch.replay(create, trades, 5.0) == pytest.approx(hist, rel=1e-9)
