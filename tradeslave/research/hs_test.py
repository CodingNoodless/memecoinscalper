"""HS: hit-savvy early buyers (HS1) and arrival dynamics (HS2) as runner predictors on every complete feed coin.
Vault "2026-10-23 hit-savvy early buyers and arrival dynamics (HS)", pre-registered 20:12 PDT.

    uv run python -u -m tradeslave.research.hs_test      -> data/m1/hs_table.json + the report
"""

from __future__ import annotations

import heapq
import json
import sqlite3
import statistics
import time
from collections import defaultdict

from sklearn.metrics import roc_auc_score

from ..paths import DATA_DIR
from .m5_bt import EXITS, ret

N_BUY, WIN, X_RUN = 20, 1800, 2.0
SPLIT = time.mktime(time.strptime("2026-10-23 06:00", "%Y-%m-%d %H:%M"))
E2 = EXITS["E2 M4 exits, 15 s holder grace"]
L5 = dict(stop=0.90, P=dict(E2["P"], poll=1.0))
E2_1S = dict(stop=0.70, P=dict(E2["P"], poll=1.0))
OUT = DATA_DIR / "m1" / "hs_table.json"
FEATS = ["savvy_n", "savvy_hits", "dud_n", "t_to_20", "accel", "burst"]


def coins():
    db = sqlite3.connect(DATA_DIR / "feed" / "trades.sqlite")
    done = {m for (m,) in db.execute("SELECT m FROM coin WHERE complete=1")}
    cur, rows, bal, h = None, [], {}, 0
    for m, t, w, b, sol, tok, vs, vt in db.execute(
            "SELECT m, t, w, b, sol, tok, vs, vt FROM tr ORDER BY m, slot, seq"):
        if m != cur:
            if cur in done and rows:
                yield cur, rows
            cur, rows, bal, h = m, [], {}, 0
        b0 = bal.get(w, 0.0)
        b1 = b0 + (tok if b else -tok)
        bal[w] = b1
        h += (b0 <= 1 < b1) - (b1 <= 1 < b0)
        if vt and vs and vs > 0 and vt > 0:                   # a few feed rows carry no curve state
            rows.append([t, vs / vt, bool(b), sol, w, tok, h])
    if cur in done and rows:
        yield cur, rows


def decision(rows):
    """Index of the trade that brings the 20th unique non-creator buyer, and those buyers in order."""
    cr, seen = rows[0][4], []
    s = set()
    for i, r in enumerate(rows):
        if r[2] and r[4] != cr and r[4] not in s:
            s.add(r[4])
            seen.append((r[0], r[4]))
            if len(seen) == N_BUY:
                return i, seen
    return None, seen


def main() -> None:
    t_end = sqlite3.connect(DATA_DIR / "feed" / "trades.sqlite").execute("SELECT max(t) FROM tr").fetchone()[0]
    items = []
    for m, rows in coins():
        k, early = decision(rows)
        if k is None:
            continue
        t_k, p_k = rows[k][0], rows[k][1]
        if t_k + WIN > t_end:
            continue                                            # outcome not resolved inside the data
        mx = max((r[1] for r in rows[k + 1:] if r[0] <= t_k + WIN), default=p_k)
        items.append(dict(m=m, t=t_k, k=k, rows=rows, early=[w for _, w in early], times=[t for t, _ in early],
                          runner=mx >= X_RUN * p_k, peak=mx / p_k))
    items.sort(key=lambda x: x["t"])
    print(f"{len(items)} coins reach {N_BUY} buyers with a resolved 30-min outcome; runner rate "
          f"{sum(x['runner'] for x in items) / len(items):.1%}", flush=True)
    stats = defaultdict(lambda: [0, 0])                         # wallet -> [resolved early-buyer coins, runner hits]
    pending = []                                                # (resolve time, idx)
    for i, x in enumerate(items):
        while pending and pending[0][0] <= x["t"]:
            _, j = heapq.heappop(pending)
            for w in items[j]["early"]:
                stats[w][0] += 1
                stats[w][1] += items[j]["runner"]
        st = [stats[w] for w in x["early"]]
        x["savvy_n"] = sum(1 for n, h in st if n >= 2 and h / n >= 0.30)
        x["savvy_hits"] = sum(h for _, h in st)
        x["dud_n"] = sum(1 for n, h in st if n >= 5 and h == 0)
        ts = x["times"]
        x["t_to_20"] = ts[-1] - x["rows"][0][0]
        t_k = x["t"]
        last = sum(1 for t in ts if t > t_k - 10)
        prev = sum(1 for t in ts if t_k - 20 < t <= t_k - 10)
        x["accel"] = (last + 1) / (prev + 1)
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        mu = statistics.mean(gaps) if gaps else 0
        x["burst"] = statistics.pstdev(gaps) / mu if mu > 0 else 0.0
        heapq.heappush(pending, (t_k + WIN, i))
    print("simulating trades ...", flush=True)
    for x in items:
        co = dict(m=x["m"], rows=x["rows"])
        x["L5"] = ret(co, x["k"], L5)
        x["E2"] = ret(co, x["k"], E2_1S)
    table = [{k: v for k, v in x.items() if k not in ("rows", "early", "times")} for x in items]
    for x in table:
        x["part"] = "design" if x["t"] < SPLIT else "holdout"
    OUT.write_text(json.dumps(table))
    report(table)


def report(T):
    for part in ("design", "holdout"):
        g = [x for x in T if x["part"] == part]
        print(f"\n{part}: n {len(g)}, runner rate {sum(x['runner'] for x in g) / len(g):.1%}, "
              f"L5 mean {statistics.mean(x['L5'] for x in g):+.1%}, E2 mean {statistics.mean(x['E2'] for x in g):+.1%}")
        y = [x["runner"] for x in g]
        for f in FEATS:
            v = [x[f] for x in g]
            try:
                auc = roc_auc_score(y, v)
            except ValueError:
                auc = float("nan")
            print(f"   AUC {f:11s} {auc:.3f}")


if __name__ == "__main__":
    main()
