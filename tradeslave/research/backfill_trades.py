"""Rebuild wallet-level trades for recorded tokens from Solana transaction history.

    uv run python -m tradeslave.research.backfill_trades --dry-run
    uv run python -m tradeslave.research.backfill_trades --limit 300

For each recorded token, every transaction that touched its bonding curve in
the first `--window` seconds is fetched and turned into one trade:
  wallet = fee payer, side = SOL into the curve (buy) or out (sell),
  sol    = change in the curve account's lamports (exact, fees excluded),
  tokens = change in the curve's token balance (exact).
Replaying those deltas from the starting reserves gives the full price path,
well past the scalper's watch window (see dataset.tokens_from_trades).

Free with a Helius free account (1M credits/month, 10 requests/s, 1 credit per
call; no card). Put its RPC URL in .env as RESEARCH_RPC_URL. The public RPC
works too, but slowly. Output: data/trades/backfill.jsonl (resumable).
"""

from __future__ import annotations

import argparse
import collections
import json
import random
import time
from pathlib import Path

from ..paths import DATA_DIR
from ..scalper.recorder import TICK_DIR, read_ticks
from .chain import PUBLIC_RPC, Rpc, address_transactions, env_rpc, host_of, signature_of

TRADE_DIR = DATA_DIR / "trades"
OUT = TRADE_DIR / "backfill.jsonl"
HISTORY_OUT = TRADE_DIR / "history.jsonl"     # written by research.history_miner (public RPC)
VERSION = 3   # v1: launch-tx bug; v2: newer-version transactions dropped. Both ignored


def account_keys(tx: dict) -> list[str]:
    """Static + lookup-table keys, tolerant of message layouts across transaction versions."""
    message = (tx.get("transaction") or {}).get("message") or {}
    raw = message.get("accountKeys") or message.get("staticAccountKeys") or []
    keys = [k if isinstance(k, str) else (k or {}).get("pubkey") for k in raw]
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    return keys + list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])


def _token_amount(balances, owner: str, mint: str) -> int:
    total = 0
    for b in balances or []:
        if b.get("owner") == owner and b.get("mint") == mint:
            total += int(b["uiTokenAmount"]["amount"])
    return total


TOTAL_SUPPLY_RAW = 1_000_000_000_000_000   # 1B tokens, 6 decimals: all minted to the curve at creation
START_VS = 30_000_000_000
START_VT = 1_073_000_000_000_000


