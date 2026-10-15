"""Autopilot: features without lookahead, samples from recordings, model gate,
model-mode trading with the trained exit policy, train/promote cycle."""

import math

import pytest

from tradeslave.autopilot import features as F
from tradeslave.autopilot import model as M
from tradeslave.autopilot import samples as S
from tradeslave.autopilot.live import ModelGate
from tradeslave.scalper.config import ScalpConfig
from tradeslave.scalper.core import Scalper
from tradeslave.scalper.recorder import TickRecorder

from test_scalper import Market, T0
from autopilot_fakes import FakeModel

try:
    import sklearn  # noqa: F401
    HAVE_SKLEARN = True
except ImportError:
    HAVE_SKLEARN = False


def mcfg(**kw):
    base = dict(entry_mode="model", fail_rate=0.0, latency_s=0.8, seed=1, min_age_s=3.0, autopilot=False)
    base.update(kw)
    return ScalpConfig(**base)


def feed(scalper, mint, factors, start=T0):
    m = Market()
    scalper.on_create(mint, mint, start, m.move(1.0), "dev", "c", 0.5, 10_000_000, False)
    for dt, f in factors:
        scalper.on_curve(mint, start + dt, m.move(f))
    return m


def test_training_snapshots_match_what_the_live_bot_sees(tmp_path):
    """A snapshot taken while replaying a whole day (future included) equals the
    features of a bot that has only seen the data up to that moment."""
    rec = TickRecorder(tmp_path)
    m = Market()
    rec.create(T0, "X", "X", m.move(1.0), "dev", "c", 0.5, 10_000_000, False)
    path_moves = [(i, 1.01 if i < 20 else 0.97) for i in range(1, 40)]
    states = []
    for dt, f in path_moves:
        st = m.move(f)
        states.append((dt, st))
        rec.curve(T0 + dt, "X", st)
    rec.close()
    snaps = S.snapshot_day(next(tmp_path.glob("*.jsonl.gz")), mcfg())
    mint, t, t0, x, clean = snaps[3]
    live = Scalper(mcfg())
    m2 = Market()
    live.on_create("X", "X", T0, m2.move(1.0), "dev", "c", 0.5, 10_000_000, False)
    for dt, st in states:
        if T0 + dt <= t:
            live.on_curve("X", T0 + dt, st)
    y = F.features(live.trackers["X"], t, live.heat(t))
    assert len(x) == len(F.FEATURES)
    assert all((u == v) or (math.isnan(u) and math.isnan(v)) for u, v in zip(x, y))


def test_tradeable_rules():
    s = Scalper(mcfg())
    feed(s, "Y", [(1, 1.01), (2, 1.01)])
    tr = s.trackers["Y"]
    assert not F.tradeable(tr, T0 + 1, 3, 300) and F.tradeable(tr, T0 + 4, 3, 300)
    tr.mayhem = True
    assert not F.tradeable(tr, T0 + 4, 3, 300)


def test_snapshot_and_labels_from_a_recorded_day(tmp_path):
    rec = TickRecorder(tmp_path)
    m = Market()
    s0 = m.move(1.0)
    rec.create(T0, "UP", "UP", s0, "dev", "c", 0.5, 10_000_000, False)
    for i in range(1, 80):
        rec.curve(T0 + i, "UP", m.move(1.01))
    rec.close()
    path = next(tmp_path.glob("*.jsonl.gz"))
    cfg = mcfg()
    out = S.build_day(path, cfg, backfill={})
    assert out and all(len(s.x) == len(F.FEATURES) for s in out)
    assert all(set(s.y) == set(S.POLICIES) for s in out)
    assert out[0].y["hold30"] > 0                                       # steadily rising token
    gaps = [b.t - a.t for a, b in zip(out, out[1:])]
    assert min(gaps) >= 2.0                                             # sampled every 2 s at most


