"""Graduated tokens on PumpSwap: collect exact pool price paths, then test scalps.

    uv run python -m tradeslave.research.graduations collect --limit 400
    uv run python -m tradeslave.research.graduations analyze

Why: on the bonding curve a round trip costs ~3.7% (1.25% fee per side plus
network fees), which ate every edge we tested. PumpSwap pools charge far less
per side, so small moves after graduation may be tradeable.

collect: for each recorded migration (free PumpPortal stream), fetch every
successful transaction touching the mint for `--window` seconds after the
migration (Helius RPC, RESEARCH_RPC_URL). The pool is the account holding both
the token and wrapped SOL; its balances after each transaction are the exact
reserves, so the price path is exact. Output: data/trades/graduations.jsonl.

analyze: a small, fixed set of entry rules x exits, simulated against the pool
with fees, fill delay and network fees, reported separately on the earlier and
later half of the migrations (a rule must win on both).
"""

from __future__ import annotations

import argparse
import bisect
import collections
import json
import random
import statistics
import time
from pathlib import Path

from ..scalper.recorder import TICK_DIR, read_ticks
from .backfill_trades import TRADE_DIR
from .chain import PUBLIC_RPC, Rpc, address_transactions, env_rpc, host_of, signature_of
from .backfill_trades import account_keys

OUT = TRADE_DIR / "graduations.jsonl"
WSOL = "So11111111111111111111111111111111111111112"
VERSION = 1


# ---------------------------------------------------------------- collect

def _balances(entries, mint_a: str, mint_b: str) -> dict[str, dict[str, int]]:
    by_owner: dict[str, dict[str, int]] = collections.defaultdict(dict)
    for b in entries or []:
        if b.get("mint") in (mint_a, mint_b) and b.get("owner"):
            by_owner[b["owner"]][b["mint"]] = by_owner[b["owner"]].get(b["mint"], 0) + int(b["uiTokenAmount"]["amount"])
    return by_owner


def parse_pool_tx(tx: dict, mint: str, pool: str | None = None) -> dict | None:
    """Pool reserves after a transaction (+ trader and side when it is a swap)."""
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None
    post = _balances(meta.get("postTokenBalances"), mint, WSOL)
    pre = _balances(meta.get("preTokenBalances"), mint, WSOL)
    candidates = [o for o, b in post.items() if mint in b and WSOL in b and (pool is None or o == pool)]
    if not candidates:
        return None
    owner = max(candidates, key=lambda o: post[o][WSOL])     # the pool holds the most SOL of anyone in the tx
    base, quote = post[owner][mint], post[owner][WSOL]
    d_quote = quote - pre.get(owner, {}).get(WSOL, 0)
    d_base = base - pre.get(owner, {}).get(mint, 0)
    if base <= 0 or quote <= 0 or (d_quote == 0 and d_base == 0):
        return None
    keys = account_keys(tx)
    swap = (d_quote > 0) != (d_base > 0)                  # one side in, the other out
    return {"k": "pool", "m": mint, "t": float(tx.get("blockTime") or 0), "slot": tx.get("slot"), "pool": owner,
            "base": base, "quote": quote, "w": keys[0] if keys else "", "swap": int(swap),
            "b": int(swap and d_quote > 0), "sol": abs(d_quote) / 1e9}


def migrations(paths) -> list[dict]:
    out = {}
    for r in read_ticks(paths):
        if r.get("k") == "migr" and r.get("m") and r["m"] not in out:
            out[r["m"]] = {"m": r["m"], "t": float(r["t"]), "sig": r.get("x_signature")}
    return sorted(out.values(), key=lambda x: x["t"])


def done_mints(path: Path | None = None) -> set[str]:
    path = path or OUT
    out = set()
    if path.exists():
        for line in path.open(encoding="utf-8"):
            if '"done"' in line:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("v") == VERSION:
                    out.add(r["m"])
    return out


