import pytest

from tradeslave.research import candle_builder as cb
from tradeslave.research.launch_recorder import parse_swap, BUY_EVENT
import struct


def test_open_is_previous_close_and_gaps_have_no_candle():
    rows = [(0, 1.0, 1, 30, "curve"), (30, 2.0, 1, 31, "curve"), (130, 1.5, 2, 30.5, "curve")]
    c = cb.build(rows, p_open=0.5)
    assert [x[0] for x in c] == [0, 120]                 # minute 60 had no trade
    assert c[0][1:5] == [0.5, 2.0, 0.5, 2.0] and c[0][5] == 2 and c[0][6] == 31
    assert c[1][1] == 2.0 and c[1][4] == 1.5 and c[1][3] == 1.5


def test_api_replay_matches_curve_math():
    # 30M tokens off a fresh curve cost 30 * 1.073/1.043 - 30 SOL (fees excluded)
    sol = 30 * 1.073e9 / (1.073e9 - 30e6) - 30
    rows = cb.curve_rows_from_api_trades([[1, 0, 100.0, "w", True, sol, 30e6, False]])
    assert rows[0][1] == pytest.approx((30 + sol) / (1.073e9 - 30e6))
    assert rows[0][3] == pytest.approx(30 + sol)


def test_events_give_exact_reserves():
    rows = cb.curve_rows_from_events([[1, 100, "u", 1, 10**9, 10**12, 31 * 10**9, 1_000_000 * 10**6]])
    assert rows[0][3] == 31 and rows[0][1] == pytest.approx(31 / 1_000_000)


def test_compare_counts_matches():
    a = [[0, 1, 1, 1, 1.0, 5, 30, "curve"], [60, 1, 1, 1, 2.0, 5, 30, "curve"]]
    b = [[0, 1, 1, 1, 1.001, 5, 30, "curve"], [60, 1, 1, 1, 2.2, 5, 30, "curve"]]
    assert cb.compare(a, b) == {"a": 2, "b": 2, "both": 2, "close_ok": 1, "vol_ok": 2}


def test_parse_swap_layout():
    body = BUY_EVENT + struct.pack("<q7Q", 123, 5, 0, 0, 0, 1000, 2000, 7) + b"\0" * (120 - 72) + b"\1" * 32 + b"\2" * 32
    ev = parse_swap(body)
    assert ev[0] == 1 and ev[1] == 123 and ev[4:] == [5, 7, 1000, 2000]
