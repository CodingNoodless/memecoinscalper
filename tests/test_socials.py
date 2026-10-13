"""Narrative / socials capture and the backfill parser."""

import asyncio
import base64
import json
import struct

import pytest

from tradeslave.scalper.backfill_meta import b58decode, find_uri_in_transaction, parse_name_symbol_uri
from tradeslave.scalper.core import Scalper
from tradeslave.scalper.engine import LiveEngine
from tradeslave.scalper import engine as engine_module
from tradeslave.scalper.recorder import TickRecorder, read_ticks
from tradeslave.scalper.report import collect
from tradeslave.scalper.socials import candidate_urls, parse_socials, socials_ok, twitter_kind
from tradeslave.scalper.strategy import entry_signal, safety_block

from test_scalper import Market, T0
from test_scalper_engine import FakeWS, create_msg
from test_scalper_zone import CLIMB, RETURN, SPIKE, STALL_BUY, new_token, step, zcfg

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


@pytest.mark.parametrize("url, kind", [
    ("https://x.com/nicoscalls/status/2109846639661781383?s=20", "tweet"),
    ("https://twitter.com/usoilreserves", "profile"),
    ("x.com/usoilreserves", "profile"),
    ("https://x.com/i/communities/1879", "community"),
    ("https://x.com/search?q=pump", "other"),
    ("https://t.me/somegroup", "other"),
])
def test_twitter_kinds(url, kind):
    assert twitter_kind(parse_socials({"twitter": url})["twitter"]) == kind


def test_parse_real_metadata_shapes():
    nothing = parse_socials({"name": "Mentalist", "symbol": "AURA", "description": "",
                             "coin_community": "https://coincommunities.org/communities/x"})
    assert nothing["fetched"] and not nothing["has_any"] and nothing["twitter_kind"] is None
    usor = parse_socials({"description": "United States Oil Reserve", "twitter": "https://x.com/usoilreserves",
                          "website": "https://usor.com"})
    assert usor["twitter_kind"] == "profile" and usor["website"] and usor["desc_len"] == 25
    assert parse_socials(None)["fetched"] is False


def test_gateway_fallbacks():
    urls = candidate_urls("https://ipfs.io/ipfs/bafkreigt2t5uetfx3")
    assert urls[0] == "https://pump.mypinata.cloud/ipfs/bafkreigt2t5uetfx3"
    assert "https://ipfs.io/ipfs/bafkreigt2t5uetfx3" in urls
    assert candidate_urls("https://metadata.j7tracker.io/m/abc") == ["https://metadata.j7tracker.io/m/abc"]
    assert candidate_urls("") == []


def test_socials_requirements():
    tweet = parse_socials({"twitter": "https://x.com/a/status/1"})
    tg = parse_socials({"telegram": "https://t.me/x"})
    none = parse_socials({"name": "x"})
    assert socials_ok(none, "off")
    assert socials_ok(tg, "any") and not socials_ok(none, "any")
    assert socials_ok(tweet, "twitter") and not socials_ok(tg, "twitter")
    assert not socials_ok(tweet, "twitter_profile")


def test_require_twitter_blocks_until_metadata_then_allows():
    scalper = Scalper(zcfg(require_socials="twitter"))
    m = new_token(scalper)
    step(scalper, "Z1", m, SPIKE + RETURN)
    tr = scalper.trackers["Z1"]
    assert safety_block(tr, T0 + 15.5, scalper.cfg) == "metadata_pending"
    scalper.on_metadata("Z1", T0 + 15.6, parse_socials({"twitter": "https://x.com/proj"}))
    assert safety_block(tr, T0 + 15.7, scalper.cfg) is None
    step(scalper, "Z1", m, STALL_BUY + CLIMB)
    assert scalper.book.closed and scalper.book.closed[0]["exit_reason"] == "take_profit"


def test_no_socials_is_blocked_when_required():
    scalper = Scalper(zcfg(require_socials="any"))
    new_token(scalper)
    scalper.on_metadata("Z1", T0 + 1, parse_socials({"name": "x"}))
    assert safety_block(scalper.trackers["Z1"], T0 + 5, scalper.cfg) == "no_socials"