def collect(args) -> None:
    url = args.rpc or env_rpc() or PUBLIC_RPC
    rpc = Rpc(url, args.rps or (3.0 if url == PUBLIC_RPC else 8.0))
    migs = migrations(sorted(TICK_DIR.glob("*.jsonl.gz")))
    done = done_mints()
    todo = [g for g in migs if g["m"] not in done]
    if args.limit and len(todo) > args.limit:
        random.Random(11).shuffle(todo)
        todo = sorted(todo[:args.limit], key=lambda g: g["t"])
    print(f"{len(migs)} recorded migrations; {len(done)} done; {len(todo)} to fetch via {host_of(url)}")
    if args.dry_run or not todo:
        return
    TRADE_DIR.mkdir(parents=True, exist_ok=True)
    started, rows_total, skipped = time.time(), 0, 0
    with OUT.open("a", encoding="utf-8") as fh:
        for n, g in enumerate(todo, 1):
            txs, stats = address_transactions(rpc, g["m"], g["t"] - 20, g["t"] + args.window, args.max_pages)
            if txs is None:
                skipped += 1
                continue
            rows, pool = [], None
            for tx in txs:
                row = parse_pool_tx(tx, g["m"], pool)
                if row:
                    pool = pool or row["pool"]                 # lock onto the first pool seen
                    row["sig"] = signature_of(tx)
                    rows.append(row)
            for i, row in enumerate(rows):
                row["i"] = i
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
            fh.write(json.dumps({"k": "done", "v": VERSION, "m": g["m"], "t_migr": g["t"], "n": len(rows),
                                 "window": args.window, **stats}) + "\n")
            fh.flush()
            rows_total += len(rows)
            if n % 10 == 0 or n == len(todo) or n <= 3:
                rate = n / max(time.time() - started, 1)
                print(f"  {n}/{len(todo)} graduations, {rows_total} pool updates, {rpc.calls} calls "
                      f"({stats.get('method')}), ~{(len(todo) - n) / rate / 60:.0f} min left, {skipped} skipped"
                      + (f", errors {rpc.errors}" if rpc.errors else ""), flush=True)
    print(f"done: {rows_total} pool updates written to {OUT}")


# ---------------------------------------------------------------- analyze

class Pool:
    __slots__ = ("m", "t_migr", "t", "base", "quote", "p", "buys_sol")

    def __init__(self, m: str, t_migr: float, rows: list[dict]):
        self.m, self.t_migr = m, t_migr
        self.t, self.base, self.quote = [], [], []
        for r in rows:
            self.t.append(max(float(r["t"]), self.t[-1] if self.t else 0.0))
            self.base.append(int(r["base"])); self.quote.append(int(r["quote"]))
        self.p = [(q / 1e9) / (b / 1e6) for b, q in zip(self.base, self.quote)]

    def idx_at(self, x: float) -> int:
        return bisect.bisect_right(self.t, x) - 1


