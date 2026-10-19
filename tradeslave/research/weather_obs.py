"""Strategy Z2: Kalshi daily-high brackets vs live station observations (pre-registered;
vault: "2026-10-19 weather same-day observations (Z2)").

    uv run python -m tradeslave.research.weather_obs fetch     # ASOS obs (IEM) + afternoon Kalshi candles
    uv run python -m tradeslave.research.weather_obs design
    uv run python -m tradeslave.research.weather_obs test

Needs `weather fetch` first (it lists the settled markets). Running max M_H = max ASOS tmpf from local
midnight to H (reports before H only). Z2a buys No on brackets already below M_H - MARGIN; Z2b prices brackets
at 16:00 from the design-period distribution of (final max - M_16) per city and season.
"""

from __future__ import annotations

import csv
import io
import json
import math
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import requests

from ..paths import DATA_DIR
from .weather import API, CITIES, OUT as WOUT, SPLIT, START, STD_OFFSET, decision_ts, fee, get, px, target_date

OUT = DATA_DIR / "weather_obs"
IEM = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
STATION = {"KXHIGHNY": "NYC", "KXHIGHCHI": "MDW", "KXHIGHMIA": "MIA", "KXHIGHAUS": "AUS", "KXHIGHLAX": "LAX",
           "KXHIGHDEN": "DEN", "KXHIGHPHIL": "PHL"}
HOURS = (12, 14, 16)
MARGINS = (1, 2, 3)
EDGES_B = (0.05, 0.10, 0.15)
FROZEN = OUT / "rule.json"
HIST_CUTOFF = "2026-08-19"                       # Kalshi /historical/cutoff: settled before 2026-08-20


def fetch_obs(series):
    st, tz = STATION[series], CITIES[series][2]
    rows = []
    for y in range(int(START[:4]), date.today().year + 1):
        r = requests.get(IEM, params={"station": st, "data": "tmpf", "year1": y, "month1": 1, "day1": 1,
                                      "year2": y + 1, "month2": 1, "day2": 2, "tz": "Etc/UTC",
                                      "format": "onlycomma", "missing": "empty"}, timeout=300)
        for rec in csv.DictReader(io.StringIO(r.text)):
            if rec.get("tmpf"):
                t = datetime.strptime(rec["valid"], "%Y-%m-%d %H:%M")
                rows.append(((t - datetime(1970, 1, 1)).total_seconds(), float(rec["tmpf"])))
    rows.sort()
    by = defaultdict(list)                                              # local day -> [(utc ts, tmpf)]
    for ts, v in rows:
        u = datetime(1970, 1, 1) + timedelta(seconds=ts)
        day = u.date().isoformat()
        off = (decision_ts(day, tz, 0) - int((datetime(u.year, u.month, u.day) - datetime(1970, 1, 1))
                                                 .total_seconds())) // 3600   # hours to add to local -> UTC
        loc = (u - timedelta(hours=off)).date().isoformat()
        by[loc].append((ts, v))
    return by


def running_max(obs, cutoff):
    vals = [v for ts, v in obs if ts < cutoff]
    return max(vals) if vals else None


def fetch():
    OUT.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    for series in CITIES:
        dst = OUT / f"{series}.json"
        if dst.exists() or not (WOUT / f"{series}.json").exists():
            continue
        obs = fetch_obs(series)
        tz = CITIES[series][2]
        mk = json.loads((WOUT / f"{series}.json").read_text())["markets"]
        days = {}
        for day, o in obs.items():
            m = {h: running_max(o, decision_ts(day, tz, h)) for h in HOURS}
            days[day] = dict(m={str(h): v for h, v in m.items()}, final=max(v for _, v in o), n=len(o))
        print(f"{series}: obs days {len(days)}", flush=True)
        # candles only where a rule could act: brackets near or below the 16:00 running max
        todo = []
        for r in mk:
            day = target_date(r["e"])
            d = days.get(day)
            if not d or d["m"]["16"] is None or r["st"] not in ("between", "greater", "less"):
                continue
            top = r["hi"] if r["st"] == "between" else (r["hi"] - 1 if r["st"] == "less" else None)
            bot = r["lo"] if r["st"] == "between" else (r["lo"] + 1 if r["st"] == "greater" else None)
            m16 = d["m"]["16"]
            if (top is not None and top <= m16 + 1) or (bot is not None and bot <= m16 + 8):
                todo.append((r, day))

        def one(x):
            r, day = x
            a, b = decision_ts(day, tz, HOURS[0]), decision_ts(day, tz, HOURS[-1] + 1)
            m = r["t"]
            path = f"/historical/markets/{m}/candlesticks" if day < HIST_CUTOFF else                 f"/series/{series}/markets/{m}/candlesticks"
            d = get(s, API + path, {"start_ts": a - 3600, "end_ts": b, "period_interval": 60})
            cs = {int(c["end_period_ts"]): (px(c, "yes_bid"), px(c, "yes_ask")) for c in d.get("candlesticks") or []}
            return dict(t=m, day=day, r=r["r"], st=r["st"], lo=r["lo"], hi=r["hi"], c=cs)

        with ThreadPoolExecutor(2) as ex:
            out = list(ex.map(one, todo))
        dst.write_text(json.dumps({"days": days, "markets": out}))
        print(f"  markets {len(out)}, with candles {sum(bool(x['c']) for x in out)}", flush=True)


