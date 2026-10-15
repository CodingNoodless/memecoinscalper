"""Rohit's method (initial_zone) and the rug filters, on synthetic launches."""

import asyncio
import json

import pytest

from tradeslave.scalper.config import ScalpConfig
from tradeslave.scalper.core import Scalper
from tradeslave.scalper.engine import LiveEngine
from tradeslave.scalper.recorder import TickRecorder, read_ticks
from tradeslave.scalper.backtest import replay
from tradeslave.scalper.strategy import entry_signal, safety_block

from test_scalper import Market, T0
from test_scalper_engine import FakeWS, create_msg, notification

SUPPLY = 1_000_000_000


def zcfg(**overrides):
    """These tests cover the initial_zone method (the default is now second_dip)."""
    base = dict(fail_rate=0.0, latency_s=0.8, seed=1, entry_mode="initial_zone", wallet_feed=False)
    base.update(overrides)
    return ScalpConfig(**base)


def new_token(scalper, mint="Z1", dev_factor=1.02, start=T0):
    """Create a token after a small dev buy (price x dev_factor) and return its market."""
    market = Market()
    state = market.move(dev_factor)
    dev_tokens = (1_073_000_000 * 10**6 - state.virtual_token_reserves) / 10**6
    scalper.on_create(mint, mint, start, state, creator="dev", dev_tokens=dev_tokens)
    return market


def step(scalper, mint, market, path, start=T0):
    for dt, factor in path:
        t = start + dt
        scalper.on_clock(t)
        scalper.on_curve(mint, t, market.move(factor))


# opening = price after the dev buy. Snipers push +60% after the 3 s sniper
# window, sellers bring it back to about +2%, a buyer steps in (stall ends),
# then the climb.
SPIKE = [(4, 1.2), (5, 1.15), (6, 1.16)]                     # +60%
RETURN = [(9, 0.85), (11, 0.85), (13, 0.88), (15, 0.975)]    # back to about the opening price
STALL_BUY = [(17, 1.003)]
CLIMB = [(18 + i, 1.02) for i in range(8)]


def test_rise_return_buy_and_sell_the_climb():
    scalper = Scalper(zcfg())
    m = new_token(scalper)
    step(scalper, "Z1", m, SPIKE + RETURN + STALL_BUY + CLIMB)
    [trade] = scalper.book.closed
    assert trade["kind"] == "initial_zone"
    assert trade["exit_reason"] == "take_profit"
    assert trade["pnl_pct"] >= 0.05
    assert -0.12 <= trade["entry_details"]["vs_opening"] <= 0.10


def test_waits_while_sellers_are_still_hitting_it():
    scalper = Scalper(zcfg())
    m = new_token(scalper)
    step(scalper, "Z1", m, SPIKE + RETURN)
    tr = scalper.trackers["Z1"]
    assert entry_signal(tr, T0 + 15.1, scalper.cfg).reason == "still_selling"
    assert not scalper.book.positions and not scalper.book.orders


def test_buy_on_touch_mode_enters_without_waiting():
    scalper = Scalper(zcfg(entry_confirm="none"))
    m = new_token(scalper)
    step(scalper, "Z1", m, SPIKE + RETURN)
    assert scalper.book.positions or scalper.book.orders


def test_no_entry_without_a_launch_spike():
    scalper = Scalper(zcfg())
    m = new_token(scalper)
    step(scalper, "Z1", m, [(4, 1.05), (6, 0.99), (8, 1.003), (9, 1.003)])
    assert entry_signal(scalper.trackers["Z1"], T0 + 9, scalper.cfg).reason == "no_launch_spike_yet"


def test_no_entry_while_still_above_the_zone():
    scalper = Scalper(zcfg())
    m = new_token(scalper)
    step(scalper, "Z1", m, SPIKE + [(9, 0.9), (10, 1.003)])
    assert entry_signal(scalper.trackers["Z1"], T0 + 10, scalper.cfg).reason == "above_buy_zone"


def test_breaking_the_floor_blocks_the_token_for_good():
    scalper = Scalper(zcfg())
    m = new_token(scalper)
    crash = [(9, 0.7), (11, 0.6), (13, 0.75)]                 # well under opening -25%
    recovery = [(16, 1.25), (17, 1.1), (18, 1.003)]          # back into the zone
    step(scalper, "Z1", m, SPIKE + crash + recovery)
    tr = scalper.trackers["Z1"]
    assert tr.floor_broken
    assert safety_block(tr, T0 + 18, scalper.cfg) == "floor_broken"
    assert not scalper.book.closed and not scalper.book.positions


