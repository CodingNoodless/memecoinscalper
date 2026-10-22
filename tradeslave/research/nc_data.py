"""Narrative-coin dataset (NC): every score >= 7 coin the M bots would have bought, with what was known at entry and
what happened next. Shared ground for the three fixes: narrative scoring, pre-buy checks, exits.

    uv run python -m tradeslave.research.nc_data      -> data/m1/nc.json

Entry (the bots' rule, roughly): the first curve trade at least 1 s after the bot scored the coin, within 180 s of
create, with market cap $4-7k. Entry price = that trade's price. Features use only trades before the entry.
Outcomes come from 1-minute candles that start after the entry minute and from the exact trades up to 120 s.
"""

from __future__ import annotations

import gzip
import json
import math
import time

from ..paths import DATA_DIR
from .early_trades import OUT as EARLY
from .entry_bt import CACHE

SOL_USD, FEE = 105.0, 0.0125 + 0.005
OUT = DATA_DIR / "m1" / "nc.json"


def cands():
    """Best score row per coin across bots (with the narrative fields), and the earliest scoring age."""
    best, age = {}, {}
    for v in ("m1", "m2", "m3"):
        for line in (DATA_DIR / v / "cands.jsonl").open(encoding="utf-8"):
            c = json.loads(line)
            if c.get("score") is None or "age" not in c:
                continue
            age[c["m"]] = min(age.get(c["m"], 9e9), c["age"])
            c["bot"] = v
            if c["m"] not in best or c["score"] > best[c["m"]]["score"]:
                best[c["m"]] = c
    return best, age


def sim(c, te, e, tp, stop, hold):
    """Net return of a position bought at price e at time te, on 1-min candles from the next full minute."""
    b = e * (1 + FEE)
    for y in c:
        if y[0] < te:
            continue
        if y[0] - te > hold:
            return y[1] * (1 - FEE) / b - 1
        if y[3] <= e * stop:
            return min(e * stop, y[1]) * 0.97 * (1 - FEE) / b - 1
        if y[2] >= e * tp:
            return e * tp * (1 - FEE) / b - 1
    return c[-1][4] * (1 - FEE) / b - 1 if c and c[-1][0] > te else None


def features(tr, t0, te, e):
    pre = [x for x in tr if x[2] < te]
    dev = tr[0][3]
    buys = [x for x in pre if x[4]]
    sells = [x for x in pre if not x[4]]
    px = [x[5] / x[6] for x in pre]
    hi = max(px) if px else e
    f = dict(age=te - t0, n_tr=len(pre), n_buyers=len({x[3] for x in buys}), n_sellers=len({x[3] for x in sells}),
             buy_sol=sum(x[5] for x in buys), sell_sol=sum(x[5] for x in sells),
             spike=hi / e, from_open=e / px[0] if px else 1.0,
             slot0_buyers=len({x[3] for x in buys if x[0] == tr[0][0]}),
             slot0_sol=sum(x[5] for x in buys if x[0] == tr[0][0]))
    dv_b = sum(x[5] for x in buys if x[3] == dev)
    dv_s = sum(x[5] for x in sells if x[3] == dev)
    f.update(dev_buy=dv_b, dev_sold=dv_s / dv_b if dv_b else 0.0)
    for w in (3, 5, 10, 20):
        last = [x for x in pre if x[2] >= te - w]
        f[f"flow{w}"] = sum(x[5] if x[4] else -x[5] for x in last)
        f[f"buyers{w}"] = len({x[3] for x in last if x[4]})
        f[f"sellers{w}"] = len({x[3] for x in last if not x[4]})
    per = {}
    for x in buys:
        per[x[3]] = per.get(x[3], 0.0) + x[5]
    top = sorted(per.values(), reverse=True)
    f["top1_share"] = top[0] / sum(top) if top else 0.0
    f["top3_share"] = sum(top[:3]) / sum(top) if top else 0.0
    f["big_sells"] = sum(1 for x in sells if x[5] >= 0.5)
    f["max_sell"] = max((x[5] for x in sells), default=0.0)
    sold = {}
    for x in sells:
        sold[x[3]] = sold.get(x[3], 0.0) + x[5]
    f["sold_out_frac"] = sum(1 for w in per if sold.get(w, 0) >= 0.8 * per[w]) / len(per) if per else 0.0
    f["last_buy"] = 1.0 if pre and pre[-1][4] else 0.0
    if len(px) >= 2:
        ts = [x[2] for x in pre if x[2] >= te - 10]
        f["ret10"] = e / px[max(0, len(px) - len(ts) - 1)] - 1
    else:
        f["ret10"] = 0.0
    return f


def main() -> None:
    best, age = cands()
    rows, t_start = [], time.time()
    for m, c in best.items():
        if c["score"] < 7:
            continue
        p = EARLY / f"{m}.json.gz"
        cp = CACHE / f"{m}.json"
        if not (p.exists() and cp.exists()):
            continue
        tr = [y for y in json.load(gzip.open(p, "rt", encoding="utf-8"))["trades"] if not y[7] and y[6] > 0]
        if len(tr) < 3:
            continue
        t0, ts = tr[0][2], tr[0][2] + age[m] + 1
        ent = next((x for x in tr if ts <= x[2] <= t0 + 180 and 4000 <= x[5] / x[6] * 1e9 * SOL_USD * k(tr) <= 7000),
                   None)
        if ent is None:
            continue
        te, e = ent[2], ent[5] / ent[6]
        cn = json.loads(cp.read_text())
        kk = 10 ** round(math.log10(cn[0][4] / (tr[0][5] / tr[0][6]))) if cn else 1
        ec = e * kk
        r = dict(m=m, name=c.get("name"), t0=t0, te=te, score=c["score"], crit=c.get("crit"), first=c.get("first"),
                 after=bool(c.get("after")), improves=c.get("improves"), model=c.get("model"), bot=c["bot"],
                 link=c.get("link"), why=c.get("why"), desc=(c.get("desc") or "")[:400], sym=c.get("sym"),
                 hour=time.localtime(t0).tm_hour, mc=e * 1e9 * SOL_USD * k(tr))
        r.update(features(tr, t0, te, e))
        after = [x for x in tr if x[2] > te]
        for s in (10, 30, 60):
            w = [x[5] / x[6] for x in after if x[2] <= te + s]
            r[f"x{s}"] = (w[-1] / e) if w else None
        r["max10m"] = max((y[2] for y in cn if te <= y[0] <= te + 600), default=None)
        r["max10m"] = r["max10m"] / ec if r["max10m"] else None
        r["max60m"] = max((y[2] for y in cn if te <= y[0] <= te + 3600), default=None)
        r["max60m"] = r["max60m"] / ec if r["max60m"] else None
        r["r_m1"] = sim(cn, te, ec, 2.0, 0.7, 600)
        r["r_m2"] = sim(cn, te, ec, 2.0, 0.9, 600)
        r["r_tp3"] = sim(cn, te, ec, 3.0, 0.7, 1800)
        rows.append(r)
    OUT.write_text(json.dumps(rows))
    print(len(rows), "coins ->", OUT, f"({time.time() - t_start:.0f} s)")


def k(tr):
    """Trade prices are SOL per UI token already (baseAmount in UI units) -> 1. Kept as a hook if that changes."""
    return 1.0


if __name__ == "__main__":
    main()
