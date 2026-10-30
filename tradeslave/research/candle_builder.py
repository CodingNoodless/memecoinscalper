"""Raw trades -> 1-minute OHLCV + SOL-side reserves (data v2). Same definition as pump.fun's old candle API (v1):

  - price = spot AFTER each trade (curve: virtual SOL / virtual tokens; PumpSwap: quote / base reserves)
  - a candle's open = the previous candle's close (the first candle opens at the price before the first trade)
  - high / low / close over the post-trade prices (and the open); volume = SOL traded, fees excluded
  - minutes without trades have no candle
  - reserve = SOL-side reserve after the candle's last trade (exact, from the chain; not derived from price)

    uv run python -m tradeslave.research.candle_builder check      # match rate vs v1 API candles on data/full_trades

Input rows: (ts, price, sol_volume, sol_reserve, phase), any order within a minute is resolved by the caller's order.
"""

from __future__ import annotations

import glob
import gzip
import json
import os
import statistics
import sys

from ..paths import DATA_DIR

V0_SOL, V0_TOK = 30.0, 1.073e9          # pump.fun curve start: virtual 30 SOL x 1.073e9 tokens
P0 = V0_SOL / V0_TOK


def build(trades, p_open: float = P0, interval: int = 60) -> list[list]:
    """trades: iterable of (ts, price, sol_volume, sol_reserve, phase) in execution order.
    Returns [[t, o, h, l, c, vol_sol, sol_reserve, phase], ...]."""
    out, cur, prev_close = [], None, p_open
    for ts, p, v, r, ph in trades:
        b = int(ts // interval) * interval
        if cur is None or b != cur[0]:
            if cur is not None:
                out.append(cur)
                prev_close = cur[4]
            cur = [b, prev_close, max(prev_close, p), min(prev_close, p), p, 0.0, r, ph]
        cur[2], cur[3], cur[4] = max(cur[2], p), min(cur[3], p), p
        cur[5] += v
        cur[6], cur[7] = r, ph
    if cur is not None:
        out.append(cur)
    return out


def curve_rows_from_events(trades) -> list[tuple]:
    """Chain TradeEvents [slot, ts, user, buy, sol_lamports, tok_raw, vs, vt] -> builder rows (exact reserves)."""
    rows = []
    for _slot, ts, _u, _b, sol, _tok, vs, vt in trades:
        if vt > 0:
            rows.append((ts, (vs / 1e9) / (vt / 1e6), sol / 1e9, vs / 1e9, "curve"))
    return rows


def curve_rows_from_api_trades(trades) -> list[tuple]:
    """data/full_trades rows [slot, idx, unix_t, wallet, is_buy, sol, tokens, amm] -> builder rows, by replaying the
    curve from its start (sol excludes fees; tokens in whole tokens). PumpSwap rows (amm) are skipped."""
    vs, vt, rows = V0_SOL, V0_TOK, []
    for _slot, _idx, t, _w, buy, sol, tok, amm in sorted(trades, key=lambda x: (x[0], x[1])):
        if amm:
            continue
        if buy:
            vs, vt = vs + sol, vt - tok
        else:
            vs, vt = vs - sol, vt + tok
        if vt <= 0:
            break
        rows.append((t, vs / vt, sol, vs, "curve"))
    return rows


def compare(a: list[list], b: list[list], tol: float = 0.005) -> dict:
    """Minute-by-minute match of two candle series (close and volume within tol)."""
    ma, mb = {x[0]: x for x in a}, {x[0]: x for x in b}
    both = sorted(set(ma) & set(mb))
    close_ok = sum(abs(ma[t][4] / mb[t][4] - 1) <= tol for t in both if mb[t][4])
    vol_ok = sum(abs(ma[t][5] - mb[t][5]) <= tol * max(mb[t][5], 1e-9) + 1e-6 for t in both)
    return {"a": len(ma), "b": len(mb), "both": len(both), "close_ok": close_ok, "vol_ok": vol_ok}


def check(limit: int = 3000) -> None:
    """Rebuild v1 candles from data/full_trades (curve phase, inside each token's fetched window) and compare."""
    files = sorted(glob.glob(str(DATA_DIR / "full_trades" / "*.json.gz")))[:limit]
    agg = {"tokens": 0, "both": 0, "close_ok": 0, "vol_ok": 0, "a_only": 0, "b_only": 0}
    tok_ok = []
    for f in files:
        d = json.load(gzip.open(f))
        lp = DATA_DIR / "long_paths" / f"{d['m']}.json"
        if not os.path.exists(lp):
            continue
        api = json.load(open(lp))["curve"]
        rows = curve_rows_from_api_trades(d["trades"])
        if not rows:
            continue
        mine = build(rows)
        t_end = min(d["t0"] + d.get("window_s", 0), rows[-1][0] + 60) if d.get("window_s") else rows[-1][0] + 60
        last_curve = mine[-1][0]
        api = [x for x in api if x[0] <= last_curve and x[0] < t_end]
        mine = [x for x in mine if x[0] < t_end]
        c = compare(mine, api)
        agg["tokens"] += 1
        for k in ("both", "close_ok", "vol_ok"):
            agg[k] += c[k]
        agg["a_only"] += c["a"] - c["both"]
        agg["b_only"] += c["b"] - c["both"]
        if c["both"]:
            tok_ok.append(c["close_ok"] / c["both"])
    b = max(agg["both"], 1)
    print(f"tokens {agg['tokens']}, minutes in both {agg['both']}: close match {agg['close_ok'] / b:.1%}, "
          f"volume match {agg['vol_ok'] / b:.1%}; minutes only in builder {agg['a_only']}, only in API {agg['b_only']}; "
          f"tokens with every close matching {sum(x == 1 for x in tok_ok) / max(len(tok_ok), 1):.1%}, "
          f"median token match {statistics.median(tok_ok) if tok_ok else 0:.1%}")


if __name__ == "__main__":
    if sys.argv[1:] == ["check"]:
        check()


def check_chain(n: int = 300, label: str = "helius-6") -> None:
    """The path v2 uses: chain TradeEvents (Helius getTransactionsForAddress) -> builder, vs v1 API candles, on n random
    v1 long_paths tokens (bonding-curve address from the tick recorder's create rows). Curve phase only."""
    import random
    sys.path.insert(0, str(DATA_DIR.parent))
    from tools.endpoints import configured, provider
    from .chain import Rpc, address_transactions
    from .chain_backfill import trades_of
    from ..scalper.recorder import TICK_DIR, read_ticks
    bc = {r["m"]: r["bc"] for r in read_ticks(sorted(TICK_DIR.glob("*.jsonl.gz"))) if r.get("k") == "create" and r.get("bc")}
    url, cnt = None, {}
    for u in configured():
        p = provider(u)
        cnt[p] = cnt.get(p, 0) + 1
        if f"{p}-{cnt[p]}" == label:
            url = u
    rpc = Rpc(url, 8.0)
    names = sorted(os.path.basename(x)[:-5] for x in glob.glob(str(DATA_DIR / "long_paths" / "*.json")))
    random.Random(31).shuffle(names)
    agg = {"tokens": 0, "both": 0, "close_ok": 0, "vol_ok": 0, "a_only": 0, "b_only": 0}
    tok_ok = []
    for m in names:
        if agg["tokens"] >= n:
            break
        if m not in bc:
            continue
        d = json.load(open(DATA_DIR / "long_paths" / f"{m}.json"))
        if len(d["curve"]) < 2:
            continue
        txs, _ = address_transactions(rpc, bc[m], d["t0"] - 30, d["t0"] + 7 * 86400, 50)
        if not txs:
            continue
        rows = curve_rows_from_events(trades_of(txs, m))
        if not rows:
            continue
        mine = build(rows)
        last = mine[-1][0]
        api = [x for x in d["curve"] if x[0] <= last]
        c = compare(mine, api)
        agg["tokens"] += 1
        for k in ("both", "close_ok", "vol_ok"):
            agg[k] += c[k]
        agg["a_only"] += c["a"] - c["both"]
        agg["b_only"] += c["b"] - c["both"]
        if c["both"]:
            tok_ok.append(c["close_ok"] / c["both"])
    b = max(agg["both"], 1)
    print(f"CHAIN vs API: tokens {agg['tokens']}, minutes in both {agg['both']}: close match {agg['close_ok'] / b:.1%}, "
          f"volume match {agg['vol_ok'] / b:.1%}; minutes only in chain build {agg['a_only']}, only in API {agg['b_only']}; "
          f"tokens with every close matching {sum(x == 1 for x in tok_ok) / max(len(tok_ok), 1):.1%}; credits ~{rpc.credits}")


if __name__ == "__main__" and sys.argv[1:2] == ["check-chain"]:
    check_chain(int(sys.argv[2]) if len(sys.argv) > 2 else 300)
