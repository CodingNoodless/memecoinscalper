"""Chain-history trade backfill: transaction parsing, curve replay, damaged recordings."""

import gzip
import json

from tradeslave.research import backfill_trades as bt
from tradeslave.research.dataset import START_VS, START_VT, load_backfill, replay_error, token_from_trades, Token, price
from tradeslave.scalper.curve import CurveState, buy_tokens_out, sell_sol_out
from tradeslave.scalper.recorder import read_ticks

MINT, CURVE, WALLET = "MintPump", "CurveAcct", "Trader1"


def tx(d_lamports, d_curve_tokens, t=1000, err=None, v0=False):
    keys = [WALLET, "Other", CURVE, "Fee"]
    pre_l = [5_000_000_000, 0, 40_000_000_000, 0]
    post_l = list(pre_l); post_l[2] += d_lamports; post_l[0] -= d_lamports
    bal = lambda amt: [{"accountIndex": 1, "mint": MINT, "owner": CURVE, "uiTokenAmount": {"amount": str(amt)}},
                       {"accountIndex": 3, "mint": "Other", "owner": CURVE, "uiTokenAmount": {"amount": "5"}}]
    message = {"accountKeys": keys}
    meta = {"err": err, "preBalances": pre_l, "postBalances": post_l,
            "preTokenBalances": bal(800_000_000_000_000), "postTokenBalances": bal(800_000_000_000_000 + d_curve_tokens)}
    if v0:  # curve account arrives through an address lookup table
        message = {"accountKeys": [WALLET, "Other"]}
        meta["loadedAddresses"] = {"writable": [CURVE, "Fee"], "readonly": []}
    return {"blockTime": t, "slot": 7, "transaction": {"message": message}, "meta": meta}


def test_parse_buy_and_sell():
    buy = bt.parse_trade(tx(500_000_000, -15_000_000_000_000), MINT, CURVE)
    assert buy["b"] == 1 and buy["w"] == WALLET and buy["sol"] == 0.5 and buy["tok"] == 15_000_000
    assert buy["dv"] == 500_000_000 and buy["dc"] == -15_000_000_000_000
    sell = bt.parse_trade(tx(-200_000_000, 6_000_000_000_000), MINT, CURVE)
    assert sell["b"] == 0 and sell["sol"] == 0.2


def test_parse_skips_failed_unrelated_and_handles_lookup_tables():
    assert bt.parse_trade(tx(500_000_000, -1, err={"x": 1}), MINT, CURVE) is None
    assert bt.parse_trade(tx(0, 0), MINT, CURVE) is None
    assert bt.parse_trade(tx(500_000_000, -1), MINT, "NotInTx") is None
    assert bt.parse_trade(tx(500_000_000, -15, v0=True), MINT, CURVE)["b"] == 1


def test_replay_matches_curve_math_and_recorded_ticks(tmp_path):
    state, rows, recorded = CurveState(START_VT, START_VS), [], Token("M", 100.0)
    recorded.t.append(100.0); recorded.vs.append(START_VS); recorded.vt.append(START_VT)
    for i, (side, amount) in enumerate([("b", 1_000_000_000), ("b", 2_000_000_000), ("s", None), ("b", 300_000_000)]):
        if side == "b":
            out = buy_tokens_out(state, amount, 0)
            dv, dc = amount, -out
        else:
            sold = 10_000_000_000_000
            dv, dc = -sell_sol_out(state, sold, 0), sold
        state = CurveState(state.virtual_token_reserves + dc, state.virtual_sol_reserves + dv)
        t = 101.0 + i
        rows.append({"k": "trade", "t": t, "m": "M", "w": f"w{i}", "b": int(dv > 0), "sol": abs(dv) / 1e9,
                     "tok": abs(dc) / 1e6, "dv": dv, "dc": dc, "slot": 1 + i, "i": 0})
        recorded.t.append(t + 0.4); recorded.vs.append(state.virtual_sol_reserves); recorded.vt.append(state.virtual_token_reserves)
    replayed = token_from_trades("M", 100.0, rows, recorded)
    assert replayed.vs[-1] == state.virtual_sol_reserves and replayed.vt[-1] == state.virtual_token_reserves
    assert replayed.p[-1] == price(state.virtual_sol_reserves, state.virtual_token_reserves)
    assert replay_error(recorded, replayed) == 0.0

    path = tmp_path / "backfill.jsonl"
    with path.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
        fh.write(json.dumps({"k": "done", "v": bt.VERSION, "m": "M", "t0": 100.0, "n": len(rows), "window": 900}) + "\n")
        fh.write(json.dumps({"k": "done", "m": "OLD", "t0": 1.0, "n": 0}) + "\n")   # v1 marker: ignored
        fh.write(json.dumps({"k": "trade", "m": "PARTIAL", "t": 1, "dv": 1, "dc": -1}) + "\n")  # no done marker
    loaded = load_backfill(path)
    assert set(loaded) == {"M"} and len(loaded["M"]["trades"]) == 4
    assert bt.done_mints(path) == {"M"}


