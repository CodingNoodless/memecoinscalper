"""Wallet-level live feed: pump.fun trade events from logs -> tracker wallet book -> features."""

import asyncio
import json
import math

from tradeslave.autopilot import features as F
from tradeslave.autopilot import samples as S
from tradeslave.scalper.config import ScalpConfig
from tradeslave.scalper.core import Scalper
from tradeslave.scalper.engine import LiveEngine
from tradeslave.scalper.recorder import TickRecorder
from tradeslave.scalper.trade_events import encode_trade_event, parse_trade_events

from test_scalper import Market, T0
from test_scalper_engine import FakeWS, create_msg

MINT = "So11111111111111111111111111111111111111112"
USER = "4TrXKzAnNoSLxdtpgtmMn6yRvwmUojwVyAK3MfuYVZ9Y"


def test_parse_real_layout_roundtrip_and_junk():
    line = encode_trade_event(MINT, 493_827_159, 17_376_518_166_910, True, USER, 1791862073,
                              30_493_827_159, 1_055_623_481_833_090)
    ev = parse_trade_events(["Program log: Instruction: Buy", line, "Program data: !!notbase64"])
    assert len(ev) == 1
    e = ev[0]
    assert e["mint"] == MINT and e["user"] == USER and e["buy"] and e["sol"] == 493_827_159
    assert e["tok"] == 17_376_518_166_910 and e["vs"] == 30_493_827_159


def test_wallet_book_and_features():
    s = Scalper(ScalpConfig(entry_mode="second_dip", autopilot=False))
    m = Market()
    s.on_create("T", "T", T0, m.move(1.0), "dev", "c", 0.5, 10_000_000, False)
    tr = s.trackers["T"]
    assert all(math.isnan(v) for v in F.wallet_features(tr, T0 + 1))      # unknown before any event
    for i, w in enumerate(["a", "b", "c", "a", "a", "a"]):
        s.on_trade("T", T0 + 1 + i, w, True, 0.5, 1_000_000_000_000)
    s.on_trade("T", T0 + 10, "dev", False, 0.2, 5_000_000_000_000)
    v = dict(zip(F.WALLET, F.wallet_features(tr, T0 + 10)))
    assert v["w_trades"] == 7 and v["w_buyers"] == 3 and v["w_sellers"] == 1
    assert v["w_top1"] == 4 / 6 and abs(v["w_dev_sold_frac"] - 0.5) < 1e-9 and v["w_bot_share"] == 4 / 7


def test_engine_subscribes_and_routes_trade_events(state_dir):
    async def scenario():
        cfg = ScalpConfig(entry_mode="second_dip", autopilot=False, record_ticks=False, wallet_feed=True,
                          watch_creator=False, fail_rate=0.0)
        engine = LiveEngine(cfg)
        engine.rpc_ws = FakeWS()
        engine.rpc_ready.set()
        market = Market()
        market.move(1.02)
        engine._on_pumpportal(create_msg(MINT, market))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(engine.rpc_ws.sent) >= 2:
                break
        sent = {r["method"]: r for r in engine.rpc_ws.sent}
        assert sent["logsSubscribe"]["params"][0]["mentions"] == [engine.watch[MINT]]
        engine._on_rpc(json.dumps({"jsonrpc": "2.0", "id": sent["logsSubscribe"]["id"], "result": 77}))
        line = encode_trade_event(MINT, 100_000_000, 3_000_000_000_000, True, USER, 1, 31_000_000_000, 10**15)
        note = {"jsonrpc": "2.0", "method": "logsNotification", "params": {"subscription": 77, "result": {
            "value": {"signature": "sig", "err": None, "logs": [line]}}}}
        engine._on_rpc(json.dumps(note))
        failed = json.loads(json.dumps(note)); failed["params"]["result"]["value"]["err"] = {"x": 1}
        engine._on_rpc(json.dumps(failed))
        return engine
    engine = asyncio.run(scenario())
    tr = engine.scalper.trackers[MINT]
    assert tr.wallet_seen and tr.w_trades == 1 and USER in tr.w_buyers and engine.stats["wallet_events"] == 1


def test_training_sees_wallets_like_the_live_feed(tmp_path):
    rec = TickRecorder(tmp_path)
    m = Market()
    rec.create(T0, "E", "E", m.move(1.0), "dev", "c", 0.5, 1, False)
    rec.close()
    trades = [{"slot": 1000, "w": "dev", "dv": 1_000_000_000, "dc": -34_612_903_225_806, "create": 1}]
    trades += [{"slot": 1000 + i, "w": f"w{i % 3}", "dv": 100_000_000, "dc": -3_000_000_000_000} for i in range(1, 40)]
    snaps = S.snapshot_day(next(tmp_path.glob("*.jsonl.gz")),
                           ScalpConfig(entry_mode="model", autopilot=False, min_age_s=3.0),
                           exact={"E": {"trades": trades, "t0": T0}})
    assert snaps
    x = dict(zip(F.FEATURES, snaps[-1][3]))
    assert x["w_buyers"] == 3 and x["w_trades"] < 39       # launch-second trades skipped, like the live feed
