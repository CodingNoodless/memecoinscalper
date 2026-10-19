"""Strategy Z: Kalshi daily-high temperature brackets vs a public weather forecast (pre-registered;
vault: "2026-10-19 weather brackets (Z)").

    uv run python -m tradeslave.research.weather fetch      # Kalshi markets + candles, Open-Meteo forecasts
    uv run python -m tradeslave.research.weather design     # fit mu/sigma, pick EDGE on design only
    uv run python -m tradeslave.research.weather test       # judge the frozen rule once

Forecast: Open-Meteo previous-runs API, temperature_2m_previous_day1 (each hour's forecast from the run 24 h
earlier) at the NWS settlement station; forecast high = max over the local day. Decision at 06:00 local on the
target day using the Kalshi hourly candle ending then (yes bid / yes ask closes). Taker fee, held to settlement.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import requests

from ..paths import DATA_DIR

API = "https://api.elections.kalshi.com/trade-api/v2"
METEO = "https://previous-runs-api.open-meteo.com/v1/forecast"
OUT = DATA_DIR / "weather"
CITIES = {   # series: (lat, lon, tz) of the NWS climate-report station
    "KXHIGHNY": (40.7790, -73.9692, "America/New_York"),      # Central Park
    "KXHIGHCHI": (41.7842, -87.7553, "America/Chicago"),      # Midway
    "KXHIGHMIA": (25.7881, -80.3169, "America/New_York"),     # Miami Intl
    "KXHIGHAUS": (30.1831, -97.6799, "America/Chicago"),      # Austin Bergstrom
    "KXHIGHLAX": (33.9382, -118.3866, "America/Los_Angeles"),  # LAX
    "KXHIGHDEN": (39.8466, -104.6562, "America/Denver"),      # Denver Intl
    "KXHIGHPHIL": (39.8733, -75.2268, "America/New_York"),    # Philadelphia Intl
}
DECISION_H = 6
SPLIT = "2025-10-01"
START = "2024-01-01"                              # the previous-runs forecast archive starts in 2024
STD_OFFSET = {"America/New_York": -5, "America/Chicago": -6, "America/Denver": -7, "America/Los_Angeles": -8}
EDGES = (0.05, 0.10, 0.15, 0.20)
FROZEN = OUT / "rule.json"
MONTHS = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}


def get(s, url, params):
    for i in range(60):                           # Kalshi allows ~4.5 requests/s sustained (measured)
        try:
            r = s.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(min(0.25 * (i + 1), 5))
                continue
            return r.json()
        except Exception:
            time.sleep(2)
    raise RuntimeError(f"gave up on {url}")


def target_date(event: str) -> str:
    d = event.split("-")[1] if "-" in event else ""   # 25JAN15
    if len(d) != 7 or d[2:5] not in MONTHS:
        return ""                                 # odd legacy ticker: dropped (sorts before START)
    return f"20{d[:2]}-{MONTHS[d[2:5]]:02d}-{d[5:]}"


def fetch_markets(series):
    s, rows = requests.Session(), []
    for path in ("/historical/markets", "/markets"):
        cur = None
        while True:
            p = {"series_ticker": series, "limit": 1000}
            if path == "/markets":
                p["status"] = "settled"
            if cur:
                p["cursor"] = cur
            d = get(s, API + path, p)
            rows += [dict(m, hist=path.startswith("/hist")) for m in d.get("markets") or []]
            cur = d.get("cursor")
            if not d.get("markets") or not cur:
                break
    seen, out = set(), []
    for m in rows:
        if m["ticker"] not in seen and m.get("result") in ("yes", "no") and target_date(m["event_ticker"]) >= START:
            seen.add(m["ticker"])
            out.append(m)
    return out


def decision_ts(date: str, tz: str, hour=DECISION_H) -> int:
    """Local hour -> unix time with US DST (2nd Sunday of March .. 1st Sunday of November; no tzdata here)."""
    y, mo, d = map(int, date.split("-"))
    day = datetime(y, mo, d)
    mar = datetime(y, 3, 8) + timedelta(days=(6 - datetime(y, 3, 8).weekday()) % 7)
    nov = datetime(y, 11, 1) + timedelta(days=(6 - datetime(y, 11, 1).weekday()) % 7)
    off = STD_OFFSET[tz] + (1 if mar <= day < nov else 0)
    return int((day + timedelta(hours=hour - off) - datetime(1970, 1, 1)).total_seconds())


def px(c, side, key="close"):
    x = c.get(side) or {}
    v = x.get(key + "_dollars", x.get(key))
    return float(v) if v not in (None, "") else None


def fetch_candles(series, markets):
    tz = CITIES[series][2]
    s = requests.Session()

    def one(m):
        dts = decision_ts(target_date(m["event_ticker"]), tz)
        path = f"/historical/markets/{m['ticker']}/candlesticks" if m["hist"] else \
            f"/series/{series}/markets/{m['ticker']}/candlesticks"
        d = get(s, API + path, {"start_ts": dts - 4 * 3600, "end_ts": dts + 3600, "period_interval": 60})
        cs = {}
        for c in d.get("candlesticks") or []:
            cs[int(c["end_period_ts"])] = (px(c, "yes_bid"), px(c, "yes_ask"))
        return dict(t=m["ticker"], e=m["event_ticker"], r=m["result"], st=m.get("strike_type"),
                    lo=m.get("floor_strike"), hi=m.get("cap_strike"), dts=dts, c=cs)

    with ThreadPoolExecutor(2) as ex:
        return list(ex.map(one, markets))


def fetch_forecast(series, start, end):
    lat, lon, tz = CITIES[series]
    d = get(requests.Session(), METEO, {"latitude": lat, "longitude": lon, "start_date": start, "end_date": end,
                                        "hourly": "temperature_2m_previous_day1", "temperature_unit": "fahrenheit",
                                        "timezone": tz})
    hi = {}
    for t, v in zip(d["hourly"]["time"], d["hourly"]["temperature_2m_previous_day1"]):
        if v is not None:
            day = t[:10]
            hi[day] = max(hi.get(day, -999.0), v)
    return hi


def fetch():
    OUT.mkdir(parents=True, exist_ok=True)
    for series in CITIES:
        if (OUT / f"{series}.json").exists():
            continue
        ms = fetch_markets(series)
        dates = sorted({target_date(m["event_ticker"]) for m in ms})
        print(f"{series}: {len(ms)} settled markets, {len(dates)} days {dates[0]}..{dates[-1]}", flush=True)
        rows = fetch_candles(series, ms)
        fc = {}
        y0 = int(dates[0][:4])
        for y in range(y0, int(dates[-1][:4]) + 1):    # one request per year
            a, b = max(dates[0], f"{y}-01-01"), min(dates[-1], f"{y}-12-31")
            if a <= b:
                fc.update(fetch_forecast(series, a, b))
        (OUT / f"{series}.json").write_text(json.dumps({"markets": rows, "forecast": fc}))
        print(f"  candles {sum(bool(r['c']) for r in rows)}/{len(rows)}, forecast days {len(fc)}", flush=True)


# ---------------------------------------------------------------- model + backtest

def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def actual_of(event_rows):
    w = [r for r in event_rows if r["r"] == "yes"]
    if len(w) != 1:
        return None
    w = w[0]
    if w["st"] == "between":
        return (w["lo"] + w["hi"]) / 2
    if w["st"] == "greater":
        return w["lo"] + 1
    if w["st"] == "less":
        return w["hi"] - 1
    return None


def prob(r, m, sd):
    if r["st"] == "between":
        return phi((r["hi"] + 0.5 - m) / sd) - phi((r["lo"] - 0.5 - m) / sd)
    if r["st"] == "greater":
        return 1 - phi((r["lo"] + 0.5 - m) / sd)
    if r["st"] == "less":
        return phi((r["hi"] - 0.5 - m) / sd)
    return None


def fee(p):
    return math.ceil(0.07 * 10 * p * (1 - p) * 100 - 1e-9) / 100 / 10


def load():
    data = {}
    for series in CITIES:
        f = OUT / f"{series}.json"
        if not f.exists():
            continue
        d = json.loads(f.read_text())
        ev = {}
        for r in d["markets"]:
            ev.setdefault(r["e"], []).append(r)
        data[series] = (ev, d["forecast"])
    return data


def fit(data):
    params = {}
    for series, (ev, fc) in data.items():
        res = []
        for e, rows in ev.items():
            day = target_date(e)
            a = actual_of(rows)
            if day < SPLIT and a is not None and day in fc:
                res.append(a - fc[day])
        if len(res) > 30:
            params[series] = (statistics.mean(res), statistics.stdev(res), len(res))
    return params


def trades(data, params, edge, lo, hi, delay=0):
    out = []
    for series, (ev, fc) in data.items():
        if series not in params:
            continue
        mu, sd, _ = params[series]
        for e, rows in ev.items():
            day = target_date(e)
            if not (lo <= day < hi) or day not in fc:
                continue
            for r in rows:
                if r["st"] not in ("between", "greater", "less"):
                    continue
                c = r["c"].get(str(r["dts"] + delay * 3600))
                if not c or c[0] is None or c[1] is None:
                    continue
                bid, ask = c
                p = prob(r, fc[day] + mu, sd)
                win = r["r"] == "yes"
                if ask < 0.99 and p - ask - fee(ask) >= edge:
                    out.append((day, series, (1.0 if win else 0.0) - ask - fee(ask)))
                elif bid > 0.01 and (1 - p) - (1 - bid) - fee(1 - bid) >= edge:
                    out.append((day, series, (0.0 if win else 1.0) - (1 - bid) - fee(1 - bid)))
    return out


def stats(x):
    v = [t[2] for t in x]
    if len(v) < 2:
        return len(v), 0.0, 0.0
    m, s = statistics.mean(v), statistics.stdev(v)
    return len(v), m * 100, m / s * math.sqrt(len(v)) if s else 0.0


def design():
    data = load()
    params = fit(data)
    for k, (mu, sd, n) in params.items():
        print(f"{k:12s} forecast error: mean {mu:+.2f}F sd {sd:.2f}F (n {n})")
    best = None
    for edge in EDGES:
        n, m, t = stats(trades(data, params, edge, "", SPLIT))
        n1, m1, t1 = stats(trades(data, params, edge, "", SPLIT, delay=1))
        print(f"design EDGE {edge:.2f}: n {n} mean {m:+.2f}c t {t:+.2f} | +1h: n {n1} mean {m1:+.2f}c t {t1:+.2f}")
        if n >= 100 and (best is None or m > best[1]):
            best = (edge, m)
    if best:
        FROZEN.write_text(json.dumps({"edge": best[0], "params": params}))
        print("frozen:", best[0])


def test():
    data = load()
    fr = json.loads(FROZEN.read_text())
    params = {k: tuple(v) for k, v in fr["params"].items()}
    edge = fr["edge"]
    x = trades(data, params, edge, SPLIT, "9999")
    days = sorted({t[0] for t in x})
    mid = days[len(days) // 2] if days else SPLIT
    print(f"TEST EDGE {edge}: n {stats(x)[0]} mean {stats(x)[1]:+.2f}c t {stats(x)[2]:+.2f}")
    for lab, part in (("first half", [t for t in x if t[0] < mid]), ("second half", [t for t in x if t[0] >= mid])):
        print(f"  {lab}: n {stats(part)[0]} mean {stats(part)[1]:+.2f}c t {stats(part)[2]:+.2f}")
    d1 = trades(data, params, edge, SPLIT, "9999", delay=1)
    print(f"  +1h delay: n {stats(d1)[0]} mean {stats(d1)[1]:+.2f}c t {stats(d1)[2]:+.2f}")
    for series in CITIES:
        part = [t for t in x if t[1] == series]
        print(f"  {series:12s} n {stats(part)[0]} mean {stats(part)[1]:+.2f}c")
    if days:
        print(f"  trades/day {len(x) / len(days):.1f} over {len(days)} days")


def main() -> None:
    {"fetch": fetch, "design": design, "test": test}[sys.argv[1]]()


if __name__ == "__main__":
    main()
