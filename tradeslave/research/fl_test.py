"""FL: wallet funding links at M5 entries (vault "2026-10-23 funding links (FL)", pre-registered 14:10 PDT).

    uv run python -u -m tradeslave.research.fl_test fetch     # funding lookups (cached in data/m1/funding.json)
    uv run python -u -m tradeslave.research.fl_test           # features, kept vs skipped, the pre-registered verdict

Funder of a wallet = in its first transaction (of its oldest 10) where its SOL rises >= 0.005, the account whose SOL
fell the most. Uses Helius getTransactionsForAddress (oldest first) on the HISTORY_RPC_URLS entry that answers.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from ..paths import DATA_DIR
from . import er_test
from .chain import _wanted_version

CACHE = DATA_DIR / "m1" / "funding.json"
TOP, FRESH_S, PUBLIC_N, PUBLIC_GAP = 20, 6 * 3600, 25, 1800
CLUSTER_MAX, CREATOR_MAX, FRESH_MAX = 0.15, 0.10, 0.50
MIN_SOL = 0.005


def helius_url():
    env = dict(x.split("=", 1) for x in Path(".env").read_text(encoding="utf-8").splitlines() if "=" in x and not x.startswith("#"))
    urls = [u.strip().strip('"') for u in env.get("HISTORY_RPC_URLS", "").split(",") if "helius" in u]
    for u in urls:
        try:
            r = requests.post(u, json={"jsonrpc": "2.0", "id": 1, "method": "getHealth"}, timeout=15)
            if r.status_code == 200 and r.json().get("result") == "ok":
                return u
        except (requests.RequestException, ValueError):
            continue
    raise SystemExit("no working Helius endpoint")


def funding(url, w, session):
    opt = {"transactionDetails": "full", "sortOrder": "asc", "limit": 10, "encoding": "json",
           "maxSupportedTransactionVersion": 0}
    for attempt in range(6):
        try:
            r = session.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "getTransactionsForAddress",
                                        "params": [w, opt]}, timeout=40)
            if r.status_code == 429:
                time.sleep(1 + 2 * attempt)
                continue
            body = r.json()
        except (requests.RequestException, ValueError):
            time.sleep(1 + attempt)
            continue
        if "error" in body:
            wanted = _wanted_version(body["error"])
            if wanted is not None and wanted > opt["maxSupportedTransactionVersion"]:
                opt["maxSupportedTransactionVersion"] = wanted      # the chain moved on: accept newer versions
                continue
            return {"err": body["error"].get("code")}
        data = (body.get("result") or {}).get("data") or []
        out = {"first": data[0]["blockTime"] if data else None, "funder": None, "t_fund": None}
        for tx in data:
            keys = tx["transaction"]["message"]["accountKeys"]
            keys = [k if isinstance(k, str) else k.get("pubkey") for k in keys]
            keys += (tx["meta"].get("loadedAddresses") or {}).get("writable", []) + \
                (tx["meta"].get("loadedAddresses") or {}).get("readonly", [])
            pre, post = tx["meta"]["preBalances"], tx["meta"]["postBalances"]
            if w not in keys[:len(pre)]:
                continue
            i = keys.index(w)
            if (post[i] - pre[i]) / 1e9 >= MIN_SOL:
                j = min((k for k in range(len(pre)) if k != i), key=lambda k: post[k] - pre[k], default=None)
                if j is not None and post[j] < pre[j]:
                    out.update(funder=keys[j], t_fund=tx["blockTime"])
                break
        return out
    return {"err": "retries"}


def holders_at(rows, k):
    bal = defaultdict(float)
    for r in rows[:k + 1]:
        bal[r[4]] += (r[5] or 0) if r[2] else -(r[5] or 0)
    top = sorted(((v, w) for w, v in bal.items() if v > 1), reverse=True)[:TOP]
    return [(w, v) for v, w in top], rows[0][4]


def feed_coin_rows(db, m):
    rr = db.execute("SELECT t, w, b, sol, tok FROM tr WHERE m=? ORDER BY slot, seq", (m,)).fetchall()
    return [[t, None, bool(b), sol, w, tok, None] for t, w, b, sol, tok in rr]


def samples():
    """[(label, part, ret, t_entry, [(wallet, balance)], creator)] for backtest entries and live M5/M6 trades."""
    er_test.resplit()
    coins, byid, ent = er_test.load()
    out = []
    for s in ent:
        rows = byid[s["m"]]["rows"]
        top, cr = holders_at(rows, s["k"])
        out.append(("bt", "train" if s["part"] in er_test.TRAIN else "valid", s["r"], rows[s["k"]][0], top, cr, s["m"]))
    db = sqlite3.connect(DATA_DIR / "feed" / "trades.sqlite")
    for bot in ("m5", "m6"):
        p = DATA_DIR / bot / "trades.jsonl"
        if not p.exists():
            continue
        for line in p.open():
            if not line.strip():
                continue
            x = json.loads(line)
            rows = feed_coin_rows(db, x["m"])
            k = max((i for i, r in enumerate(rows) if r[0] <= x["t"]), default=None)
            if k is None or k < 3:
                continue
            top, cr = holders_at(rows, k)
            out.append((bot, "live", x["ret"], x["t"], top, cr, x["m"]))
    return out


def fetch():
    url = helius_url()
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    cache = {w: v for w, v in cache.items() if "err" not in v}                # retry earlier failures
    wallets = sorted({w for *_, top, cr, _m in samples() for w in [cr] + [w for w, _ in top]} - set(cache))
    print(f"{len(wallets)} wallets to look up ({len(cache)} cached)", flush=True)
    lock, local = threading.Lock(), threading.local()
    gap, last = 1 / 6.0, [0.0]

    def one(w):
        with lock:
            wait = last[0] + gap - time.time()
            last[0] = max(time.time(), last[0] + gap)
        if wait > 0:
            time.sleep(wait)
        if not hasattr(local, "s"):
            local.s = requests.Session()
        return w, funding(url, w, local.s)

    done = 0
    with ThreadPoolExecutor(8) as ex:
        for w, res in ex.map(one, wallets):
            cache[w] = res
            done += 1
            if done % 250 == 0:
                CACHE.write_text(json.dumps(cache))
                print(f"  {done}/{len(wallets)}  errors {sum(1 for v in cache.values() if 'err' in v)}", flush=True)
    CACHE.write_text(json.dumps(cache))
    print(f"done: {len(cache)} wallets, errors {sum(1 for v in cache.values() if 'err' in v)}, "
          f"~{10 * done} Helius credits", flush=True)


def features(top, cr, t_entry, fund, public):
    tot = sum(v for _, v in top) or 1.0
    wal = dict(top)
    groups = defaultdict(list)
    for w in set(wal) | {cr}:
        f = fund.get(w) or {}
        if f.get("funder"):
            groups[f["funder"]].append((w, f.get("t_fund") or 0))
    best = 0.0
    for fu, ws in groups.items():
        if len(ws) < 2:
            continue
        if fu in public:                                      # CEX/service: only wallets funded within 30 min of each other
            ws = sorted(ws, key=lambda x: x[1])
            runs, cur = [], [ws[0]]
            for a, b in zip(ws, ws[1:]):
                cur = cur + [b] if b[1] - a[1] <= PUBLIC_GAP else (runs.append(cur) or [b])
            runs.append(cur)
            ws = max(runs, key=len)
            if len(ws) < 2:
                continue
        best = max(best, sum(wal.get(w, 0.0) for w, _ in ws) / tot)
    crf = (fund.get(cr) or {}).get("funder")
    linked = sum(v for w, v in top if w != cr and (fund.get(w) or {}).get("funder") in {cr, crf} - {None}) / tot
    fresh = sum(v for w, v in top if (fund.get(w) or {}).get("first") and t_entry - fund[w]["first"] < FRESH_S) / tot
    known = sum(v for w, v in top if (fund.get(w) or {}).get("funder")) / tot
    return dict(cluster_share=best, creator_linked=linked, fresh_share=fresh, known=known)


def summ(v):
    if not v:
        return "n   0" + " " * 24
    return f"n {len(v):3d} {statistics.mean(v):+6.1%} deep {sum(x <= -0.25 for x in v) / len(v):4.0%}"


def main() -> None:
    if "fetch" in sys.argv:
        fetch()
        return
    fund = json.loads(CACHE.read_text())
    S = samples()
    coins_by_funder = defaultdict(set)
    for *_, top, cr, m in S:
        for w in [cr] + [w for w, _ in top]:
            f = (fund.get(w) or {}).get("funder")
            if f:
                coins_by_funder[f].add(m)
    public = {f for f, ms in coins_by_funder.items() if len(ms) >= PUBLIC_N}
    rows = []
    for src, part, r, t, top, cr, m in S:
        f = features(top, cr, t, fund, public)
        f.update(src=src, part=part, ret=r)
        rows.append(f)
    print(f"{len(rows)} samples; public funders (>= {PUBLIC_N} coins): {len(public)}; "
          f"median known-funder share {statistics.median(f['known'] for f in rows):.0%}")
    for k in ("cluster_share", "creator_linked", "fresh_share"):
        print(f"{k:15s} medians: " + ", ".join(f"{p} {statistics.median(f[k] for f in rows if f['part'] == p):.2f}"
                                               for p in ("train", "valid", "live")))
    skip = lambda f: (f["cluster_share"] >= CLUSTER_MAX or f["creator_linked"] >= CREATOR_MAX  # noqa: E731
                      or f["fresh_share"] >= FRESH_MAX)
    singles = {"cluster >= 15%": lambda f: f["cluster_share"] >= CLUSTER_MAX,
               "creator_linked >= 10%": lambda f: f["creator_linked"] >= CREATOR_MAX,
               "fresh >= 50%": lambda f: f["fresh_share"] >= FRESH_MAX, "RULE (any)": skip}
    res = {}
    for name, c in singles.items():
        print(f"\n{name}")
        for p in ("train", "valid", "live"):
            g = [f for f in rows if f["part"] == p]
            kept, sk = [f["ret"] for f in g if not c(f)], [f["ret"] for f in g if c(f)]
            print(f"   {p:5s} base {summ([f['ret'] for f in g])} | kept {summ(kept)} | skipped {summ(sk)}")
            res[(name, p)] = (kept, sk, [f["ret"] for f in g])
    deep = lambda v: sum(x <= -0.25 for x in v) / len(v) if v else 0  # noqa: E731
    ok = True
    for p in ("train", "valid", "live"):
        kept, sk, base = res[("RULE (any)", p)]
        ok &= bool(sk) and deep(sk) > deep(kept)
        if p != "train":
            ok &= bool(kept) and statistics.mean(kept) >= statistics.mean(base) + 0.03
    allk = [f for f in rows if not skip(f)]
    ok &= (len(rows) - len(allk)) / len(rows) >= 0.15 and len(res[("RULE (any)", "train")][0]) >= 60
    print(f"\nFL verdict: {'PASS' if ok else 'FAIL'}  (skipped {(len(rows) - len(allk)) / len(rows):.0%} of entries)")


if __name__ == "__main__":
    main()
