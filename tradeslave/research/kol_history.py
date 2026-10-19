"""Every swap of the Kolscan KOL wallets over the recorded days, from Solana RPC (free endpoints).

    uv run python -m tradeslave.research.kol_history <kols.txt> [--from 2026-10-13] [--to 2026-10-18]

Signatures for each wallet, then one getTransaction each (or Helius getTransactionsForAddress when available),
parsed from balance changes only: the wallet's SOL change and its token-balance changes. A transaction whose
only non-SOL change is one mint is a trade (token up = buy). Works for the curve, PumpSwap and any router.
Output: data/kol_trades.jsonl = {"w", "m", "t", "b", "sol", "tok", "sig"} (sol = |SOL change incl. fees|).
"""

from __future__ import annotations

import argparse
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..paths import DATA_DIR
from .chain import Rpc, address_transactions, signature_of
from .history_miner import endpoints

OUT = DATA_DIR / "kol_trades.jsonl"
WSOL = "So11111111111111111111111111111111111111112"


def parse(tx: dict, wallet: str):
    meta = tx.get("meta") or {}
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = [k if isinstance(k, str) else k.get("pubkey") for k in msg.get("accountKeys") or []]
    keys += (meta.get("loadedAddresses") or {}).get("writable", []) + (meta.get("loadedAddresses") or {}).get("readonly", [])
    if wallet not in keys:
        return None
    i = keys.index(wallet)
    sol = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9
    d = {}
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            if b.get("owner") == wallet:
                amt = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                d[b["mint"]] = d.get(b["mint"], 0.0) + sign * amt
    if WSOL in d:
        sol += d.pop(WSOL)
    d = {m: v for m, v in d.items() if abs(v) > 1e-9}
    if len(d) != 1:
        return None
    m, tok = next(iter(d.items()))
    if abs(sol) < 0.005 or (tok > 0) == (sol > 0):         # transfers / airdrops: no SOL moved
        return None
    return {"w": wallet, "m": m, "t": tx.get("blockTime"), "b": int(tok > 0), "sol": abs(sol), "tok": abs(tok),
            "sig": signature_of(tx)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("kols")
    ap.add_argument("--from", dest="t_from", default="2026-10-13")
    ap.add_argument("--to", dest="t_to", default="2026-10-18")
    a = ap.parse_args()
    ts = [datetime.fromisoformat(x).replace(tzinfo=timezone.utc).timestamp() for x in (a.t_from, a.t_to)]
    done = set()
    if OUT.exists():
        done = {json.loads(line)["w"] for line in OUT.open() if '"done"' in line}
    todo = [w for w in Path(a.kols).read_text().split() if w not in done]
    rpcs = [Rpc(url, rps) for url, rps in endpoints()]
    print(f"{len(todo)} wallets, {len(rpcs)} RPC endpoints", flush=True)
    lock = threading.Lock()

    def worker(rpc):
        while True:
            with lock:
                if not todo:
                    return
                w = todo.pop(0)
            txs, stats = address_transactions(rpc, w, ts[0], ts[1], max_pages=60)
            rows = [r for tx in txs or [] if (r := parse(tx, w))]
            with lock, OUT.open("a") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
                f.write(json.dumps({"w": w, "done": True, "n_tx": len(txs or []), "trades": len(rows),
                                    "ok": txs is not None, **{k: v for k, v in stats.items() if k != "sigs"}}) + "\n")
                print(f"  {w[:6]}: {len(txs or [])} tx, {len(rows)} trades, {stats}", flush=True)

    th = [threading.Thread(target=worker, args=(r,)) for r in rpcs]
    for t in th:
        t.start()
    for t in th:
        t.join()


if __name__ == "__main__":
    main()
