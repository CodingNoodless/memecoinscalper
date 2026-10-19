"""Strategy V: buy the near-certain favourite on Polymarket shortly before the scheduled end, hold to resolution.

    uv run python -m tradeslave.research.poly_fav design   # grid on markets closed before CUT, freeze the best cell
    uv run python -m tradeslave.research.poly_fav test     # judge the frozen cell once on markets closed after CUT

Sourced: Polymarket favourites are slightly underpriced and longshots overpriced (favourite-longshot bias; arXiv
2609.12878, Polymarket-v1 database paper). Data: research.poly_data (hourly Yes price, last 14 days before close).
Decision at each hourly point while endDate - t <= D days: if the leading side's price is inside band B, buy it
at the NEXT hourly price + slippage (half-spread proxy: max(tick, 0.005)) + taker fee (current schedule, applied
to every market: rate x p x (1 - p) per share); hold to resolution. One entry per market.
Clustering: by event (outcomes of one event are correlated). Pre-registered in the vault note
"2026-10-18 prediction-market favourites (V)".
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from collections import defaultdict

from ..paths import DATA_DIR
from .poly_data import OUT

CUT = 1777593600.0             # 2026-05-01 00:00 UTC: design = closed before, test = closed after
DAYS = (1, 3, 7, 14)
BANDS = ((0.80, 0.90), (0.90, 0.95), (0.95, 0.97), (0.97, 0.99))
FROZEN = DATA_DIR / "poly_fav_rule.json"
RATE = {"crypto": 0.07, "sports": 0.05, "finance": 0.04, "politics": 0.04, "tech": 0.04, "mentions": 0.04,
        "economics": 0.05, "culture": 0.05, "weather": 0.05}


def fee_rate(ft: str | None) -> float:
    if not ft:
        return 0.0
    return RATE.get(ft.split("_")[0], 0.05)


def load():
    ms = {}
    for line in (OUT / "markets.jsonl").open(encoding="utf-8"):
        m = json.loads(line)
        ms[m["id"]] = m
    out = []
    for line in (OUT / "hist.jsonl").open(encoding="utf-8"):
        d = json.loads(line)
        m = ms.get(d["id"])
        if m and len(d["h"]) >= 3:
            out.append((m, sorted(d["h"])))
    return out


def trade(m, h, days, band):
    """First hourly point inside the window and band -> (return per $, hold days) or None."""
    lo, hi = band
    for k in range(len(h) - 1):
        t, p = h[k]
        if t >= m["closed"] or m["end"] - t > days * 86400:
            continue
        side, q = (0, p) if p >= 0.5 else (1, 1 - p)
        if not lo <= q < hi:
            continue
        t1, p1 = h[k + 1]
        if t1 >= m["closed"]:
            return None
        q1 = p1 if side == 0 else 1 - p1
        cost = q1 + max(m["tick"] or 0.01, 0.005) + fee_rate(m["fee"]) * q1 * (1 - q1)
        if cost >= 1:
            return None
        return (m["res"][side] - cost) / cost, max(m["closed"] - t1, 3600) / 86400
    return None


def run(data, days, band):
    return [(m["event"] or m["id"], m["closed"], *r) for m, h in data if (r := trade(m, h, days, band))]


def summary(rows, label):
    if len(rows) < 10:
        return f"{label}: n={len(rows)}"
    xs = [r[2] for r in rows]
    by = defaultdict(float)
    for e, _, x, _ in rows:
        by[e] += x
    v = list(by.values())
    sd = statistics.pstdev(v)
    tcl = statistics.mean(v) / (sd / math.sqrt(len(v))) if sd else 0.0
    hold = statistics.mean(r[3] for r in rows)
    srt = sorted(rows, key=lambda r: r[1])
    h = len(srt) // 2
    worst = min(xs)
    return (f"{label}: n={len(xs)} events {len(v)} mean {statistics.mean(xs):+.2%} win {sum(x > 0 for x in xs) / len(xs):.1%} "
            f"event-t {tcl:+.2f} hold {hold:.1f} d ({statistics.mean(xs) / hold * 365:+.0%}/yr simple) "
            f"halves {statistics.mean(r[2] for r in srt[:h]):+.2%}/{statistics.mean(r[2] for r in srt[h:]):+.2%} worst {worst:+.0%}")


FROZEN2 = DATA_DIR / "poly_v2_rule.json"
RULES2 = {"L": (0, 0.05, 0.35), "F": (None, 0.80, 0.97)}      # side (0 = Yes, None = favourite), price band


def load_all():
    ms = {json.loads(line)["id"]: json.loads(line) for line in (OUT / "markets_all.jsonl").open(encoding="utf-8")}
    out = []
    for line in (OUT / "hist_all.jsonl").open(encoding="utf-8"):
        d = json.loads(line)
        if d["id"] in ms and d["h"]:
            out.append((ms[d["id"]], sorted(d["h"])))
    return out


def trade2(m, h, hours, rule):
    """V2: one decision at scheduled end - hours, past-only activity filter (>= 3 distinct prices in 24 h)."""
    t = m["end"] - hours * 3600
    if m["closed"] <= t + 3600:
        return None
    past = [p for tt, p in h if t - 86400 <= tt <= t]
    if len(set(past)) < 3:
        return None
    nxt = [x for x in h if x[0] > t]
    if not nxt or nxt[0][0] >= m["closed"] or nxt[0][0] - t > 7200:
        return None
    p = past[-1]
    side, lo, hi = rule
    s = side if side is not None else (0 if p >= 0.5 else 1)
    q = p if s == 0 else 1 - p
    if not lo <= q < hi:
        return None
    q1 = nxt[0][1] if s == 0 else 1 - nxt[0][1]
    cost = q1 + max(m["tick"] or 0.01, 0.005) + fee_rate(m["fee"]) * q1 * (1 - q1)
    if cost >= 1:
        return None
    return (m["res"][s] - cost) / cost, max(m["closed"] - nxt[0][0], 3600) / 86400


def run2(data, hours, rule):
    return [(m["event"] or m["id"], m["closed"], *r) for m, h in data if (r := trade2(m, h, hours, rule))]


def main2(cmd):
    data = load_all()
    design = [(m, h) for m, h in data if m["closed"] < CUT]
    test = [(m, h) for m, h in data if m["closed"] >= CUT]
    print(f"V2 unbiased universe with history: design {len(design)}, test {len(test)}")
    if cmd == "v2design":
        res = []
        for name, rule in RULES2.items():
            for hours in (24, 72):
                rows = run2(design, hours, rule)
                print("  ", summary(rows, f"{name} H {hours}"))
                for lbl, cond in (("vol < $10k", lambda m: m["vol"] < 1e4), ("vol >= $50k", lambda m: m["vol"] >= 5e4)):
                    print("      ", summary(run2([x for x in design if cond(x[0])], hours, rule), lbl))
                if len(rows) >= 300:
                    by = defaultdict(float)
                    for e, _, x, _ in rows:
                        by[e] += x
                    v = list(by.values())
                    res.append((statistics.mean(v) / (statistics.pstdev(v) / math.sqrt(len(v))), name, hours))
        res.sort(reverse=True)
        if res and res[0][0] > 2:
            FROZEN2.write_text(json.dumps({"rule": res[0][1], "hours": res[0][2]}))
            print("frozen", res[0])
        else:
            print("nothing passes (event-t > 2, n >= 300)", res)
    else:
        fr = json.loads(FROZEN2.read_text())
        sel = test
        print("TEST", summary(run2(sel, fr["hours"], RULES2[fr["rule"]]), f"{fr['rule']} H {fr['hours']}"))
        for lbl, cond in (("vol < $10k", lambda m: m["vol"] < 1e4), ("vol >= $10k", lambda m: m["vol"] >= 1e4)):
            print("  ", summary(run2([x for x in sel if cond(x[0])], fr["hours"], RULES2[fr["rule"]]), lbl))


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "design"
    if cmd.startswith("v2"):
        main2(cmd)
        return
    data = load()
    design = [(m, h) for m, h in data if m["closed"] < CUT]
    test = [(m, h) for m, h in data if m["closed"] >= CUT]
    print(f"markets with history: design {len(design)}, test {len(test)}")
    if cmd == "design":
        res = []
        for d in DAYS:
            for b in BANDS:
                rows = run(design, d, b)
                print("  ", summary(rows, f"D {d:2d} band {b}"))
                if len(rows) >= 300:
                    by = defaultdict(float)
                    for e, _, x, _ in rows:
                        by[e] += x
                    v = list(by.values())
                    sd = statistics.pstdev(v)
                    res.append((statistics.mean(v) / (sd / math.sqrt(len(v))) if sd else 0, d, b))
        for cat in ("sports", "crypto", "politics", "none"):
            sub = [(m, h) for m, h in design if (m["fee"] or "none").split("_")[0] == cat]
            print(f"  by category {cat} ({len(sub)} markets):", summary(run(sub, 3, (0.90, 0.97)), "D 3 band 0.90-0.97"))
        res.sort(reverse=True)
        if res and res[0][0] > 2:
            FROZEN.write_text(json.dumps({"days": res[0][1], "band": res[0][2]}))
            print("frozen", res[0])
        else:
            print("nothing passes (event-t > 2, n >= 300)", res[:3])
    else:
        fr = json.loads(FROZEN.read_text())
        print("TEST", summary(run(test, fr["days"], tuple(fr["band"])), f"D {fr['days']} band {fr['band']}"))


if __name__ == "__main__":
    main()
