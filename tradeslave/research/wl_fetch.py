"""WL: pull the last 72 h of pump.fun curve trades made by each studied wallet (Helius getTransactionsForAddress, 2 h windows
so heavy wallets page past the cap). Keeps only TradeEvents whose user is the wallet.
Writes data/m1/wl/<wallet>.json = dict(trades=[[slot, ts, sig, mint, buy, sol, tok, vs, vt], ...], capped_windows=n).

    uv run python -u -m tradeslave.research.wl_fetch
"""

from __future__ import annotations

import json
import time

from ..paths import DATA_DIR
from ..scalper.trade_events import parse_trade_events
from .bw_test import gtfa
from .fl_test import helius_url

OUT = DATA_DIR / "m1" / "wl"
WALLETS = ["ARu4n5mFdZogZAravu7CcizaojWnS6oqka37gdLT5SZn", "DADDY96nGXz3unXJhBzWRFgmB5kwGkEi6e7VEodneZoy",
           "Bi4rd5FH5bYEN8scZ7wevxNZyNmKHdaBcvewdPFxYdLt", "Fsb6Ns75tgGRom5TJWK7ZA89XLPgCoyuhqeEdLgfbZfy",
           "EgYa22bNrcQGRJny4rGJz8eJe5CSFKtXEqB9qPQQGN9p", "5Y5nsbGfQSfTUSewkNfGUUrMe75ZdGqs8v5Nx5UX9Pge",
           "E2x1ewwXUwxABtFcssgxQLY6PrcRQuhLJRWPCR3WyeJb", "YARSciE3tFureJbF9YmeBSxY946V24DmeKhuV3AoyXW",
           "CzsxDQz6kPqrUoChLE1HYFJvj9HpgBrMys4vFFDqNdM5", "Grozoe2Lk9yu7kWPcCMuWEbrnuBYseBExkYAuHPRJUdJ",
           "ardinRsN1mNYVeoJWTBsWeYeXvuR9UUDGMsCDKpb6AT", "7djZvJGNvke9AwcBqZeUvoFVi7fT1kLW7x7BW6geCApC",
           "3pSaSmMTfds773Xdzka4q4K4ygxLFoQFZQwtB88nKpWa", "HNwEoMmVS3rSNWLT4omZ4D7VqNpD9TaDy2yN3MTpEm9H",
           "J6YEG5cmN3jbDy2BhMmvE59h9Ar8t84A9uYa5R5icKix", "2XUsvRRvLUqqU9rcu4hZH5Xcw56sceyps2VK19vsAqih",
           "387FRwow6MKDhSwuMULhKjqeXuxoBY4BbbaSntdk9LaH", "3hXxYe8XrxSn38RRE3u8xhB3d1rEhjYUTFRTx1Qr9r9A"]
HOURS, STEP = 72, 2 * 3600


def main() -> None:
    url = helius_url()
    OUT.mkdir(parents=True, exist_ok=True)
    end = time.time()
    for w in WALLETS:
        if (OUT / f"{w}.json").exists():
            continue
        trades, capped, ntx = [], 0, 0
        a = end - HOURS * 3600
        while a < end:
            txs, err = gtfa(url, w, a, min(end, a + STEP), max_pages=30)
            ntx += len(txs)
            capped += bool(err)
            for tx in txs:
                sig = (tx.get("transaction") or {}).get("signatures", [None])[0]
                for e in parse_trade_events((tx.get("meta") or {}).get("logMessages") or []):
                    if e["user"] == w:
                        trades.append([tx.get("slot"), e["ts"], sig, e["mint"], e["buy"], e["sol"] / 1e9, e["tok"] / 1e6, e["vs"] / 1e9, e["vt"] / 1e6])
            a += STEP
        (OUT / f"{w}.json").write_text(json.dumps(dict(trades=trades, capped_windows=capped, txs=ntx, t0=end - HOURS * 3600, t1=end)))
        print(f"{w[:8]}…: {ntx} txs, {len(trades)} own curve trades, {len({t[3] for t in trades})} coins, capped windows {capped}", flush=True)
    print("done")


if __name__ == "__main__":
    main()
