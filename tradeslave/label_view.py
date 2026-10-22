"""Blind labeling (LB): Rohit judges coins at the moment the bot would buy them, the future hidden until he decides.

Served by the dashboard at /label. Coins come from the NC dataset (research/nc_data.py) in a fixed random order, so
no coin is picked by its outcome. Labels go to data/m1/labels.jsonl.
"""

from __future__ import annotations

import gzip
import json
import random
import time

from .paths import DATA_DIR
from .trade_view import jsonl

NC = DATA_DIR / "m1" / "nc.json"
LABELS = DATA_DIR / "m1" / "labels.jsonl"
SHOWN = ("age", "n_buyers", "n_sellers", "buy_sol", "sell_sol", "spike", "from_open", "slot0_buyers", "slot0_sol",
         "dev_buy", "dev_sold", "flow5", "flow10", "buyers10", "sellers10", "top1_share", "top3_share", "big_sells",
         "max_sell", "sold_out_frac")


def _nc() -> dict:
    try:
        return {r["m"]: r for r in json.loads(NC.read_text()) if r.get("r_m1") is not None}
    except (OSError, ValueError):
        return {}


def _early(m: str) -> list:
    with gzip.open(DATA_DIR / "m1" / "early_trades" / f"{m}.json.gz", "rt", encoding="utf-8") as fh:
        tr = json.load(fh)["trades"]
    return [[x[2], x[5] / x[6], x[4], x[5], x[3]] for x in tr if not x[7] and x[6] > 0]


def label_stats() -> dict:
    nc, rows = _nc(), jsonl(LABELS)
    out = {"n": len(rows), "left": len(nc) - len({r["m"] for r in rows})}
    for k, sel in (("buy", True), ("skip", False)):
        v = [nc[r["m"]]["r_m1"] for r in rows if r["buy"] == sel and r["m"] in nc]
        out[k] = {"n": len(v), "mean": sum(v) / len(v) if v else None, "wins": sum(x > 0 for x in v)}
    return out


def label_next() -> dict:
    nc = _nc()
    done = {r["m"] for r in jsonl(LABELS)}
    todo = sorted(m for m in nc if m not in done)
    if not todo:
        return {"done": True, "stats": label_stats()}
    m = random.Random(len(done) * 7919 + 11).choice(todo)
    r = nc[m]
    info = {k: r.get(k) for k in ("name", "sym", "desc", "link", "score", "crit", "why", "first", "after", "mc")}
    info.update(m=m, t0=r["t0"], te=r["te"], feats={k: round(r[k], 3) for k in SHOWN if r.get(k) is not None})
    return {"info": info, "trades": [x for x in _early(m) if x[0] <= r["te"]], "stats": label_stats()}


def label_save(body: dict) -> dict:
    nc = _nc()
    m = str(body.get("m", ""))
    if m not in nc:
        return {"error": "unknown coin"}
    if m not in {r["m"] for r in jsonl(LABELS)}:
        rec = {"t": time.time(), "m": m, "buy": bool(body.get("buy")), "narr": body.get("narr"),
               "note": str(body.get("note") or "")[:500]}
        with LABELS.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    r = nc[m]
    try:
        c = json.loads((DATA_DIR / "m1" / "pm_candles" / f"{m}.json").read_text())
    except (OSError, ValueError):
        c = []
    return {"outcome": {k: r.get(k) for k in ("r_m1", "r_m2", "r_tp3", "x10", "x30", "x60", "max10m", "max60m")},
            "after": [x for x in _early(m) if x[0] > r["te"]],
            "candles": [y for y in c if r["te"] - 60 <= y[0] <= r["te"] + 1800], "stats": label_stats()}


