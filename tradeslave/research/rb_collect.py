"""RD decision point B (vault "2026-10-24 runner detection on 1,000 real runners (RD)", amendment 2): every coin from one
source. Population = graduations Aug 30 - Oct 24 that filled the curve (>= 60 s) and started under $10k.

    uv run python -u -m tradeslave.research.rb_collect created     # creation time + details for every migration
    uv run python -u -m tradeslave.research.rb_collect trades      # full curve trades: uniform sample of 4,000 + all runners
    uv run python -u -m tradeslave.research.rb_collect candles     # GeckoTerminal 1-min candles, migration .. +6 h

Curve trades come from pump.fun's swap-api trade list (Rohit's login token), paged back from the migration time. Resumable.
"""

from __future__ import annotations

import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

from ..paths import DATA_DIR
from .rn_collect import _pump_headers

RUN = DATA_DIR / "runners"
OUT = RUN / "rb"
N_SAMPLE, SEED, CURVE_MIN_S, START_MAX, MAX_PAGES = 4000, 25, 60, 10_000, 40
SWAP = "https://swap-api.pump.fun/v2/coins/{m}/trades"
KEEP = ("name", "symbol", "description", "twitter", "website", "telegram", "created_timestamp", "creator",
        "is_currently_live", "nsfw", "image_uri", "metadata_uri", "show_name")


def window():
    R = json.loads((RUN / "runners.json").read_text())
    return min(r["t"] for r in R), max(r["t"] for r in R), R


def migrations():
    lo, hi, _ = window()
    rows = [json.loads(x) for x in (RUN / "migrations.jsonl").open()]
    return [r for r in rows if lo <= r["t"] <= hi]


def created() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / "details.json"
    D = json.loads(p.read_text()) if p.exists() else {}
    H = _pump_headers()
    need = [r["m"] for r in migrations() if r["m"] not in D]
    print(f"details: {len(D)} cached, {len(need)} to fetch", flush=True)
    for i in range(0, len(need), 50):
        chunk = need[i:i + 50]
        for k in range(6):
            try:
                r = requests.post("https://frontend-api-v3.pump.fun/coins-v2/mints", headers=H, json={"mints": chunk}, timeout=30)
                if r.status_code in (200, 201):
                    for c in r.json():
                        D[c["mint"]] = {x: c.get(x) for x in KEEP}
                    for m in chunk:
                        D.setdefault(m, None)            # pump.fun has no record: stays out of the population
                    break
            except (requests.RequestException, ValueError):
                pass
            time.sleep(min(60, 3 * 2 ** k))
        if i % 2500 == 0:
            p.write_text(json.dumps(D))
            print(f"details {i}/{len(need)}", flush=True)
        time.sleep(0.25)
    p.write_text(json.dumps(D))
    print(f"details done: {len(D)}")


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def curve_trades(s, H, m, t_created, t_mig):
    """All trades from creation to migration (+60 s, so the first PumpSwap trades are included), oldest first."""
    out, cur = [], f"9999999999999999999999-{int((t_mig + 60) * 1000)}"
    for _ in range(MAX_PAGES):
        for k in range(6):
            try:
                r = s.get(SWAP.format(m=m), headers=H, params={"limit": 100, "cursor": cur}, timeout=30)
                if r.status_code == 200:
                    js = r.json()
                    break
            except (requests.RequestException, ValueError):
                pass
            time.sleep(min(30, 2 * 2 ** k))
        else:
            return out, "error"
        tr = js.get("trades") or []
        out += tr
        pg = js.get("pagination") or {}
        if not tr or not pg.get("hasMore") or _ts(tr[-1]["timestamp"]) < t_created - 5:
            break
        cur = pg["nextCursor"]
    else:
        return out[::-1], "capped"
    return out[::-1], "ok"


def trades() -> None:
    D = json.loads((OUT / "details.json").read_text())
    _, _, R = window()
    runner = {r["m"] for r in R}
    pop = []
    for r in migrations():
        d = D.get(r["m"])
        if d and d.get("created_timestamp") and r["t"] - d["created_timestamp"] / 1000 >= CURVE_MIN_S:
            pop.append(r)
    rng = random.Random(SEED)
    sample = rng.sample(pop, min(N_SAMPLE, len(pop)))
    todo = {r["m"]: dict(r, sample=True) for r in sample}
    for r in R:
        todo.setdefault(r["m"], dict(r, sample=False))
    (OUT / "population.json").write_text(json.dumps(dict(n_curve_graduations=len(pop), sample=[r["m"] for r in sample],
                                                         runners_in_window=len(runner))))
    td = OUT / "trades"
    td.mkdir(exist_ok=True)
    jobs = [x for x in todo.values() if not (td / f"{x['m']}.json").exists()]
    print(f"curve graduations in window {len(pop)}, sample {len(sample)} (runners in it {sum(r['m'] in runner for r in sample)}), "
          f"to fetch {len(jobs)}", flush=True)
    H = _pump_headers()

    def job(x):
        s = requests.Session()
        tc = D[x["m"]]["created_timestamp"] / 1000
        tr, st = curve_trades(s, H, x["m"], tc, x["t"])
        rows = [[_ts(t["timestamp"]), t["userAddress"], t["type"] == "buy", float(t["amountSol"]), float(t["baseAmount"]),
                 float(t["priceUsd"]), t["program"]] for t in tr]
        (td / f"{x['m']}.json").write_text(json.dumps(dict(m=x["m"], created=tc, t_mig=x["t"], pool=x.get("pool"),
                                                           ath=x.get("ath"), sample=x["sample"], status=st, rows=rows)))
        return st

    n = 0
    with ThreadPoolExecutor(4) as ex:
        for st in ex.map(job, jobs):
            n += 1
            if n % 100 == 0:
                print(f"trades {n}/{len(jobs)} ({st})", flush=True)
    print("trades done")


def candles() -> None:
    from .candles import Gecko
    td, cd = OUT / "trades", OUT / "candles1m"
    cd.mkdir(exist_ok=True)
    g = Gecko()
    files = sorted(td.glob("*.json"))
    for n, f in enumerate(files):
        x = json.loads(f.read_text())
        p = cd / f.name
        if p.exists() or not x.get("pool"):
            continue
        params = {"aggregate": 1, "limit": 1000, "currency": "usd", "token": x["m"], "before_timestamp": int(x["t_mig"] + 6 * 3600 + 60)}
        js = g.get(f"/pools/{x['pool']}/ohlcv/minute", params)
        lst = (((js or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        rows = sorted([[float(v) for v in c] for c in lst if c[0] >= x["t_mig"] - 60])
        p.write_text(json.dumps(dict(m=x["m"], t_mig=x["t_mig"], c=rows)))
        if n % 100 == 0:
            print(f"candles {n}/{len(files)}: {len(rows)}, 429s {g.rate_limited}", flush=True)
    print("candles done")


if __name__ == "__main__":
    {"created": created, "trades": trades, "candles": candles}[sys.argv[1]]()
