"""Rohit's paper trading on replayed coins (dashboard /label): every NC coin (scored >= 7, reached the bots' buy band)
replayed trade by trade from its create; he buys and sells when he wants, or skips it. Fills are simulated exactly
like the bots': 0.1 SOL through the bonding curve (1.25% fee each way, own price impact, 0.0005 SOL per tx) at the
curve state 1 s after the click. Records go to data/m1/paper_trades.jsonl; the bots' results on the same coins come
from NC (M1's exits at the bots' entry) and from the bots' own trade logs.
"""

from __future__ import annotations

import gzip
import json
import math
import random
import statistics
import time

from .paths import DATA_DIR
from .research.c1_curve import T0, V0, sell_curve
from .research.tf_replay import path
from .trade_view import jsonl

NC = DATA_DIR / "m1" / "nc.json"
TICKS = DATA_DIR / "m1" / "ticks"
OUT = DATA_DIR / "m1" / "paper_trades.jsonl"
K, SIZE, TX, FEE, DELAY = V0 * T0, 0.1, 0.0005, 0.0125, 1.0
SOL_USD = 105.0


def _pool() -> dict:
    try:
        rows = json.loads(NC.read_text())
    except (OSError, ValueError):
        return {}
    return {r["m"]: r for r in rows if r.get("r_m1") is not None and (TICKS / f"{r['m']}.json.gz").exists()}


def _rows(r: dict) -> list:
    """[t, spot SOL/token, is_buy, sol, wallet, tokens, holders] from create to the end of the tick window."""
    with gzip.open(TICKS / f"{r['m']}.json.gz", "rt", encoding="utf-8") as fh:
        tr = sorted(json.load(fh)["trades"], key=lambda x: (x[0], x[1]))
    pre, post = path({"t": -1.0, "p0": 1.0}, tr, r["t0"])
    return pre + post


def _bots(m: str) -> list:
    out = []
    for v in ("m1", "m2", "m3", "m4"):
        for x in jsonl(DATA_DIR / v / "trades.jsonl"):
            if x.get("m") == m and "ret" in x:
                out.append({"v": v, "ret": x["ret"], "exit": x["exit"], "t": x["t"], "t_exit": x["t_exit"]})
    return out


def _spot_at(rows: list, t: float) -> float:
    p = rows[0][1]
    for r in rows:
        if r[0] > t:
            break
        p = r[1]
    return p


def stats() -> dict:
    pool, recs = _pool(), jsonl(OUT)
    tr = [x for x in recs if x["action"] == "trade"]
    v = [x["ret"] for x in tr]
    bot_same = [pool[x["m"]]["r_m1"] for x in tr if x["m"] in pool]
    bot_all = [pool[x["m"]]["r_m1"] for x in recs if x["m"] in pool]
    m = statistics.mean(v) if v else None
    t = m / (statistics.stdev(v) / len(v) ** 0.5) if len(v) > 2 and statistics.stdev(v) else None
    return {"seen": len(recs), "left": len(pool) - len({x["m"] for x in recs}), "trades": len(v),
            "skips": len(recs) - len(v), "mean": m, "t": t, "wins": sum(x > 0 for x in v),
            "sol": round(sum(v) * SIZE, 4), "ex2": round(sum(sorted(v)[:-2]) * SIZE, 4) if len(v) > 2 else None,
            "bot_same": statistics.mean(bot_same) if bot_same else None,
            "bot_all": statistics.mean(bot_all) if bot_all else None}


def paper_next() -> dict:
    pool = _pool()
    done = {x["m"] for x in jsonl(OUT)}
    todo = sorted(m for m in pool if m not in done)
    if not todo:
        return {"done": True, "stats": stats()}
    m = random.Random(len(done) * 7919 + 23).choice(todo)
    r = pool[m]
    rows = _rows(r)
    info = {k: r.get(k) for k in ("name", "sym", "desc", "link", "score", "crit", "why", "first", "after")}
    info.update(m=m, t0=r["t0"], te=r["te"], sol_usd=SOL_USD)
    return {"info": info, "trades": [[x[0], x[1], x[2], round(x[3], 4), x[4][:6], x[6]] for x in rows],
            "stats": stats()}


def paper_save(body: dict) -> dict:
    pool = _pool()
    m = str(body.get("m", ""))
    if m not in pool:
        return {"error": "unknown coin"}
    r = pool[m]
    rows = _rows(r)
    rec = {"t": time.time(), "m": m, "name": r.get("name"), "action": "skip", "narr": body.get("narr"),
           "note": str(body.get("note") or "")[:500]}
    if body.get("t_buy") is not None:
        tb = float(body["t_buy"]) + DELAY
        ts = float(body["t_sell"]) + DELAY if body.get("t_sell") is not None else rows[-1][0]
        pb, ps = _spot_at(rows, tb), _spot_at(rows, ts)
        v = math.sqrt(pb * K)
        q = K / v - K / (v + SIZE * (1 - FEE))
        ret = (sell_curve(q, ps) - 2 * TX) / SIZE - 1
        rec.update(action="trade", t_buy=tb, t_sell=ts, p_buy=pb, p_sell=ps, ret=ret,
                   held=ts - tb, buy_after_create=tb - r["t0"], mc_buy=pb * 1e9 * SOL_USD)
    if m not in {x["m"] for x in jsonl(OUT)}:
        with OUT.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    return {"rec": rec, "bot": {"r_m1": r["r_m1"], "te": r["te"], "mc": r.get("mc"), "trades": _bots(m)},
            "stats": stats()}
