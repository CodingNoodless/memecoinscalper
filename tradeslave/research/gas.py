"""Strategy P1: Kalshi daily AAA gas-price markets vs a momentum model (pre-registered;
vault: "2026-10-19 daily gas price (P1)").

    uv run python -m tradeslave.research.gas fetch      # settled KXAAAGASD/AAAGASD markets + decision candles
    uv run python -m tradeslave.research.gas design     # rebuild AAA history, fit on design, pick EDGE
    uv run python -m tradeslave.research.gas test       # judge the frozen rule once
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import requests

from ..paths import DATA_DIR
from .weather import API, EDGES, decision_ts, fee, get, phi, px, target_date

OUT = DATA_DIR / "gas"
SERIES = ("KXAAAGASD", "AAAGASD")
DECISION_H, DELAY_H = 18, 21                      # ET on the day before the reading
FROZEN = OUT / "rule.json"
TZ = "America/New_York"


def strike(m):
    if m.get("floor_strike") is not None:
        return float(m["floor_strike"])
    try:
        return float(m["ticker"].rsplit("-", 1)[1])
    except ValueError:
        return None


def fetch():
    OUT.mkdir(parents=True, exist_ok=True)
    s, ms = requests.Session(), []
    for series in SERIES:
        for path in ("/historical/markets", "/markets"):
            cur = None
            while True:
                p = {"series_ticker": series, "limit": 1000}
                if path == "/markets":
                    p["status"] = "settled"
                if cur:
                    p["cursor"] = cur
                d = get(s, API + path, p)
                ms += [dict(m, hist=path.startswith("/hist"), series=series) for m in d.get("markets") or []]
                cur = d.get("cursor")
                if not d.get("markets") or not cur:
                    break
    seen, rows = set(), []
    for m in ms:
        if m["ticker"] in seen or m.get("result") not in ("yes", "no") or not target_date(m["event_ticker"]):
            continue
        seen.add(m["ticker"])
        rows.append(m)
    print(f"{len(rows)} settled markets, {len({m['event_ticker'] for m in rows})} events", flush=True)

    def one(m):
        day = date.fromisoformat(target_date(m["event_ticker"]))
        prev = (day - timedelta(days=1)).isoformat()
        d0, d1 = decision_ts(prev, TZ, DECISION_H), decision_ts(prev, TZ, DELAY_H)
        path = f"/historical/markets/{m['ticker']}/candlesticks" if m["hist"] else \
            f"/series/{m['series']}/markets/{m['ticker']}/candlesticks"
        d = get(s, API + path, {"start_ts": d0 - 3 * 3600, "end_ts": d1, "period_interval": 60})
        cs = {int(c["end_period_ts"]): (px(c, "yes_bid"), px(c, "yes_ask")) for c in d.get("candlesticks") or []}
        return dict(t=m["ticker"], e=m["event_ticker"], day=day.isoformat(), r=m["result"], k=strike(m),
                    st=m.get("strike_type"), d0=d0, d1=d1, c=cs)

    with ThreadPoolExecutor(2) as ex:
        out = list(ex.map(one, rows))
    (OUT / "markets.json").write_text(json.dumps(out))
    print(f"candles {sum(bool(r['c']) for r in out)}/{len(out)}")


def history(rows):
    by = {}
    for r in rows:
        if r["k"] is not None:
            by.setdefault(r["day"], []).append(r)
    v = {}
    for day, rs in by.items():
        yes = [r["k"] for r in rs if r["r"] == "yes"]
        no = [r["k"] for r in rs if r["r"] == "no"]
        if yes and no and min(no) > max(yes) and min(no) - max(yes) <= 0.0101:
            v[day] = (max(yes) + min(no)) / 2
    return v


def features(v, day):
    d = date.fromisoformat(day)
    seq = [v.get((d - timedelta(days=i)).isoformat()) for i in range(0, 9)]   # v_D, v_{D-1}, ...
    if any(x is None for x in seq[1:9]):
        return None
    deltas = [seq[i] - seq[i + 1] for i in range(1, 8)]                       # Δ_{D-1} .. Δ_{D-7}
    return seq[1], deltas[0], statistics.mean(deltas[1:])


def ols(X, y):
    """3-parameter least squares via normal equations."""
    n = len(X[0])
    A = [[sum(x[i] * x[j] for x in X) for j in range(n)] for i in range(n)]
    b = [sum(x[i] * t for x, t in zip(X, y)) for i in range(n)]
    for i in range(n):                                                        # Gauss-Jordan
        p = A[i][i]
        A[i] = [a / p for a in A[i]]
        b[i] /= p
        for j in range(n):
            if j != i:
                f = A[j][i]
                A[j] = [a - f * c for a, c in zip(A[j], A[i])]
                b[j] -= f * b[i]
    return b


def split_days(rows):
    days = sorted({r["day"] for r in rows})
    return days[int(len(days) * 0.6)]


def fit(v, cut):
    X, y = [], []
    for day in sorted(v):
        f = features(v, day)
        if day < cut and f and day in v:
            X.append([1.0, f[1], f[2]])
            y.append(v[day] - f[0])
    coef = ols(X, y)
    res = [t - sum(c * x for c, x in zip(coef, xx)) for xx, t in zip(X, y)]
    return coef, statistics.stdev(res), len(y)


def trades(rows, v, coef, sd, edge, lo, hi, delay=False):
    out = []
    for r in rows:
        if not (lo <= r["day"] < hi) or r["k"] is None or r["st"] not in (None, "greater"):
            continue
        f = features(v, r["day"])
        c = r["c"].get(str(r["d1"] if delay else r["d0"]))
        if not f or not c or c[0] is None or c[1] is None:
            continue
        mu = f[0] + coef[0] + coef[1] * f[1] + coef[2] * f[2]
        p = 1 - phi((r["k"] - mu) / sd)
        bid, ask = c
        win = r["r"] == "yes"
        if ask < 0.99 and p - ask - fee(ask) >= edge:
            out.append((r["day"], (1.0 if win else 0.0) - ask - fee(ask)))
        elif bid > 0.01 and (1 - p) - (1 - bid) - fee(1 - bid) >= edge:
            out.append((r["day"], (0.0 if win else 1.0) - (1 - bid) - fee(1 - bid)))
    return out


def stats(x):
    v = [t[1] for t in x]
    if len(v) < 2:
        return len(v), 0.0, 0.0
    m, s = statistics.mean(v), statistics.stdev(v)
    return len(v), m * 100, (m / s * math.sqrt(len(v))) if s else 0.0


def design():
    rows = json.loads((OUT / "markets.json").read_text())
    v = history(rows)
    cut = split_days(rows)
    coef, sd, n = fit(v, cut)
    print(f"AAA days rebuilt {len(v)}; design < {cut}: n {n}, coef {[round(c, 4) for c in coef]}, sd {sd * 100:.2f}c")
    best = None
    for edge in EDGES:
        a = stats(trades(rows, v, coef, sd, edge, "", cut))
        b = stats(trades(rows, v, coef, sd, edge, "", cut, delay=True))
        print(f"design EDGE {edge:.2f}: n {a[0]} mean {a[1]:+.2f}c t {a[2]:+.2f} | 21:00: n {b[0]} {b[1]:+.2f}c t {b[2]:+.2f}")
        if a[0] >= 50 and (best is None or a[1] > best[1]):
            best = (edge, a[1])
    if best:
        FROZEN.write_text(json.dumps({"edge": best[0], "coef": coef, "sd": sd, "cut": cut}))
        print("frozen:", best[0])


def test():
    rows = json.loads((OUT / "markets.json").read_text())
    v = history(rows)
    fr = json.loads(FROZEN.read_text())
    x = trades(rows, v, fr["coef"], fr["sd"], fr["edge"], fr["cut"], "9999")
    days = sorted({t[0] for t in x})
    mid = days[len(days) // 2] if days else fr["cut"]
    print(f"TEST EDGE {fr['edge']}: n {stats(x)[0]} mean {stats(x)[1]:+.2f}c t {stats(x)[2]:+.2f} over {len(days)} days")
    for lab, part in (("first half", [t for t in x if t[0] < mid]), ("second half", [t for t in x if t[0] >= mid])):
        print(f"  {lab}: n {stats(part)[0]} mean {stats(part)[1]:+.2f}c t {stats(part)[2]:+.2f}")
    d = trades(rows, v, fr["coef"], fr["sd"], fr["edge"], fr["cut"], "9999", delay=True)
    print(f"  21:00 ET: n {stats(d)[0]} mean {stats(d)[1]:+.2f}c t {stats(d)[2]:+.2f}")


def main() -> None:
    {"fetch": fetch, "design": design, "test": test}[sys.argv[1]]()


if __name__ == "__main__":
    main()
