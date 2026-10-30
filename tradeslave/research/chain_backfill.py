"""Chain backfill: full bonding-curve trade history (exact reserves) for every logged pump.fun create since Oct 21.

    uv run python -u -m tradeslave.research.chain_backfill            # runs until done; resumes from its output

Why: data v2 needs >= 14 days of unbiased launches (vault "Research datasets"); the pump.fun candle API is gone.
Measured 2026-10-30: Helius getTransactionsForAddress returns a token's whole curve history in ~0.3 s (one call for
most tokens, identical tx and trade counts to the public-RPC path on 7/7 tokens) vs ~57 s/token on the public RPC.

Universe: every create in data/c1/creates.jsonl (launch_watch create log, ~21-25k/day from Oct 21; outages on Oct 25-26
are known) with a bonding curve, mayhem excluded, older than 1 h. Processed in ONE fixed random order (seed 30), so
any prefix of the run is an unbiased random sample by creation time, rugs and dead tokens included.
Trades come from the TradeEvents in each transaction's logs (exact virtual reserves after the trade; the same decoder
as the live feed). Window: create - 30 s .. create + 7 days (the curve stops trading at graduation).

Keys: one worker per working Helius key in lab/endpoints.json (tools/endpoints.py; labels only, never keys). A key
that answers "max usage reached" is marked exhausted with its reset date and its worker stops; the others go on.
Output: data/chain/curve/part-<worker>.jsonl.gz, one token per line:
  {"m", "bc", "t0", "n_tx", "trades": [[slot, ts, user, buy, sol_lamports, tok_raw, vs, vt], ...]}
Graduates (curve reached ~115 SOL virtual = complete) also go to data/chain/graduates.jsonl {"m", "t_grad", "t0"} for
the GeckoTerminal pool-candle job (research.gecko_grads).
Progress: logs/chain_backfill.out and data/chain/curve/progress.json. Paper only: read-only RPC calls.
"""

from __future__ import annotations

import glob
import gzip
import json
import random
import sys
import threading
import time
from datetime import date

from ..paths import DATA_DIR, PROJECT_ROOT
from ..scalper.trade_events import parse_trade_events
from .chain import Rpc, address_transactions

OUT = DATA_DIR / "chain" / "curve"
CREATES = DATA_DIR / "c1" / "creates.jsonl"
FROM_T = 1792540800.0          # 2026-10-21 00:00 UTC
SEED = 30
WINDOW_S = 7 * 86400
GRAD_VS = 0.99 * 115.005e9                 # virtual SOL (lamports) when the curve completes
GRADS = DATA_DIR / "chain" / "graduates.jsonl"
ENDPOINTS = PROJECT_ROOT / "lab" / "endpoints.json"


def todo_list(done: set[str]) -> list[dict]:
    rows, seen = [], set()
    cutoff = time.time() - 3600
    with CREATES.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            m = r.get("m")
            if not m or m in seen or not r.get("bc") or r.get("mh") or not FROM_T <= r["t"] < cutoff:
                continue
            seen.add(m)
            rows.append({"m": m, "bc": r["bc"], "t0": float(r["t"])})
    rows.sort(key=lambda r: r["m"])
    random.Random(SEED).shuffle(rows)
    return [r for r in rows if r["m"] not in done]


def done_mints() -> set[str]:
    """Mints already fetched; also (re)writes graduates.jsonl from the parts so it is complete after a restart."""
    done, grads = set(), []
    for f in glob.glob(str(OUT / "part-*.jsonl.gz")):
        try:
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    done.add(d["m"])
                    g = next((t for t in d["trades"] if t[6] >= GRAD_VS), None)
                    if g:
                        grads.append({"m": d["m"], "t_grad": g[1], "t0": d["t0"]})
        except EOFError:                      # a part cut mid-write by a crash: complete lines were read
            pass
    GRADS.parent.mkdir(parents=True, exist_ok=True)
    GRADS.write_text("".join(json.dumps(g) + "\n" for g in grads), encoding="utf-8")
    return done


def helius_urls() -> list[tuple[str, str]]:
    """Working Helius keys, except the one in RESEARCH_RPC_URL (the m1-family paper bots read it: never blind them)."""
    import os
    sys.path.insert(0, str(PROJECT_ROOT))
    from tools.endpoints import configured, key_id, provider
    st = {e["label"]: e for e in json.loads(ENDPOINTS.read_text())} if ENDPOINTS.exists() else {}
    bots = key_id(os.environ["RESEARCH_RPC_URL"]) if os.environ.get("RESEARCH_RPC_URL") else None
    out, cnt = [], {}
    for u in configured():
        p = provider(u)
        cnt[p] = cnt.get(p, 0) + 1
        label = f"{p}-{cnt[p]}"
        if p == "helius" and st.get(label, {}).get("status") == "working" and key_id(u) != bots:
            out.append((label, u))
    return out