def test_snipers_holding_too_much_blocks_entries():
    scalper = Scalper(zcfg())
    m = new_token(scalper)
    early_pump = [(1, 1.4), (2, 1.4)]                        # heavy buying inside the 3 s window
    step(scalper, "Z1", m, early_pump + [(4, 1.0001)])
    tr = scalper.trackers["Z1"]
    assert tr.early_supply_pct(3.0) > 0.30
    assert safety_block(tr, T0 + 4, scalper.cfg) == "snipers_hold_too_much"


def test_sniper_window_must_close_before_trading():
    scalper = Scalper(zcfg(min_age_s=0.5))
    m = new_token(scalper)
    step(scalper, "Z1", m, [(1, 1.01)])
    assert safety_block(scalper.trackers["Z1"], T0 + 1, scalper.cfg) == "sniper_window_open"


def test_big_dev_bag_is_skipped():
    scalper = Scalper(zcfg())
    m = new_token(scalper, dev_factor=1.4)                   # dev bought ~15% of supply
    tr = scalper.trackers["Z1"]
    assert tr.dev_supply_pct > 0.10
    step(scalper, "Z1", m, [(4, 1.0001)])
    assert safety_block(tr, T0 + 4, scalper.cfg) == "dev_holds_too_much"


def test_dev_sell_exits_the_position_and_blocks_the_token():
    scalper = Scalper(zcfg())
    m = new_token(scalper)
    step(scalper, "Z1", m, SPIKE + RETURN + STALL_BUY)
    scalper.on_clock(T0 + 18)                                 # buy fills
    assert "Z1" in scalper.book.positions
    scalper.on_dev_sell("Z1", T0 + 18.5)
    scalper.on_clock(T0 + 19.5)                               # sell fills
    [trade] = scalper.book.closed
    assert trade["exit_reason"] == "dev_sold"
    assert safety_block(scalper.trackers["Z1"], T0 + 20, scalper.cfg) == "dev_sold"


def test_dev_sized_sell_on_the_curve_counts_as_dev_selling():
    scalper = Scalper(zcfg())
    m = new_token(scalper, dev_factor=1.06)                  # dev bag ~3% of supply
    tr = scalper.trackers["Z1"]
    step(scalper, "Z1", m, SPIKE)
    scalper.on_curve("Z1", T0 + 8, m.sell_tokens(tr.dev_tokens))   # one sell of exactly the dev bag
    assert tr.dev_sold and tr.dev_sold_reason == "dev_sized_sell"


def test_repeat_entry_when_price_returns_to_the_zone_again():
    scalper = Scalper(zcfg())
    m = new_token(scalper)
    second = [(30, 1.15), (32, 0.85), (34, 0.88), (37, 1.003)]  # up, back to the opening price, sellers pause
    step(scalper, "Z1", m, SPIKE + RETURN + STALL_BUY + CLIMB + second + [(38 + i, 1.02) for i in range(8)])
    assert [c["kind"] for c in scalper.book.closed] == ["initial_zone", "initial_zone"]


def test_creator_sell_counts_only_when_this_curve_confirms_it():
    scalper = Scalper(zcfg())
    m = new_token(scalper, dev_factor=1.06)                  # dev bag ~3% of supply
    tr = scalper.trackers["Z1"]
    step(scalper, "Z1", m, SPIKE)
    scalper.on_creator_sell("Z1", T0 + 7)                     # wallet sold something...
    step(scalper, "Z1", m, [(8, 0.97)])                       # ...but only a small sell here
    step(scalper, "Z1", m, [(12, 1.01)])                      # confirmation window passes
    assert not tr.dev_sold and tr.creator_sell_times == []
    scalper.on_creator_sell("Z1", T0 + 14)
    scalper.on_curve("Z1", T0 + 14.5, m.sell_tokens(tr.dev_tokens * 0.6))   # dev-sized sell on THIS curve
    assert tr.dev_sold and tr.dev_sold_reason == "creator_wallet_sell_confirmed"


