"""Hourly candles + funding for memecoin perpetuals, from Binance's public data dump (data.binance.vision).

    uv run python -m tradeslave.research.perps_data

Binance is only the price source (its API is geo-blocked here, HTTP 451; the dump is not). Memecoin
prices on Binance, Hyperliquid and the big Solana DEX pools track each other closely, so these
candles stand in for whichever venue we trade on. Output: data/perps/<SYM>.json =
{"sym", "k": [[t_open_s, o, h, l, c, quote_vol], ...], "f": [[t_s, rate], ...]} (oldest first).
Caveat (survivorship): the list is memecoins listed TODAY; ones delisted earlier are missing.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor

import requests

from ..paths import DATA_DIR

OUT = DATA_DIR / "perps"
BASE = "https://data.binance.vision/data/futures/um/monthly"
SYMBOLS = ["DOGE", "1000PEPE", "1000SHIB", "1000BONK", "WIF", "1000FLOKI", "POPCAT", "MEW", "BOME", "1000SATS",
           "TURBO", "NEIRO", "PNUT", "GOAT", "MOODENG", "ACT", "FARTCOIN", "TRUMP", "PENGU", "SPX", "BRETT", "PEOPLE",
           "MEME", "1000CAT", "PONKE", "CHILLGUY", "AI16Z", "ZEREBRO", "GRIFFAIN", "DOGS", "NOT", "1000LUNC",
           "1000CHEEMS", "USELESS", "PUMP"]
REFERENCE = ["BTC", "ETH", "SOL"]   # not memecoins: lead-lag inputs, stored the same way
MONTHS = [f"{y}-{m:02d}" for y in range(2023, 2027) for m in range(1, 13) if f"{y}-{m:02d}" <= "2026-09"]


def _rows(url: str) -> list[list[str]]:
    r = requests.get(url, timeout=60)
    if r.status_code != 200:
        return []
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        text = z.read(z.namelist()[0]).decode()
    return [row for row in csv.reader(io.StringIO(text)) if row and row[0][:1].isdigit()]


def fetch(sym: str) -> tuple[str, int]:
    k, f = {}, {}
    for mo in MONTHS:
        for row in _rows(f"{BASE}/klines/{sym}USDT/1h/{sym}USDT-1h-{mo}.zip"):
            t = int(row[0]) // 1000 if int(row[0]) > 1e11 else int(row[0])
            t = t // 1000 if t > 1e11 else t
            k[t] = [t, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[7])]
        for row in _rows(f"{BASE}/fundingRate/{sym}USDT/{sym}USDT-fundingRate-{mo}.zip"):
            t = int(row[0]) // 1000
            f[t] = [t, float(row[2])]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{sym}.json").write_text(json.dumps({"sym": sym, "k": [k[t] for t in sorted(k)],
                                                  "f": [f[t] for t in sorted(f)]}))
    return sym, len(k)


def load() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(OUT.glob("*.json")) if p.stem not in REFERENCE]


def load_ref(sym: str) -> dict:
    return json.loads((OUT / f"{sym}.json").read_text())


def main() -> None:
    with ThreadPoolExecutor(8) as ex:
        for sym, n in ex.map(fetch, [s for s in SYMBOLS + REFERENCE if not (OUT / f"{s}.json").exists()]):
            print(f"{sym:12s} {n:6d} hourly candles", flush=True)


if __name__ == "__main__":
    main()
