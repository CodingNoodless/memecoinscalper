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
import os
import random
import time
from pathlib import Path

import requests

from ..paths import DATA_DIR
from ..scalper.recorder import TICK_DIR, read_ticks

TRADE_DIR = DATA_DIR / "trades"
OUT = TRADE_DIR / "backfill.jsonl"
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"


def _env_rpc() -> str | None:
    url = os.environ.get("RESEARCH_RPC_URL")
    if url:
        return url
    env = Path(".env")
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("RESEARCH_RPC_URL="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                return value or None
    return None


class Rpc:
    def __init__(self, url: str, rps: float):
        self.url, self.min_gap, self.last = url, 1.0 / max(rps, 0.1), 0.0
        self.session = requests.Session()
        self.calls = 0

    def call(self, method: str, params: list):
        for attempt in range(8):
            wait = self.min_gap - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            self.calls += 1
            try:
                response = self.session.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                                             "params": params}, timeout=30)
            except requests.RequestException:
                time.sleep(1.5 * (attempt + 1))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(2.0 * (attempt + 1))
                continue
            try:
                body = response.json()
            except ValueError:
                time.sleep(1.0)
                continue
            if "error" in body:
                if body["error"].get("code") in (-32429, 429, -32005):
                    time.sleep(2.0 * (attempt + 1))
                    continue
                return None
            return body.get("result")
        return None


def account_keys(tx: dict) -> list[str]:
    message = tx["transaction"]["message"]
    keys = [k if isinstance(k, str) else k.get("pubkey") for k in message["accountKeys"]]
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    return keys + list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])


def _token_amount(balances, owner: str, mint: str) -> int:
    total = 0
    for b in balances or []:
        if b.get("owner") == owner and b.get("mint") == mint:
            total += int(b["uiTokenAmount"]["amount"])
    return total


def parse_trade(tx: dict, mint: str, curve: str) -> dict | None:
    """One pump.fun bonding-curve trade from a getTransaction result (json encoding)."""
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None
    keys = account_keys(tx)
    if curve not in keys:
        return None
    i = keys.index(curve)
    d_lamports = meta["postBalances"][i] - meta["preBalances"][i]
    d_tokens = (_token_amount(meta.get("postTokenBalances"), curve, mint)
                - _token_amount(meta.get("preTokenBalances"), curve, mint))
    if d_lamports == 0 or d_tokens == 0:
        return None
    return {"k": "trade", "t": float(tx.get("blockTime") or 0), "m": mint, "w": keys[0], "b": int(d_lamports > 0),
            "sol": abs(d_lamports) / 1e9, "tok": abs(d_tokens) / 1e6, "dv": d_lamports, "dc": d_tokens,
            "slot": tx.get("slot"), "src": "backfill"}


def signatures(rpc: Rpc, curve: str, t0: float, t_end: float, max_pages: int) -> list[str] | None:
    """Successful signatures touching `curve` with blockTime in [t0-30, t_end], oldest first."""
    found, before = [], None
    for _ in range(max_pages):
        options = {"limit": 1000}
        if before:
            options["before"] = before
        page = rpc.call("getSignaturesForAddress", [curve, options])
        if page is None:
            return None
        for item in page:
            bt = item.get("blockTime") or 0
            if item.get("err") is None and t0 - 30 <= bt <= t_end:
                found.append(item["signature"])
        if not page or len(page) < 1000 or (page[-1].get("blockTime") or 0) < t0 - 30:
            return list(reversed(found))
        before = page[-1]["signature"]
    return None  # too many later trades to page back to the launch


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


def done_mints(path: Path = OUT) -> set[str]:
    out = set()
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if '"k": "done"' in line or '"k":"done"' in line:
                    try:
                        out.add(json.loads(line)["m"])
                    except (ValueError, KeyError):
                        pass
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
    parser.add_argument("--max-pages", type=int, default=20, help="signature pages (1000 each) per token")
    parser.add_argument("--dry-run", action="store_true", help="only count tokens and estimate RPC calls")
    args = parser.parse_args()

    url = args.rpc or _env_rpc() or PUBLIC_RPC
    rps = args.rps or (3.0 if url == PUBLIC_RPC else 8.0)
    paths = sorted(TICK_DIR.glob("*.jsonl.gz"))
    toks = select_tokens(paths, args.since, args.until, args.min_updates)
    done = done_mints()
    todo = [t for t in toks if t["m"] not in done]
    if args.limit and len(todo) > args.limit:
        random.Random(7).shuffle(todo)
        todo = sorted(todo[:args.limit], key=lambda t: t["t0"])
    per_token = collections.Counter()
    for r in read_ticks(paths):
        if r.get("k") == "curve":
            per_token[r.get("m")] += 1
    est = sum(per_token[t["m"]] * 2 + 2 for t in todo)
    host = url.split("//", 1)[-1].split("/", 1)[0].split("?", 1)[0]
    print(f"{len(toks)} recorded tokens with >= {args.min_updates} updates; {len(done)} already done; "
          f"{len(todo)} to fetch. Estimated ~{est:,} RPC calls (~{est / rps / 60:.0f} min at {rps:g}/s) via {host}")
    if args.dry_run or not todo:
        return
    TRADE_DIR.mkdir(parents=True, exist_ok=True)
    rpc = Rpc(url, rps)
    started, trades_total, skipped = time.time(), 0, 0
    with OUT.open("a", encoding="utf-8") as fh:
        for n, tok in enumerate(todo, 1):
            sigs = signatures(rpc, tok["bc"], tok["t0"], tok["t0"] + args.window, args.max_pages)
            if sigs is None:
                skipped += 1
                continue
            rows = []
            for sig in sigs:
                tx = rpc.call("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 0,
                                                       "commitment": "confirmed"}])
                if tx:
                    trade = parse_trade(tx, tok["m"], tok["bc"])
                    if trade:
                        trade["sig"] = sig
                        rows.append(trade)
            for i, row in enumerate(rows):
                row["i"] = i  # chain order (getSignaturesForAddress order), needed to replay the curve
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
            fh.write(json.dumps({"k": "done", "m": tok["m"], "t0": tok["t0"], "n": len(rows), "window": args.window}) + "\n")
            fh.flush()
            trades_total += len(rows)
            if n % 10 == 0 or n == len(todo):
                rate = n / max(time.time() - started, 1)
                print(f"  {n}/{len(todo)} tokens, {trades_total} trades, {rpc.calls} calls, "
                      f"~{(len(todo) - n) / rate / 60:.0f} min left, {skipped} skipped", flush=True)
    print(f"done: {trades_total} trades written to {OUT}")


if __name__ == "__main__":
    main()
