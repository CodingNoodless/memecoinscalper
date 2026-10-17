"""Learned exits, the embargoed split, two-half selection and the shadow lane."""

import math

import numpy as np
import pytest

from tradeslave.autopilot import features as F
from tradeslave.autopilot import model as M
from tradeslave.autopilot import samples as S
from tradeslave.autopilot.live import ModelGate
from tradeslave.scalper.config import ScalpConfig
from tradeslave.scalper.core import Scalper
from tradeslave.scalper.strategy import Signal

from test_scalper import Market, T0
from autopilot_fakes import FakeModel

ECON = dict(size=0.15, latency=0.8, fee_bps=125, network_fee=0.0003)


def path_of(factors, dt=1.0):
    """Compact price path: one curve state per second, each moved by a factor."""
    m = Market()
    t, vs, vt = [T0], [m.vs], [m.vt]
    for i, f in enumerate(factors, 1):
        m.move(f)
        t.append(T0 + i * dt); vs.append(m.vs); vt.append(m.vt)
    return np.array(t, dtype=float), np.array(vs, dtype=np.int64), np.array(vt, dtype=np.int64)


def test_learned_exit_sells_when_the_score_drops():
    path = path_of([1.02] * 20 + [0.97] * 40)          # up 20 s, then down
    times = np.array([T0 + k for k in range(2, 60, 2)], dtype=float)
    scores = np.array([1.0 if t < T0 + 20 else 0.0 for t in times])
    ret, exit_t = M.simulate_learned(path, T0 + 1, times, scores, 0.5, 0.5, 120.0, **_e())
    assert exit_t == T0 + 20 and ret > 0.1            # sold at the top, kept the gain
    hold_ret, hold_exit = M.simulate_learned(path, T0 + 1, times, np.ones_like(scores), 0.5, 0.5, 120.0, **_e())
    assert hold_ret < ret                              # never selling rode it back down


def test_learned_exit_hard_stop_and_time_cap():
    path = path_of([0.95] * 30)
    times = np.array([T0 + k for k in range(2, 30, 2)], dtype=float)
    ret, exit_t = M.simulate_learned(path, T0, times, np.ones(len(times)), 0.5, 0.15, 120.0, **_e())
    assert exit_t <= T0 + 5 and ret < -0.15            # stop hit on a tick (fill a little worse)
    flat = path_of([1.0001] * 30)
    _, exit_t = M.simulate_learned(flat, T0, times, np.ones(len(times)), 0.5, 0.15, 10.0, **_e())
    assert exit_t == pytest.approx(T0 + 0.8 + 10.0)


def _e():
    return dict(size=ECON["size"], latency=ECON["latency"], fee_bps=ECON["fee_bps"], network_fee=ECON["network_fee"])


def test_trade_learned_one_position_per_token_at_a_time():
    path = path_of([1.01] * 60)
    samples = [S.Sample("A", T0 + k, T0, np.zeros(len(F.FEATURES), dtype=np.float32), {}, True) for k in range(2, 60, 2)]
    entry = [1.0] * len(samples)
    exit_scores = [0.0 if k % 10 == 0 else 1.0 for k in range(2, 60, 2)]   # sell signal every 10 s
    free = {**ECON, "cooldown": 0.0, "max_per_token": 99, "min_hold": 0.0}
    trades = M.trade_learned(samples, entry, 0.5, exit_scores, 0.5, 0.5, 120.0, {"A": path}, free)
    times = [t for _, _, t in trades]
    assert len(trades) >= 3 and all(b - a >= 2 for a, b in zip(times, times[1:]))
    assert M.trade_learned(samples, entry, 0.5, exit_scores, 0.5, 0.5, 120.0, {}, ECON) == []   # no path: no trade


