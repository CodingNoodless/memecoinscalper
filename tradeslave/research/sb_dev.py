"""SB stage 1: profile one serial rugger's launches (Rohit's example dev) - his coins, the wallets that buy in the first
minute, who funded them and how fresh they are, and when and how the dev exits. Calibrates the general sybil-launch
detector (sb_detect). Read-only.

    uv run python -X utf8 -m tradeslave.research.sb_dev <dev wallet> [days]
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from ..paths import DATA_DIR
from ..scalper.trade_events import parse_trade_events
from .bw_test import gtfa
from .fl_test import CACHE, NONSYSTEM, funding, helius_url
from .rd_cc import first_trades

OUT = DATA_DIR / "sb"
EARLY_S = 60                                     # "early buyer": bought in the first minute
FRESH_S = 24 * 3600                              # "fresh wallet": first transaction under 24 h before its buy


def launches(url, dev, days):
    txs, _ = gtfa(url, dev, time.time() - days * 86400, time.time(), max_pages=40)
    out = {}
    for tx in txs:
        logs = (tx.get("meta") or {}).get("logMessages") or []
        if any("Instruction: Create" in x for x in logs):           # the mint: the dev's own buy, else the new token balance
            ms = {e["mint"] for e in parse_trade_events(logs) if e["user"] == dev and e["buy"]}
            ms = ms or {b["mint"] for b in (tx.get("meta") or {}).get("postTokenBalances") or [] if b.get("mint")}
            for m in ms:
                out.setdefault(m, tx["blockTime"])
    return out, txs


def main() -> None:
    dev, days = sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 7
    url = helius_url()
    OUT.mkdir(exist_ok=True)
    mints, txs = launches(url, dev, days)
    print(f"dev {dev[:6]}: {len(txs)} transactions in {days:g} days, {len(mints)} launches", flush=True)
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    coins = []
    for m, t0 in sorted(mints.items(), key=lambda x: x[1]):
        rows = first_trades(url, m, t0)
        rows.sort(key=lambda r: (r[0], r[1]))
        if not rows:
            continue
        early = [r for r in rows if r[2] != dev and r[3] and r[1] - t0 <= EARLY_S]
        ws = sorted({r[2] for r in early})
        need = [w for w in ws + [dev] if w not in cache]
        with ThreadPoolExecutor(8) as ex:
            for w, res in zip(need, ex.map(lambda w: funding(url, w, requests.Session()), need)):
                if "err" not in res:
                    cache[w] = res
        dev_f = (cache.get(dev) or {}).get("funder")
        sol = {w: sum(r[4] for r in early if r[2] == w) for w in ws}
        tot = sum(sol.values()) or 1e-9
        by_funder = {}
        for w in ws:
            f = (cache.get(w) or {}).get("funder")
            if f and f not in NONSYSTEM:
                by_funder.setdefault(f, []).append(w)
        top_f, top_ws = max(by_funder.items(), key=lambda x: sum(sol[w] for w in x[1]), default=(None, []))
        fresh = [w for w in ws if (cache.get(w) or {}).get("first") and (min(r[1] for r in early if r[2] == w) - cache[w]["first"]) < FRESH_S]
        linked = [w for w in ws if (cache.get(w) or {}).get("funder") in {dev, dev_f} - {None}]
        dev_in = sum(r[4] for r in rows if r[2] == dev and r[3])
        dev_out = sum(r[4] for r in rows if r[2] == dev and not r[3])
        dev_sells = [r[1] for r in rows if r[2] == dev and not r[3]]
        early_out = sum(r[4] for r in rows if r[2] in set(ws) and not r[3])
        early_in = sum(r[4] for r in rows if r[2] in set(ws) and r[3])
        others_in = sum(r[4] for r in rows if r[2] not in set(ws) | {dev} and r[3])
        peak = max(r[6] / r[7] for r in rows) / (rows[0][6] / rows[0][7])
        c = dict(m=m, t0=t0, n_rows=len(rows), early_wallets=len(ws), early_sol=round(tot, 2),
                 top_funder=top_f, top_funder_wallets=len(top_ws), top_funder_share=round(sum(sol[w] for w in top_ws) / tot, 3),
                 fresh_share=round(sum(sol[w] for w in fresh) / tot, 3), dev_linked_share=round(sum(sol[w] for w in linked) / tot, 3),
                 dev_in=round(dev_in, 2), dev_out=round(dev_out, 2), dev_first_sell_s=round(min(dev_sells) - t0) if dev_sells else None,
                 early_in=round(early_in, 2), early_out=round(early_out, 2), others_in=round(others_in, 2), peak_x=round(peak, 2))
        coins.append(c)
        print(f"{time.strftime('%m-%d %H:%M', time.localtime(t0))} {m[:6]} early {len(ws):2d} wallets {tot:5.1f} SOL | top funder "
              f"{len(top_ws)} wallets {c['top_funder_share']:.0%} | fresh {c['fresh_share']:.0%} | dev-linked {c['dev_linked_share']:.0%} | "
              f"dev {dev_in:.1f} in / {dev_out:.1f} out (first sell {c['dev_first_sell_s']} s) | early net {early_out - early_in:+.1f} | "
              f"others put in {others_in:.1f} | peak {peak:.1f}x", flush=True)
    CACHE.write_text(json.dumps(cache))
    (OUT / f"dev_{dev[:8]}.json").write_text(json.dumps(dict(dev=dev, days=days, coins=coins), indent=1))
    if coins:
        def med(k):
            v = [c[k] for c in coins if c[k] is not None]
            return statistics.median(v) if v else None
        print(f"\nmedians over {len(coins)} launches: early wallets {med('early_wallets')}, top funder share {med('top_funder_share')}, "
              f"fresh {med('fresh_share')}, dev-linked {med('dev_linked_share')}, dev first sell {med('dev_first_sell_s')} s, "
              f"others put in {med('others_in')} SOL; total dev + early net {sum(c['dev_out'] - c['dev_in'] + c['early_out'] - c['early_in'] for c in coins):+.1f} SOL")


if __name__ == "__main__":
    main()
