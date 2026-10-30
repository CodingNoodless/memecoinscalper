"""PumpSwap 1-minute candles for every graduate the chain backfill finds (data v2, graduated-coin phase).

    uv run python -u -m tradeslave.research.gecko_grads          # loops: new graduates every 10 min, resumes

Graduates: data/chain/graduates.jsonl (written by research.chain_backfill: curve reached ~115 SOL virtual), so the
universe is every graduate of an unbiased random sample of all logged launches, post-graduation rugs included.
Candles: GeckoTerminal's free public API (`/pools/<pool>/ohlcv/minute`, priced in SOL via currency=token; measured
2026-10-30: 642 candles in one call), pool address derived on chain (research.pda: no lookup call), graduation ->
graduation + 48 h, fetched once the graduate is >= 48 h old (a younger one waits). The free limit acts like ~10
calls/min (research.candles), so this paces at one call per 6 s.
Output: data/chain/pool_candles/<mint>.json = {"m", "t_grad", "pool", "fetched", "c": [[t, o, h, l, c, vol_usd], ...]}.
Reserves for these candles are derived from price (sqrt(p x 85 x 206.9e6), assumption: no LP changes), as in v1.
"""

from __future__ import annotations

import json
import time

from ..paths import DATA_DIR
from .candles import Gecko, fetch_candles
from .pda import pumpswap_pool_address

GRADS = DATA_DIR / "chain" / "graduates.jsonl"
OUT = DATA_DIR / "chain" / "pool_candles"
AGE_S = 48 * 3600


def pending() -> list[dict]:
    if not GRADS.exists():
        return []
    seen, out = set(), []
    now = time.time()
    for line in GRADS.read_text(encoding="utf-8").splitlines():
        try:
            g = json.loads(line)
        except ValueError:
            continue
        if g["m"] in seen:
            continue
        seen.add(g["m"])
        if now - g["t_grad"] >= AGE_S and not (OUT / f"{g['m']}.json").exists():
            out.append(g)
    return sorted(out, key=lambda g: g["t_grad"])


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    g = Gecko()
    while True:
        todo = pending()
        print(time.strftime("%H:%M:%S"), f"{len(todo)} graduates to fetch, {len(list(OUT.glob('*.json')))} done", flush=True)
        for i, x in enumerate(todo, 1):
            pool = pumpswap_pool_address(x["m"])
            c = fetch_candles(g, pool, x["t_grad"] - 60, x["m"])
            (OUT / f"{x['m']}.json").write_text(json.dumps({"m": x["m"], "t_grad": x["t_grad"], "pool": pool,
                                                            "fetched": time.time(), "c": c}))
            if i % 50 == 0:
                print(time.strftime("%H:%M:%S"), f"{i}/{len(todo)} (429s so far {g.rate_limited})", flush=True)
        time.sleep(600)


if __name__ == "__main__":
    main()
