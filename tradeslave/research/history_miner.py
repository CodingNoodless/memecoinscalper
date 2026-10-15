"""History miner: fill in the exact chart of every launch the bot has logged,
in the background, on the FREE public Solana RPC (no Helius credits, separate
process, so the live scalper and the autopilot are never slowed down).

    uv run python -m tradeslave.research.history_miner            # runs until stopped
    uv run python -m tradeslave.research.history_miner --once     # one pass, then exit

Order: busiest tokens first (they make most of the training data), newest
first within each group; mayhem-mode tokens are skipped (the bot never trades
them). Each token: every successful transaction on its bonding curve in the
first 15 minutes (signatures, then one getTransaction each), parsed exactly
like the autopilot's backfill, written to data/trades/history.jsonl. The
autopilot's training picks these up automatically. Public RPC limits are about
40 requests per 10 s per method, so the miner paces itself at ~3 per second
and backs off on 429s.
"""

from __future__ import annotations

import argparse
import collections
import json
import time
from pathlib import Path

from ..scalper.recorder import TICK_DIR, read_ticks
from .backfill_trades import HISTORY_OUT, TRADE_DIR, VERSION, done_mints, parse_trade
from .chain import PUBLIC_RPC, Rpc, address_transactions, signature_of

SKIPS = TRADE_DIR / "history_skips.jsonl"


def candidates(tick_dir: Path, min_age_s: float = 1200.0) -> list[dict]:
    """Logged launches, busiest first (recorded curve updates), newest first within a group."""
    creates, updates = {}, collections.Counter()
    now = time.time()
    for r in read_ticks(sorted(tick_dir.glob("*.jsonl.gz"))):
        kind, mint = r.get("k"), r.get("m")
        if kind == "create" and r.get("bc"):
            t = float(r["t"])
            if now - t >= min_age_s and str(r.get("mh")) not in ("1", "True", "true"):
                creates[mint] = {"m": mint, "bc": r["bc"], "t0": t}
        elif kind == "curve":
            updates[mint] += 1
    out = list(creates.values())
    for c in out:
        c["updates"] = updates[c["m"]]
    tier = lambda c: 0 if c["updates"] >= 20 else 1 if c["updates"] >= 5 else 2 if c["updates"] >= 1 else 3
    out.sort(key=lambda c: (tier(c), -c["t0"]))
    return out


def _lock_file(path: Path):
    import os
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def skipped() -> set[str]:
    out = set()
    if SKIPS.exists():
        for line in SKIPS.open(encoding="utf-8"):
            try:
                out.add(json.loads(line)["m"])
            except (ValueError, KeyError):
                continue
    return out


def mine(rpc: Rpc, todo: list[dict], window: float, max_pages: int, log, stop_after_s: float = 0.0) -> int:
    TRADE_DIR.mkdir(parents=True, exist_ok=True)
    started, n_done, trades_total = time.time(), 0, 0
    with HISTORY_OUT.open("a", encoding="utf-8") as fh, SKIPS.open("a", encoding="utf-8") as skip_fh:
        for n, tok in enumerate(todo, 1):
            if stop_after_s and time.time() - started > stop_after_s:
                break
            txs, stats = address_transactions(rpc, tok["bc"], tok["t0"] - 30, tok["t0"] + window, max_pages)
            if txs is None:
                if stats.get("why") == "too_many":     # too much later activity to page back to the launch
                    skip_fh.write(json.dumps({"m": tok["m"], "t": time.time(), "why": "too_many"}) + "\n")
                    skip_fh.flush()
                else:                                  # RPC trouble: try again next pass, slow down a bit
                    time.sleep(5)
                continue
            if stats.get("tx_missing"):
                continue         # incomplete chart: leave it for the next pass
            rows = []
            for tx in txs:
                trade = parse_trade(tx, tok["m"], tok["bc"])
                if trade:
                    trade["sig"] = signature_of(tx)
                    rows.append(trade)
            for i, row in enumerate(rows):
                row["i"] = i
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
            fh.write(json.dumps({"k": "done", "v": VERSION, "m": tok["m"], "t0": tok["t0"], "n": len(rows),
                                 "window": window, "src": "history_miner", **stats}) + "\n")
            fh.flush()
            n_done += 1
            trades_total += len(rows)
            if n_done % 25 == 0 or n_done <= 3:
                rate = n_done / max(time.time() - started, 1) * 3600
                log(f"  {n_done} tokens mined ({trades_total} trades), ~{rate:.0f} tokens/hour, "
                    f"{len(todo) - n} left in this pass" + (f", RPC errors {rpc.errors}" if rpc.errors else ""))
    return n_done


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rpc", default=PUBLIC_RPC, help="RPC HTTP URL (default: free public mainnet)")
    parser.add_argument("--rps", type=float, default=3.0)
    parser.add_argument("--window", type=float, default=900.0)
    parser.add_argument("--max-pages", type=int, default=5, help="signature pages (1000 each) per token")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--rescan-every-s", type=float, default=1800.0, help="re-read the logged launches this often")
    args = parser.parse_args()

    def log(*parts):
        print(time.strftime("%Y-%m-%d %H:%M:%S"), *parts, flush=True)

    from ..paths import PROJECT_ROOT
    lock = _lock_file(PROJECT_ROOT / ".history_miner.lock")
    if lock is None:
        log("another history miner is already running; exiting")
        return
    rpc = Rpc(args.rpc, args.rps)
    rpc.gtfa = False                      # the public RPC has no bulk method
    log(f"history miner on {args.rpc.split('?')[0]} at {args.rps:g} req/s")
    while True:
        done, skip = done_mints(), skipped()
        todo = [c for c in candidates(TICK_DIR) if c["m"] not in done and c["m"] not in skip]
        busy = sum(1 for c in todo if c["updates"] >= 5)
        log(f"pass: {len(todo)} logged launches without a chart ({busy} with >= 5 recorded updates), "
            f"{len(done)} already mined")
        if todo:
            mine(rpc, todo, args.window, args.max_pages, log, stop_after_s=0 if args.once else args.rescan_every_s)
        if args.once:
            break
        if not todo:
            time.sleep(args.rescan_every_s)


if __name__ == "__main__":
    main()
