"""RD: runner detection on the live feed, labelled with pump.fun all-time highs (vault "2026-10-24 runner detection on 1,000
real runners (RD)", pre-registered 19:56 PDT).

    uv run python -u -m tradeslave.research.rd_test labels   # ATH for every feed coin -> data/runners/feed_ath.json
    uv run python -u -m tradeslave.research.rd_test build    # features at M5's decision moment -> data/runners/rd_samples.json
    uv run python -u -m tradeslave.research.rd_test run      # A (M5 model), B (new model), C (trading backtest)
"""

from __future__ import annotations

import bisect
import json
import math
import sqlite3
import statistics
import sys
import time
from collections import defaultdict

import numpy as np

from ..paths import DATA_DIR
from .hs_test import L5, coins
from .m5_bt import Coin, identical_top5, ret
from .runners import decision

OUT = DATA_DIR / "runners"
ATH_MIN = 300_000
FEE, TXR = 0.0125, 2 * 0.0005 / 0.1
DECU = "4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9"


def labels() -> None:
    from .rn_collect import _pump_headers, ath_batch
    db = sqlite3.connect(DATA_DIR / "feed" / "trades.sqlite")
    ms = [m for (m,) in db.execute("SELECT m FROM coin")]
    p = OUT / "feed_ath.json"
    have = json.loads(p.read_text()) if p.exists() else {}
    todo = [m for m in ms if m not in have]
    print(f"{len(ms)} feed coins, {len(todo)} to label", flush=True)
    H = _pump_headers()
    for i in range(0, len(todo), 1000):
        have.update(ath_batch(todo[i:i + 1000], H))
        p.write_text(json.dumps(have))
        print(f"labelled {min(i + 1000, len(todo))}/{len(todo)}", flush=True)
    print(f"done: {sum((v or 0) >= ATH_MIN for v in have.values())} of {len(have)} reached $300k")


def _m5_scores(c, rows, i, model):
    """M5's probability at each 10 s step from the decision moment to +180 s, as M5 trades it."""
    out = []
    for d in range(0, 181, 10):
        t = rows[i][0] + d
        if t > c.t[-1]:
            break
        f, k = c.feats(t, i)
        x = [(math.log1p(max(0.0, f[n])) if n not in ("vs_band", "vs_high") else f[n]) for n in model["feats"]]
        z = model["intercept"] + sum(w * (v - mu) / sd for w, v, mu, sd in zip(model["coef"], x, model["mu"], model["sd"]))
        p = 1 / (1 + math.exp(-z))
        buy = p >= model["P"] and f["mc_k"] <= 20 and not identical_top5(rows, k)
        out.append((d, p, buy, f["mc_k"]))
    return out


def build() -> None:
    from .br_test import state
    from .c1_curve import CREATES
    ath = json.loads((OUT / "feed_ath.json").read_text())
    model = json.loads((DATA_DIR / "m1" / "m5_model.json").read_text())
    creates = []
    for line in CREATES.open(encoding="utf-8"):
        try:
            j = json.loads(line)
            creates.append((j["t"], j.get("dev")))
        except (ValueError, KeyError):
            continue
    by_dev = defaultdict(list)
    for t, d in creates:
        by_dev[d].append(t)
    narr = {}
    for line in (DATA_DIR / "m1" / "cands.jsonl").open(encoding="utf-8"):
        if '"score"' in line:
            try:
                j = json.loads(line)
                narr.setdefault(j["m"], (j.get("score"), j["t"]))
            except (ValueError, KeyError):
                continue
    callers = set()
    for f in sorted((DATA_DIR / "callouts").glob("leaderboard_*.json"), reverse=True):
        try:
            callers = {w for u in json.loads(f.read_text(encoding="utf-8"))["callouts"] for w in u["wallets"] if not w.startswith("0x")}
            break
        except (ValueError, KeyError):
            continue
    out, n = [], 0
    for m, rows in coins():
        n += 1
        if m not in ath or len(rows) < 10:
            continue
        t0, cr = rows[0][0], rows[0][4]
        y = int((ath[m] or 0) >= ATH_MIN)
        i = decision(rows, t0)
        base = dict(m=m, t0=t0, y=y, ath=ath[m])
        if i is None:
            out.append(dict(base, reached=False))
            continue
        c = Coin({"rows": rows, "t0": t0})
        sc = _m5_scores(c, rows, i, model)
        t = rows[i][0]
        f5, k5 = c.feats(t, i)
        st = state(rows, i) if i >= 3 else {}
        launch = [r for r in rows if r[0] <= t0 + 1 and r[2] and r[4] != cr]
        dev_buy = sum(r[3] for r in rows if r[0] <= t0 + 2 and r[2] and r[4] == cr)
        pre = rows[:i + 1]
        caller_in = any(r[2] and r[4] in callers for r in pre)
        decu_in = any(r[2] and r[4] == DECU for r in pre)
        ns = narr.get(m)
        feats = dict(f5)
        feats.update({k: v for k, v in st.items() if isinstance(v, (int, float))})
        feats.update(age_s=t - t0, creator_prev=bisect.bisect_left(sorted(by_dev.get(cr, [])), t0 - 1),
                     launch_share=sum(r[5] for r in launch) / 1e9, launch_wallets=len({r[4] for r in launch}), dev_buy=dev_buy,
                     caller_in=int(caller_in), decu_in=int(decu_in), narr=(ns[0] if ns and ns[0] is not None and ns[1] <= t + 5 else -1),
                     narr_known=int(bool(ns and ns[1] <= t + 5)))
        out.append(dict(base, reached=True, i=i, t=t, f=feats, m5=[list(x) for x in sc],
                        m5_max=max((p for _, p, _, mc in sc if mc <= 20), default=0.0), m5_buy=next((d for d, _, b, _ in sc if b), None)))
        if n % 1000 == 0:
            print(f"{n} coins, {len(out)} samples", flush=True)
    (OUT / "rd_samples.json").write_text(json.dumps(out))
    print(f"built {len(out)} samples ({sum(s['reached'] for s in out)} reached M5's decision point, {sum(s['y'] for s in out)} runners)")


