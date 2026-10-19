"""Strategy H5: buy pump.fun coins when their dev pays DexScreener ("DEX PAID"), exits from research.playbook.

    uv run python -m tradeslave.research.dex_paid fetch     # DexScreener orders for every coin that ever hit 100 SOL
    uv run python -m tradeslave.research.dex_paid design    # choose the exit on design payments
    uv run python -m tradeslave.research.dex_paid test      # judge it once on test payments

Universe queried: every long_paths launch whose candles reach MIN_MC SOL market cap at any time. Traded: only
coins whose max market cap BEFORE the first order's paymentTimestamp is >= MIN_MC (selection uses the past only).
Entry at the first candle open at or after payment + DELAY s. Exits / fills / costs: research.playbook.
Pre-registered in the vault note "2026-10-18 human playbook (H)", round 5.
"""

from __future__ import annotations

import json
import statistics
import sys
import time

import numpy as np
import requests

from ..paths import DATA_DIR
from . import playbook as H

MIN_MC, DELAY = 100.0, 30.0
OUT = DATA_DIR / "dex_orders.jsonl"
FROZEN = DATA_DIR / "dex_paid_rule.json"
EXITS = dict(H.EXITS)
EXITS["flat30m"] = dict(stop=1.0, ladder=[], trail=None, structure=False, hold=1800)
EXITS["flat2h"] = dict(stop=1.0, ladder=[], trail=None, structure=False, hold=7200)


def fetch() -> None:
    have = {json.loads(line)["m"] for line in OUT.open()} if OUT.exists() else set()
    todo = []
    for p in sorted(H.LP_OUT.glob("*.json")):
        x = H.load(p)
        if x and x[0] not in have and x[2][:, 2].max() * 1e9 >= MIN_MC:
            todo.append(x[0])
    print(f"{len(todo)} coins to query", flush=True)
    s = requests.Session()
    with OUT.open("a") as f:
        for i, m in enumerate(todo, 1):
            for k in range(5):
                try:
                    r = s.get(f"https://api.dexscreener.com/orders/v1/solana/{m}", timeout=20)
                except requests.RequestException:
                    time.sleep(5)
                    continue
                if r.status_code == 429:
                    time.sleep(30)
                    continue
                if r.ok:
                    f.write(json.dumps({"m": m, **r.json()}) + "\n")
                    f.flush()
                break
            time.sleep(1.05)
            if i % 500 == 0:
                print(f"  {i}/{len(todo)}", flush=True)


def signals():
    orders = {}
    for line in OUT.open():
        d = json.loads(line)
        ts = [o["paymentTimestamp"] / 1000 for o in (d.get("orders") or []) + (d.get("boosts") or [])
              if o.get("paymentTimestamp")]
        if ts:
            orders[d["m"]] = min(ts)
    out = []
    for m, t_pay in orders.items():
        p = H.LP_OUT / f"{m}.json"
        x = H.load(p) if p.exists() else None
        if x is None:
            continue
        _, t0, c = x
        t = c[:, 0]
        before = c[t + 60 <= t_pay]
        if not len(before) or before[:, 2].max() * 1e9 < MIN_MC:
            continue
        j = int(np.searchsorted(t, t_pay + DELAY, side="left"))
        if j >= len(t) or j < 1:
            continue
        out.append((m, t_pay, c, j - 1))         # playbook.trade enters at the open of candle (i + 1)
    return out


def run(which: str):
    sig = [s for s in signals() if (s[1] < H.CUT_T0) == (which == "design")]
    res = {}
    for xn, rule in EXITS.items():
        rows = [(m, tp, r) for m, tp, c, i in sig if (r := H.trade(c, i, rule)) is not None]
        res[xn] = rows
    return sig, res


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "design"
    if cmd == "fetch":
        fetch()
        return
    sig, res = run(cmd)
    print(f"{cmd}: {len(sig)} DEX-paid signals")
    if cmd == "design":
        best = None
        for xn, rows in res.items():
            print("  ", H.summary(rows, xn))
            if len(rows) >= 50:
                m = statistics.mean(r[2] for r in rows)
                if best is None or m > best[0]:
                    best = (m, xn)
        if best:
            FROZEN.write_text(json.dumps({"exit": best[1]}))
            print("frozen", best[1])
    else:
        fr = json.loads(FROZEN.read_text())
        print("TEST", H.summary(res[fr["exit"]], fr["exit"]))


if __name__ == "__main__":
    main()
