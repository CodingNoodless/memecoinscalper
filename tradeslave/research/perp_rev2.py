"""Strategy R2: fade one-minute volume shocks on memecoin perps, with a stop and a market filter.

    uv run python -m tradeslave.research.perp_rev2 --fetch     # 1m klines incl. high/low: monthly Mar-Sep + daily October
    uv run python -m tradeslave.research.perp_rev2             # design on 2026-03..09 (R1's design AND test months)
    uv run python -m tradeslave.research.perp_rev2 --test      # judge the frozen rule once on 2026-10-01..17 (unseen)

R1 (vault "2026-10-18 perp shock reversal (R)") failed its test: median +0.42%, 58% wins, mean +0.06%,
because a few shocks kept running. R2 adds an intrabar stop (fills at the stop price, or at the bar's open
if it gaps through) and an optional filter that skips shocks shared with BTC (market-wide moves).
Costs 0.07%/side (taker 0.05% + slippage 0.02%, assumption); stops pay one extra 0.03% slippage.
"""

from __future__ import annotations

import csv
import io
import itertools
import math
import statistics
import sys
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import numpy as np
import requests

from ..paths import DATA_DIR
from .perps_data import BASE, REFERENCE, SYMBOLS

OUT = DATA_DIR / "perps_1m_hl"
DAILY = "https://data.binance.vision/data/futures/um/daily"
MONTHS = ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]
DAYS = [(date(2026, 10, 1) + timedelta(d)).isoformat() for d in range(17)]
TEST_FROM = 1790812800            # 2026-10-01 UTC
FEE, STOP_SLIP = 0.0007, 0.0003


def _rows(url: str):
    r = requests.get(url, timeout=120)
    if r.status_code != 200:
        return []
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        text = z.read(z.namelist()[0]).decode()
    out = []
    for row in csv.reader(io.StringIO(text)):
        if row and row[0][:1].isdigit():
            t = int(row[0])
            t = t // 1000000 if t > 1e14 else t // 1000
            out.append([t, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[7])])
    return out


def fetch(sym: str):
    rows = []
    for mo in MONTHS:
        rows += _rows(f"{BASE}/klines/{sym}USDT/1m/{sym}USDT-1m-{mo}.zip")
    for d in DAYS:
        rows += _rows(f"{DAILY}/klines/{sym}USDT/1m/{sym}USDT-1m-{d}.zip")
    if rows:
        OUT.mkdir(parents=True, exist_ok=True)
        a = np.array(sorted({r[0]: r for r in rows}.values()))
        np.save(OUT / f"{sym}.npy", a)
    return sym, len(rows)


_PRE: dict = {}


def pre(sym: str, a: np.ndarray):
    if sym not in _PRE:
        t, qv = a[:, 0], a[:, 5]
        r = np.r_[0.0, a[1:, 4] / a[:-1, 4] - 1]
        med = np.full(len(a), np.nan)
        med[60:] = np.median(np.lib.stride_tricks.sliding_window_view(qv, 60)[:-1], axis=1)
        gap_ok = np.r_[np.diff(t) == 60, False]
        _PRE[sym] = (r, med, gap_ok, {int(x): i for i, x in enumerate(t)})
    return _PRE[sym]


def trades(sym, a, btc, D, V, H, stop, mkt):
    """[(t, net return, side)] fading shocks; mkt = max |BTC minute return| allowed (None = no filter)."""
    r, med, gap_ok, idx = pre(sym, a)
    rb, _, _, bidx = btc
    t, o, hi, lo, c = a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4]
    n = len(a)
    cand = np.nonzero((np.abs(r) >= D) & gap_ok & (a[:, 5] >= V * np.nan_to_num(med, nan=np.inf)))[0]
    out, last = [], -1e18
    for i in cand:
        if i < 61 or i + H >= n or t[i] - last < 1800:
            continue
        if mkt is not None:
            j = bidx.get(int(t[i]))
            if j is None or abs(rb[j]) > mkt:
                continue
        side = -math.copysign(1, r[i])
        px = o[i + 1]
        ret = None
        if stop:
            sp = px * (1 - stop) if side > 0 else px * (1 + stop)
            for k in range(i + 1, i + H + 1):
                if side > 0 and lo[k] <= sp:
                    fill = min(sp, o[k]) if k > i + 1 else sp
                    ret = fill / px - 1 - STOP_SLIP
                    break
                if side < 0 and hi[k] >= sp:
                    fill = max(sp, o[k]) if k > i + 1 else sp
                    ret = -(fill / px - 1) - STOP_SLIP
                    break
        if ret is None:
            ret = side * (c[i + H] / px - 1)
        out.append((t[i], ret - 2 * FEE, side))
        last = t[i]
    return out


