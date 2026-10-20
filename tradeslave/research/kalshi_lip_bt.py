"""W-BT: the W1 maker-rewards rules replayed on the last 4 weeks of Kalshi LIP programs (pre-registered;
vault: "2026-10-19 maker rewards (W)", section W-BT).

    uv run python -m tradeslave.research.kalshi_lip_bt fetch     # ended programs, 1-min candles, trade tapes
    uv run python -m tradeslave.research.kalshi_lip_bt design    # per-series results on design, freeze the W3 list
    uv run python -m tradeslave.research.kalshi_lip_bt test      # judge W-all and W3 once

Book = best Yes bid / ask from 1-min candles (forward-filled). We rest N contracts at both bests during each
program window and re-join when the best moves. Fills: a trade hitting our side through our price, or the opposite
best reaching our price (variant A); variant B also fills on any trade hitting our side at our price. Rewards use
today's snapshot share per series (no historical depth exists).
"""

from __future__ import annotations

import json
import random
import statistics
import sys
import time
from collections import defaultdict

import requests

from ..paths import DATA_DIR
from .kalshi_lip import API, N, OUT as LIP, programs, ts
from .weather import get, px

OUT = DATA_DIR / "kalshi_lip_bt"
MARKETS = OUT / "markets.jsonl"
FROZEN = OUT / "rule.json"
FROM, SPLIT, TO = "2026-09-21", "2026-10-05", "2026-10-20"
PER_HALF = 40
GAS_STATES = {"VA", "TX", "NE", "AK", "WV", "ID", "MN", "OK", "ND", "AL", "VT", "TN", "ME", "LA", "CA", "CT", "HI", "GA"}
OTHER = {"KXRT", "KXTXERCOTPEAKD", "KXYTVIEWSW", "KXTRUMPAPPROVE", "KXBIGGESTQUAKE", "KXYTTOPVIDEO2D",
         "KXYTTOPVIDEOG2D", "KXRAIN", "KXCFBUNRANKEDUPSET", "KXTRUMPMENTION", "KXUE", "KXSTUDENTLOANGARNISH"}
UNIVERSE = OTHER | {"KXAAAGASD" + s for s in GAS_STATES}
GAP = 0.25                                       # s between requests: leave Kalshi's rate limit to the paper bots


def iso(t):
    return time.strftime("%Y-%m-%d", time.gmtime(t))


def _get(s, path, params):
    time.sleep(GAP)
    return get(s, API + path, params)


