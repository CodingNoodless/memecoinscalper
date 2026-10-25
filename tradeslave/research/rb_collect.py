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


class _Limiter:
    """One shared pace for every worker: pump.fun 429s on bursts. Halve the rate on a 429, creep back up on success."""

    def __init__(self, rate=0.7):
        import threading
        self.rate, self.next, self.lock = rate, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            now = time.time()
            t = max(now, self.next)
            self.next = t + 1 / self.rate
        time.sleep(max(0.0, t - now))

    def ok(self):
        pass

    def hit(self, reset=None):
        """429: everyone waits out the window the API reports (x-ratelimit-reset, seconds)."""
        with self.lock:
            self.next = max(self.next, time.time() + min(60.0, float(reset or 5)))


LIMIT = _Limiter()


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def curve_trades(s, H, m, t_created, t_mig):
    """All trades from creation to migration (+60 s, so the first PumpSwap trades are included), oldest first."""
    out, cur = [], f"9999999999999999999999-{int((t_mig + 60) * 1000)}"
    for _ in range(MAX_PAGES):
        for k in range(30):                      # busy coins page deep; a give-up here would drop the biggest runners
            LIMIT.wait()
            try:
                r = s.get(SWAP.format(m=m), headers=H, params={"limit": 100, "cursor": cur}, timeout=30)
                if r.status_code == 200:
                    js = r.json()
                    LIMIT.ok()
                    break
                if r.status_code == 429:
                    LIMIT.hit(r.headers.get("Retry-After") or r.headers.get("x-ratelimit-reset"))
                    continue
            except (requests.RequestException, ValueError):
                pass
            time.sleep(min(30, 2 * (k + 1)))
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
    def pending(x):
        p = td / f"{x['m']}.json"
        if not p.exists():
            return True
        try:
            return json.loads(p.read_text())["status"] == "error"       # retried on every pass
        except (OSError, ValueError, KeyError):
            return True
    jobs = [x for x in todo.values() if pending(x)]
    print(f"curve graduations in window {len(pop)}, sample {len(sample)} (runners in it {sum(r['m'] in runner for r in sample)}), "
          f"to fetch {len(jobs)}", flush=True)
    H = _pump_headers()

    def job(x):
        s = requests.Session()
        tc = D[x["m"]]["created_timestamp"] / 1000
        tr, st = curve_trades(s, H, x["m"], tc, x["t"])
        rows = [[_ts(t["timestamp"]), t["userAddress"], t["type"] == "buy", _f(t.get("amountSol")), _f(t.get("baseAmount")),
                 _f(t.get("priceUsd")), t.get("program")] for t in tr if t.get("timestamp") and t.get("userAddress")]
        (td / f"{x['m']}.json").write_text(json.dumps(dict(m=x["m"], created=tc, t_mig=x["t"], pool=x.get("pool"),
                                                           ath=x.get("ath"), sample=x["sample"], status=st, rows=rows)))
        return st

    def safe(x):
        try:
            return job(x)
        except Exception as exc:  # noqa: BLE001 - one bad coin must not stop the run; it is retried on the next pass
            return f"failed: {type(exc).__name__}"

    n = 0
    with ThreadPoolExecutor(4) as ex:
        for st in ex.map(safe, jobs):
            n += 1
            if n % 100 == 0:
                print(f"trades {n}/{len(jobs)} ({st})", flush=True)
    print("trades done")


def _page_back(s, H, m, t_end, pages):
    """Up to `pages` x 100 trades with time <= t_end, newest first (amendment 3 windows)."""
    out, cur = [], f"9999999999999999999999-{int(t_end * 1000)}"
    for _ in range(pages):
        js = None
        for k in range(30):
            LIMIT.wait()
            try:
                r = s.get(SWAP.format(m=m), headers=H, params={"limit": 100, "cursor": cur}, timeout=30)
                if r.status_code == 200:
                    js = r.json()
                    break
                if r.status_code == 429:
                    LIMIT.hit(r.headers.get("Retry-After") or r.headers.get("x-ratelimit-reset"))
                    continue
            except (requests.RequestException, ValueError):
                pass
            time.sleep(min(30, 2 * (k + 1)))
        if js is None:
            return out, "error"
        tr = js.get("trades") or []
        out += tr
        pg = js.get("pagination") or {}
        if not tr or not pg.get("hasMore"):
            break
        cur = pg["nextCursor"]
    return out, "ok"