def creation_buy(curve_tokens_after: int) -> tuple[int, int] | None:
    """(SOL lamports into the curve, curve token change) for a launch transaction.

    At launch the whole supply is minted to the curve, so its token balance goes
    from nothing to 1B minus the dev's buy, and its lamports include the new
    account's rent. Only the buy itself moves the price: rebuild it from the
    tokens bought and the starting reserves."""
    bought = TOTAL_SUPPLY_RAW - curve_tokens_after
    if bought <= 0:
        return None
    sol_in = -(-START_VS * bought // (START_VT - bought))   # ceil, as the program rounds against the buyer
    return sol_in, -bought


def normalize(row: dict) -> dict | None:
    """Fix launch rows stored before creation_buy existed (curve tokens went up on a 'buy')."""
    if (row.get("dc") or 0) > 0 and (row.get("dv") or 0) > 0:
        fixed = creation_buy(row["dc"])
        if fixed is None:
            return None
        row = dict(row, dv=fixed[0], dc=fixed[1], sol=fixed[0] / 1e9, tok=-fixed[1] / 1e6, b=1, create=1)
    return row


def parse_trade(tx: dict, mint: str, curve: str) -> dict | None:
    """One pump.fun bonding-curve trade from a getTransaction result (json encoding)."""
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None
    keys = account_keys(tx)
    d_tokens = (_token_amount(meta.get("postTokenBalances"), curve, mint)
                - _token_amount(meta.get("preTokenBalances"), curve, mint))
    if d_tokens == 0:
        return None
    if curve in keys and meta.get("postBalances") and keys.index(curve) < len(meta["postBalances"]):
        i = keys.index(curve)
        d_lamports = meta["postBalances"][i] - meta["preBalances"][i]
        if d_lamports == 0:
            return None
    else:
        # curve key not visible in this message layout: the side follows from the token
        # flow, the SOL amount is rebuilt from the curve state when the trades are replayed
        launch = not any(b.get("owner") == curve and b.get("mint") == mint for b in meta.get("preTokenBalances") or [])
        d_lamports = 1 if (launch or d_tokens < 0) else -1
        if launch:
            fixed = creation_buy(d_tokens)
            if fixed is None:
                return None
            d_lamports, d_tokens = fixed
            return {"k": "trade", "t": float(tx.get("blockTime") or 0), "m": mint, "w": keys[0] if keys else "",
                    "b": 1, "sol": d_lamports / 1e9, "tok": -d_tokens / 1e6, "dv": d_lamports, "dc": d_tokens,
                    "slot": tx.get("slot"), "src": "backfill", "create": 1}
        return {"k": "trade", "t": float(tx.get("blockTime") or 0), "m": mint, "w": keys[0] if keys else "",
                "b": int(d_tokens < 0), "sol": None, "tok": abs(d_tokens) / 1e6, "dv": None, "dc": d_tokens,
                "slot": tx.get("slot"), "src": "backfill"}
    created = d_lamports > 0 and d_tokens > 0          # launch tx: supply minted to the curve
    if created:
        fixed = creation_buy(d_tokens)
        if fixed is None:
            return None                                # launch without a dev buy
        d_lamports, d_tokens = fixed
    row = {"k": "trade", "t": float(tx.get("blockTime") or 0), "m": mint, "w": keys[0] if keys else "", "b": int(d_lamports > 0),
            "sol": abs(d_lamports) / 1e9, "tok": abs(d_tokens) / 1e6, "dv": d_lamports, "dc": d_tokens,
            "slot": tx.get("slot"), "src": "backfill"}
    if created:
        row["create"] = 1
    return row


def select_tokens(paths, since: float, until: float, min_updates: int) -> list[dict]:
    creates, updates = {}, collections.Counter()
    for r in read_ticks(paths):
        kind, mint = r.get("k"), r.get("m")
        if kind == "create" and r.get("bc"):
            t = float(r["t"])
            if since <= t < until:
                creates[mint] = {"m": mint, "bc": r["bc"], "t0": t}
        elif kind == "curve" and mint in creates:
            updates[mint] += 1
    return [c for m, c in creates.items() if updates[m] >= min_updates]


LOG_SAMPLES = TRADE_DIR / "log_samples.jsonl"
_log_budget = {"left": None}


def _keep_log_sample(tx: dict, trade: dict, limit: int = 400) -> None:
    """Keep a few raw program logs next to their exact trade, to build and verify a
    live trade-event parser (wallet-level live data) against chain truth."""
    if _log_budget["left"] is None:
        try:
            have = sum(1 for _ in LOG_SAMPLES.open(encoding="utf-8")) if LOG_SAMPLES.exists() else 0
        except OSError:
            have = limit
        _log_budget["left"] = max(0, limit - have)
    if _log_budget["left"] <= 0:
        return
    logs = [line for line in ((tx.get("meta") or {}).get("logMessages") or []) if "Program data:" in line]
    if not logs:
        return
    _log_budget["left"] -= 1
    try:
        TRADE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_SAMPLES.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"m": trade["m"], "w": trade["w"], "dv": trade["dv"], "dc": trade["dc"],
                                 "slot": trade.get("slot"), "logs": logs[:6]}) + "\n")
    except OSError:
        pass


def done_mints(path: Path | None = None) -> set[str]:
    if path is None:
        return done_mints(OUT) | done_mints(HISTORY_OUT)
    out = set()
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if '"done"' in line:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("v") == VERSION:
                        out.add(r["m"])
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rpc", default=None, help="RPC HTTP URL (default: RESEARCH_RPC_URL from .env, else public)")
    parser.add_argument("--rps", type=float, default=None, help="requests per second (default 8, public RPC 3)")
    parser.add_argument("--window", type=float, default=900.0, help="seconds after launch to fetch (default 900)")
    parser.add_argument("--min-updates", type=int, default=5, help="only tokens with this many recorded curve updates")
    parser.add_argument("--since", type=float, default=0.0, help="unix time: tokens created at/after")
    parser.add_argument("--until", type=float, default=9e12, help="unix time: tokens created before")
    parser.add_argument("--limit", type=int, default=0, help="random sample of this many tokens (0 = all)")
    parser.add_argument("--max-pages", type=int, default=20, help="pages (1000 transactions each) per token")
    parser.add_argument("--dry-run", action="store_true", help="only count tokens")
    args = parser.parse_args()

    run(rpc_url=args.rpc, rps=args.rps, window=args.window, min_updates=args.min_updates, since=args.since,
        until=args.until, limit=args.limit, max_pages=args.max_pages, dry_run=args.dry_run)


