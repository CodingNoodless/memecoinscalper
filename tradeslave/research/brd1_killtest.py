"""BRD1 train-only killtest (vault "BRD1 - Market-breadth gate for graduates"): do graduates bought in hot market
regimes clear +10% gross? No gates, no holdout. Same windows as M13v2 (inside v2 train): cut-offs frozen on graduates
created before 2026-10-23 12:00 UTC, scored on those created 2026-10-23 12:00 -> 2026-10-25 00:00.

    uv run python -m tradeslave.research.brd1_killtest

Breadth at decision time t (graduation + 60 s), from data available at t only:
  - gshare: graduations in [t - W, t) per sampled launch created in [t - W, t)   (a rate, so the sample fraction cancels)
  - b3: share of launches created in [t - W, t) with >= 3 distinct buyers by t
Hot = both measures in the fit-period top tercile (cut-offs frozen on fit). Entry/exit as M13v2 (no stop).
Run after the backfill has covered the window (it is a random sample, so partial coverage is unbiased but noisy).
"""

from __future__ import annotations

import bisect
import statistics

import numpy as np

from ..paths import DATA_DIR
from .chain_backfill import GRAD_VS
from .gzlines import records
from .m13v2_killtest import FIT_BEFORE, TEST_BEFORE, outcome

START = 1792540800.0   # 2026-10-21 00:00 UTC


def load():
    toks = {}
    for f in sorted((DATA_DIR / "chain" / "curve").glob("part-*.jsonl.gz")):
        for d in records(f):
            if d.get("m") and d["m"] not in toks and d["t0"] < TEST_BEFORE + 86400:
                tr = sorted(d["trades"], key=lambda x: (x[0] or 0, x[1]))
                g = next((t[1] for t in tr if t[6] >= GRAD_VS), None)
                buy_t = []
                seen = set()
                for t in tr:
                    if t[3] and t[2] not in seen:
                        seen.add(t[2])
                        buy_t.append(t[1])
                toks[d["m"]] = {"t0": d["t0"], "t_grad": g, "t3": buy_t[2] if len(buy_t) >= 3 else None}
    return toks


def main() -> None:
    toks = load()
    t0s = sorted((v["t0"], m) for m, v in toks.items())
    t0_list = [x[0] for x in t0s]
    grads = sorted(v["t_grad"] for v in toks.values() if v["t_grad"])
    print(f"sampled launches up to Oct 26: {len(toks)}, graduates {len(grads)}")
    for W in (1, 2, 4):
        rows = []
        for m, v in toks.items():
            if not v["t_grad"] or not START + W * 3600 <= v["t0"] < TEST_BEFORE:
                continue
            t = v["t_grad"] + 60
            i0, i1 = bisect.bisect_left(t0_list, t - W * 3600), bisect.bisect_left(t0_list, t)
            window = [toks[t0s[k][1]] for k in range(i0, i1)]
            if len(window) < 50:
                continue
            ng = bisect.bisect_left(grads, t) - bisect.bisect_left(grads, t - W * 3600)
            gshare = ng / len(window)
            b3 = sum(1 for x in window if x["t3"] is not None and x["t3"] < t) / len(window)
            rows.append((m, v["t0"], v["t_grad"], gshare, b3))
        fit = [r for r in rows if r[1] < FIT_BEFORE]
        if len(fit) < 30:
            print(f"W {W} h: too few fit graduates ({len(fit)})")
            continue
        cg, cb = np.quantile([r[3] for r in fit], 2 / 3), np.quantile([r[4] for r in fit], 2 / 3)
        test = [r for r in rows if FIT_BEFORE <= r[1] < TEST_BEFORE]
        for H in (2, 6):
            out_all, out_hot = [], []
            for m, _t0, tg, gs, b3 in test:
                o = outcome(m, tg, H)
                if o is None:
                    continue
                out_all.append(o)
                if gs >= cg and b3 >= cb:
                    out_hot.append(o)
            if not out_all:
                print(f"W {W} h, H {H} h: no test graduates with pool candles yet")
                continue
            hot = sorted(x[0] for x in out_hot)
            print(f"W {W} h, H {H} h: test graduates {len(out_all)} gross mean {statistics.mean(x[0] for x in out_all):+.1%}"
                  f" | hot n {len(out_hot)} gross mean {statistics.mean(hot) if hot else float('nan'):+.1%} median "
                  f"{statistics.median(hot) if hot else float('nan'):+.1%} net mean "
                  f"{statistics.mean(x[1] for x in out_hot) if out_hot else float('nan'):+.1%}"
                  f" ex-top3 {statistics.mean(hot[:-3]) if len(hot) > 3 else float('nan'):+.1%}")
    print("kill rule (prereg): hot-tercile gross mean < +10%, or the effect comes from one day / the top 3 trades")


if __name__ == "__main__":
    main()