def _rows(tr):
    return [[_ts(t["timestamp"]), t["userAddress"], t["type"] == "buy", _f(t.get("amountSol")), _f(t.get("baseAmount")),
             _f(t.get("priceUsd")), t.get("program")] for t in tr if t.get("timestamp") and t.get("userAddress")][::-1]


def lite() -> None:
    """Amendment 3: two 200-trade windows per coin (launch: <= created + 120 s; run-up: <= migration + 60 s)."""
    D = json.loads((OUT / "details.json").read_text())
    P = json.loads((OUT / "population.json").read_text())
    _, _, R = window()
    rows = {r["m"]: r for r in migrations()}
    full, ld = OUT / "trades", OUT / "trades_lite"
    ld.mkdir(exist_ok=True)

    def have(m):
        if (ld / f"{m}.json").exists():
            return json.loads((ld / f"{m}.json").read_text()).get("status") == "ok"
        if (full / f"{m}.json").exists():                  # a capped history lacks the launch trades
            return json.loads((full / f"{m}.json").read_text()).get("status") == "ok"
        return False
    order = [r["m"] for r in R if not have(r["m"])] + [m for m in P["sample"] if not have(m)]
    smp = set(P["sample"])
    print(f"lite: {len(order)} coins to fetch (runners first)", flush=True)
    H = _pump_headers()

    def job(m):
        s = requests.Session()
        x = rows.get(m) or next(r for r in R if r["m"] == m)
        d = D.get(m) or {}
        tc = (d.get("created_timestamp") or 0) / 1000 or x.get("created")
        a, st1 = _page_back(s, H, m, tc + 120, 2)
        b, st2 = _page_back(s, H, m, x["t"], 2)            # run-up ends AT migration (amendment 3b)
        st = "ok" if st1 == st2 == "ok" else "error"
        (ld / f"{m}.json").write_text(json.dumps(dict(m=m, created=tc, t_mig=x["t"], pool=x.get("pool"), ath=x.get("ath"),
                                                      sample=m in smp, status=st, launch=_rows(a), runup=_rows(b), runup_v2=True)))
        return st

    def safe(m):
        try:
            return job(m)
        except Exception as exc:  # noqa: BLE001
            return f"failed: {type(exc).__name__}"
    n = 0
    with ThreadPoolExecutor(2) as ex:
        for st in ex.map(safe, order):
            n += 1
            if n % 50 == 0:
                print(f"lite {n}/{len(order)} ({st})", flush=True)
    print("lite done")


def runup_fix() -> None:
    """Amendment 3b: re-fetch the run-up window (last 200 trades <= migration) for lite files fetched with the +60 s window."""
    ld = OUT / "trades_lite"
    files = [p for p in sorted(ld.glob("*.json")) if not json.loads(p.read_text()).get("runup_v2")]
    print(f"runup_fix: {len(files)} files", flush=True)
    H = _pump_headers()

    def job(p):
        x = json.loads(p.read_text())
        b, st = _page_back(requests.Session(), H, x["m"], x["t_mig"], 2)
        x.update(runup=_rows(b), runup_v2=True, status="ok" if st == "ok" and x["status"] == "ok" else "error")
        p.write_text(json.dumps(x))
        return st

    n = 0
    with ThreadPoolExecutor(2) as ex:
        for st in ex.map(job, files):
            n += 1
            if n % 100 == 0:
                print(f"runup_fix {n}/{len(files)} ({st})", flush=True)
    print("runup_fix done")


def candles() -> None:
    from .candles import Gecko
    td, cd = OUT / "trades", OUT / "candles1m"
    cd.mkdir(exist_ok=True)
    g = Gecko()
    files = sorted(td.glob("*.json")) + sorted((OUT / "trades_lite").glob("*.json"))
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
    {"created": created, "trades": trades, "lite": lite, "runup_fix": runup_fix, "candles": candles}[sys.argv[1]]()
