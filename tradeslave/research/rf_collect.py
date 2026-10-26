"""RF (vault "2026-10-25 runner forecast in the final stretch (RF)"): data for predicting runners at the curve's 60-SOL point.

    uv run python -u -m tradeslave.research.rf_collect stretch    # D1: page back RD run-up windows that start above R = 60
    uv run python -u -m tradeslave.research.rf_collect forward    # F: C1 creates Oct 22 18:00 - Oct 24 18:00 reaching R >= 60
    uv run python -u -m tradeslave.research.rf_collect forward_fix  # amendment 1: page back busy coins to the crossing
    uv run python -u -m tradeslave.research.rf_collect details    # pump.fun details + ATH for every D2 / F coin
    uv run python -u -m tradeslave.research.rf_collect all        # the three, in order

Rows are rb_collect's: [t, wallet, buy, sol, tokens, priceUsd, program]. Resumable.
"""

from __future__ import annotations

import gzip
import json
import math
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

from ..paths import DATA_DIR
from .rb_collect import OUT as RB, _page_back, _rows
from .rn_collect import _pump_headers, ath_batch

RF = DATA_DIR / "runners" / "rf"
C1 = DATA_DIR / "c1"
K, V0, R60 = 30 * 1.073e9, 30.0, 60.0
D1_END = datetime(2026, 10, 23).timestamp()
D2_LO, D2_HI = datetime(2026, 10, 20, 23, 19).timestamp(), datetime(2026, 10, 22, 9).timestamp()
F_LO, F_HI = datetime(2026, 10, 22, 18).timestamp(), datetime(2026, 10, 24, 18).timestamp()
CANDLES = "https://swap-api.pump.fun/v2/coins/{}/candles"
KEEP = ("name", "symbol", "description", "twitter", "website", "telegram", "created_timestamp", "creator", "complete",
        "pump_swap_pool", "raydium_pool")


def level(r):
    """Curve level (real SOL) at a pump trade's execution price; None for dust or AMM trades."""
    if r[6] != "pump" or r[3] < 0.01 or not r[4]:
        return None
    return math.sqrt(K * r[3] / r[4]) - V0


def crossing(rows):
    """Index of the first trade at R >= 60 that has an earlier trade below it, else None."""
    below = False
    for i, r in enumerate(rows):
        R = level(r)
        if R is None:
            continue
        if R >= R60 and below:
            return i
        below = below or R < R60 - 2
    return None


def _key(r):
    return (r[0], r[1], r[2], round(r[3], 9), round(r[4], 3))


def stretch() -> None:
    """D1 lite files whose run-up window has no crossing: prepend older trades until it does (max 8 pages)."""
    out = RF / "stretch"
    out.mkdir(parents=True, exist_ok=True)
    todo = []
    for p in sorted((RB / "trades_lite").glob("*.json")):
        x = json.loads(p.read_text())
        if x["status"] != "ok" or x["t_mig"] >= D1_END or (out / p.name).exists():
            continue
        if crossing(x["runup"]) is None:
            todo.append(x)
    print(f"stretch: {len(todo)} coins to page back", flush=True)
    H = _pump_headers()

    def job(x):
        s = requests.Session()
        rows, st = list(x["runup"]), "ok"
        for _ in range(8):
            tr, st = _page_back(s, H, x["m"], rows[0][0], 1)
            seen = {_key(r) for r in rows}
            new = [r for r in _rows(tr) if _key(r) not in seen]
            if st != "ok" or not new:
                break
            rows = new + rows
            if crossing(rows) is not None:
                break
        found = crossing(rows) is not None
        (out / f"{x['m']}.json").write_text(json.dumps(dict(m=x["m"], status="ok" if found else ("error" if st != "ok" else "none"),
                                                            rows=rows)))
        return found

    _run(job, todo, "stretch")


def _run(job, items, tag):
    def safe(x):
        try:
            return job(x)
        except Exception as exc:  # noqa: BLE001
            return f"failed: {type(exc).__name__}"
    n, got = 0, defaultdict(int)
    with ThreadPoolExecutor(2) as ex:
        for st in ex.map(safe, items):
            n += 1
            got[str(st)] += 1
            if n % 50 == 0:
                print(f"{tag} {n}/{len(items)} {dict(got)}", flush=True)
    print(f"{tag} done {dict(got)}", flush=True)


def _c1():
    mx, cr = defaultdict(float), {}
    for line in (C1 / "checks.jsonl").open():
        x = json.loads(line)
        mx[x["m"]] = max(mx[x["m"]], x.get("maxR") or 0)
    for line in (C1 / "creates.jsonl").open():
        x = json.loads(line)
        cr[x["m"]] = x
    return mx, cr


def candidates():
    mx, cr = _c1()
    mig = {}
    for line in (DATA_DIR / "runners" / "migrations.jsonl").open():
        r = json.loads(line)
        mig[r["m"]] = r["t"]
    out, instant = [], 0
    for m, r in mx.items():
        c = cr.get(m)
        if r < R60 or not c or not c.get("bc") or not F_LO <= c["t"] < F_HI:      # bc: a pump.fun curve create
            continue
        if m in mig and mig[m] - c["t"] < 60:
            instant += 1
            continue
        out.append(c)
    return out, instant