def test_serial_launcher_is_blocked_including_earlier_tokens():
    scalper = Scalper(zcfg())
    new_token(scalper, "A", start=T0)
    assert safety_block(scalper.trackers["A"], T0 + 4, scalper.cfg) != "serial_launcher"
    new_token(scalper, "B", start=T0 + 60)
    new_token(scalper, "C", start=T0 + 120)                   # third launch by "dev"
    for mint in "ABC":
        assert scalper.trackers[mint].creator_launches == 3
        assert safety_block(scalper.trackers[mint], T0 + 130, scalper.cfg) == "serial_launcher"


def test_dead_token_back_at_opening_is_not_bought():
    """Everyone sold out and nothing trades: not a stall, a dead token."""
    scalper = Scalper(zcfg())
    m = new_token(scalper)
    step(scalper, "Z1", m, SPIKE + RETURN + [(40, 1.0005)])   # one tiny buy after 25 s of silence
    sig = entry_signal(scalper.trackers["Z1"], T0 + 40, scalper.cfg)
    assert sig.reason == "market_inactive"
    assert not scalper.book.positions and not scalper.book.orders


def test_creator_sell_is_recorded_and_replayed(tmp_path):
    rec = TickRecorder(tmp_path)
    m = Market()
    state = m.move(1.06)
    dev_tokens = (1_073_000_000 * 10**6 - state.virtual_token_reserves) / 10**6
    rec.create(T0, "Z1", "Z1", state, "dev", "curve", 1.8, dev_tokens, False)
    for dt, f in SPIKE + RETURN:
        rec.curve(T0 + dt, "Z1", m.move(f))
    rec.creator_sell(T0 + 16, "Z1")
    rec.curve(T0 + 16.5, "Z1", m.sell_tokens(dev_tokens))
    for dt, f in STALL_BUY + CLIMB:
        rec.curve(T0 + dt + 2, "Z1", m.move(f))
    rec.close()
    scalper = replay(read_ticks(list(tmp_path.glob("*.jsonl.gz"))), zcfg())
    tr = scalper.trackers["Z1"]
    assert tr.dev_sold and tr.dev_tokens == pytest.approx(dev_tokens)
    assert scalper.book.closed == []                          # never traded after the dev sold


def test_engine_flags_creator_sell_from_logs(state_dir):
    async def scenario():
        engine = LiveEngine(zcfg(record_ticks=False))
        engine.rpc_ws = FakeWS()
        engine.rpc_ready.set()
        market = Market()
        market.move(1.06)                                     # dev bought ~3% at creation
        msg = json.loads(create_msg("Z1", market))
        msg["initialBuy"] = (1_073_000_000 * 10**6 - market.vt) / 10**6
        engine._on_pumpportal(json.dumps(msg))
        for _ in range(100):                     # subscriptions are paced: wait for both
            await asyncio.sleep(0.01)
            if len(engine.rpc_ws.sent) >= 2:
                break
        kinds = {r["method"]: r for r in engine.rpc_ws.sent}
        assert kinds["logsSubscribe"]["params"][0] == {"mentions": ["dev"]}
        engine._on_rpc(json.dumps({"jsonrpc": "2.0", "id": kinds["accountSubscribe"]["id"], "result": 11}))
        engine._on_rpc(json.dumps({"jsonrpc": "2.0", "id": kinds["logsSubscribe"]["id"], "result": 22}))
        buy_logs = {"jsonrpc": "2.0", "method": "logsNotification", "params": {"subscription": 22, "result": {
            "value": {"signature": "s", "err": None, "logs": ["Program log: Instruction: Buy"]}}}}
        engine._on_rpc(json.dumps(buy_logs))
        assert not engine.scalper.trackers["Z1"].dev_sold
        sell_logs = json.loads(json.dumps(buy_logs))
        sell_logs["params"]["result"]["value"]["logs"] = ["Program log: Instruction: Sell"]
        engine._on_rpc(json.dumps(sell_logs))
        assert not engine.scalper.trackers["Z1"].dev_sold      # not confirmed on this curve yet
        tr = engine.scalper.trackers["Z1"]
        engine._on_rpc(notification(11, market.sell_tokens(tr.dev_tokens)))
        return engine
    engine = asyncio.run(scenario())
    assert engine.scalper.trackers["Z1"].dev_sold
    assert engine.stats["dev_sells"] == 1 and engine.stats["creator_sell_logs"] == 1
    engine._save_wallet()
    wallet = json.loads((state_dir / "scalp_wallet.json").read_text())
    assert wallet["watchlist"][0]["safety"] == "dev_sold"
