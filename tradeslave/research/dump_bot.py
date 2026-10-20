"""Y7 forward paper bot: buy the rebound right after a big creator (>= 15 SOL) dumps into the bot wave.
No money, no transactions (vault: "2026-10-20 post-dump rebound (Y7)").

    uv run python -m tradeslave.research.dump_bot run      # paper-trade live launches until stopped
    uv run python -m tradeslave.research.dump_bot score

New launches come from launch_watch's live log (it holds the one PumpPortal connection). For each creator buy
>= MIN_SOL, poll pump.fun's trade API: when the creator's first sell appears (within DUMP_WINDOW_S of launch),
paper-buy at the first later trade's fill price; then exit at +TP / SL on later trade prices, or at the last
price after HOLD_S. Costs COST per side (curve fee + slippage).
"""

from __future__ import annotations

import json
import math
import statistics
import sys
import threading
import time
from datetime import datetime

import requests

from ..paths import DATA_DIR
from . import launch_watch as LW
from .full_trades import API

OUT = DATA_DIR / "dump_bot"
LEDGER = OUT / "paper.jsonl"
MIN_SOL, DUMP_WINDOW_S = 15.0, 60.0
TP, SL, HOLD_S = 1.0, -0.3, 600.0
COST = 0.0225
GAP = 3.2                                   # s between API calls (the API allows ~20/min)


class Api:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"Origin": "https://pump.fun", "Accept": "application/json"})
        self.lock, self.last = threading.Lock(), 0.0

    def trades(self, mint, before_ms=None):
        cur = f"9999999999990000000000-{before_ms or int(time.time() * 1000) + 60000}"
        for k in range(4):
            with self.lock:
                wait = self.last + GAP - time.time()
                if wait > 0:
                    time.sleep(wait)
                self.last = time.time()
            try:
                r = self.s.get(API.format(mint), params={"limit": 100, "cursor": cur}, timeout=20)
            except requests.RequestException:
                continue
            if r.status_code == 429:
                time.sleep(20)
                continue
            if r.ok:
                out = []
                for x in r.json().get("trades") or []:
                    out.append((datetime.fromisoformat(x["timestamp"][:19] + "+00:00").timestamp(), x["userAddress"],
                                x["type"], float(x["fillPriceSol"] or x["priceSol"])))
                return sorted(out)
        return None


def _log(ev):
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(dict(ev, logged=time.time())) + "\n")


def follow(api, c):
    m, t0, dev = c["mint"], c["t"], c["traderPublicKey"]
    dump = entry = None
    while time.time() < t0 + DUMP_WINDOW_S + 30:
        tr = api.trades(m) or []
        sells = [t for t, w, ty, p in tr if w == dev and ty == "sell" and t <= t0 + DUMP_WINDOW_S]
        if sells:
            dump = min(sells)
            after = [(t, p) for t, w, ty, p in tr if t > dump and t >= time.time() - 30]   # a price we could still get
            if after:
                entry = after[-1]
            break
        if time.time() > t0 + DUMP_WINDOW_S:
            break
        time.sleep(2)
    if dump is None or entry is None:
        _log({"k": "skip", "m": m, "x": c["solAmount"], "why": "no dump" if dump is None else "no price"})
        return
    t_in, e = entry
    _log({"k": "enter", "m": m, "x": c["solAmount"], "dump_s": dump - t0, "t_in": t_in, "px": e})
    x, why = None, "time"
    while time.time() < t_in + HOLD_S + 20:
        time.sleep(15)
        tr = api.trades(m) or []
        for t, w, ty, p in tr:
            if t <= t_in or t > t_in + HOLD_S:
                continue
            if p <= e * (1 + SL):
                x, why = e * (1 + SL), "stop"
                break
            if p >= e * (1 + TP):
                x, why = e * (1 + TP), "tp"
                break
        if x is not None:
            break
    if x is None:
        tr = api.trades(m) or []
        last = [p for t, w, ty, p in tr if t <= t_in + HOLD_S]
        x = last[-1] if last else e
    r = (x / e) * (1 - COST) / (1 + COST) - 1
    _log({"k": "exit", "m": m, "x": c["solAmount"], "ret": r, "why": why})


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    api = Api()
    f = LW.OUT.open(encoding="utf-8")
    f.seek(0, 2)                                         # only launches from now on
    print("dump_bot paper: watching launch_watch log", flush=True)
    while True:
        line = f.readline()
        if not line:
            time.sleep(0.5)
            continue
        try:
            c = json.loads(line)
        except ValueError:
            continue
        if c.get("txType") == "create" and (c.get("solAmount") or 0) >= MIN_SOL and time.time() - c["t"] < 20:
            threading.Thread(target=follow, args=(api, c), daemon=True).start()


def score():
    ev = [json.loads(x) for x in LEDGER.open(encoding="utf-8")]
    rs = [e["ret"] for e in ev if e["k"] == "exit"]
    print(f"launches followed {sum(e['k'] in ('enter', 'skip') for e in ev)}, trades {len(rs)}, "
          f"skips {sum(e['k'] == 'skip' for e in ev)}")
    if len(rs) > 2:
        m, s = statistics.mean(rs), statistics.stdev(rs)
        print(f"mean {m:+.2%} t {m / s * math.sqrt(len(rs)):+.2f} median {statistics.median(rs):+.2%} "
              f"win {sum(r > 0 for r in rs) / len(rs):.0%}; exits {[e['why'] for e in ev if e['k'] == 'exit'].count('tp')} tp")


def main() -> None:
    {"run": run, "score": score}[sys.argv[1]]()


if __name__ == "__main__":
    main()