def load(path: Path | None = None) -> list[Pool]:
    path = path or OUT
    rows, done = collections.defaultdict(list), {}
    if not path.exists():
        return []
    for line in path.open(encoding="utf-8"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("k") == "pool":
            rows[r["m"]].append(r)
        elif r.get("k") == "done" and r.get("v") == VERSION:
            done[r["m"]] = r
    pools = []
    for m, d in done.items():
        rs = sorted(rows.get(m, []), key=lambda r: (r.get("slot") or 0, r.get("i", 0)))
        if len(rs) >= 3:
            pools.append(Pool(m, float(d["t_migr"]), rs))
    return sorted(pools, key=lambda p: p.t_migr)


def simulate(pool: Pool, t_dec: float, tp: float, sl: float, hold: float, fee: float,
             size: float = 0.05, lat: float = 0.8, net_fee: float = 0.0003, trail=None) -> float | None:
    """Net return of buying `size` SOL at t_dec+lat on the pool and exiting per policy."""
    i = pool.idx_at(t_dec + lat)
    if i < 0:
        return None
    sol_in = size * 1e9 * (1 - fee)
    tokens = pool.base[i] * sol_in / (pool.quote[i] + sol_in)
    cost = size + net_fee

    def value(j):
        out = pool.quote[j] * tokens / (pool.base[j] + tokens) * (1 - fee)
        return out / 1e9 - net_fee

    t_fill, peak, exit_t = t_dec + lat, -1.0, None
    j = i + 1
    while j < len(pool.t) and pool.t[j] <= t_fill + hold:
        pnl = value(j) / cost - 1
        peak = max(peak, pnl)
        if pnl >= tp or pnl <= -sl or (trail and peak >= trail[0] and peak - pnl >= trail[1]):
            exit_t = pool.t[j]
            break
        j += 1
    k = pool.idx_at((exit_t if exit_t is not None else t_fill + hold) + lat)
    return value(k) / cost - 1


def entries(pool: Pool, rule: str) -> list[float]:
    """Decision times for one rule (at most one entry per pool, no lookahead)."""
    t0, horizon = pool.t_migr, pool.t_migr + 1200
    if rule.startswith("at+"):
        return [t0 + float(rule[3:])]
    if rule.startswith("dip"):                      # first fall of X% from the post-migration high
        x = float(rule[3:]) / 100
        high = 0.0
        for t, p in zip(pool.t, pool.p):
            if t < t0 or t > horizon:
                continue
            high = max(high, p)
            if high and p <= high * (1 - x):
                return [t]
        return []
    if rule.startswith("mom"):                      # up X% over the last 30 s
        x = float(rule[3:]) / 100
        for i, (t, p) in enumerate(zip(pool.t, pool.p)):
            if t < t0 + 30 or t > horizon:
                continue
            k = pool.idx_at(t - 30)
            if k >= 0 and p >= pool.p[k] * (1 + x):
                return [t]
        return []
    raise ValueError(rule)


RULES = ["at+2", "at+15", "at+60", "dip15", "dip30", "dip50", "mom10", "mom25"]
EXITS = {"tp5/sl5/60s": dict(tp=0.05, sl=0.05, hold=60), "tp10/sl10/120s": dict(tp=0.10, sl=0.10, hold=120),
         "tp25/sl15/300s": dict(tp=0.25, sl=0.15, hold=300), "trail20/10 sl20 600s": dict(tp=9, sl=0.20, hold=600, trail=(0.20, 0.10)),
         "hold 300s": dict(tp=9, sl=0.99, hold=300)}


def _stats(res: list[float]) -> str:
    if not res:
        return "n=0"
    s = sorted(res)
    k = max(1, len(s) // 50)
    trimmed = statistics.mean(s[:-k]) if len(s) > k else statistics.mean(s)
    return (f"n={len(s):4d} mean {statistics.mean(s)*100:+6.1f}% median {statistics.median(s)*100:+6.1f}% "
            f"trim2% {trimmed*100:+6.1f}% win {sum(x > 0 for x in s)/len(s):4.0%}")


def analyze(args) -> None:
    pools = load()
    if not pools:
        raise SystemExit("No graduation data yet. Run: uv run python -m tradeslave.research.graduations collect")
    mid = pools[len(pools) // 2].t_migr
    halves = {"early": [p for p in pools if p.t_migr < mid], "late": [p for p in pools if p.t_migr >= mid]}
    n_updates = statistics.median(len(p.t) for p in pools)
    print(f"{len(pools)} graduations with pool data (median {n_updates:.0f} pool updates in the window); "
          f"fee {args.fee:.2%} per side, size {args.size} SOL, fill delay {args.latency}s")
    # what happens after graduation, on average
    for horizon in (30, 120, 300, 900):
        rets = []
        for p in pools:
            i, j = p.idx_at(p.t_migr + 2), p.idx_at(p.t_migr + 2 + horizon)
            if i >= 0 and j >= 0:
                rets.append(p.p[j] / p.p[i] - 1)
        if rets:
            print(f"  price change {horizon:4d}s after graduation: median {statistics.median(rets)*100:+.1f}% "
                  f"mean {statistics.mean(rets)*100:+.1f}%  up {sum(r > 0 for r in rets)/len(rets):.0%}")
    winners = []
    for rule in RULES:
        print(f"\n{rule}")
        for name, pol in EXITS.items():
            line, both = [], True
            for label, group in halves.items():
                res = [r for p in group for t in entries(p, rule)
                       if (r := simulate(p, t, fee=args.fee, size=args.size, lat=args.latency, **pol)) is not None]
                line.append(f"{label}: {_stats(res)}")
                if not res or statistics.median(res) <= 0 or statistics.mean(res) <= 0:
                    both = False
            print(f"  {name:22s} " + " | ".join(line) + ("   <== positive in both halves" if both else ""))
            if both:
                winners.append((rule, name))
    print("\nVERDICT:", ("candidates " + ", ".join(f"{r} + {e}" for r, e in winners)) if winners else
          "no rule is positive (mean and median) in both halves at this fee")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--rpc", default=None)
    c.add_argument("--rps", type=float, default=None)
    c.add_argument("--window", type=float, default=1800.0, help="seconds after migration (default 1800)")
    c.add_argument("--limit", type=int, default=0)
    c.add_argument("--max-pages", type=int, default=10)
    c.add_argument("--dry-run", action="store_true")
    a = sub.add_parser("analyze")
    a.add_argument("--fee", type=float, default=0.01, help="fee per side (default 1%%, conservative)")
    a.add_argument("--size", type=float, default=0.05)
    a.add_argument("--latency", type=float, default=0.8)
    args = parser.parse_args()
    collect(args) if args.cmd == "collect" else analyze(args)


if __name__ == "__main__":
    main()
