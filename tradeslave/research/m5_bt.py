"""M5: trade only predicted runners (vault "2026-10-22 M5 runner bot (M5)", protocol pre-registered 22:55 PDT).

    uv run python -m tradeslave.research.m5_bt          # design on train, judge once on held out, write data/m1/m5_model.json

Decision moments every 10 s from a coin's first band touch to +180 s; a logistic runner model on post-band momentum
picks the entry (first moment with p >= P); exits chosen on train among E1-E4. All choices on train
(A-design + B-train); held out (A-check + B-test) is scored once.
"""

from __future__ import annotations

import bisect
import gzip
import json
import math
import statistics

import numpy as np
from sklearn.linear_model import LogisticRegression

from ..paths import DATA_DIR
from .m4_bt import B_SPLIT, OUT as COINS, SOL_USD, SIZE, part, stats
from .runners import decision
from .tf_replay import BASE, K, sim

FEATS = ["holders", "h_gain10", "h_gain30", "new10", "new30", "buy10", "buy30", "sell10", "sell30", "trades30",
         "vs_band", "vs_high", "since_band", "mc_k", "dev_hold", "top10"]
EXITS = {
    "E1 M4 exits": dict(stop=0.70, P={"hdecl": 0.10}),
    "E2 M4 exits, 15 s holder grace": dict(stop=0.70, P={"hdecl": 0.10, "hgrace": 15}),
    "E3 new-wallet stop": dict(stop=0.70, P={"hdecl": None, "nw": (15, 3, 10)}),
    "E4 runner exit": dict(stop=0.75, P={"hdecl": None, "nw": (15, 3, 10), "tp": 99.0, "trend_arm": 1.5,
                                         "trend_give": 0.70, "stall_s": 1e9, "max_hold": 900}),
}
TRAIN, HELD = ("A-design", "B-train"), ("A-check", "B-test")


class Coin:
    """Fast window queries over one coin's trades."""

    def __init__(self, co):
        self.co, self.rows = co, co["rows"]
        self.t = [r[0] for r in self.rows]
        dev = self.rows[0][4]
        self.first = []                                         # time of each wallet's first buy
        seen, bal = set(), {}
        self.dev_hold, self.top10 = [], []
        for r in self.rows:
            if r[2] and r[4] not in seen:
                seen.add(r[4])
                self.first.append(r[0])
            bal[r[4]] = bal.get(r[4], 0.0) + (r[5] if r[2] else -r[5])
            self.dev_hold.append(max(0.0, bal.get(dev, 0.0)) / 1e7)
            self.top10.append(None)
        self.dev = dev

    def idx(self, t):
        return bisect.bisect_right(self.t, t) - 1

    def feats(self, t, i_band):
        k = self.idx(t)
        rows = self.rows
        lo10, lo30 = bisect.bisect_right(self.t, t - 10), bisect.bisect_right(self.t, t - 30)
        h = rows[k][6] or 0
        h10 = rows[max(0, lo10 - 1)][6] or 0
        h30 = rows[max(0, lo30 - 1)][6] or 0
        w10, w30 = rows[lo10:k + 1], rows[lo30:k + 1]
        b = lambda w, side: sum((r[3] or 0) for r in w if r[2] == side)     # noqa: E731
        p = rows[k][1]
        hi = max(r[1] for r in rows[:k + 1])
        return dict(holders=h, h_gain10=h - h10, h_gain30=h - h30,
                    new10=len(self.first) - bisect.bisect_right(self.first, t - 10) - (len(self.first) - bisect.bisect_right(self.first, t)),
                    new30=len(self.first) - bisect.bisect_right(self.first, t - 30) - (len(self.first) - bisect.bisect_right(self.first, t)),
                    buy10=b(w10, True), buy30=b(w30, True), sell10=b(w10, False), sell30=b(w30, False), trades30=len(w30),
                    vs_band=p / rows[i_band][1], vs_high=p / hi, since_band=t - rows[i_band][0],
                    mc_k=p * 1e9 * SOL_USD / 1000, dev_hold=self.dev_hold[k], top10=0.0), k


def identical_top5(rows, k):
    bal = {}
    for r in rows[:k + 1]:
        bal[r[4]] = bal.get(r[4], 0.0) + (r[5] if r[2] else -r[5])
    top = sorted((v for v in bal.values() if v > 0), reverse=True)[:5]
    return any(abs(a - b) / max(a, b) < 0.005 for i, a in enumerate(top) for b in top[i + 1:])


