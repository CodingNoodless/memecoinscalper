"""Strategy R: 1-minute shocks on memecoin perpetuals (continuation or reversal), long AND short.

    uv run python -m tradeslave.research.perp_shock --fetch      # 1m klines, MONTHS below
    uv run python -m tradeslave.research.perp_shock              # design grid (before TEST_FROM)
    uv run python -m tradeslave.research.perp_shock --test       # judge the best design rule once

Data: Binance USD-M 1-minute klines from data.binance.vision (price source only; see perps_data).
Event: a minute whose return is <= -D or >= +D AND whose quote volume is >= V x its trailing 60-minute
median. Trade at the NEXT minute's open, in the direction of the shock (cont) or against it (rev),
exit at the close H minutes later. Cost: FEE per side (taker 0.05%, plus 0.02% slippage assumption).
One trade per coin per 30 minutes. Design = months before TEST_FROM, test = from TEST_FROM.
"""

from __future__ import annotations

import csv
import io
import itertools
import math
import statistics
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests

from ..paths import DATA_DIR
from .perps_data import BASE, SYMBOLS

OUT = DATA_DIR / "perps_1m"
MONTHS = ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]
TEST_FROM = 1782864000          # 2026-07-01 UTC (fixed before any result)
FEE = 0.0007


def fetch(sym: str) -> tuple[str, int]:
    rows = []
    for mo in MONTHS:
        r = requests.get(f"{BASE}/klines/{sym}USDT/1m/{sym}USDT-1m-{mo}.zip", timeout=120)
        if r.status_code != 200:
            continue
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            text = z.read(z.namelist()[0]).decode()
        for row in csv.reader(io.StringIO(text)):
            if row and row[0][:1].isdigit():
                t = int(row[0])
                t = t // 1000000 if t > 1e14 else t // 1000
                rows.append([t, float(row[1]), float(row[4]), float(row[7])])
    if rows:
        OUT.mkdir(parents=True, exist_ok=True)
        np.save(OUT / f"{sym}.npy", np.array(sorted(rows)))
    return sym, len(rows)


_PRE: dict = {}


def pre(a: np.ndarray):
    key = id(a)
    if key not in _PRE:
        t, o, c, qv = a[:, 0], a[:, 1], a[:, 2], a[:, 3]
        r = np.r_[0.0, c[1:] / c[:-1] - 1]                 # r[i] = return of minute i
        med = np.full(len(a), np.nan)
        med[60:] = np.median(np.lib.stride_tricks.sliding_window_view(qv, 60)[:-1], axis=1)   # trailing 60, excl. i
        gap_ok = np.r_[np.diff(t) == 60, False]             # minute i+1 exists right after i
        _PRE[key] = (t, o, c, qv, r, med, gap_ok)
    return _PRE[key]


def trades(a: np.ndarray, D: float, V: float, H: int, mode: str) -> list[tuple[float, float]]:
    t, o, c, qv, r, med, gap_ok = pre(a)
    n = len(a)
    cand = np.nonzero((np.abs(r) >= D) & gap_ok & (qv >= V * np.nan_to_num(med, nan=np.inf)))[0]
    out, last = [], -1e18
    for i in cand:
        if i < 61 or i + H >= n or t[i] - last < 1800:
            continue
        side = math.copysign(1, r[i]) * (1 if mode == "cont" else -1)
        out.append((t[i], side * (c[i + H] / o[i + 1] - 1) - 2 * FEE))
        last = t[i]
    return out


def stats(xs) -> str:
    if len(xs) < 5:
        return f"n={len(xs)}"
    m, sd = statistics.mean(xs), statistics.pstdev(xs)
    return (f"n={len(xs):5d} mean {m:+7.3%} med {statistics.median(xs):+7.3%} win {sum(x > 0 for x in xs) / len(xs):4.0%} "
            f"t {m / (sd / math.sqrt(len(xs))) if sd else 0:+5.2f}")


GRID = list(itertools.product((0.01, 0.02, 0.03, 0.05), (1, 3, 6), (1, 3, 5, 15, 60), ("cont", "rev")))


def main() -> None:
    if "--fetch" in sys.argv:
        with ThreadPoolExecutor(6) as ex:
            for sym, n in ex.map(fetch, SYMBOLS):
                print(sym, n, flush=True)
        return
    data = {p.stem: np.load(p) for p in OUT.glob("*.npy")}
    print(f"{len(data)} coins")
    res = []
    for D, V, H, mode in GRID:
        rows = [x for a in data.values() for tt, x in trades(a, D, V, H, mode) if tt < TEST_FROM]
        if len(rows) >= 50:
            res.append((statistics.mean(rows), (D, V, H, mode), rows))
    res.sort(key=lambda r: -r[0])
    print("design (before 2026-07), best rules (move, volume x, hold min, mode):")
    for m, prm, rows in res[:12]:
        print(f"  {prm}: {stats(rows)}")
    if "--test" in sys.argv and res:
        D, V, H, mode = res[0][1]
        per = [(s, tt, x) for s, a in data.items() for tt, x in trades(a, D, V, H, mode) if tt >= TEST_FROM]
        xs = [x for _, tt, x in sorted(per, key=lambda z: z[1])]
        print(f"\nTEST {res[0][1]}: {stats(xs)} | halves {stats(xs[:len(xs) // 2])} / {stats(xs[len(xs) // 2:])}")
        print(f"  2x fees: {stats([x - 2 * FEE for x in xs])}")
        bysym = {}
        for s, _, x in per:
            bysym.setdefault(s, []).append(x)
        print("  coins positive:", sum(statistics.mean(v) > 0 for v in bysym.values()), "/", len(bysym))


if __name__ == "__main__":
    main()
