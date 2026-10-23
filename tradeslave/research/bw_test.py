"""BW: the BOOST window after pump.fun migrations (vault "2026-10-23 BOOST window (BW)", pre-registered 12:46 PDT).

    uv run python -u -m tradeslave.research.bw_test fetch    # migrations + pool swaps (cached in data/m1/bw_cache.json.gz)
    uv run python -u -m tradeslave.research.bw_test          # strategies BW0-BW2 and the pre-registered verdict

Migrations = pump.fun MigrateV2 transactions touching the migration authority (39azUY...), Oct 21 00:00 to Oct 23
12:00 PDT. Pool spot price after every pool transaction = WSOL vault / token vault (post token balances owned by the
pool). Prices to migration + 600 s.
"""

from __future__ import annotations

import gzip
import json
import random
import statistics
import sys
import time
from collections import Counter

import requests

from ..paths import DATA_DIR
from .chain import _wanted_version
from .fl_test import helius_url

MIG_AUTH = "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"
WSOL = "So11111111111111111111111111111111111111112"
T_FROM = time.mktime(time.strptime("2026-10-21 00:00", "%Y-%m-%d %H:%M"))
T_TO = time.mktime(time.strptime("2026-10-23 12:00", "%Y-%m-%d %H:%M"))
CACHE = DATA_DIR / "m1" / "bw_cache.json.gz"
CAP, WINDOW, COST = 600, 600, 0.02
OPT = {"maxSupportedTransactionVersion": 1}


def gtfa(url, addr, a, b, details="full", limit=100, max_pages=40):
    out, tok = [], None
    for _ in range(max_pages):
        opt = {"transactionDetails": details, "sortOrder": "asc", "limit": limit, "encoding": "json",
               "maxSupportedTransactionVersion": OPT["maxSupportedTransactionVersion"],
               "filters": {"blockTime": {"gte": int(a), "lte": int(b)}, "status": "succeeded"}}
        if tok:
            opt["paginationToken"] = tok
        for attempt in range(8):
            try:
                r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "getTransactionsForAddress",
                                             "params": [addr, opt]}, timeout=60)
                if r.status_code == 429:
                    time.sleep(1 + attempt)
                    continue
                body = r.json()
            except (requests.RequestException, ValueError):
                time.sleep(1 + attempt)
                continue
            if "error" in body:
                w = _wanted_version(body["error"])
                if w and w > OPT["maxSupportedTransactionVersion"]:
                    OPT["maxSupportedTransactionVersion"] = opt["maxSupportedTransactionVersion"] = w
                    continue
                return out, body["error"]
            break
        else:
            return out, {"message": "retries"}
        res = body["result"]
        out += res.get("data") or []
        tok = res.get("paginationToken")
        if not tok:
            return out, None
    return out, {"message": "page cap"}


def keys_of(tx):
    la = tx["meta"].get("loadedAddresses") or {}
    return tx["transaction"]["message"]["accountKeys"] + la.get("writable", []) + la.get("readonly", [])


def pool_and_mint(tx):
    """(pool, mint) from a MigrateV2 tx: the owner holding both the token and WSOL in post token balances."""
    by_owner = {}
    for b in tx["meta"].get("postTokenBalances") or []:
        by_owner.setdefault(b.get("owner"), set()).add(b["mint"])
    for owner, mints in by_owner.items():
        if WSOL in mints and len(mints) == 2:
            return owner, next(m for m in mints if m != WSOL)
    return None, None


def spot(tx, pool, mint):
    s = t = None
    for b in tx["meta"].get("postTokenBalances") or []:
        if b.get("owner") == pool:
            amt = float(b["uiTokenAmount"].get("uiAmount") or 0)
            if b["mint"] == WSOL:
                s = amt
            elif b["mint"] == mint:
                t = amt
    return s / t if s and t else None


