import pytest

from tradeslave.research import creator


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


def test_launch_rows_groups_the_creation_slot():
    rows = [{"i": 0, "slot": 7, "w": "dev", "b": 1, "sol": 4.0, "t": 0.0},
            {"i": 1, "slot": 7, "w": "bundle", "b": 1, "sol": 2.0, "t": 0.0},
            {"i": 2, "slot": 8, "w": "bot", "b": 1, "sol": 1.0, "t": 1.0},
            {"i": 3, "slot": 9, "w": "dev", "b": 0, "sol": 3.0, "t": 2.0}]
    d = creator.launch_rows("M", 0.0, rows)
    assert d["x"] == 6.0                                       # dev + bundle in the creation slot
    assert [r["w"] for r in d["outside"]] == ["bot"]          # the group's later sells are not "outside"
    assert creator.launch_rows("M", 0.0, []) is None