def test_copycat_twitter_links_are_counted():
    scalper = Scalper(zcfg(max_twitter_reuse=1))
    for i, mint in enumerate(["A", "B", "C"]):
        new_token(scalper, mint, start=T0 + i)
    for i, mint in enumerate(["A", "B", "C"]):
        scalper.trackers[mint].creator_launches = 1   # isolate the copycat rule from the serial-launcher rule
        scalper.on_metadata(mint, T0 + i, parse_socials({"twitter": "https://x.com/viral/status/9?s=20"}))
    assert [scalper.trackers[m].twitter_reuse for m in "ABC"] == [0, 1, 2]
    assert safety_block(scalper.trackers["A"], T0 + 10, scalper.cfg) != "copycat_twitter"
    assert safety_block(scalper.trackers["B"], T0 + 10, scalper.cfg) == "copycat_twitter"


def test_engine_loads_metadata_in_background(state_dir, monkeypatch):
    monkeypatch.setattr(engine_module, "fetch_metadata",
                        lambda uri, timeout_s=4.0: {"twitter": "https://x.com/proj"} if uri else None)

    async def scenario():
        engine = LiveEngine(zcfg(record_ticks=False, watch_creator=False))
        msg = json.loads(create_msg("S1", Market()))
        msg["uri"] = "https://ipfs.io/ipfs/bafkreiexample"
        engine._on_pumpportal(json.dumps(msg))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if engine.scalper.trackers["S1"].socials is not None:
                break
        return engine
    engine = asyncio.run(scenario())
    tr = engine.scalper.trackers["S1"]
    assert tr.socials["twitter_kind"] == "profile"
    assert engine.stats["meta_ok"] == 1 and engine.stats["with_twitter"] == 1


def _borsh(*strings):
    return b"".join(struct.pack("<I", len(s.encode())) + s.encode() for s in strings)


def test_backfill_reads_uri_from_create_event_log():
    event = bytes(8) + _borsh("Wolf", "WOLF", "https://ipfs.io/ipfs/bafkreiwolf") + bytes(96)
    tx = {"meta": {"logMessages": ["Program log: Instruction: Create",
                                   "Program data: " + base64.b64encode(event).decode()]},
          "transaction": {"message": {"instructions": []}}}
    assert find_uri_in_transaction(tx) == ("Wolf", "WOLF", "https://ipfs.io/ipfs/bafkreiwolf")


def test_backfill_reads_uri_from_instruction_data():
    ix = bytes(8) + _borsh("United States Oil Reserve", "USOR", "https://ipfs.io/ipfs/bafkreiusor") + bytes(32)
    tx = {"meta": {"logMessages": [], "innerInstructions": []},
          "transaction": {"message": {"instructions": [{"data": b58encode(ix)}]}}}
    assert find_uri_in_transaction(tx)[1] == "USOR"
    assert b58decode(b58encode(b"\0\0hello")) == b"\0\0hello"


def test_garbage_is_not_mistaken_for_metadata():
    assert parse_name_symbol_uri(bytes(64)) is None
    assert parse_name_symbol_uri(bytes(8) + _borsh("a", "b", "not a url")) is None


def test_report_splits_by_socials_from_recorded_metadata(tmp_path):
    rec = TickRecorder(tmp_path)
    for mint, socials in (("X1", {"twitter": "https://x.com/p"}), ("N1", {"name": "n"})):
        m = Market()
        s = m.move(1.02)
        rec.create(T0, mint, mint, s, "dev" + mint, "c", 0.6,
                   (1_073_000_000 * 10**6 - s.virtual_token_reserves) / 10**6, False)
        rec.metadata(T0 + 1, mint, parse_socials(socials))
        for dt, f in SPIKE + RETURN + STALL_BUY + CLIMB:
            rec.curve(T0 + dt, mint, m.move(f))
    rec.close()
    rows = collect(read_ticks(list(tmp_path.glob("*.jsonl.gz"))), zcfg(), 0.09, 0.10, 60)
    assert {r["mint"]: r["socials"] for r in rows} == {"X1": "profile", "N1": "none"}