# ---------------------------------------------------------------- rules

def season(day):
    return (int(day[5:7]) % 12) // 3                                     # DJF, MAM, JJA, SON


def load():
    out = {}
    for series in CITIES:
        f = OUT / f"{series}.json"
        if f.exists():
            out[series] = json.loads(f.read_text())
    return out


def top_of(r):
    return r["hi"] if r["st"] == "between" else (r["hi"] - 1 if r["st"] == "less" else None)


def quote(r, ts):
    c = r["c"].get(str(ts))
    if not c or c[0] is None or c[1] is None:
        return None
    return c


def z2a(data, margin, lo, hi, delay=0):
    out = []
    for series, d in data.items():
        tz = CITIES[series][2]
        for r in d["markets"]:
            day = r["day"]
            if not (lo <= day < hi):
                continue
            top = top_of(r)
            if top is None:
                continue
            for h in HOURS:
                m = d["days"][day]["m"].get(str(h))
                if m is None or top > m - margin:
                    continue
                q = quote(r, decision_ts(day, tz, h + delay))
                if not q or q[0] <= 0.01:
                    break
                cost = 1 - q[0]
                if cost + fee(cost) > 0.97:
                    break
                out.append((day, series, (0.0 if r["r"] == "yes" else 1.0) - cost - fee(cost)))
                break
    return out


def excess_dist(data, lo, hi):
    """(series, season) -> sorted list of final max - M_16 on days in [lo, hi)."""
    dist = defaultdict(list)
    for series, d in data.items():
        for day, x in d["days"].items():
            if lo <= day < hi and x["m"]["16"] is not None and x["n"] >= 20:
                dist[(series, season(day))].append(x["final"] - x["m"]["16"])
    return {k: sorted(v) for k, v in dist.items()}


def z2b(data, dist, edge, lo, hi, delay=0):
    out = []
    for series, d in data.items():
        tz = CITIES[series][2]
        for r in d["markets"]:
            day = r["day"]
            if not (lo <= day < hi):
                continue
            m = d["days"][day]["m"].get("16")
            ex = dist.get((series, season(day)))
            if m is None or not ex or len(ex) < 30:
                continue
            fin = [m + e for e in ex]                                   # empirical final-high scenarios
            if r["st"] == "between":
                p = sum(r["lo"] - 0.5 <= f < r["hi"] + 0.5 for f in fin) / len(fin)
            elif r["st"] == "greater":
                p = sum(f >= r["lo"] + 0.5 for f in fin) / len(fin)
            else:
                p = sum(f < r["hi"] - 0.5 for f in fin) / len(fin)
            q = quote(r, decision_ts(day, tz, 16 + delay))
            if not q:
                continue
            bid, ask = q
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
    return len(v), m * 100, (m / s * math.sqrt(len(v))) if s else 0.0


def fmt(x):
    n, m, t = stats(x)
    return f"n {n} mean {m:+.2f}c t {t:+.2f}"


def design():
    data = load()
    best_a = best_b = None
    for mg in MARGINS:
        a = z2a(data, mg, "", SPLIT)
        print(f"Z2a design MARGIN {mg}: {fmt(a)} | +1h: {fmt(z2a(data, mg, '', SPLIT, 1))}")
        if stats(a)[0] >= 100 and (best_a is None or stats(a)[1] > best_a[1]):
            best_a = (mg, stats(a)[1])
    # Z2b: distribution from the first half of design, evaluated on the second half (no in-sample scoring)
    days = sorted({day for d in data.values() for day in d["days"] if day < SPLIT})
    mid = days[len(days) // 2]
    dist = excess_dist(data, "", mid)
    for e in EDGES_B:
        b = z2b(data, dist, e, mid, SPLIT)
        print(f"Z2b design EDGE {e:.2f} (dist < {mid}, scored {mid}..{SPLIT}): {fmt(b)} | +1h: "
              f"{fmt(z2b(data, dist, e, mid, SPLIT, 1))}")
        if stats(b)[0] >= 100 and (best_b is None or stats(b)[1] > best_b[1]):
            best_b = (e, stats(b)[1])
    FROZEN.write_text(json.dumps({"margin": best_a and best_a[0], "edge_b": best_b and best_b[0]}))
    print("frozen:", best_a, best_b)


def test():
    data = load()
    fr = json.loads(FROZEN.read_text())
    for lab, x, x1 in (
            ("Z2a", z2a(data, fr["margin"], SPLIT, "9999"), z2a(data, fr["margin"], SPLIT, "9999", 1)),
            ("Z2b", *(lambda dist: (z2b(data, dist, fr["edge_b"], SPLIT, "9999"),
                                    z2b(data, dist, fr["edge_b"], SPLIT, "9999", 1)))(excess_dist(data, "", SPLIT)))):
        days = sorted({t[0] for t in x})
        mid = days[len(days) // 2] if days else SPLIT
        print(f"TEST {lab}: {fmt(x)} over {len(days)} days | halves {fmt([t for t in x if t[0] < mid])} / "
              f"{fmt([t for t in x if t[0] >= mid])} | +1h {fmt(x1)}")
        for series in CITIES:
            print(f"    {series:12s} {fmt([t for t in x if t[1] == series])}")


def main() -> None:
    {"fetch": fetch, "design": design, "test": test}[sys.argv[1]]()


if __name__ == "__main__":
    main()
