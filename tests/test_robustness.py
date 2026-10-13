"""Crash-proofing: Windows file locks, graduated curves, bad messages."""

import asyncio
import json
import os

import pytest

from tradeslave import state
from tradeslave.scalper.config import ScalpConfig
from tradeslave.scalper.core import Scalper
from tradeslave.scalper.curve import GRADUATED, CurveState, decode_bonding_curve, encode_bonding_curve
from tradeslave.scalper.engine import LiveEngine
from tradeslave.scalper.recorder import TickRecorder, read_ticks
from tradeslave.scalper.report import collect
from tradeslave.scalper.strategy import entry_signal

from test_scalper import Market, T0
from test_scalper_engine import FakeWS, create_msg
from test_scalper_zone import RETURN, SPIKE, new_token, step, zcfg


def test_save_json_retries_while_a_reader_holds_the_file(state_dir, monkeypatch):
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise PermissionError(5, "Access is denied")   # what Windows raises
        return real_replace(src, dst)

    monkeypatch.setattr(state.os, "replace", flaky_replace)
    state.save_json("w.json", {"ok": 1})
    assert state.load_json("w.json", None) == {"ok": 1}
    assert calls["n"] == 4
    assert [p.name for p in state_dir.iterdir()] == ["w.json"]   # no temp file left behind


def test_graduated_curve_is_recognised_not_a_decode_failure():
    data = encode_bonding_curve(CurveState(0, 0, 0, 0, 0, True))
    assert decode_bonding_curve(data) is GRADUATED
    assert decode_bonding_curve(encode_bonding_curve(CurveState(0, 0, 0, 0, 0, False))) is None


def test_engine_marks_graduated_tokens_complete(state_dir):
    async def scenario():
        engine = LiveEngine(zcfg(record_ticks=False, watch_creator=False))
        engine.rpc_ws = FakeWS()
        engine.rpc_ready.set()
        engine._on_pumpportal(create_msg("G1", Market()))
        await asyncio.sleep(0)
        req = engine.rpc_ws.sent[0]
        engine._on_rpc(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": 5}))
        engine._on_rpc(json.dumps({"jsonrpc": "2.0", "method": "accountNotification", "params": {
            "subscription": 5, "result": {"context": {"slot": 1}, "value": {
                "data": [encode_bonding_curve(CurveState(0, 0, 0, 0, 0, True)), "base64"]}}}}))
        return engine
    engine = asyncio.run(scenario())
    assert engine.stats["graduated"] == 1 and engine.stats["decode_fail"] == 0
    assert engine.scalper.trackers["G1"].state.complete


def test_a_bad_message_does_not_stop_the_engine(state_dir):
    async def scenario():
        engine = LiveEngine(zcfg(record_ticks=False))

        def boom(raw):
            raise KeyError("unexpected field")
        engine._guard(boom, "{}")
        engine._guard(engine._on_rpc, "not json at all")
        engine._guard(engine._on_pumpportal, json.dumps({"txType": "create", "mint": 1}))
        return engine
    engine = asyncio.run(scenario())
    assert engine.stats["handler_errors"] == 1


def test_heavy_selling_guard():
    scalper = Scalper(zcfg(max_recent_net_sell_sol=1.0, entry_confirm="none"))
    m = new_token(scalper)
    step(scalper, "Z1", m, SPIKE + RETURN)
    assert entry_signal(scalper.trackers["Z1"], T0 + 15.1, scalper.cfg).reason == "heavy_selling"


def test_touch_report_uses_only_information_known_at_the_touch(tmp_path):
    rec = TickRecorder(tmp_path)
    m = Market()
    s = m.move(1.02)
    rec.create(T0, "R1", "R1", s, "dev", "c", 0.6, (1_073_000_000 * 10**6 - s.virtual_token_reserves) / 10**6, False)
    for dt, f in SPIKE + RETURN + [(17, 1.003)] + [(18 + i, 1.03) for i in range(4)]:
        rec.curve(T0 + dt, "R1", m.move(f))
    rec.creator_sell(T0 + 30, "R1")                     # a LATER dev dump must not change the verdict at touch
    rec.curve(T0 + 30.5, "R1", m.sell_tokens(10_000_000))
    rec.close()
    [row] = collect(read_ticks(list(tmp_path.glob("*.jsonl.gz"))), zcfg(), up=0.09, down=0.10, horizon=60)
    assert row["verdict"] == "clean"
    assert row["outcome"] == "bounce"


def test_subscriptions_are_paced(state_dir):
    import time as _time

    async def scenario():
        engine = LiveEngine(zcfg(record_ticks=False, watch_creator=False, rpc_subs_per_s=20.0))
        engine.rpc_ws = FakeWS()
        engine.rpc_ready.set()
        for i in range(6):
            engine.watch[f"M{i}"] = f"curve{i}"
        start = _time.monotonic()
        await asyncio.gather(*(engine._subscribe_curve(f"M{i}", f"curve{i}") for i in range(6)))
        return engine, _time.monotonic() - start
    engine, elapsed = asyncio.run(scenario())
    assert len(engine.rpc_ws.sent) == 6
    assert elapsed >= 5 / 20.0 * 0.9          # 6 requests at 20/s take >= 0.25 s


def test_closed_curve_account_stops_watching(state_dir):
    async def scenario():
        engine = LiveEngine(zcfg(record_ticks=False, watch_creator=False))
        engine.rpc_ws = FakeWS()
        engine.rpc_ready.set()
        engine._on_pumpportal(create_msg("C1", Market()))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if engine.rpc_ws.sent:
                break
        req = engine.rpc_ws.sent[0]
        engine._on_rpc(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": 9}))
        engine._on_rpc(json.dumps({"jsonrpc": "2.0", "method": "accountNotification", "params": {
            "subscription": 9, "result": {"context": {"slot": 1}, "value": {"data": ["", "base64"]}}}}))
        await engine._clock_tick(0.0, 0.0)
        return engine
    engine = asyncio.run(scenario())
    assert engine.stats["closed_accounts"] == 1 and engine.stats["decode_fail"] == 0
    assert "C1" not in engine.watch
