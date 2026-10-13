"""Live engine message handling, offline: fake PumpPortal + RPC messages."""

import asyncio
import json

from tradeslave.scalper.config import ScalpConfig
from tradeslave.scalper.curve import encode_bonding_curve
from tradeslave.scalper.engine import LiveEngine

from test_scalper import LAUNCH_DUMP_BOUNCE, RALLY, Market


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


def create_msg(mint, market, pool="pump"):
    s = market.state()
    return json.dumps({
        "txType": "create", "mint": mint, "symbol": "TEST", "pool": pool,
        "bondingCurveKey": f"curve-{mint}", "traderPublicKey": "dev",
        "vSolInBondingCurve": s.virtual_sol, "vTokensInBondingCurve": s.virtual_token_reserves / 1e6,
    })


def notification(sub_id, state):
    return json.dumps({"jsonrpc": "2.0", "method": "accountNotification", "params": {
        "subscription": sub_id,
        "result": {"context": {"slot": 1}, "value": {"data": [encode_bonding_curve(state), "base64"]}}}})


def test_engine_end_to_end_offline(state_dir, monkeypatch):
    clock = {"t": 1_791_850_000.0}
    monkeypatch.setattr("tradeslave.scalper.engine.time.time", lambda: clock["t"])

    async def scenario():
        engine = LiveEngine(ScalpConfig(record_ticks=False, fail_rate=0.0))
        engine.rpc_ws = FakeWS()
        engine.rpc_ready.set()
        market = Market()
        engine._on_pumpportal(create_msg("MINT1", market))
        engine._on_pumpportal(create_msg("OTHER", Market(), pool="bonk"))  # other launchpad: ignored
        await asyncio.sleep(0)  # let the subscribe task run
        [req] = engine.rpc_ws.sent
        assert req["method"] == "accountSubscribe" and req["params"][0] == "curve-MINT1"
        engine._on_rpc(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": 4242}))
        assert engine.mint_by_sub == {4242: "MINT1"}
        start = clock["t"]
        for dt, factor in LAUNCH_DUMP_BOUNCE + RALLY:
            clock["t"] = start + dt
            engine.scalper.on_clock(clock["t"])
            engine._on_rpc(notification(4242, market.move(factor)))
        return engine

    engine = asyncio.run(scenario())
    assert list(engine.watch) == ["MINT1"]
    [trade] = engine.scalper.book.closed
    assert trade["exit_reason"] == "take_profit"
    lines = (state_dir / "scalp_trades.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["trade_id"] == trade["trade_id"]
    assert engine.stats["decode_fail"] == 0
    engine._save_wallet()
    wallet = json.loads((state_dir / "scalp_wallet.json").read_text())
    assert wallet["balance_sol"] > 1.0


def test_watchlist_cap(state_dir):
    async def scenario():
        engine = LiveEngine(ScalpConfig(record_ticks=False, max_watched=2))
        for i in range(5):
            engine._on_pumpportal(create_msg(f"M{i}", Market()))
        return engine
    engine = asyncio.run(scenario())
    assert len(engine.watch) == 2
    assert engine.stats["skipped_full"] == 3