def mark_exhausted(label: str, err: str) -> None:
    with LOCK:
        rows = json.loads(ENDPOINTS.read_text())
        t = date.today()
        for r in rows:
            if r["label"] == label:
                r.update(status="exhausted", last_error=err[:80], checked=time.strftime("%Y-%m-%d %H:%M"),
                         reset_date=date(t.year + (t.month == 12), t.month % 12 + 1, 1).isoformat())
        ENDPOINTS.write_text(json.dumps(rows, indent=1))


LOCK = threading.Lock()


def trades_of(txs: list, mint: str) -> list[list]:
    out = []
    for tx in txs:
        if (tx.get("meta") or {}).get("err") is not None:
            continue
        for e in parse_trade_events((tx.get("meta") or {}).get("logMessages") or []):
            if e["mint"] == mint:
                out.append([tx.get("slot"), e["ts"], e["user"], int(e["buy"]), e["sol"], e["tok"], e["vs"], e["vt"]])
    return out


def worker(label: str, url: str, q: list, stats: dict) -> None:
    rpc = Rpc(url, 8.0)
    rpc.attempts = 4
    OUT.mkdir(parents=True, exist_ok=True)
    fh = gzip.open(OUT / f"part-{label}.jsonl.gz", "at", encoding="utf-8")
    n_since_flush = 0
    while True:
        with LOCK:
            if not q:
                break
            tok = q.pop()
        txs, st = address_transactions(rpc, tok["bc"], tok["t0"] - 30, tok["t0"] + WINDOW_S, 200)
        if txs is None:
            err = json.dumps(rpc.errors)[:200]
            if "429" in err or "max usage" in err.lower():
                from tools.endpoints import probe
                if probe(url)["status"] == "exhausted":
                    mark_exhausted(label, "429 max usage reached")
                    with LOCK:
                        q.append(tok)
                        stats[label]["status"] = "exhausted"
                    print(time.strftime("%H:%M:%S"), f"{label} exhausted; worker stops", flush=True)
                    break
            with LOCK:
                stats[label]["fail"] += 1
                if st.get("why") == "too_many":
                    stats[label]["too_many"] += 1
            continue
        tr = trades_of(txs, tok["m"])
        grad = next((t for t in tr if t[6] >= GRAD_VS), None)
        if grad:
            with LOCK, GRADS.open("a", encoding="utf-8") as g:
                g.write(json.dumps({"m": tok["m"], "t_grad": grad[1], "t0": tok["t0"]}) + "\n")
        fh.write(json.dumps({**tok, "n_tx": len(txs), "trades": tr}, separators=(",", ":")) + "\n")
        n_since_flush += 1
        if n_since_flush >= 50:
            fh.flush()
            n_since_flush = 0
        with LOCK:
            s = stats[label]
            s["ok"] += 1
            s["credits"] = rpc.credits
    fh.close()


def main() -> None:
    done = done_mints()
    q = todo_list(done)
    q.reverse()                                  # pop() takes from the end: keep the fixed random order
    keys = helius_urls()
    print(time.strftime("%H:%M:%S"), f"done {len(done)}, todo {len(q)}, working Helius keys {len(keys)}", flush=True)
    stats = {lab: {"ok": 0, "fail": 0, "too_many": 0, "credits": 0, "status": "working"} for lab, _ in keys}
    threads = [threading.Thread(target=worker, args=(lab, u, q, stats), daemon=True) for lab, u in keys]
    for t in threads:
        t.start()
    t0 = time.time()
    while any(t.is_alive() for t in threads):
        time.sleep(60)
        ok = sum(s["ok"] for s in stats.values())
        rate = ok / max(time.time() - t0, 1) * 3600
        summary = {"t": time.strftime("%Y-%m-%d %H:%M"), "done_before": len(done), "ok": ok, "left": len(q),
                   "tokens_per_h": round(rate), "eta_h": round(len(q) / rate, 1) if rate else None, "keys": stats}
        (OUT / "progress.json").write_text(json.dumps(summary, indent=1))
        print(time.strftime("%H:%M:%S"), f"ok {ok} left {len(q)} rate {rate:.0f}/h "
              + " ".join(f"{k}:{v['ok']}/{v['fail']}/{v['status'][:4]}/{v['credits']}cr" for k, v in stats.items()), flush=True)
    print("finished", flush=True)


if __name__ == "__main__":
    main()
