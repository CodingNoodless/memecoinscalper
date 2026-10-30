"""What do pump.fun / PumpSwap traders pay per transaction, and how often do their transactions fail? (cost model input)

    uv run python -m tradeslave.research.fee_probe --n 150

Samples the most recent signatures of each program (getSignaturesForAddress) and reads every transaction's meta:
total fee (base 5,000 lamports per signature + priority fee; Jito tips are separate transfers and are counted from SOL
sent to the 8 public Jito tip accounts) and whether it failed (meta.err). Read-only, one RPC endpoint from .env
(--rpc public|research; research = RESEARCH_RPC_URL). The fail rate comes from the
signature list (up to 1,000 per program, err field); fees from getTransaction on the first --n. Only hostnames are printed. Writes data/cost/fee_probe.json.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from urllib.parse import urlsplit

import requests

from .. import config  # noqa: F401  (loads .env)
from ..paths import DATA_DIR

PROGRAMS = {"pump": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
            "pumpswap": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"}
JITO_TIP = {"96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5", "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
            "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY", "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
            "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh", "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
            "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL", "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT"}
OUT = DATA_DIR / "cost" / "fee_probe.json"
LAMPORTS = 1e9


def rpc(url, method, params):
    for attempt in range(5):
        r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
        if r.status_code == 429:
            time.sleep(1 + attempt)
            continue
        r.raise_for_status()
        return r.json().get("result")
    return None


def jito_tip(tx) -> int:
    keys = [k if isinstance(k, str) else k.get("pubkey") for k in tx["transaction"]["message"]["accountKeys"]]
    pre, post = tx["meta"]["preBalances"], tx["meta"]["postBalances"]
    return sum(max(0, post[i] - pre[i]) for i, k in enumerate(keys) if k in JITO_TIP)


def quantiles(v):
    v = sorted(v)
    if not v:
        return {"n": 0}
    q = lambda p: v[min(len(v) - 1, int(p * len(v)))]  # noqa: E731
    return {"n": len(v), "p10": q(.1), "p50": q(.5), "p75": q(.75), "p90": q(.9), "mean": statistics.mean(v)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--rpc", choices=("public", "research"), default="public")
    a = ap.parse_args()
    url = os.getenv("RESEARCH_RPC_URL") if a.rpc == "research" else "https://api.mainnet-beta.solana.com"
    print("rpc host:", urlsplit(url).hostname)
    out = {"t": time.time(), "rpc_host": urlsplit(url).hostname}
    for name, prog in PROGRAMS.items():
        sigs = rpc(url, "getSignaturesForAddress", [prog, {"limit": 1000}]) or []
        sig_failed = sum(s.get("err") is not None for s in sigs)
        fees, tips, failed, seen = [], [], 0, 0
        for s in sigs[:a.n]:
            time.sleep(0.12)
            tx = rpc(url, "getTransaction", [s["signature"], {"maxSupportedTransactionVersion": 0, "encoding": "json"}])
            if not tx or not tx.get("meta"):
                continue
            seen += 1
            failed += tx["meta"]["err"] is not None
            fees.append(tx["meta"]["fee"] / LAMPORTS)
            tips.append(jito_tip(tx) / LAMPORTS)
        out[name] = {"sigs": len(sigs), "sig_fail_rate": sig_failed / len(sigs) if sigs else None, "txs": seen, "failed": failed, "fail_rate": failed / seen if seen else None,
                     "fee_sol": quantiles(fees), "fee_plus_tip_sol": quantiles([f + t for f, t in zip(fees, tips)]),
                     "share_with_jito_tip": sum(t > 0 for t in tips) / seen if seen else None}
        r = out[name]
        print(f"{name}: {len(sigs)} sigs, failed {(r['sig_fail_rate'] or 0):.1%}; {seen} txs, failed {failed} ({(r['fail_rate'] or 0):.1%}); fee SOL p50 {r['fee_sol'].get('p50', 0):.6f} "
              f"p90 {r['fee_sol'].get('p90', 0):.6f}; fee+tip p50 {r['fee_plus_tip_sol'].get('p50', 0):.6f} "
              f"p90 {r['fee_plus_tip_sol'].get('p90', 0):.6f}; jito share {(r['share_with_jito_tip'] or 0):.0%}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1))
    print("wrote", OUT.relative_to(DATA_DIR.parent))


if __name__ == "__main__":
    main()
