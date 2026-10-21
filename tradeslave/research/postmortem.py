"""Post-mortem of every closed M1/M2 paper trade: why the losers lost, and what the sold coins did afterwards.

    uv run python -m tradeslave.research.postmortem

Prices after the exit come from pump.fun 1-minute candles (priced in SOL; cached in data/m1/pm_candles).
Diagnosis only: nothing here is a validated rule. Any fix it suggests must be tested forward.
"""

from __future__ import annotations

import datetime as dt
import json
import statistics
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests

from ..paths import DATA_DIR
from .long_paths import fetch

CACHE = DATA_DIR / "m1" / "pm_candles"
SIZE = 0.1
STOP_AT = {"m1": 0.70, "m2": 0.90}


def load(v):
    return [x for x in map(json.loads, (DATA_DIR / v / "trades.jsonl").open(encoding="utf-8")) if "ret" in x]


def candles(s, mint, t0):
    p = CACHE / f"{mint}.json"
    if p.exists():
        return json.loads(p.read_text())
    c = fetch(s, mint, t0 - 1800) or []
    if not c or c[0][0] > t0 - 600:                       # 1m endpoint returns the newest 1000: prepend 5m candles
        c5 = fetch(s, mint, t0 - 1800, "5m") or []
        c = [x for x in c5 if not c or x[0] < c[0][0]] + c
    p.write_text(json.dumps(c))
    return c


def window(c, a, b):
    return [x for x in c if a <= x[0] < b]


def classify(v, x, c):
    t, te, p0 = x["t"], x["t_exit"], x["p0"]
    pre = window(c, t - 600, t - 60)
    post = window(c, te + 60, te + 3600)                  # candles that start a full minute after the exit
    after_hi = max((y[2] for y in post), default=x["p_exit"]) / p0
    after_lo = min((y[3] for y in post), default=x["p_exit"]) / p0
    end = (post[-1][4] if post else x["p_exit"]) / p0
    r = dict(v=v, name=x["name"], m=x["m"], t=t, hour=dt.datetime.fromtimestamp(t).hour, score=x["score"],
             mc=x.get("mc"), exit=x["exit"], ret=x["ret"], fill=x["p_exit"] / p0, peak=x["peak"] / p0,
             held=te - t, high=x.get("high"), after_hi=after_hi, after_lo=after_lo, end60=end,
             pre_lo=min((y[3] for y in pre), default=p0) / p0, pre_hi=max((y[2] for y in pre), default=p0) / p0,
             has_post=bool(post))
    if x["ret"] < 0:
        if x["exit"] == "STOP" and r["fill"] < STOP_AT[v] - 0.10:
            r["why"] = "rug gap (stop filled far below its level)"
        elif r["peak"] >= 1.3:
            r["why"] = "gave back a gain (was up >= 30%)"
        elif after_hi >= 2.0:
            r["why"] = "shaken out (coin later did 2x from our entry)"
        elif after_hi >= 1.0:
            r["why"] = "sold, coin came back above entry"
        else:
            r["why"] = "bad token (never got back to entry)"
    else:
        r["why"] = "win"
    r["left"] = after_hi / r["fill"]                      # best price within an hour after we sold, vs our sale
    return r


def pct(v):
    return f"{statistics.mean(v):+.1%}" if v else "-"


def main() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    trades = [("m1", x) for x in load("m1")] + [("m2", x) for x in load("m2")]
    first = {}
    for _, x in trades:
        first[x["m"]] = min(first.get(x["m"], x["t"]), x["t"])
    s = requests.Session()
    s.headers.update({"Origin": "https://pump.fun", "Accept": "application/json"})
    with ThreadPoolExecutor(4) as ex:
        cs = dict(zip(first, ex.map(lambda m: candles(s, m, first[m]), first)))
    rows = [classify(v, x, cs[x["m"]]) for v, x in trades]
    (DATA_DIR / "m1" / "postmortem.json").write_text(json.dumps(rows))
    for v in ("m1", "m2"):
        rr = [r for r in rows if r["v"] == v]
        loss = [r for r in rr if r["ret"] < 0]
        print(f"\n===== {v.upper()}: {len(rr)} trades, {len(loss)} losers, total {sum(r['ret'] for r in rr) * SIZE:+.3f} SOL, "
              f"losses {sum(r['ret'] for r in loss) * SIZE:+.3f} SOL, wins {sum(r['ret'] for r in rr if r['ret'] > 0) * SIZE:+.3f} SOL")
        g = defaultdict(list)
        for r in loss:
            g[r["why"]].append(r)
        for k, z in sorted(g.items(), key=lambda kv: sum(r["ret"] for r in kv[1])):
            print(f"  {k:48s} n {len(z):3d}  lost {sum(r['ret'] for r in z) * SIZE:+.3f} SOL  avg {pct([r['ret'] for r in z])}"
                  f"  avg fill {statistics.mean(r['fill'] for r in z):.2f}x  held {statistics.median(r['held'] for r in z):.0f}s")
        print("  exit reasons on losers:", dict(sorted(defaultdict(int, {e: sum(r['exit'] == e for r in loss)
                                                                         for e in {r['exit'] for r in loss}}).items())))
        print("  losers by entry: bought after a pump (10-min low <= 0.6x entry):",
              sum(r["pre_lo"] <= 0.6 for r in loss), "of", len(loss),
              "| winners:", sum(r["pre_lo"] <= 0.6 for r in rr if r["ret"] > 0), "of", sum(r["ret"] > 0 for r in rr))
        early = sorted((r for r in rr if r["left"] >= 2.0 and r["has_post"]), key=lambda r: -r["left"])
        print(f"  SOLD TOO EARLY (coin did >= 2x more within 1 h of our sale): {len(early)} of {len(rr)}")
        for r in early[:25]:
            print(f"    {dt.datetime.fromtimestamp(r['t']):%m-%d %H:%M} {r['name'][:22]:22s} sc {r['score']} {r['exit']:5s} "
                  f"ret {r['ret']:+6.1%}  sold at {r['fill']:.2f}x, then high {r['after_hi']:.1f}x of entry ({r['left']:.1f}x our sale)")
        held = [r for r in rr if r["has_post"]]
        print("  hold-to-+60-min instead of our exit (close 1 h after the sale, all trades): "
              f"actual avg {pct([r['ret'] for r in held])} vs price-only {pct([r['end60'] - 1 for r in held])}")


if __name__ == "__main__":
    main()
