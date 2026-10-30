"""M13v2 train-only killtest (vault "M13v2 - Runner model at migration on clean data"): does a curve-feature model
rank graduates well enough that its top-K clears +10% gross per trade? No gates, no holdout.

    uv run python -m tradeslave.research.m13v2_killtest

Inside v2 TRAIN only: fit on graduates created before 2026-10-23 12:00 UTC, score those created 2026-10-23 12:00 ->
2026-10-25 00:00 (v2 train ends later than that). Features use curve trades up to the graduation trade only (no pool
data); the dev wallet comes from the creates log. Entry: open of the first pool candle at or after graduation + 60 s.
Exit: open of the first candle at or after entry + H h (else the last close in the 48 h window). No stop.
Costs: cost_model.trade_return at 0.1 SOL, pool reserves derived from price.
"""

from __future__ import annotations

import bisect
import json
import math
import statistics

import numpy as np

from ..cost_model import trade_return
from ..paths import DATA_DIR
from .chain_backfill import GRAD_VS
from .gzlines import records
from .research_data import K_POOL

FIT_BEFORE = 1792756800.0        # 2026-10-23 12:00 UTC (checked with calendar.timegm)
TEST_BEFORE = 1792886400.0       # 2026-10-25 00:00 UTC (checked with calendar.timegm)
POOLS = DATA_DIR / "chain" / "pool_candles"


def devs() -> dict[str, str]:
    out = {}
    with (DATA_DIR / "c1" / "creates.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("m") and r.get("dev"):
                out.setdefault(r["m"], r["dev"])
    return out


def features(d: dict, dev: str | None) -> tuple[list[float], float] | None:
    tr = sorted(d["trades"], key=lambda x: (x[0] or 0, x[1]))
    gi = next((i for i, t in enumerate(tr) if t[6] >= GRAD_VS), None)
    if gi is None:
        return None
    tr = tr[:gi + 1]                                  # up to and including the graduation trade
    t0, t_grad = d["t0"], tr[-1][1]
    launch = [t for t in tr if t[1] <= t0 + 120]
    last = tr[-200:]
    buys = {}
    for _s, _ts, u, b, sol, *_ in tr:
        if b:
            buys[u] = buys.get(u, 0) + sol
    tot = sum(buys.values()) or 1
    top10 = sum(sorted(buys.values(), reverse=True)[:10]) / tot
    dev_b = sum(t[5] for t in tr if t[2] == dev and t[3])
    dev_s = sum(t[5] for t in tr if t[2] == dev and not t[3])
    lb = sum(t[4] for t in last if t[3]) or 1
    ls = sum(t[4] for t in last if not t[3])
    f = [math.log1p(len({t[2] for t in launch if t[3]})), math.log1p(sum(t[4] for t in launch if t[3]) / 1e9),
         top10, math.log1p(dev_b / 1e6), dev_s / dev_b if dev_b else 0.0, math.log1p(max(t_grad - t0, 0) / 60),
         ls / lb, math.log1p(len({t[2] for t in last})), math.log1p(len(tr)), math.log1p(len(buys))]
    return f, t_grad


def outcome(m: str, t_grad: float, hours: float) -> tuple[float, float] | None:
    p = POOLS / f"{m}.json"
    if not p.exists():
        return None
    c = json.loads(p.read_text()).get("c") or []
    c = [x for x in c if x[4] > 0]
    ts = [x[0] for x in c]
    i = bisect.bisect_left(ts, t_grad + 60)
    if i >= len(c):
        return None
    p_in, t_in = c[i][1], c[i][0]
    j = bisect.bisect_left(ts, t_in + hours * 3600)
    p_out = c[j][1] if j < len(c) else c[-1][4]
    g = p_out / p_in - 1
    net = trade_return(g, 0.1, math.sqrt(p_in * K_POOL), math.sqrt(p_out * K_POOL))
    return g, net


def main() -> None:
    from sklearn.linear_model import LogisticRegression
    dv = devs()
    rows = []
    for f in sorted((DATA_DIR / "chain" / "curve").glob("part-*.jsonl.gz")):
        for d in records(f):
            if d.get("t0", 1e12) >= TEST_BEFORE:
                continue
            fe = features(d, dv.get(d["m"]))
            if fe:
                rows.append((d["m"], d["t0"], *fe))
    seen, uniq = set(), []
    for r in rows:
        if r[0] not in seen:
            seen.add(r[0])
            uniq.append(r)
    print(f"train-window graduates (t0 < Oct 25): {len(uniq)}; with pool candles: {sum((POOLS / f'{r[0]}.json').exists() for r in uniq)}")
    for H in (2, 6, 24):
        data = [(m, t0, f, o) for m, t0, f, tg in uniq if (o := outcome(m, tg, H))]
        fit = [x for x in data if x[1] < FIT_BEFORE]
        test = [x for x in data if FIT_BEFORE <= x[1] < TEST_BEFORE]
        if len(fit) < 50 or len(test) < 30:
            print(f"H {H}: too few graduates with pool candles (fit {len(fit)}, test {len(test)}) - wait for gecko_grads")
            continue
        X = np.array([x[2] for x in fit])
        mu, sd = X.mean(0), X.std(0) + 1e-9
        y = np.array([x[3][0] >= 0.5 for x in fit])
        if y.sum() < 5:
            print(f"H {H}: only {y.sum()} winners (>= +50%) in fit; can't fit")
            continue
        model = LogisticRegression(max_iter=2000, C=0.5).fit((X - mu) / sd, y)
        sc = model.predict_proba((np.array([x[2] for x in test]) - mu) / sd)[:, 1]
        order = np.argsort(-sc)
        g_all = [x[3][0] for x in test]
        n_all = [x[3][1] for x in test]
        print(f"H {H} h: fit {len(fit)} (winners {int(y.sum())}), test {len(test)}: all graduates gross mean "
              f"{statistics.mean(g_all):+.1%} median {statistics.median(g_all):+.1%}, net mean {statistics.mean(n_all):+.1%}")
        for K in (10, 20, 33):
            k = max(3, int(len(test) * K / 100))
            top = [test[i][3] for i in order[:k]]
            g = sorted(x[0] for x in top)
            ex3 = statistics.mean(g[:-3]) if len(g) > 3 else float("nan")
            print(f"   top {K}% (n {k}): gross mean {statistics.mean(g):+.1%}, median {statistics.median(g):+.1%}, "
                  f"net mean {statistics.mean(x[1] for x in top):+.1%}, gross ex-top3 {ex3:+.1%}")
    print("kill rule (prereg): top-K gross mean < +10% at the chosen H, or positive only with the top 3 trades")


if __name__ == "__main__":
    main()
