"""ST: identify streamers' wallets from coins they traded (Helius history), for the streamer copy test.

    uv run python -u -m tradeslave.research.st_find creator <mint>             -> signer of the mint's first transaction
    uv run python -u -m tradeslave.research.st_find common <mint> <mint> ...   -> wallets that bought every (or most) given coin(s)
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter

from ..paths import DATA_DIR
from .bw_test import gtfa
from .fl_test import helius_url

CACHE = DATA_DIR / "m1" / "st_buyers.json"


def signer(tx):
    keys = tx["transaction"]["message"]["accountKeys"]
    k = keys[0]
    return k if isinstance(k, str) else k.get("pubkey")


def buyers(url, mint):
    """{wallet: n buys} for one mint, from its full history."""
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    if mint in cache:
        return Counter(cache[mint])
    txs, err = gtfa(url, mint, 0, time.time(), max_pages=120)
    if err:
        print("  helius error", err.get("message") if isinstance(err, dict) else err)
    out = Counter()
    for tx in txs:
        meta = tx.get("meta") or {}
        pre = {(b.get("owner"), b.get("mint")): float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0) for b in meta.get("preTokenBalances") or []}
        post = {(b.get("owner"), b.get("mint")): float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0) for b in meta.get("postTokenBalances") or []}
        w = signer(tx)
        if post.get((w, mint), 0) > pre.get((w, mint), 0):
            out[w] += 1
    cache[mint] = dict(out)
    CACHE.write_text(json.dumps(cache))
    print(f"  {mint[:10]}…: {len(txs)} txs, {len(out)} buying wallets", flush=True)
    return out


def main() -> None:
    url = helius_url()
    if sys.argv[1] == "creator":
        txs, err = gtfa(url, sys.argv[2], 0, time.time(), max_pages=1, limit=5)
        if txs:
            print("first tx signer (creator):", signer(txs[0]), "| block time", time.strftime("%m-%d %H:%M:%S", time.localtime(txs[0].get("blockTime") or 0)))
        else:
            print("no history", err)
        return
    mints = sys.argv[2:]
    B = [buyers(url, m) for m in mints]
    cnt = Counter()
    for b in B:
        for w in b:
            cnt[w] += 1
    for k in range(len(mints), 1, -1):
        ws = [w for w, c in cnt.items() if c == k]
        print(f"wallets that bought {k} of the {len(mints)} coins: {len(ws)}")
        for w in ws[:15]:
            print("   ", w, [b.get(w, 0) for b in B])
        if ws:
            break


if __name__ == "__main__":
    main()
