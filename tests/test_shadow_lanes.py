"""Shadow lanes, snapshot-cadence entries, robust shadow picks and the wallet feed's daily budget."""

import math

from tradeslave.autopilot import model as M
from tradeslave.autopilot.live import ModelGate
from tradeslave.scalper.config import ScalpConfig
from tradeslave.scalper.core import Scalper

from test_scalper import Market, T0
from autopilot_fakes import FakeModel
from test_learned_exits import learned_card


def mcfg(**kw):
    base = dict(entry_mode="model", fail_rate=0.0, latency_s=0.8, seed=1, min_age_s=3.0, autopilot=False)
    base.update(kw)
    return ScalpConfig(**base)


def card(version="v1", policy="tp10_sl10_60s"):
    ev = M.Eval(trades=50, tokens=40, mean=0.02, median=0.01, win=0.5, total=1.0, top_token_share=0.1)
    return M.Card(version=version, created=0, policy=policy, threshold=0.01, n_tokens=100, n_samples=1000,
                  exact_share=0.5, data_from=0, data_to=1, validation=ev, holdout=ev, paper_ok=False, proven=False)


def test_gate_scores_entries_only_at_training_snapshot_times(tmp_path):
    M.save_champion(card(), FakeModel(0.05), directory=tmp_path, name="shadow")
    gate = ModelGate(mcfg(), path=tmp_path / "shadow.pkl", shadow=True)
    s = Scalper(mcfg())
    m = Market()
    s.on_create("Z", "Z", T0, m.move(1.0), "dev", "c", 0.5, 10_000_000, False)
    for i in range(1, 9):
        s.on_curve("Z", T0 + i, m.move(1.01))
    tr = s.trackers["Z"]
    assert gate.entry(tr, T0 + 8.5).reason == "not_snapshot_time"       # no fresh curve update now
    assert gate.entry(tr, T0 + 8).ok and gate.entry(tr, T0 + 8).ok     # same instant may be re-asked
    s.on_curve("Z", T0 + 9, m.move(1.01))
    assert gate.entry(tr, T0 + 9).reason == "not_snapshot_time"         # only 1 s after the last score
    s.on_curve("Z", T0 + 10, m.move(1.01))
    assert gate.entry(tr, T0 + 10).ok


def test_lanes_trade_independently_never_hit_the_daily_limit_and_refill(tmp_path):
    for name in ("shadow", "shadow_1"):
        M.save_champion(learned_card(exit_threshold=0.02), FakeModel(0.05), directory=tmp_path, name=name)
    cfg = mcfg(daily_loss_limit_sol=0.01)
    s = Scalper(cfg)
    lanes = [s.add_shadow(ModelGate(cfg, path=tmp_path / f"{n}.pkl", shadow=True)) for n in ("shadow", "shadow_1")]
    assert s.shadow is lanes[0] and len(s.shadows) == 2
    lanes[0].book.day, lanes[0].book.day_pnl_sol = "2026-10-12", -5.0     # a terrible day so far
    lanes[1].book.balance_sol = 0.01                                     # drained wallet
    m = Market()
    s.on_create("Z", "Z", T0, m.move(1.0), "dev", "c", 0.5, 10_000_000, False)
    for i in range(1, 12):
        s.on_curve("Z", T0 + i, m.move(1.01))
    assert all("Z" in lane.book.positions for lane in lanes)            # both still test, each its own wallet
    assert lanes[1].refills == 1 and not s.book.positions
    assert not s.forget("Z")
    lanes[0].book.closed.append({"pnl_sol": 0.1})
    lanes[0].reset()
    assert lanes[0].book.closed == [] and lanes[0].book.day_pnl_sol == 0.0


def _cand(mean, halves, trades, policy="tp10_sl10_60s", scorer="policy", q=0.9, exit=None):
    ev = M.Eval(trades=trades, tokens=trades, mean=mean, half_means=halves, t_stat=1.5, top_token_share=0.1)
    return M.Candidate(ev, policy, q, None, None, scorer, {"q": q}, exit)


def test_shadow_picks_prefer_robust_frequent_candidates_across_families():
    lucky = _cand(0.12, (0.28, -0.03), 38, q=0.99)                       # big mean, one lucky half, few trades
    steady = _cand(0.03, (0.04, 0.02), 90, q=0.9)
    gate = _cand(0.01, (0.0, 0.01), 260, scorer="abs", q=0.0)
    learned = _cand(0.02, (0.03, -0.01), 70, policy="learned_same", scorer="chart60", exit={"scorer": "same"})
    worse = _cand(-0.02, (-0.01, -0.03), 120, q=0.95)
    picks = M.shadow_picks([steady], [lucky, steady, gate, learned, worse])
    assert picks[0] is steady and lucky not in picks                    # robust beats the lucky number
    assert len(picks) == 4 and gate in picks and learned in picks        # every exit family gets a live test
    assert len({id(p) for p in picks}) == 4


def test_wallet_feed_unpauses_on_a_new_utc_day_and_paces_the_budget(monkeypatch, tmp_path):
    from tradeslave.autopilot import live
    from tradeslave.scalper.engine import LiveEngine
    monkeypatch.setattr(live, "CHAMPION_PKL", tmp_path / "none.pkl")
    eng = LiveEngine(mcfg(record_ticks=False, shadow=False, wallet_feed_max_mb_per_day=100.0))
    day1_noon = 1_792_000_000 - (1_792_000_000 % 86400) + 43200          # 12:00 UTC
    assert eng._wallet_feed_on(day1_noon)
    eng.wlog_mb_today = 52.0                                             # 52% spent by noon: within 50% + 5% slack
    assert eng._wallet_feed_on(day1_noon)
    eng.wlog_mb_today = 60.0                                             # ahead of the clock: skip this launch
    assert not eng._wallet_feed_on(day1_noon) and eng.stats["wallet_paced"] == 1
    eng.wallet_feed_paused = True                                        # daily cap hit
    assert not eng._wallet_feed_on(day1_noon + 3600)
    assert eng._wallet_feed_on(day1_noon + 43200 + 60)                   # next UTC day: budget starts over
    assert not eng.wallet_feed_paused and eng.wlog_mb_today == 0.0