def test_trade_learned_cooldown_cap_and_min_hold():
    path = path_of([1.01] * 120)
    samples = [S.Sample("A", T0 + k, T0, np.zeros(len(F.FEATURES), dtype=np.float32), {}, True) for k in range(2, 120, 2)]
    entry = [1.0] * len(samples)
    exit_scores = [0.0] * len(samples)                  # the exit model wants out at every re-score
    econ = {**ECON, "cooldown": 30.0, "max_per_token": 2, "min_hold": 10.0}
    trades = M.trade_learned(samples, entry, 0.5, exit_scores, 0.5, 0.5, 120.0, {"A": path}, econ)
    assert len(trades) == 2                             # capped per token
    assert trades[1][2] - trades[0][2] >= 10.0 + 30.0   # held >= min_hold, then waited out the cooldown
    _, exit_t = M.simulate_learned(path, T0 + 2, np.array([T0 + 4.0, T0 + 8.0, T0 + 14.0]), np.zeros(3), 0.5, 0.5,
                                   120.0, **_e(), min_hold=10.0)
    assert exit_t == T0 + 14.0                          # the first two sell signals came before min_hold


def test_trade_cooldown_and_cap():
    samples = [S.Sample("A", T0 + k, T0, np.zeros(1), {"hold30": 0.01}, True) for k in range(0, 300, 2)]
    trades = M.trade(samples, [1.0] * len(samples), "hold30", 0.5, cooldown=30.0, max_per_token=2)
    assert [t - T0 for _, _, t in trades] == [0, 62]    # hold 30 s (+1) + 30 s cooldown, then the cap
    assert len(M.trade(samples, [1.0] * len(samples), "hold30", 0.5, cooldown=0.0, max_per_token=99)) == 10


def test_own_impact_sell_returns_the_buy_minus_fees():
    from tradeslave.research.dataset import Token
    from tradeslave.research.sim import simulate
    tok = Token("A", T0, t=[T0, T0 + 100], vs=[30_000_000_000] * 2, vt=[1_073_000_000_000_000] * 2)
    own = simulate(tok, T0, tp=9, sl=0.99, hold=30, size=0.15, network_fee=0.0003, own_impact=True)
    paper = simulate(tok, T0, tp=9, sl=0.99, hold=30, size=0.15, network_fee=0.0003, own_impact=False)
    fees_only = (0.15 / 1.0125 * 0.9875 - 0.0003) / 0.1503 - 1        # 1.25% each way + network fee
    assert own == pytest.approx(fees_only, abs=2e-4)
    assert paper < own - 0.008                          # the old fill paid our own price impact twice (~1%)


def test_split_embargo_drops_tokens_right_after_each_boundary():
    samples = [S.Sample(f"m{k}", T0 + k * 100, T0 + k * 100, np.zeros(1), {}, True) for k in range(100)]
    tr, va, ho = M.split(samples, embargo_s=600)
    assert len(tr) == 60 and len(va) == 14 and len(ho) == 14      # 6 tokens embargoed after each boundary
    assert max(s.t0 for s in tr) + 600 < min(s.t0 for s in va)


def test_eligible_needs_both_validation_halves_positive():
    good = M.evaluate([("a", 0.05, 1), ("b", 0.04, 2), ("c", 0.03, 3), ("d", 0.05, 4)] * 10)
    assert good.half_means and min(good.half_means) > 0
    lucky = M.evaluate([(f"x{i}", -0.02, i) for i in range(20)] + [(f"y{i}", 0.2, 100 + i) for i in range(20)])
    assert lucky.mean > 0 and not M.eligible(lucky)


def mcfg(**kw):
    base = dict(entry_mode="model", fail_rate=0.0, latency_s=0.8, seed=1, min_age_s=3.0, autopilot=False)
    base.update(kw)
    return ScalpConfig(**base)


def feed(scalper, mint, factors, start=T0):
    m = Market()
    scalper.on_create(mint, mint, start, m.move(1.0), "dev", "c", 0.5, 10_000_000, False)
    for dt, f in factors:
        scalper.on_curve(mint, start + dt, m.move(f))


