"""PumpSwap graduation research: pool parsing, AMM simulation, rules, analysis."""

import json

from tradeslave.research import graduations as g

MINT, POOL, USER = "GradMint", "PoolPda", "Trader"


def bal(owner, mint, amount):
    return {"owner": owner, "mint": mint, "uiTokenAmount": {"amount": str(amount)}}


def pool_tx(t, base_pre, quote_pre, base_post, quote_post, slot=1, mint=MINT):
    pre = [bal(POOL, mint, base_pre), bal(POOL, g.WSOL, quote_pre), bal(USER, g.WSOL, 10**9)] if base_pre else []
    post = [bal(POOL, mint, base_post), bal(POOL, g.WSOL, quote_post), bal(USER, g.WSOL, 10**9)]
    return {"blockTime": t, "slot": slot, "meta": {"err": None, "preTokenBalances": pre, "postTokenBalances": post},
            "transaction": {"message": {"accountKeys": [USER, POOL]}, "signatures": [f"sig{t}"]}}


def test_parse_pool_tx_reads_reserves_and_side():
    created = g.parse_pool_tx(pool_tx(100, 0, 0, 200_000_000 * 10**6, 85 * 10**9), MINT)
    assert created["pool"] == POOL and created["swap"] == 0
    buy = g.parse_pool_tx(pool_tx(101, 200_000_000 * 10**6, 85 * 10**9, 199_000_000 * 10**6, 85_500_000_000), MINT)
    assert buy["swap"] == 1 and buy["b"] == 1 and buy["w"] == USER and abs(buy["sol"] - 0.5) < 1e-9
    sell = g.parse_pool_tx(pool_tx(102, 199 * 10**12, 855 * 10**8, 200 * 10**12, 85 * 10**9), MINT)
    assert sell["swap"] == 1 and sell["b"] == 0
    assert g.parse_pool_tx(pool_tx(103, 1, 1, 1, 1), MINT) is None                     # nothing moved
    assert g.parse_pool_tx(pool_tx(104, 0, 0, 5, 5), MINT, pool="OtherPool") is None


def make_pool(prices, t0=1000.0):
    rows = []
    base = 200_000_000 * 10**6
    for i, p in enumerate(prices):
        quote = int(p * (base / 1e6) * 1e9)
        rows.append({"t": t0 + i, "base": base, "quote": quote, "slot": i, "i": 0})
    return g.Pool("M", t0, rows)


def test_simulate_charges_fees_and_follows_price():
    flat = make_pool([4.25e-7] * 100)
    r = g.simulate(flat, 1000.0, tp=9, sl=0.99, hold=30, fee=0.01)
    assert -0.035 < r < -0.030                    # two 1% fees + 2x 0.0003 SOL network fee on 0.05 SOL
    up = make_pool([4.25e-7 * (1.01 ** i) for i in range(100)])
    assert g.simulate(up, 1000.0, tp=0.05, sl=0.05, hold=60, fee=0.003) > 0.03
    down = make_pool([4.25e-7 * (0.99 ** i) for i in range(100)])
    assert g.simulate(down, 1000.0, tp=0.05, sl=0.05, hold=60, fee=0.003) < -0.04


def test_entry_rules_have_no_lookahead():
    prices = [1.0, 1.2, 1.5, 1.4, 1.1, 1.0, 1.3] + [1.3] * 60
    p = make_pool([x * 1e-7 for x in prices])
    assert g.entries(p, "at+15") == [1015.0]
    assert g.entries(p, "dip30") == [1005.0]      # 1.5 high -> 1.05 or lower first at t+5
    assert g.entries(p, "dip50") == []
    mom = make_pool([1e-7] * 31 + [1.3e-7] * 40)
    assert g.entries(mom, "mom25") == [1031.0]


def test_collect_and_analyze_end_to_end(tmp_path, monkeypatch, capsys):
    from tradeslave.scalper.recorder import TickRecorder
    rec = TickRecorder(tmp_path)
    for k in range(6):
        rec.migration(1791936000.0 + 600 * k, f"M{k}", {"signature": f"s{k}", "pool": "pump-amm"})
    rec.close()
    monkeypatch.setattr(g, "TICK_DIR", tmp_path)
    monkeypatch.setattr(g, "OUT", tmp_path / "grad.jsonl")
    monkeypatch.setattr(g, "TRADE_DIR", tmp_path)

    def fake_txs(rpc, address, t_from, t_to, max_pages=10):
        t0 = t_from + 20
        txs = [pool_tx(t0, 0, 0, 200 * 10**12, 85 * 10**9, slot=1, mint=address)]
        base, quote = 200 * 10**12, 85 * 10**9
        for i in range(1, 400):
            nb = base - 10**11; nq = int(quote * base / nb)
            txs.append(pool_tx(t0 + i, base, quote, nb, nq, slot=1 + i, mint=address)); base, quote = nb, nq
        return txs, {"method": "gtfa", "sigs": len(txs)}

    monkeypatch.setattr(g, "address_transactions", fake_txs)
    monkeypatch.setattr("sys.argv", ["x", "collect", "--rpc", "https://example.invalid"])
    g.main()
    assert g.done_mints(tmp_path / "grad.jsonl") == {f"M{k}" for k in range(6)}
    monkeypatch.setattr("sys.argv", ["x", "analyze", "--fee", "0.003"])
    g.main()
    out = capsys.readouterr().out
    assert "6 graduations with pool data" in out and "VERDICT" in out
