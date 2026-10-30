"""tradeslave/sizing.py: guarded fractional Kelly and the drawdown throttle."""

from tradeslave.sizing import drawdown_scale, growth_optimal, kelly_fraction, position_size


def test_no_edge_no_size():
    assert kelly_fraction([0.05, -0.05] * 50) == 0.0                 # zero mean
    assert kelly_fraction([-0.25] * 80 + [3.0] * 5) == 0.0           # lottery: positive point mean, lower bound < 0
    assert kelly_fraction([0.3, -0.1] * 10) == 0.0                   # too few trades


def test_real_edge_is_sized_and_capped():
    good = [0.25, -0.15] * 100                                        # clear edge, t ~ 5.6
    f = kelly_fraction(good, cap=1.0)
    assert 0 < f < growth_optimal(good) / 2                           # haircut + half Kelly
    assert kelly_fraction(good) == 0.10                               # default cap


def test_rugs_bound_the_fraction():
    assert growth_optimal([0.5] * 50 + [-1.0]) < 1.0                  # a -100% trade forbids betting it all


def test_drawdown_throttle():
    assert drawdown_scale(95, 100) == 1.0
    assert abs(drawdown_scale(82.5, 100) - 0.5) < 1e-9
    assert drawdown_scale(70, 100) == 0.0
    assert position_size(10, 20, [0.25, -0.15] * 100) == 0.0         # 50% down: stop trading