def sampled_mints(tag: str = "all", path: Path | None = None) -> set[str]:
    """Mints drawn at random for an unbiased sample (see run(sample=...)); by default from the
    autopilot backfill and the history miner (which draws its own random launches each pass)."""
    out = set()
    for p in ([path] if path is not None else [OUT, HISTORY_OUT]):
        out |= _sampled_in(p, tag)
    return out


def _sampled_in(path: Path, tag: str) -> set[str]:
    out = set()
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if '"sample"' in line:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("k") == "sample" and r.get("tag") == tag:
                        out.add(r["m"])
    return out


def run(rpc_url=None, rps=None, window=900.0, min_updates=5, since=0.0, until=9e12, limit=0, max_pages=20,
        dry_run=False, log=None, sample: str | None = None, max_credits: int = 0) -> int:
    """Fetch trades for recorded tokens not yet done. Returns the number of tokens fetched."""
    log = log or (lambda *a: print(*a, flush=True))
    url = rpc_url or env_rpc() or PUBLIC_RPC
    rps = rps or (3.0 if url == PUBLIC_RPC else 8.0)
    paths = sorted(TICK_DIR.glob("*.jsonl.gz"))
    toks = select_tokens(paths, since, until, min_updates)
    done = done_mints()
    if sample:
        # Unbiased draw from ALL launches in the window (already-fetched tokens count as drawn),
        # so launch-time research isn't skewed toward tokens that turned out active.
        already = sampled_mints(sample)
        pool = [t for t in toks if t["m"] not in already]
        random.Random(int(time.time())).shuffle(pool)
        chosen = pool[:limit] if limit else pool
        if not dry_run and chosen:
            TRADE_DIR.mkdir(parents=True, exist_ok=True)
            with OUT.open("a", encoding="utf-8") as fh:
                for t in chosen:
                    fh.write(json.dumps({"k": "sample", "tag": sample, "m": t["m"], "t0": t["t0"]}) + "\n")
        todo = sorted((t for t in chosen if t["m"] not in done), key=lambda t: t["t0"])
    else:
        todo = [t for t in toks if t["m"] not in done]
    if not sample and limit and len(todo) > limit:
        random.Random(7).shuffle(todo)
        todo = sorted(todo[:limit], key=lambda t: t["t0"])
    log(f"{len(toks)} recorded tokens with >= {min_updates} updates; {len(done)} already done; "
          f"{len(todo)} to fetch via {host_of(url)}")
    if dry_run or not todo:
        return 0
    TRADE_DIR.mkdir(parents=True, exist_ok=True)
    rpc = Rpc(url, rps)
    started, trades_total, skipped, missing = time.time(), 0, 0, 0
    with OUT.open("a", encoding="utf-8") as fh:
        for n, tok in enumerate(todo, 1):
            if max_credits and rpc.credits >= max_credits:
                log(f"  credit budget reached (~{rpc.credits} credits); stopping at {n - 1}/{len(todo)} tokens")
                break
            if n == 1:
                log("  fetching the first token...")
            txs, stats = address_transactions(rpc, tok["bc"], tok["t0"] - 30, tok["t0"] + window, max_pages)
            if txs is None:
                skipped += 1
                continue
            rows = []
            for tx in txs:
                trade = parse_trade(tx, tok["m"], tok["bc"])
                if trade:
                    trade["sig"] = signature_of(tx)
                    rows.append(trade)
                    _keep_log_sample(tx, trade)
            for i, row in enumerate(rows):
                row["i"] = i  # chain order, needed to replay the curve
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
            missing += stats.get("tx_missing", 0)
            fh.write(json.dumps({"k": "done", "v": VERSION, "m": tok["m"], "t0": tok["t0"], "n": len(rows),
                                 "window": window, **stats}) + "\n")
            fh.flush()
            trades_total += len(rows)
            if n % 10 == 0 or n == len(todo) or n <= 5:
                rate = n / max(time.time() - started, 1)
                log(f"  {n}/{len(todo)} tokens, {trades_total} trades, {rpc.calls} calls ({stats.get('method')}), "
                      f"~{(len(todo) - n) / rate / 60:.0f} min left, {skipped} skipped, {missing} tx not returned"
                      + (f", errors {rpc.errors}" if rpc.errors else ""))
    log(f"done: {trades_total} trades written to {OUT} (~{rpc.credits} Helius credits)")
    run.last_credits = rpc.credits
    return len(todo) - skipped


if __name__ == "__main__":
    main()
