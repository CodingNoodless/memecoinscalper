"""Binance USD-M positioning metrics (5-minute): open interest, top-trader and account long/short ratios,
taker buy/sell volume ratio, from data.binance.vision daily 'metrics' files.

    uv run python -m tradeslave.research.perp_metrics            # fetch FROM..TO for every symbol

Output: data/perp_metrics/<SYM>.npy rows [t, oi, oi_usd, top_acct_ls, top_pos_ls, acct_ls, taker_ls].
"""

from __future__ import annotations

import csv
import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np
import requests

from ..paths import DATA_DIR
from .perp_chart import NEWER
from .perps_data import SYMBOLS

OUT = DATA_DIR / "perp_metrics"
URL = "https://data.binance.vision/data/futures/um/daily/metrics/{s}USDT/{s}USDT-metrics-{d}.zip"
FROM, TO = date(2024, 7, 1), date(2026, 10, 16)


def day(sess: requests.Session, sym: str, d: date) -> list:
    for _ in range(3):
        try:
            r = sess.get(URL.format(s=sym, d=d.isoformat()), timeout=30)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            return []
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            text = z.read(z.namelist()[0]).decode()
        out = []
        for row in csv.reader(io.StringIO(text)):
            if not row or not row[0][:1].isdigit():
                continue
            t = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
            try:
                out.append([t] + [float(x) if x else np.nan for x in row[2:8]])
            except ValueError:
                continue
        return out
    return []


def fetch(sym: str) -> tuple[str, int]:
    if (OUT / f"{sym}.npy").exists():
        return sym, -1
    sess = requests.Session()
    days = [FROM + timedelta(i) for i in range((TO - FROM).days + 1)]
    rows = []
    with ThreadPoolExecutor(6) as ex:
        for r in ex.map(lambda d: day(sess, sym, d), days):
            rows += r
    if rows:
        np.save(OUT / f"{sym}.npy", np.array(sorted(rows)))
    return sym, len(rows)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(3) as ex:
        for sym, n in ex.map(fetch, SYMBOLS + NEWER + ["BTC"]):
            print(sym, n, flush=True)


if __name__ == "__main__":
    main()