def learned_card(exit_threshold=0.5):
    ev = M.Eval(trades=50, tokens=40, mean=0.02, median=0.01, win=0.5, total=1.0, top_token_share=0.1)
    return M.Card(version="v2", created=0, policy="learned", threshold=0.01, n_tokens=100, n_samples=1000,
                  exact_share=0.5, data_from=0, data_to=1, validation=ev, holdout=ev, paper_ok=False, proven=False,
                  exit={"scorer": "same", "threshold": exit_threshold, "sl": 0.15, "hold": 120.0, "q": 0.5, "cols": None})


def test_shadow_gate_trades_unproven_models_and_learned_exit_sells(tmp_path):
    cfg = mcfg()
    M.save_champion(learned_card(exit_threshold=0.02), FakeModel(0.05), directory=tmp_path, name="shadow")
    gate = ModelGate(cfg, path=tmp_path / "shadow.pkl", shadow=True)
    assert gate.card is not None and "shadow-testing" in gate.status and "learned exits" in gate.status
    s = Scalper(cfg)
    s.add_shadow(gate)
    feed(s, "Z", [(i, 1.01) for i in range(1, 12)])
    lane = s.shadow
    assert "Z" in lane.book.positions and not s.book.positions        # shadow trades, main lane has no model
    assert lane.position_policy["Z"]["learned"] and not s.forget("Z")  # shadow position keeps the tracker
    gate.model = FakeModel(0.0)                                         # score now below the exit threshold
    tr = s.trackers["Z"]
    for i in range(12, 20):
        s.on_curve("Z", T0 + i, type(tr.state)(tr.state.virtual_token_reserves - 10 ** 12, tr.state.virtual_sol_reserves + 10 ** 7))
    assert lane.book.closed and lane.book.closed[0]["exit_reason"] == "model_exit"
    assert s.book.closed == [] and s.trackers["Z"].losses == 0         # main lane's state untouched


def test_champion_gate_refuses_unproven_learned_card(tmp_path):
    M.save_champion(learned_card(), FakeModel(0.05), directory=tmp_path)
    gate = ModelGate(mcfg(), path=tmp_path / "champion.pkl")
    s = Scalper(mcfg())
    feed(s, "Q", [(i, 1.01) for i in range(1, 10)])
    assert gate.entry(s.trackers["Q"], T0 + 9).reason == "model_not_proven"


def test_load_samples_fills_price_paths_fresh_and_from_cache(tmp_path):
    from tradeslave.scalper.recorder import TickRecorder
    ticks, cache = tmp_path / "ticks", tmp_path / "cache"
    rec = TickRecorder(ticks)
    m = Market()
    rec.create(T0, "P", "P", m.move(1.0), "dev", "c", 0.5, 10_000_000, False)
    for i in range(1, 60):
        rec.curve(T0 + i, "P", m.move(1.01 if i < 30 else 0.99))
    rec.close()
    for _ in range(2):                                   # second pass reads the day cache
        found: dict = {}
        out = S.load_samples(mcfg(), 14, tick_dir=ticks, cache_dir=cache, log=lambda *a: None, price_paths=found)
        assert out and set(found) == {"P"} and len(found["P"][0]) >= 50


def test_live_model_entries_follow_the_same_per_token_rules():
    sc = Scalper(ScalpConfig(model_reentry_cooldown_s=30.0, model_max_trades_per_token=2))
    book = sc.book
    assert sc._model_blocked(book, "A", T0) == ""
    book.trades_on["A"], book.last_close_t["A"] = 1, T0
    assert sc._model_blocked(book, "A", T0 + 10) == "reentry_cooldown"
    assert sc._model_blocked(book, "A", T0 + 31) == ""
    book.trades_on["A"] = 2
    assert sc._model_blocked(book, "A", T0 + 999) == "max_trades_per_token"
