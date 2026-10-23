"""M5 v2 search on TRAINING data only (vault "2026-10-22 M5 runner bot (M5)", pre-registered 23:08 PDT).

    uv run python -m tradeslave.research.m5_search          # train search; writes data/m1/m5v2_design.json
    uv run python -m tradeslave.research.m5_search held     # the frozen design on held out, once

Target: training mean >= +20%/trade with >= 20 trades, > 0 without the best 2, and each training part > 0.
"""

from __future__ import annotations

import gzip
import itertools
import json
import math
import statistics
import sys

import numpy as np
from sklearn.linear_model import LogisticRegression

from ..paths import DATA_DIR
from .m4_bt import OUT as COINS, stats
from .m5_bt import FEATS, HELD, TRAIN, identical_top5, ret, samples

DESIGN = DATA_DIR / "m1" / "m5v2_design.json"
ROUND = 2 if "r2" in sys.argv else 1
if ROUND == 1:
    STOPS, FFS, GRACES = (0.70, 0.80, 0.85), (None, (20, 1.0), (30, 1.0), (30, 1.1)), (15, None)
    TPS = {"tp2": {"tp": 2.0}, "tp1.5": {"tp": 1.5}, "half1.5+tp2": {"tp": 2.0, "half_tp": 1.5}}
    QS, VS, HS, CS, WINS = (0.8, 0.9, 0.95, 0.97, 0.98, 0.99), (0.0, 0.8, 0.9), (0, 30, 50), (10000, 20000), (180,)
else:                                   # round 2 (pre-registered 23:10): refine around round 1's cluster
    STOPS, FFS, GRACES = (0.70, 0.80), ((20, 1.1), (30, 1.1), (30, 1.2), (45, 1.15), (60, 1.2)), (None,)
    TPS = {"tp2": {"tp": 2.0}, "tp2.5": {"tp": 2.5}, "tp3": {"tp": 3.0},
           "half2+trail30": {"tp": 99.0, "half_tp": 2.0, "trend_arm": 2.0, "trend_give": 0.70, "max_hold": 900}}
    QS, VS, HS, CS, WINS = (0.9, 0.93, 0.95, 0.97, 0.98), (0.0,), (50, 70, 100), (8000, 10000, 12000), (120, 180)


def exit_def(stop, ff, grace, tp):
    P = {"hdecl": 0.10 if grace is not None else None, "hgrace": grace or 0, "ff": ff, **TPS[tp]}
    return dict(stop=stop, P=P)


def prepare():
    with gzip.open(COINS, "rt", encoding="utf-8") as fh:
        coins = json.load(fh)
    byid = {co["m"]: co for co in coins}
    S = samples(coins)
    tr = [s for s in S if s["part"] in TRAIN]
    X = lambda z: np.array([[math.log1p(max(0.0, s["f"][k])) if k not in ("vs_band", "vs_high") else s["f"][k]   # noqa: E731
                             for k in FEATS] for s in z])
    Xtr = X(tr)
    mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
    model = LogisticRegression(C=0.3, max_iter=2000).fit((Xtr - mu) / sd, np.array([s["y"] for s in tr]))
    for s, p in zip(S, model.predict_proba((X(S) - mu) / sd)[:, 1]):
        s["p"] = float(p)
    S.sort(key=lambda s: s["t"])
    return byid, S, tr, (mu, sd, model)


def entries(S, byid, parts, P, v, H, C, W=180):
    first, out = set(), []
    for s in S:
        if s["part"] not in parts or s["m"] in first:
            continue
        f = s["f"]
        if s["p"] >= P and f["vs_high"] >= v and f["holders"] >= H and f["mc_k"] <= C / 1000 and f["since_band"] <= W:
            first.add(s["m"])
            if not identical_top5(byid[s["m"]]["rows"], s["k"]):
                out.append(s)
    return out


def main() -> None:
    byid, S, tr, (mu, sd, model) = prepare()
    ps = sorted({round(float(np.quantile([s["p"] for s in tr], q)), 4) for q in QS})
    cache, rows = {}, []
    exits = list(itertools.product(STOPS, FFS, GRACES, TPS))
    for P, v, H, C, W in itertools.product(ps, VS, HS, CS, WINS):
        ent = entries(S, byid, TRAIN, P, v, H, C, W)
        if len(ent) < 20:
            continue
        for ex in exits:
            r = [cache.setdefault((s["m"], s["k"], ex), ret(byid[s["m"]], s["k"], exit_def(*ex))) for s in ent]
            byp = {pp: [x for s, x in zip(ent, r) if s["part"] == pp] for pp in TRAIN}
            ok = (sum(sorted(r)[:-2]) > 0 and all(byp[pp] and statistics.mean(byp[pp]) > 0 for pp in TRAIN))
            rows.append((statistics.mean(r), ok, (P, v, H, C, W), ex, r, byp))
    good = sorted((z for z in rows if z[1]), key=lambda z: -z[0])
    print(f"settings tried {len(rows)}; meeting the guardrails {len(good)}")
    print("\nTOP 15 on train (guardrails met):")
    for m, ok, en, ex, r, byp in good[:15]:
        print(f"   P {en[0]:.3f} v {en[1]} H {en[2]:3d} C {en[3] // 1000:2d}k W {en[4]} | stop {ex[0]} ff {ex[1]} grace {ex[2]} {ex[3]:12s} | "
              f"{stats(r)} | " + " ".join(f"{pp} {statistics.mean(byp[pp]):+.1%}({len(byp[pp])})" for pp in TRAIN))
    best_any = max(rows, key=lambda z: z[0]) if rows else None
    if best_any:
        print(f"\nbest train mean ignoring guardrails: {best_any[0]:+.1%} (n {len(best_any[4])})")
    if not good or good[0][0] < 0.20:
        print("\nTARGET NOT REACHED on train (>= +20% with guardrails): held out stays sealed.")
        return
    m, ok, en, ex, r, byp = good[0]
    DESIGN.write_text(json.dumps({"entry": en, "exit": ex, "train_mean": m, "train_n": len(r),
                                  "mu": mu.tolist(), "sd": sd.tolist(), "coef": model.coef_[0].tolist(),
                                  "intercept": float(model.intercept_[0]), "feats": FEATS}))
    print(f"\nFROZEN: {en} {ex} train mean {m:+.1%} n {len(r)} -> {DESIGN}")


def held() -> None:
    d = json.loads(DESIGN.read_text())
    byid, S, tr, _ = prepare()
    P, v, H, C, W = d["entry"]
    ex = tuple(tuple(x) if isinstance(x, list) else x for x in d["exit"])
    out = {}
    for pp in TRAIN + HELD:
        ent = entries(S, byid, (pp,), P, v, H, C, W)
        out[pp] = [(s["t"], ret(byid[s["m"]], s["k"], exit_def(*ex))) for s in ent]
        print(f"   {pp:8s} {stats([x for _, x in out[pp]])}")
    h = sorted(x for pp in HELD for x in out[pp])
    a = sorted(x for pp in TRAIN + HELD for x in out[pp])
    print(f"   HELD OUT {stats([x for _, x in h])}")
    print(f"   ALL      {stats([x for _, x in a])}")
    json.dump({"held": h, "all": a}, open(DATA_DIR / "m1" / "m5v2_results.json", "w"))


if __name__ == "__main__":
    held() if sys.argv[1:2] == ["held"] else main()