def fetch():
    url = helius_url()
    cache = json.loads(gzip.open(CACHE, "rt").read()) if CACHE.exists() else {}
    if "migrations" not in cache:
        migs, t = [], T_FROM
        while t < T_TO:                                          # 2 h chunks keep each listing small
            txs, err = gtfa(url, MIG_AUTH, t, min(t + 7200, T_TO))
            for tx in txs:
                if any("MigrateV2" in x for x in tx["meta"].get("logMessages") or []):
                    pool, mint = pool_and_mint(tx)
                    if pool:
                        migs.append({"t": tx["blockTime"], "pool": pool, "mint": mint})
            print(f"  {time.strftime('%m-%d %H:%M', time.localtime(t))}: {len(migs)} migrations so far"
                  + (f" (err {err})" if err else ""), flush=True)
            t += 7200
        cache["migrations"] = migs
        gzip.open(CACHE, "wt").write(json.dumps(cache))
    migs = cache["migrations"]
    random.Random(7).shuffle(migs)
    sample = sorted(migs[:CAP], key=lambda m: m["t"])
    print(f"{len(migs)} migrations found; sampling {len(sample)}", flush=True)
    paths = cache.setdefault("paths", {})
    for i, m in enumerate(sample):
        if m["pool"] in paths:
            continue
        txs, err = gtfa(url, m["pool"], m["t"], m["t"] + WINDOW)
        pts, buyers = [], Counter()
        for tx in txs:
            p = spot(tx, m["pool"], m["mint"])
            if p:
                pts.append((tx["blockTime"], p))
            buyers[keys_of(tx)[0]] += 1
        paths[m["pool"]] = {"t": m["t"], "mint": m["mint"], "pts": pts, "n": len(txs), "err": bool(err),
                            "top_payer": buyers.most_common(1)[0] if buyers else None}
        if (i + 1) % 25 == 0:
            gzip.open(CACHE, "wt").write(json.dumps(cache))
            print(f"  {i + 1}/{len(sample)} pools", flush=True)
    gzip.open(CACHE, "wt").write(json.dumps(cache))
    print("done", flush=True)


def price_at(pts, t):
    """Last spot at or before t (None before the first trade)."""
    p = None
    for ts, v in pts:
        if ts > t:
            break
        p = v
    return p


def summ(v):
    if not v:
        return "n   0"
    return (f"n {len(v):3d} mean {statistics.mean(v):+6.1%} median {statistics.median(v):+6.1%} "
            f"win {sum(x > 0 for x in v) / len(v):4.0%} <=-25% {sum(x <= -0.25 for x in v) / len(v):4.0%}")


def main() -> None:
    if "fetch" in sys.argv:
        fetch()
        return
    cache = json.loads(gzip.open(CACHE, "rt").read())
    P = sorted((p for p in cache["paths"].values() if p["pts"] and not p["err"]), key=lambda p: p["t"])
    print(f"{len(P)} migrations with prices (of {len(cache['paths'])} fetched, {len(cache['migrations'])} found)")
    strat = {"BW0 buy +2 s, sell +300 s": (2, 300), "BW1 buy +30 s, sell +300 s": (30, 300),
             "BW2 buy +2 s, sell +120 s": (2, 120), "(info) buy +2 s, sell +600 s": (2, 600)}
    half = len(P) // 2
    res = {}
    for name, (a, b) in strat.items():
        r = []
        for p in P:
            pa, pb = price_at(p["pts"], p["t"] + a), price_at(p["pts"], p["t"] + b)
            r.append(pb / pa - 1 - COST if pa and pb else None)
        ok = [x for x in r if x is not None]
        h1 = [x for x in r[:half] if x is not None]
        h2 = [x for x in r[half:] if x is not None]
        res[name] = (ok, h1, h2)
        print(f"{name:30s} all {summ(ok)}\n{'':30s} 1st half {summ(h1)}\n{'':30s} 2nd half {summ(h2)}")
    ok, h1, h2 = res["BW0 buy +2 s, sell +300 s"]
    passed = (len(ok) >= 200 and statistics.median(ok) > 0 and statistics.mean(h1) >= 0.03
              and statistics.mean(h2) >= 0.03)
    print(f"\nBW0 verdict: {'PASS' if passed else 'FAIL'}")


if __name__ == "__main__":
    main()
