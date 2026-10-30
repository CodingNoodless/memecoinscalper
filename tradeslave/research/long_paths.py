"""Long price paths (1-minute candles, priced in SOL, from launch) for a RANDOM sample of launches.

    uv run python -m tradeslave.research.long_paths --n 12000

Source: pump.fun's own candle API (swap-api.pump.fun/v2/coins/<mint>/candles, interval 1m, currency SOL,
from createdTs): one call covers the bonding curve and, after graduation, PumpSwap. Measured 2026-10-18:
40 calls in 1.7 s, no throttling (GeckoTerminal managed ~3 tokens/min). Minutes without trades have no
candle. Launches are drawn at random from every create the recorder logged (mayhem-mode excluded), so
there is no survivor or activity bias. Output: data/long_paths/<mint>.json = {"m", "t0", "curve": [[t, o, h, l, c, vol_sol]]}.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from ..paths import DATA_DIR
from ..scalper.recorder import TICK_DIR, read_ticks

OUT = DATA_DIR / "long_paths"
API = "https://swap-api.pump.fun/v2/coins/{}/candles"


def fetch(s: requests.Session, mint: str, t0: float, interval: str = "1m") -> list | None:
    for k in range(5):
        try:
            r = s.get(API.format(mint), params={"interval": interval, "limit": 1000, "currency": "SOL",
                                                 "createdTs": int(t0 * 1000)}, timeout=30)
        except requests.RequestException:
            time.sleep(2 * (k + 1))
            continue
        if r.status_code == 429:
            time.sleep(5 * (k + 1))
            continue
        if r.ok:
            return [[x["timestamp"] / 1000, float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]),
                     float(x["volume"])] for x in r.json()]
        if r.status_code in (400, 404):
            return []
        time.sleep(2 * (k + 1))
    return None


def fix_truncated() -> None:
    """The 1m endpoint returns the NEWEST 1000 candles: for busier coins the series starts hours after launch.
    Prepend 5-minute candles (which reach back to creation) for the missing early part; marks "early5m"."""
    s = requests.Session()
    s.headers.update({"Origin": "https://pump.fun", "Accept": "application/json"})
    n = 0
    for p in sorted(OUT.glob("*.json")):
        d = json.loads(p.read_text())
        c = d.get("curve") or []
        if len(c) < 1000 or d.get("early5m") is not None or c[0][0] - d["t0"] <= 300:
            continue
        c5 = fetch(s, d["m"], d["t0"], "5m")
        if c5 is None:
            continue
        d["early5m"] = len([x for x in c5 if x[0] + 300 <= c[0][0]])
        d["curve"] = [x for x in c5 if x[0] + 300 <= c[0][0]] + c
        p.write_text(json.dumps(d))
        n += 1
    print(f"fixed {n} truncated series")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=12000)
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--creates", action="store_true",
                    help="draw launches from data/c1/creates.jsonl (launch_watch create log, Oct 21+) instead of the ticks")
    a = ap.parse_args()
    if a.fix:
        fix_truncated()
        return
    OUT.mkdir(parents=True, exist_ok=True)
    t0s = {}
    if a.creates:
        with (DATA_DIR / "c1" / "creates.jsonl").open(encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                if r.get("m") and not r.get("mh"):
                    t0s.setdefault(r["m"], float(r["t"]))
    else:
        for r in read_ticks(sorted(TICK_DIR.glob("*.jsonl.gz"))):
            if r.get("k") == "create" and r.get("m") and r.get("bc") and not r.get("mh"):
                t0s[r["m"]] = float(r["t"])
    mints = sorted(t0s)
    random.Random(11).shuffle(mints)
    todo = [m for m in mints[: a.n] if not (OUT / f"{m}.json").exists()]
    print(f"{len(t0s)} logged launches; fetching {len(todo)} at random", flush=True)
    s = requests.Session()
    s.headers.update({"Origin": "https://pump.fun", "Accept": "application/json"})

    def one(m):
        c = fetch(s, m, t0s[m])
        if c is not None:
            (OUT / f"{m}.json").write_text(json.dumps({"m": m, "t0": t0s[m], "curve": c}))
        return c is not None

    with ThreadPoolExecutor(6) as ex:
        for i, ok in enumerate(ex.map(one, todo), 1):
            if i % 1000 == 0:
                print(f"  {i}/{len(todo)}", flush=True)


if __name__ == "__main__":
    main()
