"""BR gap: where exactly do the BR bots' fills differ from our best mirror? Per holdout round trip, from the feed with slots:
their own fill vs the curve price just before their buy, end of their slot, end of their second, +1 s; same for the sell;
and what their buy reacts to (trades earlier in the same slot, slot gap to the previous trade).

    uv run python -u -m tradeslave.research.br_gap
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict

from ..paths import DATA_DIR
from . import br_test as B
from .hs_test import coins

FEE = 0.0125


def main() -> None:
    db = sqlite3.connect(DATA_DIR / "feed" / "trades.sqlite")
    t_end = db.execute("SELECT max(t) FROM tr").fetchone()[0]
    allc = [(m, r) for m, r in coins() if r[0][0] + B.WIN <= t_end]
    tr = B.trips(allc)
    bots = B.bots(tr)
    want = defaultdict(set)
    for w in bots:
        for x in tr[w]:
            if x["t"] >= B.SPLIT:
                want[x["m"]].add(w)
    print(f"{len(bots)} bots, {sum(len(v) for v in want.values())} holdout trips in {len(want)} coins", flush=True)
    R = defaultdict(list)
    for m, ws in want.items():
        rows = [r for r in db.execute("SELECT slot, seq, t, w, b, sol, tok, vs, vt FROM tr WHERE m=? ORDER BY slot, seq", (m,))
                if r[7] and r[8] and r[7] > 0 and r[8] > 0]
        if not rows:
            continue
        s0 = rows[0][0]
        px = [r[7] / r[8] for r in rows]                      # curve price after each trade

        def last(cond):
            i = max((j for j, r in enumerate(rows) if cond(r)), default=None)
            return px[i] if i is not None else None
        for w in ws:
            mine = [(j, r) for j, r in enumerate(rows) if r[3] == w]
            buys = [(j, r) for j, r in mine if r[4]]
            sells = [(j, r) for j, r in mine if not r[4] and r[0] >= buys[0][1][0]] if buys else []
            if not buys or not sells:
                continue
            jb, rb = buys[0]
            js, rs = sells[0]
            si = sum(r[5] for _, r in buys)
            so = sum(r[5] for _, r in sells)
            fill_b = rb[5] / rb[6]                              # their SOL per token on the first buy
            fill_s = rs[5] / rs[6]
            pre_b = px[jb - 1] if jb else px[0]
            same_slot_before = sum(1 for r in rows[:jb] if r[0] == rb[0])
            gap = rb[0] - rows[jb - 1][0] if jb else 0
            R["own_roi_after_fee"].append(so * (1 - FEE) / (si * (1 + FEE)) - 1)
            R["own_first_fill_ret"].append(fill_s / fill_b * (1 - FEE) / (1 + FEE) - 1)
            R["buy_fill_vs_pre"].append(fill_b / pre_b - 1)
            for lab, pb, ps in (
                    ("end_of_slot", last(lambda r: r[0] == rb[0]), last(lambda r: r[0] == rs[0])),
                    ("next_slot", last(lambda r: r[0] <= rb[0] + 1), last(lambda r: r[0] <= rs[0] + 1)),
                    ("end_of_second", last(lambda r: r[2] <= rb[2]), last(lambda r: r[2] <= rs[2])),
                    ("plus_1s", last(lambda r: r[2] <= rb[2] + 1), last(lambda r: r[2] <= rs[2] + 1))):
                R[f"mirror_{lab}"].append(ps / pb * (1 - FEE) ** 2 - 1)
                R[f"entry_cost_{lab}"].append(pb / fill_b - 1)
                R[f"exit_cost_{lab}"].append(1 - ps / fill_s)
            R["trades_same_slot_before_their_buy"].append(same_slot_before)
            R["slot_gap_to_prev_trade"].append(gap)
            R["slots_since_launch"].append(rb[0] - s0)
            R["hold_slots"].append(rs[0] - rb[0])
            R["sell_same_slot_as_buy"].append(rs[0] == rb[0])
    print(f"trips measured: {len(R['own_roi_after_fee'])}")
    for k, v in R.items():
        v = [float(x) for x in v]
        if k.startswith(("mirror", "own", "entry", "exit", "buy_fill")):
            print(f"  {k:38s} mean {statistics.mean(v):+7.2%}  median {statistics.median(v):+7.2%}")
        else:
            print(f"  {k:38s} mean {statistics.mean(v):8.2f}  median {statistics.median(v):6.1f}  share>0 {sum(x > 0 for x in v) / len(v):.0%}")


if __name__ == "__main__":
    main()
