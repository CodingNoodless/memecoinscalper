"""Research data v2: chain-built 1-min candles + exact reserves for an unbiased sample of all launches since Oct 21.

    uv run python -m tradeslave.research.research_data_v2 status      # coverage so far (no files written)
    uv run python -m tradeslave.research.research_data_v2 build       # writes data/research/v2/{train,validation,holdout}

Sources: research.chain_backfill (every logged create since 2026-10-21 in a fixed random order: any prefix is an
unbiased sample, rugs and dead tokens included) -> research.candle_builder (curve phase, exact virtual reserves);
graduates get their PumpSwap candles from research.gecko_grads appended after graduation (reserves derived from price,
assumption: no LP changes; pool volume is USD, kept in field 9, field 6 = None for pool candles).
Split: by creation time, 60 / 20 / 20, a 24 h gap after each boundary (Rohit 2026-10-30: all new data uses 24 h
gaps), each split's candles cut at the next split's start. Holdout = most recent 20%, read only by a pre-registered
one-shot test (tools/gate_check.py --final). v1 (data/research/{train,validation,holdout}) stays read-only.
Row per token: {"m", "t0", "graduated", "t_grad", "rugged", "died", "n_trades", "c": [[t, o, h, l, c, vol_sol, sol_reserve,
phase, vol_usd], ...]}
"""

from __future__ import annotations

import glob
import gzip
import hashlib
import json
import math
import sys
import time

from ..paths import DATA_DIR
from .candle_builder import build, curve_rows_from_events
from .research_data import K_POOL

CURVE = DATA_DIR / "chain" / "curve"
POOLS = DATA_DIR / "chain" / "pool_candles"
OUT = DATA_DIR / "research" / "v2"
GAP_S = 24 * 3600
GRAD_VS = 0.99 * 115.005e9


def tokens():
    from .gzlines import records
    seen = set()
    for f in sorted(glob.glob(str(CURVE / "part-*.jsonl.gz"))):
        for d in records(f):
            if d.get("m") and d["m"] not in seen:
                seen.add(d["m"])
                yield d


def assemble(d: dict) -> dict | None:
    tr = sorted(d["trades"], key=lambda x: (x[0] or 0, x[1]))
    c = [x + [None] for x in build(curve_rows_from_events(tr))]
    g = next((x for x in tr if x[6] >= GRAD_VS), None)
    t_grad = g[1] if g else None
    pool_ok = None
    if g:
        p = POOLS / f"{d['m']}.json"
        pool_ok = p.exists()
        if pool_ok:
            pc = json.loads(p.read_text()).get("c") or []
            c = [x for x in c if x[0] <= t_grad] + [[t, o, h, lo, cl, None, round(math.sqrt(cl * K_POOL), 4), "pool", v]
                                                   for t, o, h, lo, cl, v in pc if t > t_grad and cl > 0]
    if not c:
        return {"m": d["m"], "t0": d["t0"], "graduated": bool(g), "t_grad": t_grad, "pool_candles": pool_ok,
                "rugged": False, "died": True, "n_trades": 0, "c": []}
    max_h = max(x[2] for x in c)
    rugged = any((i and x[4] <= 0.3 * c[i - 1][4]) or (x[1] and x[4] <= 0.3 * x[1]) for i, x in enumerate(c))
    died = c[-1][4] <= 0.2 * max_h or c[-1][0] < time.time() - GAP_S
    return {"m": d["m"], "t0": d["t0"], "graduated": bool(g), "t_grad": t_grad, "pool_candles": pool_ok,
            "rugged": rugged, "died": died, "n_trades": len(tr), "c": c}


def shares(part) -> dict:
    n = len(part)
    if not n:
        return {"tokens": 0}
    t0 = [x["t0"] for x in part]
    g = [x for x in part if x["graduated"]]
    return {"tokens": n, "t0_min": time.strftime("%Y-%m-%d %H:%M", time.gmtime(min(t0))),
            "t0_max": time.strftime("%Y-%m-%d %H:%M", time.gmtime(max(t0))), "graduated": len(g),
            "graduated_with_pool_candles": sum(bool(x["pool_candles"]) for x in g),
            "rugged_or_died_share": round(sum(x["rugged"] or x["died"] for x in part) / n, 4),
            "no_trade_share": round(sum(x["n_trades"] == 0 for x in part) / n, 4)}


def status() -> None:
    toks = [assemble(d) for d in tokens()]
    toks = [t for t in toks if t]
    s = shares(toks)
    days = (max(t["t0"] for t in toks) - min(t["t0"] for t in toks)) / 86400 if toks else 0
    print(json.dumps({**s, "creation_span_days": round(days, 2)}, indent=1))


def build_splits() -> dict:
    toks = sorted((t for t in (assemble(d) for d in tokens()) if t), key=lambda x: x["t0"])
    n = len(toks)
    b1, b2 = toks[int(n * 0.6)]["t0"], toks[int(n * 0.8)]["t0"]
    bounds = {"train": (-math.inf, b1, b1 + GAP_S), "validation": (b1 + GAP_S, b2, b2 + GAP_S),
              "holdout": (b2 + GAP_S, math.inf, math.inf)}
    man = {"built": time.strftime("%Y-%m-%d %H:%M"), "version": "v2", "gap_hours": 24,
           "sources": ["chain_backfill (Helius getTransactionsForAddress, creates since 2026-10-21, random order)",
                       "gecko_grads (GeckoTerminal PumpSwap 1-min candles for graduates)"],
           "universe": "random sample of logged pump.fun creates (data/c1/creates.jsonl), rugs and dead included",
           "all": shares(toks), "splits": {}}
    for name, (lo, hi, cut) in bounds.items():
        part = [x for x in toks if lo <= x["t0"] < hi]
        for x in part:
            x["c"] = [k for k in x["c"] if k[0] < cut]
        d = OUT / name
        d.mkdir(parents=True, exist_ok=True)
        f = d / "tokens.jsonl.gz"
        with gzip.open(f, "wt", encoding="utf-8") as fh:
            for x in part:
                fh.write(json.dumps(x, separators=(",", ":")) + "\n")
        info = shares(part)
        info.update(sha256=hashlib.sha256(f.read_bytes()).hexdigest(),
                    t0_from=None if lo == -math.inf else time.strftime("%Y-%m-%d %H:%M", time.gmtime(lo)),
                    t0_before=None if hi == math.inf else time.strftime("%Y-%m-%d %H:%M", time.gmtime(hi)))
        man["splits"][name] = info
    man["dropped_in_gaps"] = n - sum(s["tokens"] for s in man["splits"].values())
    (OUT / "manifest.json").write_text(json.dumps(man, indent=1))
    return man


if __name__ == "__main__":
    if sys.argv[1:] == ["status"]:
        status()
    elif sys.argv[1:] == ["build"]:
        m = build_splits()
        print(json.dumps({k: m["splits"][k] for k in m["splits"]}, indent=1))
