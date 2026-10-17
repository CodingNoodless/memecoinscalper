"""History miner: fill in the exact chart of every launch the bot has logged,
in the background, on FREE RPC endpoints (no Helius credits, separate
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

More endpoints = proportionally faster: list extra free RPC endpoints (Alchemy,
QuickNode, Chainstack, dRPC, Ankr, ...) in .env as HISTORY_RPC_URLS, comma
separated, optionally with a per-endpoint rate as a URL fragment (fragments are
never sent to the server):

    HISTORY_RPC_URLS=https://solana-mainnet.g.alchemy.com/v2/KEY#rps=8,https://other.example/KEY

Each endpoint gets its own worker thread; the public RPC is always one of them.
A token one endpoint can't serve (pruned history, errors) goes to the next
endpoint before it is left for a later pass.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import queue
import random
import threading
import time
from pathlib import Path

from ..scalper.recorder import TICK_DIR, read_ticks
from .backfill_trades import HISTORY_OUT, TRADE_DIR, VERSION, done_mints, parse_trade, sampled_mints
from .chain import PUBLIC_RPC, Rpc, address_transactions, host_of, signature_of

SKIPS = TRADE_DIR / "history_skips.jsonl"
# An endpoint that fails this many tokens in a row is rested (rate-limited or out of free quota):
# grinding through its 429 back-offs once stalled a whole pass for 8 hours.
REST_AFTER_FAILS, REST_S = 4, 600.0
# Unbiased launch sample (launch-sniping research needs ALL launches, not just busy ones): each pass
# draws this many logged launches at random, whatever happened to them, and mines them first,
# interleaved with the busy queue. Quiet launches have few transactions, so they are cheap.
SAMPLE_PER_PASS = 100


def draw_sample(all_cands: list[dict], already: set[str], k: int, rng: random.Random) -> list[dict]:
    """k launches drawn uniformly from every logged launch not drawn before (mined or not)."""
    pool = [c for c in all_cands if c["m"] not in already]
    return rng.sample(pool, min(k, len(pool)))


def interleave(sample: list[dict], rest: list[dict]) -> list[dict]:
    """sample[0], rest[0], sample[1], rest[1], ... then whatever is left (each mint once)."""
    out, seen = [], set()
    for i in range(max(len(sample), len(rest))):
        for c in (sample[i] if i < len(sample) else None, rest[i] if i < len(rest) else None):
            if c is not None and c["m"] not in seen:
                seen.add(c["m"])
                out.append(c)
    return out


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


def endpoints(default_rps: float = 3.0, extra: str | None = None) -> list[tuple[str, float]]:
    """(url, req/s) for every endpoint the miner may use: the public RPC plus HISTORY_RPC_URLS."""
    if extra is None:
        extra = os.environ.get("HISTORY_RPC_URLS", "")
        if not extra and Path(".env").exists():
            for line in Path(".env").read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.strip().startswith("HISTORY_RPC_URLS="):
                    extra = line.split("=", 1)[1].strip().strip('"').strip("'")
    out, seen = [(PUBLIC_RPC, default_rps)], {PUBLIC_RPC}
    for item in (x.strip() for x in extra.split(",")):
        if not item:
            continue
        url, _, frag = item.partition("#")
        rps = default_rps
        if frag.startswith("rps="):
            try:
                rps = float(frag[4:])
            except ValueError:
                pass
        if url not in seen:
            seen.add(url)
            out.append((url, rps))
    return out


def _fetch(rpc, tok: dict, window: float, max_pages: int):
    """('ok', rows, stats) | ('skip', None, stats) | ('retry', None, stats)."""
    txs, stats = address_transactions(rpc, tok["bc"], tok["t0"] - 30, tok["t0"] + window, max_pages)
    if txs is None:
        return ("skip" if stats.get("why") == "too_many" else "retry"), None, stats
    if stats.get("tx_missing"):
        return "retry", None, stats            # incomplete chart (pruned history?): another endpoint / pass
    rows = []
    for tx in txs:
        trade = parse_trade(tx, tok["m"], tok["bc"])
        if trade:
            trade["sig"] = signature_of(tx)
            rows.append(trade)
    return "ok", rows, stats


def mine(rpcs, todo: list[dict], window: float, max_pages: int, log, stop_after_s: float = 0.0) -> int:
    """Mine `todo` with one worker thread per endpoint (`rpcs`: one Rpc or a list). Returns charts written."""
    rpcs = list(rpcs) if isinstance(rpcs, (list, tuple)) else [rpcs]
    TRADE_DIR.mkdir(parents=True, exist_ok=True)
    started = time.time()
    work: queue.Queue = queue.Queue()
    for tok in todo:
        work.put((tok, frozenset()))           # (token, endpoints that already failed it)
    lock = threading.Lock()
    state = {"n": 0, "trades": 0, "left": len(todo)}
    per_host: collections.Counter = collections.Counter()
    stop = threading.Event()
    hosts = [host_of(getattr(r, "url", "") or f"rpc{i}") for i, r in enumerate(rpcs)]

    with HISTORY_OUT.open("a", encoding="utf-8") as fh, SKIPS.open("a", encoding="utf-8") as skip_fh:

        def finish(tok, status, rows, stats, i) -> None:
            with lock:
                state["left"] -= 1
                if status == "skip":
                    skip_fh.write(json.dumps({"m": tok["m"], "t": time.time(), "why": "too_many"}) + "\n")
                    skip_fh.flush()
                if status != "ok":
                    return
                for j, row in enumerate(rows):
                    row["i"] = j
                    fh.write(json.dumps(row, separators=(",", ":")) + "\n")
                fh.write(json.dumps({"k": "done", "v": VERSION, "m": tok["m"], "t0": tok["t0"], "n": len(rows),
                                     "window": window, "src": "history_miner", "host": hosts[i], **stats}) + "\n")
                fh.flush()
                state["n"] += 1
                state["trades"] += len(rows)
                per_host[hosts[i]] += 1
                n = state["n"]
                if n % 10 == 0 or n <= 3:
                    rate = n / max(time.time() - started, 1) * 3600
                    errors = collections.Counter()
                    for r in rpcs:
                        errors.update(getattr(r, "errors", None) or {})
                    log(f"  {n} tokens mined ({state['trades']} trades), ~{rate:.0f} tokens/hour, "
                        f"{state['left']} left in this pass"
                        + (f" | by endpoint {dict(per_host)}" if len(rpcs) > 1 else "")
                        + (f" | RPC errors {dict(errors)}" if errors else ""))

        def worker(i: int, rpc) -> None:
            while not stop.is_set() and state["left"] > 0:
                if stop_after_s and time.time() - started > stop_after_s:
                    stop.set()
                    return
                try:
                    tok, failed = work.get(timeout=1.0)
                except queue.Empty:
                    continue
                if i in failed:                    # leave it for an endpoint that hasn't tried it
                    work.put((tok, failed))
                    time.sleep(0.5)
                    continue
                try:
                    status, rows, stats = _fetch(rpc, tok, window, max_pages)
                except Exception as exc:  # noqa: BLE001 - one bad token must not stop the pass
                    status, rows, stats = "retry", None, {"why": type(exc).__name__}
                if status == "retry":
                    fails[i] += 1
                    if fails[i] >= REST_AFTER_FAILS:
                        log(f"  {hosts[i]}: {fails[i]} tokens failed in a row ({stats.get('why') or 'missing transactions'})"
                            f"; resting it {REST_S / 60:.0f} min")
                        fails[i] = 0
                        if stop.wait(REST_S):
                            work.put((tok, failed))
                            return
                    tried = failed | {i}
                    if len(tried) < len(rpcs):
                        work.put((tok, tried))
                        continue
                    finish(tok, "retry", None, stats, i)     # every endpoint failed: try again next pass
                    time.sleep(5 if len(rpcs) == 1 else 1)   # RPC trouble: slow down a bit
                    continue
                fails[i] = 0
                finish(tok, status, rows, stats, i)

        fails = collections.Counter()
        if len(rpcs) == 1:
            worker(0, rpcs[0])
        else:
            threads = [threading.Thread(target=worker, args=(i, r), daemon=True, name=f"miner-{hosts[i]}")
                       for i, r in enumerate(rpcs)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
    return state["n"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rpc", default=None, help="one RPC HTTP URL only (default: public + HISTORY_RPC_URLS)")
    parser.add_argument("--rps", type=float, default=3.0, help="requests/s per endpoint without its own #rps=")
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
    urls = [(args.rpc, args.rps)] if args.rpc else endpoints(args.rps)
    rpcs = []
    for url, rps in urls:
        rpc = Rpc(url, rps)
        rpc.gtfa = False                  # per-signature fetches only: no bulk methods that cost credits
        rpc.attempts, rpc.fail_fast = 4, True   # a struggling endpoint hands the token on instead of grinding
        rpcs.append(rpc)
    log(f"history miner on {len(rpcs)} endpoint(s): " + ", ".join(f"{host_of(u)} at {r:g} req/s" for u, r in urls))
    rng = random.Random()
    while True:
        done, skip = done_mints(), skipped()
        cands = candidates(TICK_DIR)
        drawn = draw_sample(cands, sampled_mints("all"), SAMPLE_PER_PASS, rng)
        with HISTORY_OUT.open("a", encoding="utf-8") as fh:      # recorded at draw time: no survivor bias
            for c in drawn:
                fh.write(json.dumps({"k": "sample", "tag": "all", "m": c["m"], "t0": c["t0"], "src": "history_miner"}) + "\n")
        todo = interleave([c for c in drawn if c["m"] not in done and c["m"] not in skip],
                          [c for c in cands if c["m"] not in done and c["m"] not in skip])
        busy = sum(1 for c in todo if c["updates"] >= 5)
        log(f"pass: {len(todo)} logged launches without a chart ({busy} with >= 5 recorded updates), "
            f"{len(done)} already mined; {len(drawn)} drawn at random for the unbiased launch sample (mined first)")
        if todo:
            mine(rpcs, todo, args.window, args.max_pages, log, stop_after_s=0 if args.once else args.rescan_every_s)
        if args.once:
            break
        if not todo:
            time.sleep(args.rescan_every_s)


if __name__ == "__main__":
    main()