def stats(xs) -> str:
    if len(xs) < 5:
        return f"n={len(xs)}"
    m, sd = statistics.mean(xs), statistics.pstdev(xs)
    return (f"n={len(xs):5d} mean {m:+7.3%} med {statistics.median(xs):+7.3%} win {sum(x > 0 for x in xs) / len(xs):4.0%} "
            f"t {m / (sd / math.sqrt(len(xs))) if sd else 0:+5.2f}")


def clustered_t(rows) -> str:
    cl = defaultdict(list)
    for tt, x in rows:
        cl[int(tt) // 3600].append(x)
    cm = [statistics.mean(v) for v in cl.values()]
    if len(cm) < 5:
        return "clusters < 5"
    return f"{len(cm)} hour-clusters, t {statistics.mean(cm) / (statistics.pstdev(cm) / math.sqrt(len(cm))):+.2f}"


GRID = list(itertools.product((0.015, 0.02, 0.03), (3, 6), (30, 60, 120), (None, 0.02, 0.04, 0.08), (None, 0.003)))


def load():
    data = {p.stem: np.load(p) for p in OUT.glob("*.npy")}
    btc = data.pop("BTC")
    for s in ("ETH", "SOL"):
        data.pop(s, None)
    return data, pre("BTC", btc)


def main() -> None:
    if "--fetch" in sys.argv:
        with ThreadPoolExecutor(8) as ex:
            for sym, n in ex.map(fetch, SYMBOLS + REFERENCE):
                print(sym, n, flush=True)
        return
    data, btc = load()
    res = []
    for D, V, H, stop, mkt in GRID:
        rows = [(tt, x) for s, a in data.items() for tt, x, _ in trades(s, a, btc, D, V, H, stop, mkt) if tt < TEST_FROM]
        if len(rows) >= 300:
            xs = [x for _, x in rows]
            h = len(rows) // 2
            srt = sorted(rows)
            m1 = statistics.mean(x for _, x in srt[:h])
            m2 = statistics.mean(x for _, x in srt[h:])
            res.append((min(m1, m2), (D, V, H, stop, mkt), xs, rows))
    res.sort(key=lambda r: -r[0])
    print("design 2026-03..09, ranked by the WORSE of its two time halves (rule: D, V, H, stop, BTC filter):")
    for worst, prm, xs, rows in res[:15]:
        print(f"  {prm}: {stats(xs)} | worse half {worst:+.3%} | {clustered_t(rows)}")
    if "--test" in sys.argv:
        prm = FROZEN
        rows = [(tt, x, sd) for s, a in data.items() for tt, x, sd in trades(s, a, btc, *prm) if tt >= TEST_FROM]
        xs = [x for _, x, _ in sorted(rows)]
        print(f"\nTEST (2026-10-01..17) {prm}: {stats(xs)} | {clustered_t([(tt, x) for tt, x, _ in rows])}")
        print(f"  halves {stats(xs[:len(xs) // 2])} / {stats(xs[len(xs) // 2:])}")
        print(f"  2x costs {stats([x - 2 * FEE for x in xs])}")
        print(f"  long {stats([x for _, x, sd in rows if sd > 0])} | short {stats([x for _, x, sd in rows if sd < 0])}")


FROZEN = (0.03, 3, 120, None, None)   # frozen 2026-10-18 from the design run, before any October look


if __name__ == "__main__":
    main()
