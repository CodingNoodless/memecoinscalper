"""Research tools: dataset loading, simulator, wallet study, narrative scoring, new recordings."""

import asyncio
import json

import pytest

from tradeslave.research import narrative, wallets
from tradeslave.research.dataset import load_tokens
from tradeslave.research.sim import simulate, summary
from tradeslave.scalper.engine import LiveEngine
from tradeslave.scalper.recorder import TickRecorder
from tradeslave.scalper.socials import parse_socials

from test_scalper import Market, T0
from test_scalper_engine import FakeWS, create_msg
from test_scalper_zone import zcfg

U = 10**6


def record_token(rec, mint, factors, start=T0, creator="dev"):
    m = Market()
    s = m.move(1.02)
    rec.create(start, mint, mint[:4], s, creator, "c", 0.6, (1_073_000_000 * U - s.virtual_token_reserves) / U, False,
               uri=f"https://ipfs.io/ipfs/{mint}", name=f"{mint} coin")
    for dt, f in factors:
        rec.curve(start + dt, mint, m.move(f))
    return m


def test_dataset_and_simulator(tmp_path):
    rec = TickRecorder(tmp_path)
    record_token(rec, "UP", [(i, 1.02) for i in range(1, 40)])
    record_token(rec, "DOWN", [(i, 0.98) for i in range(1, 40)])
    rec.close()
    toks = load_tokens(list(tmp_path.glob("*.jsonl.gz")))
    assert toks["UP"].uri.endswith("UP") and toks["UP"].name == "UP coin"
    up = simulate(toks["UP"], T0 + 5, tp=0.10, sl=0.10, hold=60)
    down = simulate(toks["DOWN"], T0 + 5, tp=0.10, sl=0.10, hold=60)
    assert up >= 0.10 and down <= -0.10
    flat_fees = simulate(toks["UP"], T0 + 50, tp=9, sl=0.99, hold=5)   # no ticks after: pure round-trip cost
    assert flat_fees == pytest.approx(-0.0369, abs=0.002)
    assert summary([up, down, None])["n"] == 2


def test_wallet_study_finds_the_wallet_that_buys_winners(tmp_path):
    rec = TickRecorder(tmp_path)
    for i in range(8):
        start = T0 + i * 100
        winner = i % 2 == 0
        mint = f"W{i}" if winner else f"L{i}"
        record_token(rec, mint, [(k, 1.03 if winner else 0.97) for k in range(1, 60)], start=start)
        rec.trade(start + 2, mint, "SMART" if winner else "DUMB", True, 0.5, 1e6, 31, 1.05e9, "s")
        rec.trade(start + 2.5, mint, "RANDO", True, 0.5, 1e6, 31, 1.05e9, "s")
        rec.trade(start + 40, mint, "SMART" if winner else "DUMB", False, 1.0 if winner else 0.2, 1e6, 31, 1.05e9, "s")
    rec.close()
    paths = list(tmp_path.glob("*.jsonl.gz"))
    trades = wallets.load_trades(paths)
    toks = load_tokens(paths)
    profiles = wallets.wallet_profiles(trades, toks, until=T0 + 400)
    assert profiles["SMART"]["pnl_sol"] > 0 and profiles["SMART"]["win_rate"] == 1.0
    assert profiles["DUMB"]["pnl_sol"] < 0
    signals = wallets.follow_signals(trades, {"SMART"}, T0 + 400, T0 + 900, min_sol=0.2)
    assert {m for _, m in signals} == {"W4", "W6"}


def test_narrative_labels_text_and_report(tmp_path, monkeypatch, capsys):
    rec = TickRecorder(tmp_path)
    record_token(rec, "N1", [(i, 1.02) for i in range(1, 60)])
    rec.metadata(T0 + 1, "N1", parse_socials({"name": "Viral Cat", "symbol": "VCAT", "description": "the cat from the tweet",
                                               "twitter": "https://x.com/catacc"}))
    rec.close()
    tok = load_tokens(list(tmp_path.glob("*.jsonl.gz")))["N1"]
    lab = narrative.labels(tok)
    assert lab["trade"] is not None and lab["best10m"] > 0
    text = narrative.narrative_text(tok, {})
    assert text["name"] == "Viral Cat" and text["twitter"] == "profile: @catacc"
    rows = [{"t": T0 + i, "trade": 0.1 if i % 2 else -0.1, "best10m": 0.2, "score": 8 if i % 2 else 2} for i in range(60)]
    narrative.report(rows)
    out = capsys.readouterr().out
    assert "high 7-10" in out and "VERDICT" in out


def test_narrative_ask_parses_ollama_json(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self): return {"response": json.dumps({"narrative_score": 7, "theme": "cat", "low_effort": False})}
    monkeypatch.setattr(narrative.requests, "post", lambda *a, **k: R())
    assert narrative.ask("u", "m", {"name": "x", "symbol": "x", "description": "", "twitter": "none", "website": "none"}, 5)["narrative_score"] == 7


def test_socials_keep_the_narrative_text():
    s = parse_socials({"name": "  Viral   Cat ", "symbol": "VCAT", "description": "x" * 999})
    assert s["name"] == "Viral Cat" and len(s["description"]) == 400


def test_engine_records_wallet_trades_and_respects_the_daily_cap(state_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("PUMPPORTAL_API_KEY", "test-key")
    from tradeslave.scalper import recorder as recorder_module

    async def scenario():
        engine = LiveEngine(zcfg(record_ticks=False, trade_feed=True, trade_feed_max_events_per_day=2))
        engine.recorder = TickRecorder(tmp_path)
        engine.pp_ws = FakeWS()
        engine.rpc_ready.clear()
        engine._on_pumpportal(create_msg("T1", Market()))
        await engine._trade_sub("T1")
        assert engine.pp_ws.sent[-1] == {"method": "subscribeTokenTrade", "keys": ["T1"]}
        trade = {"txType": "buy", "mint": "T1", "traderPublicKey": "WALLET", "solAmount": 0.4, "tokenAmount": 1e6,
                 "vSolInBondingCurve": 31.0, "vTokensInBondingCurve": 1.04e9, "signature": "sig"}
        engine._on_pumpportal(json.dumps(trade))
        engine._on_pumpportal(json.dumps(trade))          # hits the cap of 2
        await asyncio.sleep(0.01)
        engine._on_pumpportal(json.dumps({"txType": "migrate", "mint": "T1", "pool": "pump-amm", "signature": "x"}))
        engine.recorder.close()
        return engine
    engine = asyncio.run(scenario())
    assert engine.trade_feed_paused
    assert {"method": "unsubscribeTokenTrade", "keys": ["T1"]} in engine.pp_ws.sent
    records = [r for r in recorder_module.read_ticks(list(tmp_path.glob("*.jsonl.gz")))]
    assert sum(r["k"] == "trade" for r in records) == 2
    assert [r["w"] for r in records if r["k"] == "trade"] == ["WALLET", "WALLET"]
    assert sum(r["k"] == "migr" for r in records) == 1