def auc(y, s):
    from sklearn.metrics import roc_auc_score
    return roc_auc_score(y, s) if len(set(y)) > 1 else float("nan")


def ladder_ret(rows, k):
    """M12r exits on the feed path: -10% stop until 2x, then 1.25x / N-1 ratchet; stalled / dying take-profit; fills +1 s."""
    ts = [r[0] for r in rows]
    pe, t0 = rows[k][1], rows[k][0]
    peak, t_peak, on, stop = pe, t0, False, 0.90
    per_min = defaultdict(int)

    def fill(t):
        return rows[max(0, bisect.bisect_right(ts, t + 1) - 1)][1] / pe * (1 - FEE) ** 2 - 1 - TXR
    for j in range(k + 1, len(rows)):
        t, p = rows[j][0], rows[j][1]
        per_min[int(t // 60)] += 1
        if p > peak:
            peak, t_peak = p, t
        r = p / pe
        if not on and r >= 2:
            on, stop = True, 1.25
        if on:
            n = int(peak / pe)
            if n >= 3:
                stop = max(stop, float(n - 1))
            w60 = [x for x in rows[max(k, bisect.bisect_left(ts, t - 60)):j + 1]]
            flow = sum(x[3] if x[2] else -x[3] for x in w60)
            amax = max(per_min.values())
            if r <= stop or (t - t_peak > 120 and flow <= 0) or (amax >= 5 and len(w60) < 0.25 * amax) or t - t0 > 3600:
                return fill(t)
        elif r <= stop or t - t0 > 1800:
            return fill(t)
    return rows[-1][1] / pe * (1 - FEE) ** 2 - 1 - TXR


def run() -> None:
    from sklearn.ensemble import HistGradientBoostingClassifier
    S = [s for s in json.loads((OUT / "rd_samples.json").read_text())]
    S.sort(key=lambda s: s["t0"])
    mid = S[len(S) // 2]["t0"]
    D, H = [s for s in S if s["t0"] < mid], [s for s in S if s["t0"] >= mid]
    base = lambda xs: sum(s["y"] for s in xs) / len(xs)  # noqa: E731
    print(f"population {len(S)} coins ({sum(s['y'] for s in S)} reached $300k, base rate {base(S):.2%}); design {len(D)} / holdout {len(H)}")
    print(f"reached M5's decision point: {sum(s['reached'] for s in S)} ({sum(s['y'] for s in S if s['reached'])} of the runners)")
    # A. M5's model on holdout (coins that never reach the decision point count as not flagged, score 0)
    yH = [s["y"] for s in H]
    a_m5 = auc(yH, [s.get("m5_max", 0.0) for s in H])
    flag = [s for s in H if s.get("m5_buy") is not None]
    prec_m5 = sum(s["y"] for s in flag) / len(flag) if flag else float("nan")
    rec_m5 = sum(s["y"] for s in flag) / max(1, sum(yH))
    print(f"\nA. M5 runner model (holdout): AUC {a_m5:.3f} | flags {len(flag)} coins, precision {prec_m5:.1%} vs base {base(H):.2%}, recall {rec_m5:.1%}")
    # B. new model, trained on design coins that reached the decision point
    keys = sorted({k for s in S if s["reached"] for k in s["f"]})
    def X(xs):
        return np.array([[float(s["f"].get(k, 0) if s["f"].get(k) is not None else 0) for k in keys] for s in xs])
    Dr, Hr = [s for s in D if s["reached"]], [s for s in H if s["reached"]]
    clf = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_depth=4, l2_regularization=1.0, random_state=7)
    clf.fit(X(Dr), [s["y"] for s in Dr])
    pD = clf.predict_proba(X(Dr))[:, 1]
    thr = float(np.percentile(pD, 90))
    pH = dict(zip([s["m"] for s in Hr], clf.predict_proba(X(Hr))[:, 1]))
    a_b = auc(yH, [pH.get(s["m"], 0.0) for s in H])
    top = [s for s in Hr if pH[s["m"]] >= thr]
    prec_b = sum(s["y"] for s in top) / len(top) if top else float("nan")
    rec_b = sum(s["y"] for s in top) / max(1, sum(yH))
    print(f"B. new model (holdout): AUC {a_b:.3f} | top 10% (design cut {thr:.3f}): {len(top)} coins, precision {prec_b:.1%}, recall {rec_b:.1%}")
    okB = a_b - a_m5 >= 0.05 and prec_b > prec_m5
    print(f"   B VERDICT: {'PASS' if okB else 'FAIL'} (AUC +{a_b - a_m5:.3f}, precision {prec_b:.1%} vs M5 {prec_m5:.1%})")
    imp = sorted(zip(keys, clf._predictors[0][0].nodes["feature_idx"] if False else [0] * len(keys)))
    # permutation importance on holdout (report)
    try:
        from sklearn.inspection import permutation_importance
        Xh, yh = X(Hr), np.array([s["y"] for s in Hr])
        pi = permutation_importance(clf, Xh, yh, scoring="roc_auc", n_repeats=5, random_state=7)
        best = sorted(zip(keys, pi.importances_mean), key=lambda x: -x[1])[:10]
        print("   top features (holdout permutation AUC drop):", ", ".join(f"{k} {v:+.3f}" for k, v in best))
    except Exception as exc:  # noqa: BLE001
        print("   importance failed:", type(exc).__name__)
    del imp
    # C. trading backtest of B's top 10% on holdout, +1 s fills
    byc = dict(coins())
    res = []
    for s in top:
        rows = byc.get(s["m"])
        if not rows:
            continue
        ts = [r[0] for r in rows]
        k = max(0, bisect.bisect_right(ts, s["t"] + 1) - 1)
        if k >= len(rows) - 2:
            continue
        co = dict(m=s["m"], rows=[[*r[:6], None] for r in rows])
        res.append((s["t0"], s["y"], ret(co, k, L5), ladder_ret(rows, k)))
    hm = statistics.median([t for t, *_ in res]) if res else 0
    for j, lab in ((2, "L5 exits"), (3, "M12r ratchet")):
        v = [r[j] for r in res]
        if not v:
            print(f"C. {lab}: no trades")
            continue
        h = [[r[j] for r in res if (r[0] < hm) == q] for q in (True, False)]
        ok = len(v) >= 60 and statistics.mean(v) > 0 and all(x and statistics.mean(x) > 0 for x in h)
        print(f"C. B's top 10%, {lab}: n {len(v)} mean {statistics.mean(v):+.1%} med {statistics.median(v):+.1%} win {sum(x > 0 for x in v) / len(v):.0%} "
              f"<=-25% {sum(x <= -0.25 for x in v) / len(v):.0%} | halves {statistics.mean(h[0]) if h[0] else float('nan'):+.1%} / "
              f"{statistics.mean(h[1]) if h[1] else float('nan'):+.1%} -> {'PASS' if ok else 'FAIL'}")
    m5t = [s for s in flag]
    v5 = []
    for s in m5t:
        rows = byc.get(s["m"])
        if not rows:
            continue
        ts = [r[0] for r in rows]
        k = max(0, bisect.bisect_right(ts, rows[s["i"]][0] + s["m5_buy"] + 1) - 1)
        if k < len(rows) - 2:
            v5.append(ret(dict(m=s["m"], rows=[[*r[:6], None] for r in rows]), k, L5))
    if v5:
        print(f"   (report) M5's own flags, L5 exits: n {len(v5)} mean {statistics.mean(v5):+.1%} win {sum(x > 0 for x in v5) / len(v5):.0%}")
    json.dump(dict(keys=keys, thr=thr), open(OUT / "rd_model_meta.json", "w"))


if __name__ == "__main__":
    {"labels": labels, "build": build, "run": run}[sys.argv[1]]()
