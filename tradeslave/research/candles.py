"""1-minute candles of graduated tokens (PumpSwap pools) from GeckoTerminal's free API.

    uv run python -m tradeslave.research.candles            # fetch everything not fetched yet
    uv run python -m tradeslave.research.candles --limit 50

Universe: every graduation the scalper recorded (`migr` ticks). For each token we look up its
PumpSwap pool and page back through 1-minute candles (priced in SOL) from now to the
graduation. Free API: ~30 calls/minute, so this paces itself at one call every 2.1 s.
Output: data/candles/<mint>.json = {"m", "t_grad", "pool", "fetched", "c": [[t, o, h, l, c, vol_usd], ...]}
(oldest first). Minutes without trades have no candle.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import requests

from ..paths import DATA_DIR
from ..scalper.recorder import TICK_DIR, read_ticks
from .pda import pumpswap_pool_address

API = "https://api.geckoterminal.com/api/v2/networks/solana"
CANDLE_DIR = DATA_DIR / "candles"
CALL_GAP_S = 6.0   # measured: ~1 HTTP 429 per token at 3 s; the free limit acts like ~10 calls/min
SOL_ID = "solana_So11111111111111111111111111111111111111112"


class Gecko:
    def __init__(self, gap: float = CALL_GAP_S):
        self.gap, self.last, self.rate_limited = gap, 0.0, 0
        self.s = requests.Session()
        self.s.headers["Accept"] = "application/json"

    def get(self, path: str, params: dict | None = None, tries: int = 5):
        for k in range(tries):
            wait = self.last + self.gap - time.time()
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            try:
                r = self.s.get(f"{API}{path}", params=params, timeout=20)
            except requests.RequestException:
                time.sleep(5 * (k + 1))
                continue
            if r.status_code == 429:
                self.rate_limited += 1
                time.sleep(20 * (k + 1))
                continue
            if r.status_code == 404:
                return None
            if r.ok:
                return r.json()
            time.sleep(5 * (k + 1))
        return None


def graduations(tick_dir: Path = TICK_DIR) -> dict[str, float]:
    """mint -> graduation time, from the recorder's migration events."""
    out = {}
    for r in read_ticks(sorted(tick_dir.glob("*.jsonl.gz"))):
        if r.get("k") == "migr" and r.get("m"):
            out.setdefault(r["m"], float(r["t"]))
    return out


def pumpswap_pool(g: Gecko, mint: str) -> str | None:
    js = g.get(f"/tokens/{mint}/pools", {"page": 1})
    best, best_res = None, -1.0
    for p in (js or {}).get("data") or []:
        rel = p.get("relationships") or {}
        dex = ((rel.get("dex") or {}).get("data") or {}).get("id")
        pair = {((rel.get(k) or {}).get("data") or {}).get("id") for k in ("base_token", "quote_token")}
        res = float((p.get("attributes") or {}).get("reserve_in_usd") or 0)
        if dex == "pumpswap" and pair == {f"solana_{mint}", SOL_ID} and res > best_res:
            best, best_res = p["attributes"]["address"], res
    return best


WINDOW_H = 48.0     # candles from graduation to graduation + 48 h (what the runner rules use)


def fetch_candles(g: Gecko, pool: str, since: float, mint: str, max_pages: int = 4) -> list[list[float]]:
    rows = {}
    before = since + WINDOW_H * 3600 if since + WINDOW_H * 3600 < time.time() - 60 else None
    for _ in range(max_pages):
        params = {"aggregate": 1, "limit": 1000, "currency": "token", "token": mint}   # our token, priced in SOL
        if before:
            params["before_timestamp"] = int(before)
        js = g.get(f"/pools/{pool}/ohlcv/minute", params)
        lst = (((js or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        if not lst:
            break
        for c in lst:
            rows[int(c[0])] = [float(x) for x in c]
        oldest = min(int(c[0]) for c in lst)
        if oldest <= since or len(lst) < 1000:
            break
        before = oldest
    return [rows[t] for t in sorted(rows)]


def run(limit: int = 0, min_age_h: float = 6.0, log=print) -> int:
    CANDLE_DIR.mkdir(parents=True, exist_ok=True)
    grads = graduations()
    now = time.time()
    todo = [(m, t) for m, t in sorted(grads.items(), key=lambda x: x[1])
            if now - t >= min_age_h * 3600 and not (CANDLE_DIR / f"{m}.json").exists()]
    # Alternate oldest / newest so both the design and the test period (research.runners) fill up together.
    todo = [x for pair in zip(todo, reversed(todo)) for x in pair][:len(todo)]
    todo = list(dict.fromkeys(todo))
    if limit:
        todo = todo[:limit]
    log(f"{len(grads)} recorded graduations; {len(todo)} to fetch (older than {min_age_h:g} h)")
    g, n = Gecko(), 0
    for i, (mint, t_grad) in enumerate(todo, 1):
        pool = pumpswap_pool_address(mint)             # canonical pool, derived: saves an API call
        candles = fetch_candles(g, pool, t_grad, mint)
        if not candles:                                 # not the canonical pool: ask the API
            pool = pumpswap_pool(g, mint)
            candles = fetch_candles(g, pool, t_grad, mint) if pool else []
        (CANDLE_DIR / f"{mint}.json").write_text(json.dumps(
            {"m": mint, "t_grad": t_grad, "pool": pool, "fetched": time.time(), "c": candles}))
        n += 1
        if i % 25 == 0 or i <= 3:
            log(f"  {i}/{len(todo)} tokens, last: {len(candles)} candles, 429s so far {g.rate_limited}")
    return n


def load(min_candles: int = 1) -> list[dict]:
    out = []
    for p in sorted(CANDLE_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text())
        except ValueError:
            continue
        if len(d.get("c") or []) >= min_candles:
            out.append(d)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-age-h", type=float, default=6.0, help="only tokens graduated at least this long ago")
    a = ap.parse_args()
    run(a.limit, a.min_age_h, log=lambda *x: print(time.strftime("%H:%M:%S"), *x, flush=True))


if __name__ == "__main__":
    main()
