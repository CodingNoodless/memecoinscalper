"""Long price paths (1-minute candles, priced in SOL, launch -> +6 h) for a RANDOM sample of launches.

    uv run python -m tradeslave.research.long_paths --n 1500

GeckoTerminal indexes the pump.fun bonding curve itself as a pool (dex "pump-fun", pool address = the
bonding-curve account), and the PumpSwap pool after graduation. Launches are drawn at random from every
create the recorder logged (no survivor or activity bias: the exact-trade set was mined busiest-first). Each gets both: data/long_paths/<mint>.json = {"m", "t0", "curve": [...],
"swap": [...]} with candles [t, o, h, l, c, vol].
"""

from __future__ import annotations

import argparse
import json
import random

from ..paths import DATA_DIR
from ..scalper.recorder import TICK_DIR, read_ticks
from .candles import Gecko
from .pda import pumpswap_pool_address

OUT = DATA_DIR / "long_paths"
HOURS = 6
GRAD_NEAR_SOL = 3.5e-7    # curve price per token near graduation (launch ~2.8e-8, graduation ~4.1e-7)


def candles(g: Gecko, pool: str, mint: str, t0: float) -> list:
    rows = {}
    before = t0 + HOURS * 3600
    for _ in range(1):                          # 1000 minutes > 6 h
        js = g.get(f"/pools/{pool}/ohlcv/minute",
                   {"aggregate": 1, "limit": 1000, "currency": "token", "token": mint, "before_timestamp": int(before)})
        for c in (((js or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []:
            rows[int(c[0])] = [float(x) for x in c]
    return [rows[t] for t in sorted(rows) if t >= t0 - 60]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3000)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    bc, t0s = {}, {}
    for r in read_ticks(sorted(TICK_DIR.glob("*.jsonl.gz"))):
        if r.get("k") == "create" and r.get("m") and r.get("bc") and not r.get("mh"):
            bc[r["m"]], t0s[r["m"]] = r["bc"], float(r["t"])
    mints = sorted(bc)
    random.Random(11).shuffle(mints)
    mints = [m for m in mints[: a.n] if not (OUT / f"{m}.json").exists()]
    print(f"{len(bc)} logged launches; fetching {len(mints)} at random", flush=True)
    g = Gecko()
    for i, m in enumerate(mints, 1):
        t0 = t0s[m]
        cur = candles(g, bc[m], m, t0)
        near_grad = cur and max(c[2] for c in cur) >= GRAD_NEAR_SOL       # only then can a PumpSwap pool exist
        swap = candles(g, pumpswap_pool_address(m), m, t0) if near_grad else []
        (OUT / f"{m}.json").write_text(json.dumps({"m": m, "t0": t0, "curve": cur, "swap": swap}))
        if i % 50 == 0:
            print(f"  {i}/{len(mints)} (429s {g.rate_limited})", flush=True)


if __name__ == "__main__":
    main()
