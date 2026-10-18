"""Forward test of R3 (buy memecoin perps after a 1-minute crash, hold 120 min), frozen 2026-10-18.

    uv run python -m tradeslave.research.perp_forward            # score every published day since FORWARD_FROM
    uv run python -m tradeslave.research.perp_forward --loop     # re-check every 6 h

Rule (vault "2026-10-18 perp shock reversal (R)", R3): LONG after a minute with return <= -3% and quote
volume >= 3x its trailing 60-min median; enter at the next minute's open, exit at the close 120 min later;
one trade per coin per 30 min; 0.07%/side. Universe: research.perps_data.SYMBOLS (35 memecoins).
Binance publishes each day's 1-minute klines on data.binance.vision the next day; we download the day
plus the previous day (for the 60-minute volume baseline) and keep only trades entered on that day.
Results: data/perp_forward.jsonl (one row per trade) and a running summary. Paper only.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np

from ..paths import DATA_DIR
from .perp_rev2 import DAILY, _rows, clustered_t, pre, stats, trades
from .perps_data import SYMBOLS

FORWARD_FROM = date(2026, 10, 18)
RULE = (0.03, 3, 120, None, None)
OUT = DATA_DIR / "perp_forward.jsonl"


def day_klines(sym: str, d: date) -> np.ndarray | None:
    rows = []
    for dd in (d - timedelta(1), d, d + timedelta(1)):        # +1 day so 120-min holds can close
        rows += _rows(f"{DAILY}/klines/{sym}USDT/1m/{sym}USDT-1m-{dd.isoformat()}.zip")
    if not rows:
        return None
    return np.array(sorted({r[0]: r for r in rows}.values()))


def score_day(d: date) -> list[dict] | None:
    btc = day_klines("BTC", d)
    if btc is None or btc[-1, 0] < datetime(d.year, d.month, d.day, 23, 59, tzinfo=timezone.utc).timestamp():
        return None                                             # the day isn't fully published yet
    lo = datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()
    out = []
    bpre = pre(f"BTC{d}", btc)
    for sym in SYMBOLS:
        a = day_klines(sym, d)
        if a is None:
            continue
        for tt, x, side in trades(f"{sym}{d}", a, bpre, *RULE):
            if side > 0 and lo <= tt < lo + 86400:
                out.append({"day": d.isoformat(), "sym": sym, "t": tt, "ret": x})
    return out


def run_once() -> None:
    done = set()
    if OUT.exists():
        done = {json.loads(x)["day"] for x in OUT.read_text().splitlines() if x.strip()}
    d = FORWARD_FROM
    while d < date.today():
        if d.isoformat() not in done:
            rows = score_day(d)
            if rows is None:
                break
            with OUT.open("a", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r) + "\n")
                if not rows:
                    fh.write(json.dumps({"day": d.isoformat(), "sym": None, "t": None, "ret": None}) + "\n")
            print(f"{d}: {len(rows)} trades", flush=True)
        d += timedelta(1)
    rows = [json.loads(x) for x in OUT.read_text().splitlines()] if OUT.exists() else []
    rows = [r for r in rows if r["ret"] is not None]
    print(f"R3 forward since {FORWARD_FROM}: {stats([r['ret'] for r in rows])} | "
          f"{clustered_t([(r['t'], r['ret']) for r in rows])}")


def main() -> None:
    while True:
        run_once()
        if "--loop" not in sys.argv:
            return
        time.sleep(6 * 3600)


if __name__ == "__main__":
    main()
