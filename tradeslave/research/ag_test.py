"""AG: coin age gate on M5 v1 entries (vault "2026-10-23 coin age gate (AG)", pre-registered 13:40 PDT).

    uv run python -u -m tradeslave.research.ag_test

Same as cv_test.m5_entries (model refit on train, P = train p90, identical-top-5 veto, E2 exits), except a sample only
counts once the coin is >= A seconds past its first trade. ER2 split (train before Oct 23 00:00 PDT); validation is
every validation coin.
"""

from __future__ import annotations

import gzip
import json
import math
import statistics

import numpy as np
from sklearn.linear_model import LogisticRegression

from . import er_test

AGES = (0, 30, 60)


def entries(coins, A):
    from .m5_bt import EXITS, FEATS, identical_top5, ret, samples
    byid = {co["m"]: co for co in coins}
    S = samples(coins)
    tr = [s for s in S if s["part"] in er_test.TRAIN]
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
        rows = byid[s["m"]]["rows"]
        if s["p"] >= P and s["m"] not in first and rows[s["k"]][0] - rows[0][0] >= A:
            first[s["m"]] = s
    ent = [s for s in first.values() if not identical_top5(byid[s["m"]]["rows"], s["k"])]
    for s in ent:
        s["r"] = ret(byid[s["m"]], s["k"], EXITS["E2 M4 exits, 15 s holder grace"])
    return ent


def summ(v):
    if not v:
        return "n   0"
    return (f"n {len(v):4d} mean {statistics.mean(v):+6.1%} median {statistics.median(v):+6.1%} "
            f"<=-25% {sum(x <= -0.25 for x in v) / len(v):4.0%} win {sum(x > 0 for x in v) / len(v):4.0%}")


def main() -> None:
    er_test.resplit()
    from . import m5_bt
    with gzip.open(er_test.COINS, "rt", encoding="utf-8") as fh:
        coins = json.load(fh)
    for co in coins:
        co["part"] = m5_bt.part(co)
    res = {}
    for A in AGES:
        ent = entries(coins, A)
        tr = [s["r"] for s in ent if s["part"] in er_test.TRAIN]
        va = [s["r"] for s in ent if s["part"] in er_test.HELD]
        res[A] = (tr, va)
        print(f"AG{A:<3d} TRAIN {summ(tr)} | VALID {summ(va)}", flush=True)
    (b_tr, b_va), (a_tr, a_va) = res[0], res[30]
    deep = lambda v: sum(x <= -0.25 for x in v) / len(v)  # noqa: E731
    ok = (deep(a_tr) < deep(b_tr) and deep(a_va) < deep(b_va)
          and statistics.mean(a_va) >= statistics.mean(b_va) + 0.03 and len(a_tr) >= 100)
    print(f"\nAG30 verdict (deep share lower on train and validation, validation mean >= base + 3 pts, >= 100 train): "
          f"{'PASS' if ok else 'FAIL'}; +20% train bar: {'met' if statistics.mean(a_tr) >= 0.20 else 'not met'}")


if __name__ == "__main__":
    main()
