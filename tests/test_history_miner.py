"""History miner: order of work, writing charts, skips, merging with the autopilot's backfill."""

import json

from tradeslave.research import backfill_trades as bt
from tradeslave.research import history_miner as hm
from tradeslave.research.dataset import load_backfill
from tradeslave.scalper.curve import CurveState
from tradeslave.scalper.recorder import TickRecorder

from test_backfill_trades import tx, MINT, CURVE

T = 1791900000.0


def record(tmp_path):
    rec = TickRecorder(tmp_path)
    s = CurveState(bt.START_VT, bt.START_VS)
    rec.create(T, "QUIET", "Q", s, "dev", "cq")
    rec.create(T + 10, "BUSY_OLD", "B", s, "dev", "cb1")
    rec.create(T + 20, "BUSY_NEW", "B", s, "dev", "cb2")
    rec.create(T + 30, "MAYHEM", "M", s, "dev", "cm", mayhem=True)
    for i in range(25):
        rec.curve(T + 11 + i, "BUSY_OLD", s)
        rec.curve(T + 21 + i, "BUSY_NEW", s)
    rec.close()


def test_candidates_busiest_and_newest_first_and_no_mayhem(tmp_path):
    record(tmp_path)
    order = [c["m"] for c in hm.candidates(tmp_path, min_age_s=0)]
    assert order == ["BUSY_NEW", "BUSY_OLD", "QUIET"]


def test_mine_writes_charts_skips_and_merges(tmp_path, monkeypatch):
    for mod in (bt, hm):
        monkeypatch.setattr(mod, "TRADE_DIR", tmp_path)
    monkeypatch.setattr(bt, "OUT", tmp_path / "backfill.jsonl")
    monkeypatch.setattr(bt, "HISTORY_OUT", tmp_path / "history.jsonl")
    monkeypatch.setattr(hm, "HISTORY_OUT", tmp_path / "history.jsonl")
    monkeypatch.setattr(hm, "SKIPS", tmp_path / "skips.jsonl")

    def fake(rpc, address, t_from, t_to, max_pages=5):
        if address == "busy":
            return None, {"why": "too_many"}
        if address == "flaky":
            return None, {"why": "rpc_error"}
        return [tx(500_000_000, -15_000_000_000_000)], {"method": "sigs", "sigs": 1}

    monkeypatch.setattr(hm, "address_transactions", fake)
    monkeypatch.setattr(hm.time, "sleep", lambda s: None)
    todo = [{"m": MINT, "bc": CURVE, "t0": T}, {"m": "BIG", "bc": "busy", "t0": T}, {"m": "F", "bc": "flaky", "t0": T}]
    rpc = type("R", (), {"errors": {}})()
    n = hm.mine(rpc, todo, 900.0, 5, log=lambda *a: None)
    assert n == 1
    assert hm.skipped() == {"BIG"}                      # flaky RPC is retried later, not skipped forever
    assert MINT in bt.done_mints()
    # the same token also fetched by the autopilot: counted once
    (tmp_path / "backfill.jsonl").write_text(
        json.dumps({"k": "trade", "m": MINT, "t": T, "w": "x", "b": 1, "dv": 1, "dc": -1, "slot": 1, "i": 0}) + "\n"
        + json.dumps({"k": "done", "v": bt.VERSION, "m": MINT, "t0": T}) + "\n")
    merged = load_backfill()
    assert len(merged[MINT]["trades"]) == 1 and merged[MINT]["trades"][0]["w"] == "x"


def test_endpoints_parse_public_first_rates_and_dedup():
    eps = hm.endpoints(3.0, extra="https://a.example/k1#rps=8, https://b.example/k2,https://a.example/k1,"
                                  + hm.PUBLIC_RPC)
    assert eps == [(hm.PUBLIC_RPC, 3.0), ("https://a.example/k1", 8.0), ("https://b.example/k2", 3.0)]


def test_mine_hands_a_token_to_another_endpoint(tmp_path, monkeypatch):
    for mod in (bt, hm):
        monkeypatch.setattr(mod, "TRADE_DIR", tmp_path)
    monkeypatch.setattr(bt, "HISTORY_OUT", tmp_path / "history.jsonl")
    monkeypatch.setattr(hm, "HISTORY_OUT", tmp_path / "history.jsonl")
    monkeypatch.setattr(hm, "SKIPS", tmp_path / "skips.jsonl")
    monkeypatch.setattr(hm.time, "sleep", lambda s: None)
    seen = []

    def fake(rpc, address, t_from, t_to, max_pages=5):
        seen.append(rpc.url)
        if rpc.url == "pruned":                 # this endpoint has no history for the token
            return [], {"method": "sigs", "sigs": 1, "tx_missing": 1}
        return [tx(500_000_000, -15_000_000_000_000)], {"method": "sigs", "sigs": 1}

    monkeypatch.setattr(hm, "address_transactions", fake)
    rpcs = [type("R", (), {"errors": {}, "url": u})() for u in ("pruned", "https://full.example/k")]
    todo = [{"m": MINT, "bc": CURVE, "t0": T}, {"m": "X2", "bc": CURVE, "t0": T + 1}]
    n = hm.mine(rpcs, todo, 900.0, 5, log=lambda *a: None)
    assert n == 2 and "https://full.example/k" in seen
    assert MINT in bt.done_mints()


def test_unbiased_sample_is_drawn_from_all_launches_and_mined_first():
    import random
    from tradeslave.research import history_miner as hm
    cands = [{"m": f"m{i}", "updates": 50 if i < 5 else 0} for i in range(100)]
    drawn = hm.draw_sample(cands, already={"m1", "m2"}, k=10, rng=random.Random(1))
    assert len(drawn) == 10 and not {"m1", "m2"} & {c["m"] for c in drawn}
    order = hm.interleave(drawn, cands)
    assert order[0] is drawn[0] and order[2] is drawn[1]          # sampled launches go first, interleaved
    assert sorted(c["m"] for c in order) == sorted(c["m"] for c in cands)   # every mint exactly once