def make_card(policy="tp10_sl10_60s", paper_ok=True, threshold=0.01):
    ev = M.Eval(trades=50, tokens=40, mean=0.02, median=0.01, win=0.5, total=1.0, top_token_share=0.1)
    return M.Card(version="v1", created=0, policy=policy, threshold=threshold, n_tokens=100, n_samples=1000,
                  exact_share=0.5, data_from=0, data_to=1, validation=ev, holdout=ev, paper_ok=paper_ok, proven=False)


def test_gate_loads_champion_and_decides(tmp_path):
    cfg = mcfg()
    gate = ModelGate(cfg, path=tmp_path / "champion.pkl")
    assert gate.card is None and "learning" in gate.status
    M.save_champion(make_card(), FakeModel(0.05), directory=tmp_path)
    assert gate.reload() and gate.card.version == "v1" and not gate.reload()
    s = Scalper(cfg)
    feed(s, "Z", [(i, 1.01) for i in range(1, 10)])
    sig = gate.entry(s.trackers["Z"], T0 + 9)
    assert sig.ok and sig.details["policy"] == "tp10_sl10_60s" and sig.details["policy_rules"]["tp"] == 0.10
    gate.model = FakeModel(0.0)
    assert gate.entry(s.trackers["Z"], T0 + 9).reason == "model_says_no"
    M.save_champion(make_card(paper_ok=False), FakeModel(0.05), directory=tmp_path)
    import os, time
    os.utime(tmp_path / "champion.pkl", (time.time() + 5, time.time() + 5))
    gate.reload()
    assert gate.entry(s.trackers["Z"], T0 + 9).reason == "model_not_proven"


def test_model_mode_trades_with_the_trained_exit_policy():
    cfg = mcfg()
    s = Scalper(cfg)
    s.model = type("G", (), {})()
    from tradeslave.scalper.strategy import Signal
    rules = {"tp": 0.03, "sl": 0.5, "hold": 600.0}
    s.model.entry = lambda tr, now: Signal(True, "model", {"policy_rules": rules, "policy": "x"})
    feed(s, "W", [(i, 1.02) for i in range(1, 15)])
    closed = s.book.closed
    assert closed and closed[0]["exit_reason"] == "take_profit"         # policy tp=3%, not the config's 5%
    no_model = Scalper(cfg)
    feed(no_model, "V", [(i, 1.02) for i in range(1, 15)])
    assert not no_model.book.closed and no_model.entry_block_reasons.get("no_model_yet")


def synthetic_samples(n_tokens=900, per=6, signal=True):
    import random
    rnd = random.Random(3)
    out = []
    for k in range(n_tokens):
        t0 = 1_000_000.0 + k * 10
        for j in range(per):
            x = [rnd.random() for _ in F.FEATURES]
            good = x[0] > 0.8 if signal else rnd.random() > 0.8
            y = {p: (0.08 if good else -0.03) + rnd.uniform(-0.01, 0.01) for p in S.POLICIES}
            out.append(S.Sample(f"m{k}", t0 + j * 400, t0, x, y, True))
    return out


@pytest.mark.skipif(not HAVE_SKLEARN, reason="scikit-learn not installed")
def test_train_cycle_finds_a_real_signal_and_promotes(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "AUTO_DIR", tmp_path)
    monkeypatch.setattr(M, "CHAMPION_PKL", tmp_path / "champion.pkl")
    card, model, info = M.train_cycle(synthetic_samples(), log=lambda *a: None)
    assert card is not None and card.paper_ok and card.holdout.mean > 0.03
    assert M.compete(card, model, info, log=lambda *a: None)
    M.save_champion(card, model, directory=tmp_path)
    row = M.record_history(card, info, True, directory=tmp_path)
    assert row["promoted"] and (tmp_path / "history.jsonl").exists()


@pytest.mark.skipif(not HAVE_SKLEARN, reason="scikit-learn not installed")
def test_train_cycle_refuses_noise():
    card, model, info = M.train_cycle(synthetic_samples(signal=False), log=lambda *a: None)
    assert card is None or not card.paper_ok or card.holdout.mean < 0.03


