"""CLN1 train-only killtest (vault "CLN1 - Original vs clone tickers"): does the flow leader of a same-ticker cluster
clear +10% gross? No gates, no holdout. Launches created 2026-10-21 -> 2026-10-25 00:00 UTC only (inside v2 train).

    uv run python -m tradeslave.research.cln1_killtest

Clusters come from the full create log (data/c1/creates.jsonl): at each trade time t, the cluster is every launch with
the same symbol (case-insensitive) created in [t - 60 min, t] - backward only, so later clones never inform a decision.
Curve SOL per member = virtual SOL - 30 from the chain backfill. A cluster whose members aren't all backfilled is
skipped (counted). Trigger: >= N members, the leader has >= 40 SOL virtual and >= LEAD x the curve SOL of every other
member. Entry: the leader's next trade (its post-trade price, latency proxy). Exit: the leader's first trade >= H min
later (else its last trade). No stop. One trade per symbol per 60 min. Costs: cost_model at 0.1 SOL, exact reserves.
"""

from __future__ import annotations

import bisect
import heapq
import json
import statistics
from collections import defaultdict

from ..cost_model import trade_return
from ..paths import DATA_DIR
from .gzlines import records

START, END = 1792540800.0, 1792886400.0      # 2026-10-21 00:00 -> 2026-10-25 00:00 UTC (calendar.timegm)
WIN = 3600


def main() -> None:
    by_sym = defaultdict(list)
    with (DATA_DIR / "c1" / "creates.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("sym") and r.get("bc") and not r.get("mh") and START <= r["t"] < END:
                by_sym[r["sym"].strip().lower()].append((r["t"], r["m"]))
    need = {m for s, L in by_sym.items() if len(L) >= 3 for _, m in L}
    trades = {}
    for f in sorted((DATA_DIR / "chain" / "curve").glob("part-*.jsonl.gz")):
        for d in records(f):
            if d.get("m") in need and d["m"] not in trades:
                trades[d["m"]] = sorted(([t[1], t[6] / 1e9, t[7] / 1e6] for t in d["trades"]), key=lambda x: x[0])
    print(f"symbols with >= 3 launches in the window: {sum(len(L) >= 3 for L in by_sym.values())}; members {len(need)}, "
          f"backfilled {len(trades)} ({len(trades) / max(len(need), 1):.0%})")
    for N in (3, 5):
        for LEAD in (2, 4):
            res = {30: [], 120: []}
            skipped = 0
            for sym, L in by_sym.items():
                if len(L) < N:
                    continue
                L.sort()
                if any(m not in trades for _, m in L):
                    skipped += 1
                    continue
                stream = heapq.merge(*[[(t, m, vs, vt) for t, vs, vt in trades[m]] for _, m in L])
                cur = {}
                done_until = -1.0
                for t, m, vs, vt in stream:
                    cur[m] = vs
                    if t < done_until:
                        continue
                    members = [mm for tc, mm in L if t - WIN <= tc <= t]
                    if len(members) < N:
                        continue
                    sol = {mm: max(cur.get(mm, 30.0) - 30.0, 0.0) for mm in members}
                    lead = max(sol, key=sol.get)
                    if cur.get(lead, 0) < 40 or any(sol[o] * LEAD > sol[lead] for o in members if o != lead):
                        continue
                    tr = trades[lead]
                    ts = [x[0] for x in tr]
                    i = bisect.bisect_right(ts, t)
                    if i >= len(tr):
                        break
                    p_in, r_in = tr[i][1] / tr[i][2], tr[i][1]
                    for H in res:
                        j = bisect.bisect_left(ts, tr[i][0] + H * 60)
                        x = tr[j] if j < len(tr) else tr[-1]
                        g = (x[1] / x[2]) / p_in - 1
                        res[H].append((g, trade_return(g, 0.1, r_in, x[1])))
                    done_until = t + WIN
            for H, r in res.items():
                if not r:
                    print(f"N {N} LEAD {LEAD}x H {H}: n 0 (skipped incomplete clusters {skipped})")
                    continue
                g = sorted(x[0] for x in r)
                print(f"N {N} LEAD {LEAD}x H {H:>3} min: n {len(r)}, gross mean {statistics.mean(g):+.1%} median "
                      f"{statistics.median(g):+.1%}, net mean {statistics.mean(x[1] for x in r):+.1%}, ex-top3 "
                      f"{statistics.mean(g[:-3]) if len(g) > 3 else float('nan'):+.1%} (skipped incomplete {skipped})")
    print("kill rule (prereg): leader gross mean < +10%, or < 100 clusters in train (data too thin)")


if __name__ == "__main__":
    main()