def samples(coins):
    out = []
    for co in coins:
        c = Coin(co)
        i = decision(co["rows"], co["t0"])
        if i is None:
            continue
        for d in range(0, 181, 10):
            t = co["rows"][i][0] + d
            if t > c.t[-1]:
                break
            f, k = c.feats(t, i)
            if f["mc_k"] > 20:
                continue
            k1 = c.idx(t + 1)
            later = [r[1] for r in co["rows"][k1 + 1:] if r[0] <= co["rows"][k1][0] + 1800]
            if not later:
                continue
            out.append(dict(m=co["m"], part=part(co), t=t, k=k1, f=f, y=int(max(later) >= 2 * co["rows"][k1][1])))
    return out


def ret(co, k, ex):
    rows = co["rows"]
    p0 = rows[k][1]
    v = math.sqrt(p0 * K)
    q = K / v - K / (v + SIZE * (1 - 0.0125))
    x = dict(v="m1", t=rows[k][0], p0=p0, q=q, stop=ex["stop"], high=False)
    return sim(x, rows[:k + 1], rows[k + 1:], dict(BASE, **ex["P"]))[0]


def main() -> None:
    with gzip.open(COINS, "rt", encoding="utf-8") as fh:
        coins = json.load(fh)
    byid = {co["m"]: co for co in coins}
    S = samples(coins)
    tr = [s for s in S if s["part"] in TRAIN]
    X = lambda z: np.array([[math.log1p(max(0.0, s["f"][k])) if k not in ("vs_band", "vs_high") else s["f"][k] for k in FEATS] for s in z])  # noqa: E731
    Xtr, ytr = X(tr), np.array([s["y"] for s in tr])
    mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
    model = LogisticRegression(C=0.3, max_iter=2000).fit((Xtr - mu) / sd, ytr)
    for s, p in zip(S, model.predict_proba((X(S) - mu) / sd)[:, 1]):
        s["p"] = float(p)
    print(f"samples {len(S)} (train {len(tr)}, runner rate {ytr.mean():.1%}); coins with a band touch "
          f"{len({s['m'] for s in S})}")
    from sklearn.metrics import roc_auc_score
    for pp in TRAIN + HELD:
        z = [s for s in S if s["part"] == pp]
        print(f"   sample AUC {pp:8s} {roc_auc_score([s['y'] for s in z], [s['p'] for s in z]):.3f}  (n {len(z)})")
    print("   weights: " + ", ".join(f"{k} {w:+.2f}" for k, w in sorted(zip(FEATS, model.coef_[0]), key=lambda z: -abs(z[1]))))

    def entries(P, parts):
        first = {}
        for s in sorted(S, key=lambda s: s["t"]):
            if s["part"] in parts and s["p"] >= P and s["m"] not in first:
                first[s["m"]] = s
        return [s for s in first.values() if not identical_top5(byid[s["m"]]["rows"], s["k"])]

    grid = sorted({round(float(np.quantile([s["p"] for s in tr], q)), 4) for q in (0.5, 0.7, 0.8, 0.85, 0.9, 0.93, 0.95, 0.97, 0.98, 0.99)})
    cache = {}
    best, table = None, []
    for P in grid:
        ent = entries(P, TRAIN)
        for xn, ex in EXITS.items():
            r = [cache.setdefault((s["m"], s["k"], xn), ret(byid[s["m"]], s["k"], ex)) for s in ent]
            table.append((P, xn, r))
            if len(r) >= 20 and (best is None or (sum(r), statistics.mean(r)) > (sum(best[2]), statistics.mean(best[2]))):
                best = (P, xn, r)
    print("\nTRAIN grid (P = runner-probability threshold):")
    for P, xn, r in table:
        print(f"   P {P:.3f} {xn:32s} {stats(r)}")
    P, xn, _ = best
    print(f"\nCHOSEN on train: P >= {P:.3f}, {xn}")
    res = {}
    for pp in TRAIN + HELD:
        ent = entries(P, (pp,))
        res[pp] = [(s["t"], ret(byid[s["m"]], s["k"], EXITS[xn])) for s in ent]
        print(f"   {pp:8s} {stats([r for _, r in res[pp]])}")
    held = sorted(x for pp in HELD for x in res[pp])
    allr = sorted(x for pp in TRAIN + HELD for x in res[pp])
    print(f"   HELD OUT {stats([r for _, r in held])}")
    print(f"   ALL      {stats([r for _, r in allr])}")
    json.dump({"feats": FEATS, "mu": mu.tolist(), "sd": sd.tolist(), "coef": model.coef_[0].tolist(),
               "intercept": float(model.intercept_[0]), "P": P, "exit": xn,
               "held": held, "all": allr}, open(DATA_DIR / "m1" / "m5_model.json", "w"))


if __name__ == "__main__":
    main()