def test_live_engine_in_model_mode_has_a_gate_and_reports_status(monkeypatch, tmp_path):
    from tradeslave.scalper.engine import LiveEngine
    from tradeslave.autopilot import live
    monkeypatch.setattr(live, "CHAMPION_PKL", tmp_path / "none.pkl")
    eng = LiveEngine(mcfg(record_ticks=False))
    assert eng.scalper.model is not None and "learning" in eng.scalper.model.status


def test_market_heat_and_feed_gaps(tmp_path):
    s = Scalper(mcfg())
    for i in range(5):
        s.on_create(f"T{i}", "T", T0 + i * 100, None)
    s.on_migration(T0 + 10)
    s.on_migration(T0 - 4000)                                  # older than an hour: not counted
    assert s.heat(T0 + 450) == (5.0, 1.0)
    assert s.heat(T0 + 900) == (2.0, 1.0)                      # launches before T0+300 left the 10-min window
    # tokens alive while the feed was down are dropped from training data
    rec = TickRecorder(tmp_path)
    m1, m2 = Market(), Market()
    rec.create(T0, "A", "A", m1.move(1.0), "dev", "c", 0.5, 1, False)
    rec.create(T0, "B", "B", m2.move(1.0), "dev", "c", 0.5, 1, False)
    for i in range(1, 30):
        rec.curve(T0 + i, "A", m1.move(1.01))
    rec.feed(T0 + 31, False)
    rec.feed(T0 + 90, True)
    rec.create(T0 + 100, "C", "C", Market().move(1.0), "dev", "c", 0.5, 1, False)
    m3 = Market()
    for i in range(1, 30):
        rec.curve(T0 + 100 + i, "C", m3.move(1.01))
    rec.close()
    snaps = S.snapshot_day(next(tmp_path.glob("*.jsonl.gz")), mcfg())
    assert {x[0] for x in snaps} == {"C"}


def test_engine_prefers_helius_websocket(monkeypatch):
    from tradeslave.scalper.engine import LiveEngine
    monkeypatch.setenv("RESEARCH_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=abc")
    eng = LiveEngine(mcfg(record_ticks=False, entry_mode="second_dip"))
    assert eng.rpc_endpoints() == ["wss://mainnet.helius-rpc.com/?api-key=abc", "wss://api.mainnet-beta.solana.com"]
    eng2 = LiveEngine(mcfg(record_ticks=False, entry_mode="second_dip", rpc_ws="wss://x.example"))
    assert eng2.rpc_endpoints()[0] == "wss://x.example"


def test_exact_chain_charts_replace_gappy_recordings(tmp_path):
    from tradeslave.research.dataset import slot_ticks
    rec = TickRecorder(tmp_path)
    m = Market()
    s0 = m.move(1.0)
    rec.create(T0, "E", "E", s0, "dev", "c", 0.5, 1, False)
    rec.curve(T0 + 1, "E", m.move(1.01))          # the feed caught only one update...
    rec.feed(T0 + 2, False)                        # ...then went down
    rec.feed(T0 + 200, True)
    rec.close()
    # chain truth: a buy every slot for 40 slots
    trades = [{"slot": 1000, "dv": 1_000_000_000, "dc": -34_612_903_225_806, "create": 1}]
    trades += [{"slot": 1000 + i, "dv": 100_000_000, "dc": -3_000_000_000_000} for i in range(1, 40)]
    ticks = slot_ticks(trades, T0)
    assert len(ticks) == 40 and abs(ticks[10][0] - (T0 + 4.0)) < 1e-9
    path = next(tmp_path.glob("*.jsonl.gz"))
    no_exact = S.snapshot_day(path, mcfg())
    assert no_exact == []                                          # the gappy token is dropped
    snaps = S.snapshot_day(path, mcfg(), exact={"E": {"trades": trades, "t0": T0}})
    assert snaps and all(x[4] for x in snaps)                      # exact = clean
    out = S.build_day(path, mcfg(), backfill={"E": {"trades": trades, "t0": T0}})
    assert out and all(s.exact and s.clean and s.f for s in out)
