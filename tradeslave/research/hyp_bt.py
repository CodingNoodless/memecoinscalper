"""Backtests of the registered hypotheses (vault Strategies/Hypotheses/) on data/research/, costs from cost_model.

    uv run python -m tradeslave.research.hyp_bt run LP1          # grid on train, best-on-train variant -> validation
    uv run python -m tradeslave.research.hyp_bt holdout LP1 --spend-holdout   # ONE shot, writes holdout.jsonl only

Protocol (same for every hypothesis): every grid variant runs on TRAIN; the variant with the best train mean net
return is chosen (validation is not used to choose, so it stays out-of-sample); that one variant runs on VALIDATION,
plus every numeric parameter shifted -20% / +20% (param stability). Output in lab/results/<id>/:
trades.jsonl (train + validation rows of the chosen variant, `split` column), shift_*.jsonl (validation),
grid.json (train summary per variant). Then: uv run python tools/gate_check.py --id <id>.

Simulation rules (vault note "Simulation rules", shared): signal on a finished candle's close, entry at the next
candle's open; exits on later candles (open past a level fills at that open; a stop inside a candle fills at the
stop with the measured gap; stop wins ties; time exit at the first open after the horizon; a token that stops trading
exits at its last close). One open trade per token. Paper only.
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import itertools
import json
import math
import statistics
import time
from pathlib import Path

from ..cost_model import COSTS, trade_return
from ..paths import DATA_DIR, PROJECT_ROOT
from .research_data import K_CURVE, K_POOL

SPLITS = DATA_DIR / "research"
RESULTS = PROJECT_ROOT / "lab" / "results"
HOLDOUT_LOG = PROJECT_ROOT / "lab" / "holdout_log.jsonl"
SIZE = 0.1
T, O, H, LO, C, V, RES, PH = range(8)


def reserve(p: float, phase: str) -> float:
    return math.sqrt(max(p, 0.0) * (K_POOL if phase == "pool" else K_CURVE))


def load(split: str, allow_holdout: bool = False) -> list[dict]:
    if split == "holdout" and not allow_holdout:
        raise SystemExit("REFUSED: the holdout is read only by the one-shot `holdout --spend-holdout` command.")
    with gzip.open(SPLITS / split / "tokens.jsonl.gz", "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


# ---------- simulation ----------
def entry_fill(c: list, i: int) -> tuple[float, float, int, bool]:
    """Fill for a signal on candle i's close, at close time + latency. If the next candle starts in the very next
    minute, someone traded right after the signal: fill at that candle's open and check only its stop (the order of
    its high and low after our fill is unknown). Otherwise nobody traded for a full minute, so the curve / pool price
    was still close_i (static without trades): fill there, and every later candle is wholly after the entry.
    Returns (price, time, first candle index to scan, scan-first-candle-stop-only). No future candle decides whether
    the trade exists, so tokens that never trade again are kept (and exit at their last close)."""
    t_fill = c[i][T] + 60 + COSTS.entry_latency_s
    if i + 1 < len(c) and c[i + 1][T] <= c[i][T] + 60:
        return c[i + 1][O], t_fill, i + 1, True
    return c[i][C], t_fill, i + 1, False


def simulate(c: list, i: int, tgt: float | None, stop: float | None, hold_s: float) -> tuple[dict, int]:
    """Enter after the signal on candle i (see entry_fill). Returns (trade, index of the exit candle)."""
    p_in, t_in, j, first_stop_only = entry_fill(c, i)
    up = p_in * (1 + tgt) if tgt else math.inf
    dn = p_in * (1 - stop) if stop else -math.inf
    exit_p, stop_fill, k_exit, why = None, False, i, "end"
    for k in range(j, len(c)):
        x = c[k]
        if not (first_stop_only and k == j):
            if x[T] >= t_in + hold_s:
                exit_p, k_exit, why = x[O], k, "time"
                break
            if x[O] <= dn:
                exit_p, k_exit, why = x[O], k, "stop_gap_open"
                break
            if x[O] >= up:
                exit_p, k_exit, why = x[O], k, "target_open"
                break
        if x[LO] <= dn:
            exit_p, stop_fill, k_exit, why = dn, True, k, "stop"
            break
        if not (first_stop_only and k == j) and x[H] >= up:
            exit_p, k_exit, why = up, k, "target"
            break
    if exit_p is None:
        k_exit = len(c) - 1
        exit_p = c[k_exit][C]
        if k_exit > i and c[k_exit][T] >= t_in + hold_s:
            why = "time"
    gross = exit_p / p_in - 1
    ph_in = c[j][PH] if j < len(c) and first_stop_only else c[i][PH]
    r_in, r_out = reserve(p_in, ph_in), reserve(exit_p, c[k_exit][PH])
    ret = trade_return(gross, SIZE, r_in, r_out, stop_fill=stop_fill)
    return {"t": t_in, "gross": gross, "sol_reserve_in": r_in, "sol_reserve_out": r_out, "stop_fill": stop_fill,
            "ret": ret, "why": why}, k_exit


def run_signals(tokens, signal, p, once=False) -> list[dict]:
    """signal(tok, i, p) -> True on candle i's close. Entry at i+1. One open trade per token."""
    out = []
    for tok in tokens:
        c = tok["c"]
        i = 0
        while i < len(c):
            if signal(tok, i, p):
                tr, k = simulate(c, i, p.get("tgt"), p.get("stop"), p["hold"] * 60)
                tr["m"] = tok["m"]
                out.append(tr)
                if once:
                    break
                i = max(k, i + 1)
            else:
                i += 1
    return out


