"""Complete trade history (every trade, wallet, slot) for the RANDOM launch sample of research.long_paths.

    uv run python -m tradeslave.research.full_trades

Source: pump.fun swap-api `/v2/coins/<mint>/trades` (newest first, 100 per page, cursor paging), covering
the bonding curve (program "pump") and PumpSwap after graduation ("pump_amm"). Free; Cloudflare rate
limits it: ~23 calls at 1/s, then ~60 s of 429 (measured 2026-10-18), so calls are paced at ~20/min.
The cursor is "<slotIndexId>-<unix ms>" and paging starts before that time, so each launch is fetched
for its first WINDOW_S seconds only (starts the cursor at t0 + WINDOW_S).
Same launches and order as long_paths (random, no activity bias). Output:
data/full_trades/<mint>.json.gz = {"m", "t0", "trades": [[slot, idx, unix_t, wallet, is_buy, sol, tokens, amm]]}
oldest first.
"""

from __future__ import annotations

import gzip
import json
import random
import threading
import time
from datetime import datetime

import requests

from ..paths import DATA_DIR
from ..scalper.recorder import TICK_DIR, read_ticks
from .long_paths import OUT as PATHS

OUT = DATA_DIR / "full_trades"
API = "https://swap-api.pump.fun/v2/coins/{}/trades"
WINDOW_S = 1800
MAX_PAGES = 300                 # 30,000 trades; a cursor that never ends must not stall the run


class Pace:
    def __init__(self, rate: float = 0.3):
        self.rate, self.next, self.ok, self.lock = rate, 0.0, 0, threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.time()
            t = max(now, self.next)
            self.next = t + 1 / self.rate
        time.sleep(max(0.0, t - now))

    def good(self) -> None:
        with self.lock:
            self.ok += 1
            if self.ok % 300 == 0:
                self.rate = min(self.rate * 1.03, 0.36)

    def bad(self) -> None:
        with self.lock:
            self.rate = max(self.rate * 0.9, 0.2)
            self.next = time.time() + 65


def ts(s: str) -> float:
    return datetime.fromisoformat(s[:19] + "+00:00").timestamp()


def fetch(s: requests.Session, pace: Pace, mint: str, t0: float) -> list | None:
    rows, cur = [], f"9999999999990000000000-{int((t0 + WINDOW_S) * 1000)}"
    for _ in range(MAX_PAGES):
        p = {"limit": 100}
        if cur:
            p["cursor"] = cur
        for k in range(8):
            pace.wait()
            try:
                r = s.get(API.format(mint), params=p, timeout=30)
            except requests.RequestException:
                time.sleep(3 * (k + 1))
                continue
            if r.status_code == 429:
                pace.bad()
                continue
            if r.status_code in (400, 404):
                return rows
            if r.ok:
                pace.good()
                break
            time.sleep(3 * (k + 1))
        else:
            return None
        d = r.json()
        for x in d["trades"]:
            sid = x["slotIndexId"]
            rows.append([int(sid[:12]), int(sid[12:]), ts(x["timestamp"]), x["userAddress"], x["type"] == "buy",
                         float(x["amountSol"]), float(x["baseAmount"]), x["program"] != "pump"])
        cur = d["pagination"].get("nextCursor")
        if not d["pagination"].get("hasMore") or not cur or (rows and rows[-1][2] < t0 - 60):
            break
    else:
        return None                                 # runaway pagination: skip this launch
    rows.reverse()
    return rows


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    t0s = {}
    for r in read_ticks(sorted(TICK_DIR.glob("*.jsonl.gz"))):
        if r.get("k") == "create" and r.get("m") and r.get("bc") and not r.get("mh"):
            t0s[r["m"]] = float(r["t"])
    mints = [p.stem for p in PATHS.glob("*.json") if p.stem in t0s]
    random.Random(11).shuffle(mints)
    for p in OUT.glob("*.json.gz"):                 # early files were fetched without the window
        if '"window_s"' not in gzip.open(p, "rt", encoding="utf-8").read(200):
            p.unlink()
    todo = [m for m in mints if not (OUT / f"{m}.json.gz").exists()]
    print(f"{len(mints)} random launches; fetching {len(todo)}", flush=True)
    s = requests.Session()
    s.headers.update({"Origin": "https://pump.fun", "Accept": "application/json"})
    pace = Pace()
    n_tr, t_start = 0, time.time()
    for i, m in enumerate(todo, 1):
        rows = fetch(s, pace, m, t0s[m])
        if rows is None:
            continue
        with gzip.open(OUT / f"{m}.json.gz", "wt", encoding="utf-8") as fh:
            json.dump({"m": m, "t0": t0s[m], "window_s": WINDOW_S, "trades": rows}, fh)
        n_tr += len(rows)
        if i % 50 == 0:
            print(f"  {i}/{len(todo)} launches, {n_tr} trades, {time.time() - t_start:.0f} s, pace {pace.rate:.1f}/s",
                  flush=True)


if __name__ == "__main__":
    main()
