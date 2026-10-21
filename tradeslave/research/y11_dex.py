"""Y11-DEX: the frozen Y11 rule (bid 8% below the previous 1-min close, TP +5%, no stop, 24 h) on Solana spot
pools, paper only (pre-registered; vault: "2026-10-21 Y11-DEX resting bids on Solana pools").

    uv run python -m tradeslave.research.y11_dex universe   # pools (from the Y11 fetch log), TVL, fee tier -> pools.json
    uv run python -m tradeslave.research.y11_dex run        # design sanity + the one holdout look

Fill: low <= bid * (1 - 0.003), entry at the bid. Exits can't execute on the fill candle or the next (1-candle keeper
delay): TP fills at the TP on a candle j >= fill + 2 whose high >= TP * 1.003; the time exit executes at the close
of the candle after 1,440 min. Costs per side: pool fee + Jupiter trigger fee (0.1% base + routing assumption) +
price impact (order / half the pool TVL); plus 0.002 SOL per round trip.
"""

from __future__ import annotations

import json
import math
import re
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
import requests

from .listings import GT, get
from .major_wick import COINS, OUT

POOLS = OUT / "pools.json"
SOLPX = OUT / "sol_daily.json"
D, T, H = 0.08, 0.05, 1440                       # frozen Y11
THRU = 0.003
MIN_TVL = 500_000
JUP_BASE, JUP_ROUTE = 0.001, 0.001               # sourced base fee; routing fee is an assumption (0-0.5%)
FIXED_SOL = 0.002
SIZES = (0.1, 0.25, 1.0)
SOL_USDC = "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2"


def ts(s):
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


DESIGN = (ts("2026-04-23"), ts("2026-07-01"))     # sanity only
HOLD = (ts("2026-07-01"), ts("2026-10-21"))


def pool_fee(s, addr, dex, mcap_usd, sol_usd):
    """Fee fraction per side from the DEX's own API; (fee, source)."""
    try:
        if dex.startswith("raydium"):
            d = s.get("https://api-v3.raydium.io/pools/info/ids", params={"ids": addr}, timeout=30).json()["data"][0]
            return float(d["feeRate"]), "raydium api feeRate"
        if dex.startswith("orca"):
            d = s.get(f"https://api.orca.so/v2/solana/pools/{addr}", timeout=30).json()["data"]
            return float(d["feeRate"]) / 1e6, "orca api feeRate"
        if dex.startswith("meteora"):
            r = s.get(f"https://dlmm-api.meteora.ag/pair/{addr}", timeout=30)
            if r.ok:
                return float(r.json()["base_fee_percentage"]) / 100, "meteora dlmm base fee (dynamic fee not included)"
        if dex.startswith("pump"):
            from .shock import fee
            return fee(mcap_usd / sol_usd * 1e9 if mcap_usd else 1e18), "pumpswap official tier by market cap"
    except Exception as exc:  # noqa: BLE001 - fall through to the assumption
        print("fee lookup failed", addr, exc)
    return 0.003, "unknown: 0.3% assumption"


