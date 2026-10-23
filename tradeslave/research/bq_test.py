"""BQ: buy quality (A2) and big-holder dumps (D1) at M5's decision (vault "2026-10-23 buy quality and big-holder
dumps (BQ)", pre-registered 01:20 PDT).

    uv run python -m tradeslave.research.bq_test
"""

from __future__ import annotations

import bisect
import gzip
import json
import math
import statistics
from collections import Counter

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

from .m4_bt import OUT as COINS
from .m5_bt import EXITS, FEATS, HELD, TRAIN, identical_top5, ret, samples

PARTS = TRAIN + HELD


class Feats:
    def __init__(self, rows):
        self.rows, self.t = rows, [r[0] for r in rows]
        t0, self.dev = rows[0][0], rows[0][4]
        self.early = {r[4] for r in rows if r[2] and r[0] <= t0 + 1 and r[4] != self.dev}
        self.bal_at = []                        # top1 (ex creator), top1 incl creator, top1 wallet; per row
        bal = {}
        for r in rows:
            bal[r[4]] = bal.get(r[4], 0.0) + (r[5] if r[2] else -r[5])
            # full scan per row is slow; keep a lazy snapshot instead
            self.bal_at.append(None)
        self._bal = None

    def balances(self, k):
        bal = {}
        for r in self.rows[:k + 1]:
            bal[r[4]] = bal.get(r[4], 0.0) + (r[5] if r[2] else -r[5])
        return bal

    def at(self, k):
        t = self.rows[k][0]
        lo = bisect.bisect_right(self.t, t - 30)
        w = self.rows[lo:k + 1]
        buy = sum(r[3] or 0 for r in w if r[2])
        big = sum(r[3] or 0 for r in w if r[2] and (r[3] or 0) >= 0.5 and r[4] not in self.early)
        small = sum(1 for r in w if (r[3] or 0) < 0.05)
        bal = self.balances(k)
        ex = sorted(((v, w_) for w_, v in bal.items() if w_ != self.dev and v > 0), reverse=True)
        top1 = ex[0][0] / 1e7 if ex else 0.0
        top1_dev = max(top1, max(0.0, bal.get(self.dev, 0.0)) / 1e7)
        return dict(big_share=big / buy if buy else 0.0, small_share=small / len(w) if w else 0.0,
                    top1=top1, top1_dev=top1_dev, top1_w=ex[0][1] if ex else None, bal=bal)


def main() -> None:
    with gzip.open(COINS, "rt", encoding="utf-8") as fh:
        coins = json.load(fh)
    byid = {co["m"]: co for co in coins}
    F = {co["m"]: Feats(co["rows"]) for co in coins}
    S = samples(coins)
    for s in S:
        d = F[s["m"]].at(s["k"])
        d.pop("bal")
        s.update(d)

    print("TEST 1 separation (AUC vs runner label)")
    for name, sign in (("big_share", 1), ("small_share", -1), ("top1", -1), ("top1_dev", -1)):
        a = []
        for pp in PARTS:
            z = [s for s in S if s["part"] == pp]
            a.append(roc_auc_score([s["y"] for s in z], [sign * s[name] for s in z]))
        print(f"   {'+' if sign > 0 else '-'}{name:11s} " + "  ".join(f"{pp} {x:.3f}" for pp, x in zip(PARTS, a))
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

    f = lambda v: (f"n {len(v):3d} mean {statistics.mean(v):+6.1%} <=-40% {sum(x <= -0.4 for x in v) / len(v):4.0%}"  # noqa: E731
                   if v else "n   0")
    for name, good_high in (("big_share", True), ("small_share", False), ("top1", False)):
        cut = statistics.median(s[name] for s in ent if s["part"] in TRAIN)
        good = (lambda s: s[name] > cut) if good_high else (lambda s: s[name] <= cut)
        print(f"\nTEST 2 {name}: good = {'>' if good_high else '<='} {cut:.3f} (train median)")
        wins = {}
        for pp in PARTS + ("HELD", "ALL"):
            sel = [s for s in ent if s["part"] == pp or (pp == "HELD" and s["part"] in HELD) or pp == "ALL"]
            g = [s["r"] for s in sel if good(s)]
            b = [s["r"] for s in sel if not good(s)]
            wins[pp] = bool(g and b and statistics.mean(g) > statistics.mean(b))
            print(f"   {pp:8s} good: {f(g)} | bad: {f(b)}")
        hg = [s["r"] for s in ent if s["part"] in HELD and good(s)]
        ok = wins["A-check"] and wins["B-test"] and sum(wins[pp] for pp in PARTS) >= 3 and hg and statistics.mean(hg) > 0
        print(f"   -> {'PASS' if ok else 'FAIL'} (good > bad in {sum(wins[pp] for pp in PARTS)}/4, both held "
              f"{wins['A-check'] and wins['B-test']}, held good mean {statistics.mean(hg) if hg else float('nan'):+.1%})")

    print("\nTEST 3 descriptive: largest sell within 60 s after entry, entries ending <= -40%")
    who, pct = Counter(), []
    for s in ent:
        if s["r"] > -0.4:
            continue
        co, k = byid[s["m"]], s["k"]
        fe = F[s["m"]]
        top1_w = fe.at(k)["top1_w"]
        t = co["rows"][k][0]
        sells = [r for r in co["rows"][k + 1:] if r[0] <= t + 60 and not r[2]]
        if not sells:
            who["no sell in 60 s"] += 1
            continue
        r = max(sells, key=lambda r: r[3] or 0)
        kind = ("creator" if r[4] == fe.dev else "top-1 holder at entry" if r[4] == top1_w
                else "early wallet" if r[4] in fe.early else "other")
        who[kind] += 1
        pct.append((kind, r[5] / 1e7, r[3]))
    print(f"   n {sum(who.values())}: " + ", ".join(f"{k} {v}" for k, v in who.most_common()))
    for kind in {k for k, _, _ in pct}:
        z = [(p, sol) for k, p, sol in pct if k == kind]
        print(f"   {kind:22s} median sell {statistics.median(p for p, _ in z):.1f}% of supply, "
              f"{statistics.median(sol for _, sol in z):.2f} SOL")


if __name__ == "__main__":
    main()
