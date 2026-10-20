"""W4 and its branches: a real market maker on Kalshi LIP markets, replayed on history (pre-registered; vault:
"2026-10-19 maker rewards (W)", section W4 out-of-sample, and "2026-10-20 market-making branches").

    uv run python -m tradeslave.research.w4 shares        # live book share per series (incl. short programs)
    uv run python -m tradeslave.research.w4 fetch         # design + test samples for every idea
    uv run python -m tradeslave.research.w4 design        # per idea: series filter, cap, pull-before-close
    uv run python -m tradeslave.research.w4 test          # judge each frozen idea once

Quoting (W2/W4 rules): when the spread is >= 2c we bid 1c above the best on that side, alone at the front, so any
taker hitting that side fills us (model F). With a 1c spread we join the best at the back of the queue and only
fill on a trade through our price or a cross. After a fill we keep quoting; a Yes lot and a No lot in one market
net to $1 at once; net inventory per market is capped. Model A (always back of queue) and B (always front at the
best, no price improvement) are reported as bounds.
"""

from __future__ import annotations

import json
import math
import random
import statistics
import sys
import time
from collections import defaultdict

import requests

from ..paths import DATA_DIR
from .kalshi_lip import API, N, evaluate, ts
from .kalshi_lip_bt import OUT as BT, UNIVERSE as CORE, fee, iso, shares as snap_shares
from .weather import get, px

OUT = DATA_DIR / "w4"
PROGS = BT / "programs.json"
DESIGN, TEST = ("2026-09-21", "2026-10-20"), ("2026-08-21", "2026-09-21")
PER_IDEA = 300
CAPS, PULLS = (20, 40), (0, 1, 3)                 # inventory cap (contracts); stop quoting H hours before close
GAP = 0.25


def _attention(se):
    return se.startswith(("KXYTVIEWS", "KXYTTOPVIDEO", "KXYTDAILY", "KXALBUMSTREAMS", "KXARTISTSTREAMS")) or se in {
        "KXRT", "KXTRUMPMENTION", "KXHEADLINE", "KXFEATURE", "KXWORLDNEWSMENTION"}


IDEAS = {
    "core": lambda se: se in CORE,                                                   # W4 itself
    "gas": lambda se: se.startswith("KXAAAGASD") or se == "KXDIESELD",               # branch of P1
    "weather": lambda se: se.startswith(("KXTEMP", "KXHIGH", "KXLOW", "KXRAIN", "KXSNOW")),   # branch of Z/Z2
    "short": lambda se: se.endswith("15M"),                                          # branch of M (15-min markets)
    "attention": _attention,                                                         # YouTube/RT/mentions
}


def _get(s, path, params):
    time.sleep(GAP)
    return get(s, API + path, params)


def programs():
    return [p for p in json.loads(PROGS.read_text()) if p["incentive_type"] == "liquidity"]


# ---------------------------------------------------------------- live share per series (short programs included)

def shares():
    """Our 20-contract share of each series' live books, for series the hourly snapshots skip (< 6 h left)."""
    OUT.mkdir(parents=True, exist_ok=True)
    s, now = requests.Session(), time.time()
    live = defaultdict(list)
    cur, ps = None, []
    while True:
        p = {"limit": 1000}
        if cur:
            p["cursor"] = cur
        d = _get(s, "/incentive_programs", p)
        ps += d.get("incentive_programs") or []
        cur = d.get("next_cursor")
        if not cur or not d.get("incentive_programs"):
            break
    for p in ps:
        se = p["market_ticker"].split("-")[0]
        if p["incentive_type"] == "liquidity" and ts(p["start_date"]) <= now < ts(p["end_date"]) \
                and any(f(se) for f in IDEAS.values()):
            live[se].append(p)
    f = OUT / "shares_live.jsonl"
    with f.open("a", encoding="utf-8") as out:
        for se, xs in live.items():
            random.Random(int(now)).shuffle(xs)
            for p in xs[:15]:
                b = _get(s, f"/markets/{p['market_ticker']}/orderbook", {}).get("orderbook_fp") or {}
                e = evaluate(b, p)
                if e:
                    out.write(json.dumps(dict(se=se, t=now, share=e["share"], met=e["met"])) + "\n")
    print(f"measured {len(live)} live series", flush=True)