def universe():
    s = requests.Session()
    log = (OUT.parent.parent / "logs" / "major_wick_fetch.out")
    log = log if log.exists() else __import__("pathlib").Path("logs/major_wick_fetch.out")
    fetched = {m.group(1): m.group(2) for m in re.finditer(r"^(\S+) (\w{32,44}) \d+$", log.read_text(), re.M)}
    sol = (get(s, f"{GT}/networks/solana/pools/{SOL_USDC}") or {})["data"]["attributes"]
    sol_usd = float(sol["base_token_price_usd"])
    out = {}
    for sym in COINS:
        addr = fetched.get(sym)
        if not addr:
            print(sym, "no fetched pool")
            continue
        d = (get(s, f"{GT}/networks/solana/pools/{addr}") or {}).get("data")
        if not d:
            print(sym, "pool lookup failed")
            continue
        a, dex = d["attributes"], d["relationships"]["dex"]["data"]["id"]
        tvl = float(a.get("reserve_in_usd") or 0)
        mcap = float(a.get("market_cap_usd") or a.get("fdv_usd") or 0)
        f, src = pool_fee(s, addr, dex, mcap, sol_usd)
        out[sym] = dict(pool=addr, name=a["name"], dex=dex, tvl=tvl, fee=f, fee_src=src, eligible=tvl >= MIN_TVL)
        print(f"{sym:9s} {a['name'][:22]:22s} {dex:16s} TVL ${tvl / 1e6:7.2f}M fee {f:.3%} ({src}) "
              f"{'IN' if tvl >= MIN_TVL else 'out'}", flush=True)
    d = get(s, f"{GT}/networks/solana/pools/{SOL_USDC}/ohlcv/day", dict(aggregate=1, limit=1000, currency="usd"))
    SOLPX.write_text(json.dumps({int(r[0]) // 86400: r[4] for r in d["data"]["attributes"]["ohlcv_list"]}))
    POOLS.write_text(json.dumps(out, indent=1))


def trades(a, lo, hi):
    """a: rows (t, o, h, l, c) oldest first. Returns [(t_fill, gross return, bid price)] for fills in [lo, hi)."""
    t, h, l, c = a[:, 0], a[:, 2], a[:, 3], a[:, 4]
    out, k, n = [], 1, len(a)
    while k < n:
        if t[k] - t[k - 1] > 60 or not lo <= t[k] < hi:
            k += 1
            continue
        bid = c[k - 1] * (1 - D)
        if l[k] > bid * (1 - THRU):
            k += 1
            continue
        tp, end, x, j = bid * (1 + T), t[k] + H * 60, None, k + 1
        while x is None:
            j += 1
            if j >= n:
                x, j = None, n - 1
                break
            if t[j] > end:                        # time exit decided at the first candle past H: executes at its close
                x = c[j]
                break
            if h[j] >= tp * (1 + THRU):
                x = tp
        if x is None:                             # still open at the end of the data
            break
        out.append((t[k], x / bid - 1, bid))
        k = j + 1
    return out


def net(g, size_sol, sol_usd, info, route=JUP_ROUTE, mult=1.0):
    side = mult * (info["fee"] + JUP_BASE + route + size_sol * sol_usd / (info["tvl"] / 2))
    fixed = mult * FIXED_SOL / size_sol
    return (1 + g) * (1 - side) / (1 + side) - 1 - fixed, side


def summary(rows):
    v = [r for _, _, r in rows]
    if len(v) < 3:
        return dict(n=len(v)), f"n {len(v)} " + " ".join(f"{x:+.1%}" for x in v)
    m, sd = statistics.mean(v), statistics.stdev(v)
    per = defaultdict(float)
    for t, _, r in rows:
        per[int(t // 86400)] += r
    pv = list(per.values())
    td = statistics.mean(pv) / statistics.stdev(pv) * math.sqrt(len(pv)) if len(pv) > 2 and statistics.stdev(pv) else 0
    srt = sorted(v)
    trim = srt[:len(srt) - max(1, math.ceil(0.05 * len(srt)))]
    wins, losses = [x for x in v if x > 0], [x for x in v if x <= 0]
    eq, peak, mdd = 1.0, 1.0, 0.0
    for x in v:
        eq *= 1 + x
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    st = dict(n=len(v), mean=m, median=statistics.median(v), t_trade=m / sd * math.sqrt(len(v)) if sd else 0, t_daily=td,
              days=len(pv), win=len(wins) / len(v), avg_win=statistics.mean(wins) if wins else 0,
              avg_loss=statistics.mean(losses) if losses else 0,
              pf=sum(wins) / -sum(losses) if losses and sum(losses) else float("inf"), mdd=mdd, worst=min(v),
              trim_mean=statistics.mean(trim) if trim else 0)
    return st, (f"n {st['n']:4d} mean {m:+.2%} median {st['median']:+.2%} t(trade) {st['t_trade']:+.2f} "
                f"t(daily) {td:+.2f} win {st['win']:.0%} trim5% {st['trim_mean']:+.2%}")


def run():
    pools = json.loads(POOLS.read_text())
    sol = {int(k): v for k, v in json.loads(SOLPX.read_text()).items()}
    solp = lambda t: sol.get(int(t // 86400)) or 190.0      # noqa: E731
    coins = {s: p for s, p in pools.items() if p["eligible"]}
    print("universe:", ", ".join(coins))
    res = {}
    for name, (lo, hi) in (("DESIGN (sanity)", DESIGN), ("HOLDOUT", HOLD)):
        raw = []
        for sym, info in coins.items():
            f = OUT / f"dex_{sym}.npy"
            if not f.exists():
                print("  missing candles", sym)
                continue
            raw += [(sym, t, g) for t, g, _ in trades(np.load(f), lo, hi)]
        raw.sort(key=lambda x: x[1])
        print(f"\n== {name}: {len(raw)} fills, gross mean "
              f"{statistics.mean([g for *_, g in raw]) if raw else 0:+.2%}")
        for size in SIZES:
            for route in ((JUP_ROUTE,) if size != 0.25 else (0.0, JUP_ROUTE, 0.005)):
                rows = [(t, s, net(g, size, solp(t), coins[s], route)[0]) for s, t, g in raw]
                st, txt = summary(rows)
                print(f"  {size:4.2f} SOL route {route:.1%}: {txt}")
                res[(name, size, route)] = (st, rows)
        rows = res[(name, 0.25, JUP_ROUTE)][1]
        tot = sum(r for *_, r in rows) or 1e-12
        print("  per coin (0.25 SOL, main costs):")
        for sym in coins:
            v = [(t, s, r) for t, s, r in rows if s == sym]
            side = net(0, 0.25, 190.0, coins[sym])[1]
            print(f"    {sym:9s} side cost {side:.2%} | {summary(v)[1]} | share of P&L "
                  f"{sum(r for *_, r in v) / tot:+.0%}")
        st2, txt2 = summary([(t, s, net(g, 0.25, solp(t), coins[s], mult=2.0)[0]) for s, t, g in raw])
        print(f"  2x cost stress (all costs doubled): {txt2}")
    st, rows = res[("HOLDOUT", 0.25, JUP_ROUTE)]
    tot = sum(r for *_, r in rows)
    shares = {s: sum(r for _, ss, r in rows if ss == s) / tot for s in coins} if rows and tot else {}
    checks = dict(mean=st.get("mean", -1) >= 0.01, t=st.get("t_daily", 0) >= 2.0, n=st.get("n", 0) >= 60,
                  concentration=bool(shares) and max(shares.values()) <= 0.25 and tot > 0,
                  trim=st.get("trim_mean", -1) > 0)
    print("\nPASS checks (holdout, 0.25 SOL, main costs):", checks, "->", "PASS" if all(checks.values()) else "KILL")
    for k, v in st.items():
        print(f"  {k}: {v:+.4f}" if isinstance(v, float) else f"  {k}: {v}")


def main() -> None:
    {"universe": universe, "run": run}[sys.argv[1]]()


if __name__ == "__main__":
    main()
