"""Exact bonding-curve trades around every M-bot entry (traded coins and NC coins), from Helius history.

    uv run python -m tradeslave.research.ticks

One getTransactionsForAddress call returns up to 1,000 curve transactions (about 20 credits), so a coin costs a
call or two. Window per coin: 120 s before its first entry (bot trade or NC entry) to 900 s after its last.
Output: data/m1/ticks/<mint>.json.gz = {"m", "a", "b", "trades": [[slot, t, wallet, is_buy, sol, tokens]]},
chain order. SOL and tokens are the curve's own balance changes (fees excluded).
"""

from __future__ import annotations

import gzip
import json
from concurrent.futures import ThreadPoolExecutor

from ..paths import DATA_DIR
from .backfill_trades import parse_trade
from .chain import Rpc, address_transactions, env_rpc
from .nc_data import OUT as NC

OUT = DATA_DIR / "m1" / "ticks"
PRE, POST = 120, 900


def wanted() -> dict:
    """mint -> {"bc", "a", "b"}: window around all known entries."""
    w: dict = {}
    for v in ("m1", "m2", "m3"):
        for line in (DATA_DIR / v / "trades.jsonl").open(encoding="utf-8"):
            x = json.loads(line)
            if "ret" not in x or not x.get("bc"):
                continue
            r = w.setdefault(x["m"], {"bc": x["bc"], "a": x["t"], "b": x["t"]})
            r["a"], r["b"] = min(r["a"], x["t"]), max(r["b"], x["t"])
    bcs = {}
    for v in ("m1", "m2", "m3"):                       # NC coins: curve address from the bots' own records, if any
        for line in (DATA_DIR / v / "cands.jsonl").open(encoding="utf-8"):
            c = json.loads(line)
            if c.get("bc"):
                bcs[c["m"]] = c["bc"]
    for r in json.loads(NC.read_text()):
        bc = w.get(r["m"], {}).get("bc") or bcs.get(r["m"])
        if bc is None:
            continue
        x = w.setdefault(r["m"], {"bc": bc, "a": r["te"], "b": r["te"]})
        x["a"], x["b"] = min(x["a"], r["te"]), max(x["b"], r["te"])
    return w


def curve_of(m: str) -> str:
    """pump.fun bonding-curve PDA for a mint."""
    from solders.pubkey import Pubkey
    prog = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
    return str(Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(m))], prog)[0])


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    w = wanted()
    nc = json.loads(NC.read_text())
    for r in nc:                                       # NC coins the bots never bought: derive the curve address
        if r["m"] not in w:
            w[r["m"]] = {"bc": curve_of(r["m"]), "a": r["te"], "b": r["te"]}
    todo = [(m, x) for m, x in w.items() if not (OUT / f"{m}.json.gz").exists()]
    print(len(w), "coins;", len(todo), "to fetch", flush=True)
    rpc = Rpc(env_rpc(), 4.0)
    rpc.gtfa = None

    def one(item):
        m, x = item
        a, b = x["a"] - PRE, x["b"] + POST
        txs, st = address_transactions(rpc, x["bc"], a, b, 20)
        if txs is None:
            return m, st.get("why") or "fail"
        rows = []
        for tx in txs:
            t = parse_trade(tx, m, x["bc"])
            if t:                                      # sol is None when the curve's SOL change isn't visible
                rows.append([t["slot"], t["t"], t["w"], t["b"], t["sol"], t["tok"]])
        with gzip.open(OUT / f"{m}.json.gz", "wt", encoding="utf-8") as fh:
            json.dump({"m": m, "a": a, "b": b, "trades": rows}, fh)
        return m, len(rows)

    bad = 0
    with ThreadPoolExecutor(3) as ex:
        for i, (m, res) in enumerate(ex.map(one, todo), 1):
            bad += isinstance(res, str)
            if i % 50 == 0:
                print(f"  {i}/{len(todo)} (failed {bad}), credits ~{getattr(rpc, 'credits', '?')}", flush=True)
    print("done; failed", bad, "credits ~", getattr(rpc, "credits", "?"), flush=True)


if __name__ == "__main__":
    main()
