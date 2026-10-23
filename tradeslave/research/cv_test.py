"""CV: veto M5 entries while the creator still holds > T% (vault "2026-10-23 creator dump veto (CV)", pre-registered
01:30 PDT). T chosen on train entries only.

    uv run python -m tradeslave.research.cv_test
"""

from __future__ import annotations

import gzip
import json
import math
import statistics

import numpy as np
from sklearn.linear_model import LogisticRegression

from .m4_bt import OUT as COINS
from .m5_bt import EXITS, FEATS, HELD, TRAIN, identical_top5, ret, samples

PARTS = TRAIN + HELD
TS = (0.5, 1, 2, 3, 5, 8)


def m5_entries(coins):
    """M5 v1-style entries (model fit on train, first sample with p >= train p90), E2 returns."""
    byid = {co["m"]: co for co in coins}
    S = samples(coins)
    tr = [s for s in S if s["part"] in TRAIN]
    X = lambda z: np.array([[math.log1p(max(0.0, s["f"][k])) if k not in ("vs_band", "vs_high") else s["f"][k]  # noqa: E731
                             for k in FEATS] for s in z])
    Xtr = X(tr)
    mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
    model = LogisticRegression(C=0.3, max_iter=2000).fit((Xtr - mu) / sd, np.array([s["y"] for s in tr]))
    for s, p in zip(S, model.predict_proba((X(S) - mu) / sd)[:, 1]):
        s["p"] = float(p)
    P = float(np.quantile([s["p"] for s in tr], 0.90))
    first = {}
    for s in sorted(S, key=lambda s: s["t"]):
        if s["p"] >= P and s["m"] not in first:
            first[s["m"]] = s
    ent = [s for s in first.values() if not identical_top5(byid[s["m"]]["rows"], s["k"])]
    for s in ent:
        s["r"] = ret(byid[s["m"]], s["k"], EXITS["E2 M4 exits, 15 s holder grace"])
    return ent


def main() -> None:
    with gzip.open(COINS, "rt", encoding="utf-8") as fh:
        coins = json.load(fh)
    ent = m5_entries(coins)
    tr = [s for s in ent if s["part"] in TRAIN]
    print("dev_hold at entry, quartiles:", np.quantile([s["f"]["dev_hold"] for s in ent], [0.25, 0.5, 0.75, 0.9]).round(2))
    print("TRAIN choice of T:")
    best = None
    for T in TS:
        kept = [s["r"] for s in tr if s["f"]["dev_hold"] <= T]
        share = len(kept) / len(tr)
        m = statistics.mean(kept) if kept else float("nan")
        print(f"   T {T:4}%  kept {len(kept):3d} ({share:4.0%}) mean {m:+.1%}")
        if share >= 0.5 and (best is None or m > best[1]):
            best = (T, m)
    T = best[0]
    print(f"CHOSEN T = {T}%")
    f = lambda v: (f"n {len(v):3d} mean {statistics.mean(v):+6.1%} <=-40% {sum(x <= -0.4 for x in v) / len(v):4.0%}"  # noqa: E731
                   if v else "n   0")
    ok_parts = []
    for pp in PARTS + ("HELD",):
        sel = [s for s in ent if s["part"] == pp or (pp == "HELD" and s["part"] in HELD)]
        k = [s["r"] for s in sel if s["f"]["dev_hold"] <= T]
        v = [s["r"] for s in sel if s["f"]["dev_hold"] > T]
        print(f"   {pp:8s} kept: {f(k)} | vetoed: {f(v)} | all {statistics.mean([s['r'] for s in sel]):+.1%}")
        if pp in HELD:
            ok_parts.append(bool(k and v and statistics.mean(k) > statistics.mean(v)
                                 and sum(x <= -0.4 for x in k) / len(k) < sum(x <= -0.4 for x in v) / len(v)))
    hk = [s["r"] for s in ent if s["part"] in HELD and s["f"]["dev_hold"] <= T]
    ha = [s["r"] for s in ent if s["part"] in HELD]
    ok = all(ok_parts) and statistics.mean(hk) >= statistics.mean(ha) + 0.03
    print(f"\nCV {'PASS' if ok else 'FAIL'}: held parts ok {ok_parts}, held kept {statistics.mean(hk):+.1%} vs all "
          f"{statistics.mean(ha):+.1%}; kept > 0: {statistics.mean(hk) > 0}")


if __name__ == "__main__":
    main()