def fetch():
    OUT.mkdir(parents=True, exist_ok=True)
    done = set()
    if MARKETS.exists():
        done = {json.loads(line)["t"] for line in MARKETS.open(encoding="utf-8")}
    now = time.time()
    by = defaultdict(list)
    for p in programs():
        if p["incentive_type"] != "liquidity" or p["market_ticker"].split("-")[0] not in UNIVERSE:
            continue
        end = ts(p["end_date"])
        if FROM <= iso(end) < TO and end < now:
            by[p["market_ticker"]].append(p)
    pick = []
    for series in sorted(UNIVERSE):
        for lo, hi in (("", SPLIT), (SPLIT, "9")):
            ms = sorted(tk for tk, ps in by.items() if tk.split("-")[0] == series
                        and lo <= iso(max(ts(p["end_date"]) for p in ps)) < hi)
            random.Random(0).shuffle(ms)
            pick += ms[:PER_HALF]
    print(f"{len(by)} markets with ended programs in the universe; sampled {len(pick)}, done {len(done)}", flush=True)
    s = requests.Session()
    with MARKETS.open("a", encoding="utf-8") as f:
        for k, tk in enumerate(pick):
            if tk in done:
                continue
            m = _get(s, f"/markets/{tk}", {}).get("market") or {}
            if m.get("result") not in ("yes", "no"):
                continue
            ps = by[tk]
            a, b = min(int(ts(p["start_date"])) for p in ps), max(int(ts(p["end_date"])) for p in ps)
            a, b = max(a, int(ts(m["open_time"]))), min(b, int(ts(m["close_time"])))
            if b - a < 120:
                continue
            series = tk.split("-")[0]
            cs = {}
            for c0 in range(a - 120, b, 4000 * 60):
                d = _get(s, f"/series/{series}/markets/{tk}/candlesticks",
                         {"start_ts": c0, "end_ts": min(c0 + 4000 * 60, b), "period_interval": 1})
                for c in d.get("candlesticks") or []:
                    cs[int(c["end_period_ts"])] = (px(c, "yes_bid"), px(c, "yes_ask"))
            tr, cur = [], None
            while True:
                p = {"ticker": tk, "min_ts": a, "max_ts": b, "limit": 1000}
                if cur:
                    p["cursor"] = cur
                d = _get(s, "/markets/trades", p)
                tr += [(ts(x["created_time"]), x["taker_side"], float(x["yes_price_dollars"]), float(x["count_fp"]))
                       for x in d.get("trades") or []]
                cur = d.get("cursor")
                if not cur or not d.get("trades"):
                    break
            settle = m.get("settlement_ts") or m.get("expiration_time") or m["close_time"]
            f.write(json.dumps(dict(t=tk, series=series, r=m["result"], settle=ts(settle) if isinstance(settle, str)
                                    else settle, a=a, b=b, c=cs, tr=tr,
                                    progs=[(ts(p["start_date"]), ts(p["end_date"]), p["period_reward"] / 1e4)
                                           for p in ps])) + "\n")
            f.flush()
            if k % 25 == 0:
                print(f"  {k}/{len(pick)} {tk} candles {len(cs)} trades {len(tr)}", flush=True)
    print("fetch done", flush=True)


# ---------------------------------------------------------------- simulation

def shares():
    """series -> (median 20-contract share where target met, fraction of snapshot rows with target met)."""
    sh, met, rows = defaultdict(list), defaultdict(int), defaultdict(int)
    for f in LIP.glob("snapshot_*.json"):
        for r in json.loads(f.read_text()):
            se = r["market_ticker"].split("-")[0]
            rows[se] += 1
            if r["ev"]["met"]:
                met[se] += 1
                sh[se].append(r["ev"]["share"])
    return {se: (statistics.median(sh[se]) if sh[se] else 0.0, met[se] / rows[se]) for se in rows}