def all_shares():
    sh = dict(snap_shares())
    rows = defaultdict(list)
    f = OUT / "shares_live.jsonl"
    if f.exists():
        for line in f.open(encoding="utf-8"):
            r = json.loads(line)
            rows[r["se"]].append(r)
    for se, rs in rows.items():
        if se in sh and sh[se][0] > 0:
            continue                                   # hourly snapshots already cover it
        met = [r["share"] for r in rs if r["met"]]
        sh[se] = (statistics.median(met) if met else 0.0, len(met) / len(rs))
    return sh


# ---------------------------------------------------------------- data

def fetch():
    OUT.mkdir(parents=True, exist_ok=True)
    now, by = time.time(), defaultdict(list)
    for p in programs():
        end = ts(p["end_date"])
        if TEST[0] <= iso(end) < DESIGN[1] and end < now:
            by[p["market_ticker"]].append(p)
    s = requests.Session()
    for idea, f in IDEAS.items():
        for lab, (lo, hi) in (("design", DESIGN), ("test", TEST)):
            dst = OUT / f"{idea}_{lab}.jsonl"
            if idea == "core" and lab == "design":
                continue                               # W-BT already holds this sample
            done = {json.loads(x)["t"] for x in dst.open(encoding="utf-8")} if dst.exists() else set()
            ms = defaultdict(list)
            for tk, ps in by.items():
                se = tk.split("-")[0]
                if f(se) and lo <= iso(max(ts(p["end_date"]) for p in ps)) < hi:
                    ms[se].append(tk)
            per = max(10, PER_IDEA // max(len(ms), 1))
            pick = []
            for se in sorted(ms):
                xs = sorted(ms[se])
                random.Random(0).shuffle(xs)
                pick += xs[:per]
            random.Random(1).shuffle(pick)
            pick = pick[:PER_IDEA]
            print(f"{idea} {lab}: {len(ms)} series, {len(pick)} markets, done {len(done)}", flush=True)
            with dst.open("a", encoding="utf-8") as out:
                for tk in pick:
                    if tk in done:
                        continue
                    try:
                        row = _market(s, tk, by[tk])
                    except Exception as e:                # one bad market must not stop the sample
                        print("  skip", tk, e, flush=True)
                        continue
                    if row:
                        out.write(json.dumps(row) + "\n")
                        out.flush()
    print("fetch done", flush=True)


def _market(s, tk, ps):
    m = _get(s, f"/markets/{tk}", {}).get("market") or {}
    if m.get("result") not in ("yes", "no"):
        return None
    a, b = min(int(ts(p["start_date"])) for p in ps), max(int(ts(p["end_date"])) for p in ps)
    a, b = max(a, int(ts(m["open_time"]))), min(b, int(ts(m["close_time"])))
    if b - a < 120:
        return None
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
        if not cur or not d.get("trades") or len(tr) > 50000:
            break
    settle = m.get("settlement_ts") or m.get("expiration_time") or m["close_time"]
    return dict(t=tk, series=series, r=m["result"], settle=ts(settle) if isinstance(settle, str) else settle,
                close=ts(m["close_time"]), a=a, b=b, c=cs, tr=tr,
                progs=[(ts(p["start_date"]), ts(p["end_date"]), p["period_reward"] / 1e4) for p in ps])


def load(idea, lab):
    if idea == "core" and lab == "design":
        f = BT / "markets.jsonl"
        rows = [json.loads(x) for x in f.open(encoding="utf-8")]
        return [r for r in rows if DESIGN[0] <= iso(max(p[1] for p in r["progs"])) < DESIGN[1]]
    f = OUT / f"{idea}_{lab}.jsonl"
    return [json.loads(x) for x in f.open(encoding="utf-8")] if f.exists() else []


# ---------------------------------------------------------------- market-maker replay

def sim(m, share, metfrac, cap, pull_h, model):
    a, b = m["a"], m["b"]
    close = m.get("close", b)
    cs = sorted((int(k), v) for k, v in m["c"].items())
    nmin = (b - a) // 60
    trades = defaultdict(list)
    for t, side, yp, n in m["tr"]:
        trades[int((t - a) // 60)].append((side, round(yp * 100)))
    pool = [0.0] * nmin
    for s0, e0, usd in m["progs"]:
        pm = usd / max((e0 - s0) / 60, 1)
        for i in range(max(0, int((s0 - a) // 60)), min(nmin, int((e0 - a) // 60))):
            pool[i] += pm
    pos, basis = {"yes": 0, "no": 0}, {"yes": 0.0, "no": 0.0}
    cash = rew = capdays = 0.0
    fills = 0
    j, book = 0, (None, None)

    def at(i):
        nonlocal j, book
        t = a + 60 * i
        while j < len(cs) and cs[j][0] <= t:
            bid, ask = cs[j][1]
            book = (round(bid * 100) if bid else None, round(ask * 100) if ask and ask < 1 else None)
            j += 1
        return book

    def fill(side, p):
        nonlocal cash, fills
        c = N * p / 100 + fee(N, p / 100)
        cash -= c
        pos[side] += N
        basis[side] += c
        fills += 1
        pr = min(pos["yes"], pos["no"])
        if pr:
            for s in ("yes", "no"):
                basis[s] -= basis[s] * pr / pos[s]
                pos[s] -= pr
            cash += pr

    for i in range(nmin):
        bid, ask = at(i)
        t = a + 60 * i
        quoting = t < close - pull_h * 3600
        best = {"yes": bid if bid and bid >= 1 else None, "no": 100 - ask if ask and ask <= 99 else None}
        spread = (ask - bid) if (bid and ask) else 0
        o, front = {}, {}
        for s in ("yes", "no"):
            room = (pos[s] - pos["no" if s == "yes" else "yes"]) < cap
            if not (quoting and room and best[s]):
                o[s] = None
                continue
            if model == "F" and spread >= 2:
                o[s], front[s] = best[s] + 1, True
            else:
                o[s], front[s] = best[s], model == "B"
        done = set()
        for side, yc in trades.get(i, []):
            us = "yes" if side == "no" else "no"                       # taker "no" sells into Yes bids
            p = yc if us == "yes" else 100 - yc
            if us in done or o[us] is None:
                continue
            if p < o[us] or (front[us] and p <= o[us]) or (model == "B" and p == o[us]):
                fill(us, o[us])
                done.add(us)
        nb, na = at(i + 1) if i + 1 < nmin else (bid, ask)
        if "yes" not in done and o["yes"] and na is not None and na <= o["yes"]:
            fill("yes", o["yes"])
            done.add("yes")
        if "no" not in done and o["no"] and nb is not None and 100 - nb <= o["no"]:
            fill("no", o["no"])
            done.add("no")
        for s in ("yes", "no"):
            if s not in done and o[s] and pool[i] > 0:
                rew += pool[i] * share / 2 * metfrac
                capdays += N * o[s] / 100 / 1440
        capdays += (basis["yes"] + basis["no"]) / 1440
    capdays += (basis["yes"] + basis["no"]) * max(m["settle"] - b, 60) / 86400
    cash += pos[m["r"]]
    return dict(rew=rew, pnl=cash, net=rew + cash, capdays=capdays, fills=fills)


def run(rows, sh, cap, pull, model, only=None):
    out = []
    for m in rows:
        if only is not None and m["series"] not in only:
            continue
        s, mf = sh.get(m["series"], (0.0, 0.0))
        out.append(dict(sim(m, s, mf, cap, pull, model), series=m["series"],
                        day=iso(max(p[1] for p in m["progs"]))))
    return out


def daily(xs):
    """Per calendar day (market end): net / capital-days. Returns the list of daily ROIs."""
    by = defaultdict(lambda: [0.0, 0.0])
    for x in xs:
        by[x["day"]][0] += x["net"]
        by[x["day"]][1] += x["capdays"]
    return [n / c for d, (n, c) in sorted(by.items()) if c > 1]


def summary(xs):
    cd = sum(x["capdays"] for x in xs)
    roi = sum(x["net"] for x in xs) / cd if cd else 0.0
    d = daily(xs)
    t = statistics.mean(d) / statistics.stdev(d) * math.sqrt(len(d)) if len(d) > 2 and statistics.stdev(d) else 0.0
    return roi, t, len(d)


def line(xs):
    roi, t, nd = summary(xs)
    return (f"n {len(xs):4d} rew ${sum(x['rew'] for x in xs):8.2f} trading ${sum(x['pnl'] for x in xs):+8.2f} "
            f"fills {sum(x['fills'] for x in xs):5d} cap-days {sum(x['capdays'] for x in xs):7.0f} "
            f"ROI/day {roi:+.2%} (daily t {t:+.2f}, {nd} days)")


def design():
    sh = all_shares()
    frozen = {}
    for idea in IDEAS:
        rows = load(idea, "design")
        if not rows:
            print(idea, "no design data")
            continue
        best = None
        for model in ("F", "A"):                       # amendment 1: improve by 1c (F) or join at the best (A)
            for cap in CAPS:
                for pull in PULLS:
                    xs = run(rows, sh, cap, pull, model)
                    keep = sorted({x["series"] for x in xs
                                   if sum(1 for y in xs if y["series"] == x["series"]) >= 5
                                   and sum(y["net"] for y in xs if y["series"] == x["series"]) > 0})
                    sel = [x for x in xs if x["series"] in keep]
                    r = summary(sel)[0]
                    print(f"DESIGN {idea:9s} {model} cap {cap} pull {pull}h: all {line(xs)}\n"
                          f"                                kept {len(keep)} series: {line(sel)}")
                    if best is None or r > best[0]:
                        best = (r, cap, pull, keep, model)
        frozen[idea] = dict(cap=best[1], pull=best[2], series=best[3], model=best[4])
        print(f"FROZEN {idea}: model {best[4]} cap {best[1]} pull {best[2]}h series {best[3]}", flush=True)
    (OUT / "rule.json").write_text(json.dumps(frozen, indent=1))


def test():
    sh = all_shares()
    fr = json.loads((OUT / "rule.json").read_text())
    for idea, r in fr.items():
        rows = load(idea, "test")
        only = set(r["series"])
        days = sorted({iso(max(p[1] for p in m["progs"])) for m in rows if m["series"] in only})
        mid = days[len(days) // 2] if days else ""
        for model in dict.fromkeys((r["model"], "A", "B")):  # the frozen model is the judged one
            xs = run(rows, sh, r["cap"], r["pull"], model, only)
            print(f"TEST {idea:9s} model {model}{' (judged)' if model == r['model'] else ''}: {line(xs)}")
            if model == r["model"]:
                print(f"     halves: {line([x for x in xs if x['day'] < mid])}\n"
                      f"             {line([x for x in xs if x['day'] >= mid])}")
                for se in sorted(only):
                    part = [x for x in xs if x["series"] == se]
                    if part:
                        print(f"       {se:22s} {line(part)}")


def main() -> None:
    {"shares": shares, "fetch": fetch, "design": design, "test": test}[sys.argv[1]]()


if __name__ == "__main__":
    main()