# ---------- hypotheses ----------
def _drop_vol(c, i, d):
    """Time-based: close_i <= (1-d) x max close of candles in the previous 5 minutes, and candle i's volume >= 2x the
    median of the previous 10 one-minute bins (minutes without trades count as zero volume)."""
    ti = c[i][T]
    prev = [x for x in c[max(0, i - 10):i] if x[T] >= ti - 300]
    if not prev:
        return False
    bins = [0.0] * 10
    for x in c[max(0, i - 10):i]:
        b = int((ti - x[T]) // 60) - 1
        if 0 <= b < 10:
            bins[b] += x[V]
    med = statistics.median(bins)
    return c[i][C] <= (1 - d) * max(x[C] for x in prev) and med > 0 and c[i][V] >= 2 * med


def sig_lp1(tok, i, p):
    c = tok["c"]
    x = c[i]
    return (x[PH] == "curve" and x[T] + 60 - tok["t0"] >= 180 and x[RES] >= 40 and _drop_vol(c, i, p["drop"]))


def sig_lp2(tok, i, p):
    c = tok["c"]
    x = c[i]
    if x[PH] != "pool" or reserve(x[C], "pool") < 60:
        return False
    first = tok.get("_pool0")
    if first is None:
        first = tok["_pool0"] = next((y[T] for y in c if y[PH] == "pool"), math.inf)
    return x[T] - first >= 1800 and _drop_vol(c, i, p["drop"])


def sig_max1(tok, i, p):
    c = tok["c"]
    x = c[i]
    if x[PH] != "curve" or x[T] + 60 - tok["t0"] < 900 or x[RES] < p["res"]:
        return False
    if tok.get("_max1_done"):
        return False
    tok["_max1_done"] = True                       # one decision per token: the first qualifying candle
    ts = [y[T] for y in c[:i + 1]]

    def close_asof(t):                             # last close at or before minute t (as-of lookup)
        k = bisect.bisect_right(ts, t) - 1
        return c[k][C] if k >= 0 else c[0][O]
    max_ret = max([(c[k][C] / c[k - 1][C] - 1) for k in range(1, i + 1)] + [0.0])
    ups = sum(close_asof(x[T] - 60 * m) > close_asof(x[T] - 60 * (m + 1)) for m in range(5))  # last 5 minutes
    k10 = bisect.bisect_right(ts, x[T] - 600) - 1
    r10 = c[k10][RES] if k10 >= 0 else c[0][RES]
    return max_ret <= p["maxret"] and ups >= 4 and x[RES] >= 1.2 * r10


def sig_vd1(tok, i, p):
    c = tok["c"]
    x = c[i]
    st = tok.setdefault("_vd", {"peak5": 0.0, "maxh": 0.0, "i": -1})
    for k in range(st["i"] + 1, i + 1):            # incremental, only candles <= i
        v5 = sum(y[V] for y in c[max(0, k - 5):k + 1] if y[T] > c[k][T] - 300)
        st["peak5"] = max(st["peak5"], v5)
        st["maxh"] = max(st["maxh"], c[k][H])
        st["v5"] = v5
        st["i"] = k
    if x[PH] != "curve" or x[T] + 60 - tok["t0"] < 600 or x[RES] < 45 or st["peak5"] <= 0:
        return False
    return st["v5"] <= p["vfrac"] * st["peak5"] and x[C] >= 0.7 * st["maxh"]


def run_tod1(tokens, p) -> list[dict]:
    """At UTC hour h0 each night: buy every token with a candle closing in [T0-300, T0], age 10-120 min at T0,
    reserve >= R; exit at the first open >= T0 + K h; stop 30%."""
    if not tokens:
        return []
    lo = min(t["c"][0][T] for t in tokens)
    hi = max(t["c"][-1][T] for t in tokens)
    day0 = math.floor(lo / 86400) * 86400
    out = []
    for d in range(int((hi - day0) / 86400) + 2):
        t0n = day0 + d * 86400 + p["h0"] * 3600
        if not lo <= t0n <= hi:
            continue
        for tok in tokens:
            age = t0n - tok["t0"]
            if not 600 <= age <= 7200:
                continue
            c = tok["c"]
            ts = [x[T] + 60 for x in c]                # close times
            i = bisect.bisect_right(ts, t0n) - 1
            if i < 0 or ts[i] < t0n - 300 or c[i][RES] < p["res"]:
                continue
            hold = t0n + p["k"] * 3600 - (c[i][T] + 60)
            tr, _ = simulate(c, i, None, p["stop"], hold)
            tr.update(m=tok["m"], night=t0n)
            out.append(tr)
    return out


HYP = {
    "LP1": dict(signal=sig_lp1, grid={"drop": [0.20, 0.25, 0.30], "tgt": [0.15, 0.25, 0.40], "hold": [10, 30]},
                fixed={"stop": 0.15}),
    "LP2": dict(signal=sig_lp2, grid={"drop": [0.15, 0.20, 0.30], "tgt": [0.15, 0.30], "hold": [15, 60]},
                fixed={"stop": 0.15}),
    "MAX1": dict(signal=sig_max1, once=True, grid={"res": [38, 45], "maxret": [0.20, 0.35, 0.50], "hold": [30, 90]},
                 fixed={"tgt": 0.25, "stop": 0.20}),
    "TOD1": dict(runner=run_tod1, grid={"h0": [23, 0], "k": [1, 2, 3], "res": [35, 50]}, fixed={"stop": 0.30},
                 no_shift=("h0",)),
    "VD1": dict(signal=sig_vd1, grid={"vfrac": [0.10, 0.20, 0.30], "hold": [30, 120], "stop": [0.15, 0.25]},
                fixed={"tgt": 0.30}),
}


def run_one(hid, tokens, p):
    h = HYP[hid]
    for t in tokens:                                    # drop per-token caches between runs
        for k in [k for k in t if k.startswith("_")]:
            del t[k]
    if "runner" in h:
        return h["runner"](tokens, p)
    return run_signals(tokens, h["signal"], p, once=h.get("once", False))


def summ(r):
    if not r:
        return {"n": 0}
    rets = [x["ret"] for x in r]
    win = sum(x for x in rets if x > 0)
    loss = -sum(x for x in rets if x < 0)
    return {"n": len(rets), "mean": round(statistics.mean(rets), 4), "median": round(statistics.median(rets), 4),
            "win": round(sum(x > 0 for x in rets) / len(rets), 3), "pf": round(win / loss, 3) if loss else None,
            "exits": {w: sum(x["why"] == w for x in r) for w in sorted({x["why"] for x in r})}}


def cmd_run(hid):
    h = HYP[hid]
    d = RESULTS / hid
    d.mkdir(parents=True, exist_ok=True)
    train, val = load("train"), load("validation")
    keys = list(h["grid"])
    grid = []
    for combo in itertools.product(*(h["grid"][k] for k in keys)):
        p = dict(zip(keys, combo), **h["fixed"])
        s = summ(run_one(hid, train, p))
        grid.append({"p": p, **s})
        print(f"train {p}: {s}", flush=True)
    ok = [g for g in grid if g["n"] >= 30] or grid
    best = max(ok, key=lambda g: g.get("mean", -9))["p"]
    tr_rows = run_one(hid, train, best)
    va_rows = run_one(hid, val, best)
    with (d / "trades.jsonl").open("w", encoding="utf-8") as fh:
        for split, rows in (("train", tr_rows), ("validation", va_rows)):
            for x in rows:
                fh.write(json.dumps({**x, "split": split}) + "\n")
    for f in d.glob("shift_*.jsonl"):
        f.unlink()
    for k, v in best.items():
        if k in h.get("no_shift", ()) or not isinstance(v, (int, float)):
            continue
        for tag, mult in (("down", 0.8), ("up", 1.2)):
            q = dict(best, **{k: v * mult})
            rows = run_one(hid, val, q)
            with (d / f"shift_{k}_{tag}.jsonl").open("w", encoding="utf-8") as fh:
                for x in rows:
                    fh.write(json.dumps({**x, "split": "validation"}) + "\n")
    (d / "grid.json").write_text(json.dumps({"chosen": best, "chosen_by": "best train mean (n >= 30)",
                                             "grid": grid, "built": time.strftime("%Y-%m-%d %H:%M")}, indent=1))
    print(f"chosen on train: {best}")
    print(f"train {summ(tr_rows)}")
    print(f"validation {summ(va_rows)}")


def cmd_holdout(hid):
    if HOLDOUT_LOG.exists() and any(json.loads(x).get("id") == hid for x in HOLDOUT_LOG.read_text().splitlines() if x.strip()):
        raise SystemExit(f"REFUSED: {hid} already used its holdout shot.")
    best = json.loads((RESULTS / hid / "grid.json").read_text())["chosen"]
    rows = run_one(hid, load("holdout", allow_holdout=True), best)
    with (RESULTS / hid / "holdout.jsonl").open("w", encoding="utf-8") as fh:
        for x in rows:
            fh.write(json.dumps({**x, "split": "holdout"}) + "\n")
    print(f"holdout trades written for {hid} ({len(rows)} rows); score with tools/gate_check.py --final {hid}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("run", "holdout"))
    ap.add_argument("id", choices=sorted(HYP))
    ap.add_argument("--spend-holdout", action="store_true")
    a = ap.parse_args()
    if a.cmd == "run":
        cmd_run(a.id)
    elif not a.spend_holdout:
        raise SystemExit("holdout needs --spend-holdout (one shot per id; only after every validation gate passes)")
    else:
        cmd_holdout(a.id)


if __name__ == "__main__":
    main()