def fee(n, p):
    return -(-0.0175 * n * p * (1 - p) * 100 // 1) / 100


def simulate(m, variant, share, metfrac):
    a, b = m["a"], m["b"]
    cs = sorted((int(k), v) for k, v in m["c"].items())
    nmin = (b - a) // 60
    trades = defaultdict(list)
    for t, side, yp, n in m["tr"]:
        trades[int((t - a) // 60)].append((side, round(yp * 100)))
    pool = [0.0] * nmin                                              # $ per minute from active programs
    for s0, e0, usd in m["progs"]:
        per_min = usd / max((e0 - s0) / 60, 1)
        for i in range(max(0, int((s0 - a) // 60)), min(nmin, int((e0 - a) // 60))):
            pool[i] += per_min
    win = {"yes": m["r"] == "yes", "no": m["r"] == "no"}
    o = {"yes": None, "no": None}
    filled = {"yes": False, "no": False}
    rew = pnl = capdays = 0.0
    fills = []
    j, book = 0, (None, None)

    def at(i):                                                       # best yes bid / ask (cents) at minute i start
        nonlocal j, book
        t = a + 60 * i
        while j < len(cs) and cs[j][0] <= t:
            bid, ask = cs[j][1]
            book = (round(bid * 100) if bid else None, round(ask * 100) if ask and ask < 1 else None)
            j += 1
        return book

    for i in range(nmin):
        bid, ask = at(i)
        best = {"yes": bid if bid and bid >= 1 else None, "no": 100 - ask if ask and ask <= 99 else None}
        for side in ("yes", "no"):
            if not filled[side] and best[side] != o[side]:
                o[side] = best[side]                                 # join / re-join the best
        for side, yc in trades.get(i, []):                           # taker_side "no" hits Yes bids, "yes" hits No bids
            us = "yes" if side == "no" else "no"
            p = yc if us == "yes" else 100 - yc
            if filled[us] or o[us] is None:
                continue
            if p < o[us] or (variant == "B" and p == o[us]):
                filled[us] = True
                fills.append((us, o[us], a + 60 * i))
        nb, na = at(i + 1) if i + 1 < nmin else (bid, ask)
        if not filled["yes"] and o["yes"] and na is not None and na <= o["yes"]:
            filled["yes"] = True
            fills.append(("yes", o["yes"], a + 60 * i))
        if not filled["no"] and o["no"] and nb is not None and 100 - nb <= o["no"]:
            filled["no"] = True
            fills.append(("no", o["no"], a + 60 * i))
        for side in ("yes", "no"):
            if not filled[side] and o[side] and pool[i] > 0:
                rew += pool[i] * share / 2 * metfrac
                capdays += N * o[side] / 100 / 1440
    for side, p, t in fills:
        cost = N * p / 100 + fee(N, p / 100)
        pnl += N * (1.0 if win[side] else 0.0) - cost
        capdays += cost * max(m["settle"] - t, 60) / 86400
    return dict(rew=rew, pnl=pnl, net=rew + pnl, capdays=capdays, fills=len(fills))


def run(variant, lo, hi, only=None):
    sh = shares()
    out = []
    for line in MARKETS.open(encoding="utf-8"):
        m = json.loads(line)
        if only is not None and m["series"] not in only:
            continue
        end = iso(max(p[1] for p in m["progs"]))
        if not (lo <= end < hi):
            continue
        s, mf = sh.get(m["series"], (0.0, 0.0))
        out.append(dict(simulate(m, variant, s, mf), series=m["series"], end=end))
    return out


def roi(xs):
    cd = sum(x["capdays"] for x in xs)
    return sum(x["net"] for x in xs) / cd if cd else 0.0


def line(xs):
    r, n = sum(x["rew"] for x in xs), len(xs)
    return (f"n {n:4d} rewards ${r:8.2f} fills {sum(x['fills'] for x in xs):4d} trading ${sum(x['pnl'] for x in xs):+8.2f} "
            f"net ${sum(x['net'] for x in xs):+8.2f} cap-days {sum(x['capdays'] for x in xs):7.1f} "
            f"ROI/day {roi(xs):+.2%}")


def group(series):
    return "KXAAAGASD*" if series.startswith("KXAAAGASD") else series


def design():
    keep = []
    for v in ("A", "B"):
        xs = run(v, "", SPLIT)
        print(f"DESIGN variant {v}: {line(xs)}")
        for se in sorted({x["series"] for x in xs}):
            part = [x for x in xs if x["series"] == se]
            print(f"    {se:22s} {line(part)}")
            if v == "A" and len(part) >= 10 and sum(x["net"] for x in part) > 0:
                keep.append(se)
    FROZEN.write_text(json.dumps({"w3": keep}))
    print("frozen W3 series:", keep)


def test():
    w3 = set(json.loads(FROZEN.read_text())["w3"])
    for lab, only in (("W-all", None), ("W3", w3)):
        for v in ("A", "B"):
            xs = run(v, SPLIT, "9", only)
            wk = [[x for x in xs if x["end"] < "2026-10-12"], [x for x in xs if x["end"] >= "2026-10-12"]]
            print(f"TEST {lab} {v}: {line(xs)}")
            print(f"    week 1: {line(wk[0])}\n    week 2: {line(wk[1])}")
            for g in sorted({group(x["series"]) for x in xs}):
                print(f"      {g:22s} {line([x for x in xs if group(x['series']) == g])}")


def main() -> None:
    {"fetch": fetch, "design": design, "test": test}[sys.argv[1]]()


if __name__ == "__main__":
    main()