def test_read_ticks_survives_a_damaged_member(tmp_path):
    path = tmp_path / "day.jsonl.gz"
    first = gzip.compress(b"".join(json.dumps({"k": "curve", "n": i}).encode() + b"\n" for i in range(2000)))
    broken = first[: len(first) // 2]                       # a run killed mid-write
    second = gzip.compress(b"".join(json.dumps({"k": "create", "n": i}).encode() + b"\n" for i in range(5)))
    tail = gzip.compress(b'{"k":"meta","n":0}\n{"k":"meta","n":1}\n')[:-12]   # still being written
    path.write_bytes(broken + second + tail)
    rows = list(read_ticks([path]))
    kinds = [r["k"] for r in rows]
    assert kinds.count("create") == 5                       # the next run's data is not lost
    assert 0 < kinds.count("curve") < 2000                  # the damaged run keeps what was decodable
    assert "meta" in kinds


def test_select_tokens_and_dry_run(tmp_path, monkeypatch, capsys):
    from tradeslave.scalper.recorder import TickRecorder
    rec = TickRecorder(tmp_path)
    s = CurveState(START_VT, START_VS)
    rec.create(1791936000.0, "A", "A", s, "dev", "curveA")
    rec.create(1791936001.0, "B", "B", s, "dev", "curveB")
    for i in range(6):
        rec.curve(1791936002.0 + i, "A", s)
    rec.close()
    toks = bt.select_tokens(list(tmp_path.glob("*.jsonl.gz")), 0, 9e12, 5)
    assert [t["m"] for t in toks] == ["A"] and toks[0]["bc"] == "curveA"
    monkeypatch.setattr(bt, "TICK_DIR", tmp_path)
    monkeypatch.setattr(bt, "OUT", tmp_path / "none.jsonl")
    monkeypatch.setattr("sys.argv", ["x", "--dry-run", "--rpc", "https://example.invalid"])
    bt.main()
    assert "1 to fetch" in capsys.readouterr().out


def launch_tx(d_lamports, curve_tokens_after):
    t = tx(d_lamports, 1)
    t["meta"]["preTokenBalances"] = []
    t["meta"]["postTokenBalances"] = [{"accountIndex": 1, "mint": MINT, "owner": CURVE,
                                       "uiTokenAmount": {"amount": str(curve_tokens_after)}}]
    return t


def test_launch_transaction_counts_only_the_dev_buy():
    bought = 2_115_327_702_796
    launch = launch_tx(59_259_258 + 1_493_520, bt.TOTAL_SUPPLY_RAW - bought)   # rent + buy; supply minted to the curve
    row = bt.parse_trade(launch, MINT, CURVE)
    assert row["create"] == 1 and row["b"] == 1 and row["dc"] == -bought
    assert abs(row["dv"] - 59_259_258) <= 1                              # rent excluded, matches the create message
    old = {"k": "trade", "t": 1, "m": "M", "dv": 60_752_778, "dc": bt.TOTAL_SUPPLY_RAW - bought, "b": 1}
    assert bt.normalize(old)["dc"] == -bought                            # rows saved before the fix are repaired
    no_buy = launch_tx(1_493_520, bt.TOTAL_SUPPLY_RAW)
    assert bt.parse_trade(no_buy, MINT, CURVE) is None


class FakeRpc:
    def __init__(self, gtfa_ok):
        self.gtfa_ok, self.calls, self.errors = gtfa_ok, [], {}
        self.gtfa = None

    def request(self, method, params):
        self.calls.append(method)
        if method == "getTransactionsForAddress":
            if not self.gtfa_ok:
                return None, {"code": -32601, "message": "Method not found"}
            token = params[1].get("paginationToken")
            page = [{"slot": 1, "blockTime": 10, "meta": {}, "transaction": {"signatures": ["a"], "message": {"accountKeys": []}}}]
            return ({"data": page, "paginationToken": None if token else "1:0"}), None
        if method == "getSignaturesForAddress":
            return [{"signature": "s2", "blockTime": 12, "err": None}, {"signature": "s1", "blockTime": 11, "err": None},
                    {"signature": "bad", "blockTime": 11, "err": {"x": 1}}], None
        if method == "getTransaction":
            return ({"slot": 2, "blockTime": 11, "meta": {}, "transaction": {"message": {"accountKeys": []}}}
                    if params[0] != "s2" else None), None
        raise AssertionError(method)

    def call(self, method, params):
        return self.request(method, params)[0]


def test_address_transactions_prefers_bulk_method_and_falls_back():
    from tradeslave.research.chain import address_transactions, signature_of
    fast = FakeRpc(True)
    txs, stats = address_transactions(fast, "addr", 0, 100)
    assert stats["method"] == "gtfa" and len(txs) == 2 and fast.gtfa is True
    slow = FakeRpc(False)
    txs, stats = address_transactions(slow, "addr", 0, 100)
    assert slow.gtfa is False and stats["method"] == "sigs"
    assert stats["sigs"] == 2 and stats["tx_missing"] == 1 and [signature_of(t) for t in txs] == ["s1"]
    address_transactions(slow, "addr", 0, 100)
    assert slow.calls.count("getTransactionsForAddress") == 1          # not retried once known unavailable
