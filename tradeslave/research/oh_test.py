"""OH: sniper/bundle overhang at M5's decision (vault "2026-10-23 sniper overhang test (OH)", pre-registered 00:55 PDT).

    uv run python -m tradeslave.research.oh_test

Early wallets = first buy within 1 s of the coin's first trade, creator excluded. oh = their token balance at the
decision row (% of supply); early_sold = share of their peak balance already sold.
"""

from __future__ import annotations

import gzip
import json
import math
import statistics

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

from .m4_bt import OUT as COINS
from .m5_bt import EXITS, FEATS, HELD, TRAIN, identical_top5, ret, samples

PARTS = TRAIN + HELD


def overhang(rows):
    """Per row: (oh %, oh incl. creator %, early_sold share)."""
    t0, dev = rows[0][0], rows[0][4]
    early, bal, out, peak = set(), {}, [], 0.0
    for r in rows:
        if r[2] and r[0] <= t0 + 1 and r[4] != dev:
            early.add(r[4])
        bal[r[4]] = bal.get(r[4], 0.0) + (r[5] if r[2] else -r[5])
        e = sum(max(0.0, bal[w]) for w in early)
        peak = max(peak, e)
        out.append((e / 1e7, (e + max(0.0, bal.get(dev, 0.0))) / 1e7, 1 - e / peak if peak else 0.0))
    return out


def main() -> None:
    with gzip.open(COINS, "rt", encoding="utf-8") as fh:
        coins = json.load(fh)
    byid = {co["m"]: co for co in coins}
    oh = {co["m"]: overhang(co["rows"]) for co in coins}
    S = samples(coins)
    for s in S:
        s["oh"], s["oh_dev"], s["early_sold"] = oh[s["m"]][s["k"]]

    print("TEST 1 separation (AUC vs runner label; -oh means lower overhang -> runner)")
    for name, sign in (("oh", -1), ("oh_dev", -1), ("early_sold", 1)):
        a = []
        for pp in PARTS:
            z = [s for s in S if s["part"] == pp]
            a.append(roc_auc_score([s["y"] for s in z], [sign * s[name] for s in z]))
        print(f"   {name:10s} " + "  ".join(f"{pp} {x:.3f}" for pp, x in zip(PARTS, a))
              + ("  PASS" if min(a) >= 0.55 else "  fail"))

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
    cut = statistics.median(s["oh"] for s in ent if s["part"] in TRAIN)
    print(f"\nTEST 2 trading: P >= {P:.3f} (train p90), exit E2, split at train median oh = {cut:.2f}%")
    f = lambda v: (f"n {len(v):3d} mean {statistics.mean(v):+6.1%} <=-40% {sum(x <= -0.4 for x in v) / len(v):4.0%}"  # noqa: E731
                   if v else "n   0")
    wins = {}
    for pp in PARTS + ("HELD", "ALL"):
        sel = [s for s in ent if (s["part"] == pp or (pp == "HELD" and s["part"] in HELD) or pp == "ALL")]
        lo = [s["r"] for s in sel if s["oh"] <= cut]
        hi = [s["r"] for s in sel if s["oh"] > cut]
        wins[pp] = bool(lo and hi and statistics.mean(lo) > statistics.mean(hi))
        print(f"   {pp:8s} low oh: {f(lo)} | high oh: {f(hi)}")
    held_lo = [s["r"] for s in ent if s["part"] in HELD and s["oh"] <= cut]
    ok = (wins["A-check"] and wins["B-test"] and sum(wins[pp] for pp in PARTS) >= 3
          and held_lo and statistics.mean(held_lo) > 0)
    print(f"\nTEST 2 {'PASS' if ok else 'FAIL'} (low > high in {sum(wins[pp] for pp in PARTS)}/4 parts, "
          f"both held: {wins['A-check'] and wins['B-test']}, held low-oh mean "
          f"{statistics.mean(held_lo) if held_lo else float('nan'):+.1%})")
    print("\ndescriptive: oh at entry, quartiles", np.quantile([s["oh"] for s in ent], [0.25, 0.5, 0.75]).round(2),
          "| early_sold quartiles", np.quantile([s["early_sold"] for s in ent], [0.25, 0.5, 0.75]).round(2))


if __name__ == "__main__":
    main()
