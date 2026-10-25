"""RD test A (vault "2026-10-24 runner detection on 1,000 real runners (RD)"): the existing M5 runner model at its own decision
point (the $4-7k band), on the case-control files (first 300 trades per coin, Helius). Report only: the random negatives are
not time-matched (data note), so only in-window negatives are used and the result carries that caveat.

    uv run python -X utf8 -m tradeslave.research.rd_a
"""

from __future__ import annotations

import json
import math

from ..paths import DATA_DIR

CC = DATA_DIR / "runners" / "cc"


def m5_prob(x, model):
    from .m5_bt import Coin
    from .runners import decision
    bal, h, rows = {}, 0, []
    for slot, ts, w, buy, sol, tok, vs, vt in sorted(x["rows"], key=lambda r: (r[0], r[1])):
        if not vt:
            continue
        b0 = bal.get(w, 0.0)
        b1 = b0 + (tok if buy else -tok)
        bal[w] = b1
        h += (b0 <= 1 < b1) - (b1 <= 1 < b0)
        rows.append([float(ts), vs / vt, buy, sol, w, tok, h])
    if len(rows) < 10:
        return None
    t0 = rows[0][0]
    i = decision(rows, t0)
    if i is None:
        return None
    f, _ = Coin({"rows": rows, "t0": t0}).feats(rows[i][0] + 60, i)
    v = [(math.log1p(max(0.0, f[n])) if n not in ("vs_band", "vs_high") else f[n]) for n in model["feats"]]
    z = model["intercept"] + sum(w * (a - mu) / sd for w, a, mu, sd in zip(model["coef"], v, model["mu"], model["sd"]))
    return 1 / (1 + math.exp(-z))


def main() -> None:
    from sklearn.metrics import roc_auc_score
    model = json.loads((DATA_DIR / "m1" / "m5_model.json").read_text())
    R = json.loads((DATA_DIR / "runners" / "runners.json").read_text())
    lo = min(r["t"] for r in R) - 3 * 86400
    runners = {r["m"] for r in R}
    groups = {"positive": [], "random (in window)": [], "graduated non-runner": []}
    for kind, key in (("positive", "positive"), ("random", "random (in window)"), ("graduated", "graduated non-runner")):
        for p in (CC / kind).glob("*.json"):
            x = json.loads(p.read_text())
            if kind != "positive" and (x["m"] in runners or (kind == "random" and x["created"] < lo)):
                continue
            groups[key].append(m5_prob(x, model))
    for k, v in groups.items():
        got = [p for p in v if p is not None]
        print(f"{k:24s} {len(v):5d} coins, reach the band {len(got):5d}, M5 >= cut {sum(p >= model['P'] for p in got):4d}")
    pos = [p for p in groups["positive"] if p is not None]
    for k in ("random (in window)", "graduated non-runner"):
        neg = [p for p in groups[k] if p is not None]
        if neg:
            auc = roc_auc_score([1] * len(pos) + [0] * len(neg), pos + neg)
            print(f"AUC runners vs {k}: {auc:.3f}  (runners flagged {sum(p >= model['P'] for p in pos) / len(pos):.0%}, "
                  f"{k} flagged {sum(p >= model['P'] for p in neg) / len(neg):.0%})")


if __name__ == "__main__":
    main()
