"""Strategy N4: pick launches whose graduation odds beat the lottery price, hold to graduation.

    uv run python -m tradeslave.research.grad_model

The bonding curve floors the price at the launch level, and graduation pays ~14x launch, so a buy at
minute m is a lottery ticket with bounded downside. Random launches graduate ~1.3% of the time, so
buying blindly loses 4-10%/trade (measured). The question: can features known at minute m pick
tokens whose odds are high enough?

Data: research.long_paths (random launches, pump.fun 1-minute candles in SOL) + recorder metadata
(dev buy, socials, name) + the creator's track record up to that moment (launches and graduations
before this one). Label: net return of SIZE SOL bought at the first trade after minute m, sold at
graduation or after HOLD_H hours (research.dip_runner fill model). Design/test cut fixed at
dip_runner.DESIGN_CUT_T; the model is fit on design only.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor

from ..scalper.recorder import TICK_DIR, read_ticks
from .dip_runner import DESIGN_CUT_T, GRAD_PRICE, PRIO, SIZE, buy, sell_value
from .long_paths import OUT

P0 = 2.795e-8
HOLD_H = 6
MINUTES = (2, 5, 10)


def meta():
    creates, info, grads = {}, defaultdict(dict), {}
    for r in read_ticks(sorted(TICK_DIR.glob("*.jsonl.gz"))):
        k, m = r.get("k"), r.get("m")
        if k == "create" and m:
            creates[m] = r
        elif k == "meta" and m:
            info[m] = r
        elif k == "migr" and m:
            grads.setdefault(m, float(r["t"]))
    # creator track record, strictly before each launch
    by_creator = defaultdict(list)
    for m, r in creates.items():
        if r.get("cr"):
            by_creator[r["cr"]].append((float(r["t"]), m))
    record = {}
    for cr, lst in by_creator.items():
        lst.sort()
        for i, (t, m) in enumerate(lst):
            prior = lst[:i]
            record[m] = (len(prior), sum(1 for _, pm in prior if pm in grads and grads[pm] < t))
    return creates, info, record


def features(d: dict, minute: float, cr: dict, mi: dict, rec: tuple) -> list[float] | None:
    t0, c = d["t0"], d["curve"]
    now = t0 + minute * 60
    past = [x for x in c if x[0] + 60 <= now]
    if not past:
        return None
    closes = [x[4] for x in past]
    last = closes[-1]
    if last >= GRAD_PRICE * 0.98:
        return None
    vol = [x[5] for x in past]
    recent = [x for x in past if x[0] + 60 > now - 180]
    prev2 = [x for x in past if x[0] + 60 <= now - 120]
    return [
        last / P0, max(x[2] for x in past) / P0, last / max(closes), len(past), sum(vol),
        sum(x[5] for x in recent), len(recent), (now - (past[-1][0] + 60)) / 60,
        last / (prev2[-1][4] if prev2 else P0), statistics.pstdev([math.log(x[4] / x[1]) for x in past if x[1] > 0]) if len(past) > 1 else 0,
        float(cr.get("ib") or 0), float(cr.get("dt") or 0), len(cr.get("nm") or ""), len(cr.get("s") or ""),
        float(bool(mi.get("x_twitter"))), float(bool(mi.get("x_telegram"))), float(bool(mi.get("x_website"))),
        float(mi.get("x_desc_len") or 0), float(bool(mi)),
        float(rec[0]), float(rec[1]), float(rec[1] / rec[0]) if rec[0] else -1.0,
        ((t0 % 86400) / 3600),
    ]


NAMES = ["mult", "max_mult", "from_peak", "active_min", "vol_sol", "vol_3m", "active_3m", "idle_min", "ret_2m", "vol_1m",
         "dev_buy", "dev_tokens", "name_len", "sym_len", "twitter", "telegram", "website", "desc_len", "has_meta",
         "creator_launches", "creator_grads", "creator_grad_rate", "hour_utc"]


def outcome(d: dict, minute: float) -> float | None:
    t0, c = d["t0"], d["curve"]
    now = t0 + minute * 60
    nxt = [x for x in c if x[0] + 60 > now]
    if not nxt:
        past = [x for x in c if x[0] + 60 <= now]
        p = past[-1][4] if past else P0
        tokens, net = buy(p, SIZE)
        return sell_value(p, tokens, net) - SIZE - 2 * PRIO
    tokens, net = buy(nxt[0][1], SIZE)
    for x in nxt:
        if x[4] >= GRAD_PRICE * 0.98 or x[0] > t0 + HOLD_H * 3600:
            return sell_value(x[4], tokens, net) - SIZE - 2 * PRIO
    return sell_value(nxt[-1][4], tokens, net) - SIZE - 2 * PRIO


def summary(xs) -> str:
    if len(xs) < 5:
        return f"n={len(xs)}"
    m, sd = statistics.mean(xs), statistics.pstdev(xs)
    gains = sum(x for x in xs if x > 0)
    return (f"n={len(xs):5d} ROI {m / SIZE * 100:+7.1f}% med {statistics.median(xs) / SIZE * 100:+6.1f}% "
            f"win {sum(x > 0 for x in xs) / len(xs):4.0%} t {m / (sd / math.sqrt(len(xs))) if sd else 0:+5.2f} "
            f"top-share {max(xs) / gains if gains > 0 else 1:.0%}")


def main() -> None:
    creates, info, record = meta()
    toks = [json.loads(p.read_text()) for p in OUT.glob("*.json")]
    toks = [d for d in toks if d["m"] in creates]
    print(f"{len(toks)} random launches with paths; design {sum(d['t0'] < DESIGN_CUT_T for d in toks)}")
    for minute in MINUTES:
        X, y, is_test, grad = [], [], [], []
        for d in toks:
            f = features(d, minute, creates[d["m"]], info.get(d["m"], {}), record.get(d["m"], (0, 0)))
            if f is None:
                continue
            o = outcome(d, minute)
            if o is None:
                continue
            X.append(f)
            y.append(o)
            is_test.append(d["t0"] >= DESIGN_CUT_T)
            grad.append(max(x[4] for x in d["curve"]) >= GRAD_PRICE * 0.98)
        X, y, is_test = np.array(X), np.array(y), np.array(is_test)
        tr, te = ~is_test, is_test
        model = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=15,
                                              min_samples_leaf=40, l2_regularization=1.0, random_state=0)
        model.fit(X[tr], y[tr])
        pred = model.predict(X[te])
        yt = y[te]
        order = np.argsort(-pred)
        g = np.array(grad)[te]
        print(f"\nminute {minute}: design n={tr.sum()} (all-in ROI {y[tr].mean() / SIZE:+.1%}), test n={te.sum()} "
              f"(all-in ROI {yt.mean() / SIZE:+.1%}, graduation {g.mean():.2%})")
        for frac in (0.01, 0.02, 0.05, 0.10, 0.20):
            k = max(5, int(frac * len(order)))
            sel = order[:k]
            print(f"   top {frac:4.0%} by model: {summary(list(yt[sel]))} | graduation rate {g[sel].mean():.1%}")
        imp = sorted(zip(NAMES, np.abs(np.corrcoef(np.c_[X[tr], y[tr]].T)[-1, :-1])), key=lambda z: -np.nan_to_num(z[1]))[:6]
        print("   top |corr| with outcome (design):", ", ".join(f"{n} {v:.3f}" for n, v in imp))


if __name__ == "__main__":
    main()