def _candles(s, m, t0):
    for k in range(6):
        try:
            r = s.get(CANDLES.format(m), params={"interval": "5m", "limit": 1000, "currency": "SOL", "createdTs": int(t0 * 1000)},
                      headers={"Origin": "https://pump.fun", "Accept": "application/json"}, timeout=30)
            if r.status_code == 429:
                time.sleep(5 * (k + 1))
                continue
            if r.ok:
                return [[x["timestamp"] / 1000, float(x["high"])] for x in r.json() if x.get("high") is not None]
            if r.status_code in (400, 404):
                return []
        except (requests.RequestException, ValueError):
            pass
        time.sleep(2 * (k + 1))
    return None


def forward() -> None:
    out = RF / "forward"
    out.mkdir(parents=True, exist_ok=True)
    cand, instant = candidates()
    todo = [c for c in cand if not (out / f"{c['m']}.json").exists()]
    print(f"forward: {len(cand)} candidates ({instant} instant migrations dropped), {len(todo)} to fetch", flush=True)
    H = _pump_headers()

    def job(c):
        s = requests.Session()
        m, t0 = c["m"], c["t"]
        cs = _candles(s, m, t0)
        if cs is None:
            return "candle error"
        hit = next((t for t, h in sorted(cs) if t >= t0 - 300 and h > 0 and math.sqrt(h * K) - V0 >= R60), None)
        rec = dict(m=m, created=t0, status="none", launch=[], stretch=[])
        if hit is not None:
            a, st1 = _page_back(s, H, m, t0 + 120, 2)
            b, st2 = _page_back(s, H, m, hit + 300, 4)
            rec.update(launch=_rows(a), stretch=_rows(b), t_candle=hit,
                       status="ok" if st1 == st2 == "ok" else "error")
        (out / f"{m}.json").write_text(json.dumps(rec))
        return rec["status"]

    _run(job, todo, "forward")


def forward_fix() -> None:
    """Amendment 1: forward files whose 400-trade window starts after the crossing (busy coins): page back until found."""
    files = []
    for p in sorted((RF / "forward").glob("*.json")):
        x = json.loads(p.read_text())
        if x["status"] == "ok" and x["stretch"] and crossing(x["stretch"]) is None and not x.get("fix1"):
            files.append(p)
    print(f"forward_fix: {len(files)} files", flush=True)
    H = _pump_headers()

    def job(p):
        x = json.loads(p.read_text())
        s, rows, st = requests.Session(), list(x["stretch"]), "ok"
        for _ in range(8):
            if rows[0][0] <= x["created"] + 1:
                break
            tr, st = _page_back(s, H, x["m"], rows[0][0], 1)
            seen = {_key(r) for r in rows}
            new = [r for r in _rows(tr) if _key(r) not in seen]
            if st != "ok" or not new:
                break
            rows = new + rows
            if crossing(rows) is not None:
                break
        x.update(stretch=rows, fix1=True)
        p.write_text(json.dumps(x))
        return crossing(rows) is not None

    _run(job, files, "forward_fix")


def d2_mints():
    out = []
    for p in sorted((C1 / "trades").glob("*.json.gz")):
        x = json.loads(gzip.open(p, "rt", encoding="utf-8").read())
        if D2_LO <= x["c"]["t"] < D2_HI:
            out.append(x["c"]["m"])
    return out


def details() -> None:
    p = RF / "details.json"
    D = json.loads(p.read_text()) if p.exists() else {}
    mints = sorted(set(d2_mints()) | {q.stem for q in (RF / "forward").glob("*.json")})
    need = [m for m in mints if m not in D]
    print(f"details: {len(mints)} coins, {len(need)} to fetch", flush=True)
    H = _pump_headers()
    for i in range(0, len(need), 50):
        chunk = need[i:i + 50]
        for k in range(6):
            try:
                r = requests.post("https://frontend-api-v3.pump.fun/coins-v2/mints", headers=H, json={"mints": chunk}, timeout=30)
                if r.status_code in (200, 201):
                    for c in r.json():
                        D[c["mint"]] = {x: c.get(x) for x in KEEP}
                    break
            except (requests.RequestException, ValueError):
                pass
            time.sleep(min(60, 3 * 2 ** k))
        time.sleep(0.3)
    ath = ath_batch(mints, H)
    for m in mints:
        if D.get(m) is not None:
            D[m]["ath"] = ath.get(m)
    p.write_text(json.dumps(D))
    print(f"details done: {sum(v is not None for v in D.values())}/{len(mints)} with records, "
          f"{sum(1 for m in mints if (D.get(m) or {}).get('ath') is not None)} with ATH; fetched {time.strftime('%Y-%m-%d %H:%M')}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    for c in (("stretch", "forward", "forward_fix", "details") if cmd == "all" else (cmd,)):
        globals()[c]()
