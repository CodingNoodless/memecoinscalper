"""PX: one table of every entry we have (M1-M6 live trades + M5 backtest entries), the same variables at entry for each,
and each entry's return replayed through fixed exits. Feeds px_search (vault "2026-10-23 pattern search for M9 (PX)").

    uv run python -u -m tradeslave.research.px_build      -> data/m1/px_table.json
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import sys
import time
from collections import defaultdict

from ..paths import DATA_DIR
from . import er_test
from .fl_test import CACHE, NONSYSTEM, PUBLIC, features, holders_at
from .m5_bt import EXITS, ret

OUT = DATA_DIR / "m1" / "px_table.json"
SEED_PX = DATA_DIR / "m1" / "serial_seed_px.json"
SPLIT = time.mktime(time.strptime("2026-10-23 00:00", "%Y-%m-%d %H:%M"))      # design before, holdout from
E2 = EXITS["E2 M4 exits, 15 s holder grace"]
EXITS_PX = {"L5": dict(stop=0.90, P=dict(E2["P"], poll=1.0)), "E2": dict(stop=0.70, P=dict(E2["P"], poll=1.0))}
FAMILY = {"m1": "m1", "m2": "m1", "m3": "m1", "m4": "m1", "m5": "m5", "m6": "m5"}


def feed_coin(db, m):
    tr = db.execute("SELECT t, w, b, sol, tok, vs, vt FROM tr WHERE m=? ORDER BY slot, seq", (m,)).fetchall()
    bal, h, rows = defaultdict(float), 0, []
    for t, w, b, sol, tok, vs, vt in tr:
        b0 = bal[w]
        b1 = b0 + (tok if b else -tok)
        bal[w] = b1
        h += (b0 <= 1 < b1) - (b1 <= 1 < b0)
        if vt:
            rows.append([t, vs / vt, bool(b), sol, w, tok, h])
    return rows


def feats(rows, k, fund, public, seen):
    """Variables known at the entry row k (nothing after it)."""
    t, p = rows[k][0], rows[k][1]
    pre = rows[:k + 1]
    t0 = pre[0][0]
    win = lambda s: [r for r in pre if r[0] > t - s]  # noqa: E731
    w30, w60 = win(30), win(60)
    buy = lambda ws: sum(r[3] for r in ws if r[2])  # noqa: E731
    sell = lambda ws: sum(r[3] for r in ws if not r[2])  # noqa: E731
    first = {}
    for r in pre:
        if r[2]:
            first.setdefault(r[4], r[0])
    p60 = next((r[1] for r in pre if r[0] >= t - 60), p)
    lo60 = min((r[1] for r in w60), default=p)
    hi = max(r[1] for r in pre)
    top, cr = holders_at(rows, k)
    bal = defaultdict(float)
    for r in pre:
        bal[r[4]] += (r[5] or 0) if r[2] else -(r[5] or 0)
    early = [r for r in pre if r[0] <= t0 + 10 and r[2]]
    f = dict(
        age_s=t - t0, mc_sol=p * 1e9, holders=rows[k][6] or 0, trades_60=len(w60),
        buyers_30=len({r[4] for r in w30 if r[2]}), sellers_30=len({r[4] for r in w30 if not r[2]}),
        new_wallets_30=sum(1 for ft in first.values() if ft > t - 30), flow_30=buy(w30) - sell(w30),
        buy_ratio_60=buy(w60) / sell(w60) if sell(w60) else 9.9, ret_60=p / p60 if p60 else 1.0,
        from_lo60=p / lo60 if lo60 else 1.0, from_hi=p / hi if hi else 1.0,
        top10=sum(v for _, v in top[:10]) / 1e9, top1=(top[0][1] / 1e9) if top else 0.0,
        creator_hold=max(0.0, bal.get(cr, 0.0)) / 1e9, early_buy_sol=sum(r[3] for r in early),
        avg_buy_60=(buy(w60) / max(1, sum(1 for r in w60 if r[2]))), unique_buyers=len(first),
        sell_share_60=sell(w60) / max(1e-9, buy(w60) + sell(w60)), hour=time.localtime(t).tm_hour)
    if fund is not None:
        need = [cr] + [w for w, _ in top]
        known = sum(1 for w in need if (fund.get(w) or {}).get("funder"))
        if known >= 0.8 * len(need):
            fl = features(top, cr, t, fund, public)
            f.update(cluster=fl["cluster_share"], creator_linked=fl["creator_linked"], fresh=fl["fresh_share"])
            fu = (fund.get(cr) or {}).get("funder")
            if fu and fu not in NONSYSTEM:
                f["serial"] = seen[fu]
                f["serial_private"] = seen[fu] if fu not in public else 0
                seen[fu] += 1
    return f


def main() -> None:
    fund = json.loads(CACHE.read_text())
    public = set(json.loads(PUBLIC.read_text())) if PUBLIC.exists() else set()
    db = sqlite3.connect(DATA_DIR / "feed" / "trades.sqlite")
    ent = []                                                   # (t, family, src, mint, coin, k, extra)
    er_test.resplit()
    allc, byid, bt = er_test.load()
    allc = {c["m"]: c for c in allc}
    for s in bt:
        co = byid[s["m"]]
        ent.append((co["rows"][s["k"]][0], "m5", "bt", s["m"], co, s["k"], {}))
    for v, fam in FAMILY.items():
        for line in (DATA_DIR / v / "trades.jsonl").open(encoding="utf-8"):
            if not line.strip():
                continue
            x = json.loads(line)
            ent.append((x["t"], fam, v, x["m"], None, None, dict(score=x.get("score"), actual=x["ret"])))
    ent.sort(key=lambda e: e[0])
    seen_key, seen_fund, out, skipped, want = set(), defaultdict(int), [], defaultdict(int), set()
    for t, fam, src, m, co, k, extra in ent:
        if (m, fam) in seen_key:
            skipped["duplicate coin in family"] += 1
            continue
        if co is None:
            rows = feed_coin(db, m)
            if len(rows) < 10 and m in allc:                     # before the feed: Helius ticks in the backtest set
                rows = allc[m]["rows"]
            k = max((i for i, r in enumerate(rows) if r[0] <= t), default=None)
            if k is None or k < 3 or len(rows) <= k + 3:
                skipped["no feed coverage"] += 1
                continue
            co = dict(m=m, rows=rows)
        seen_key.add((m, fam))
        if "fetch" in sys.argv:
            top, cr = holders_at(co["rows"], k)
            want |= {cr} | {w for w, _ in top}
            continue
        f = feats(co["rows"], k, fund, public, seen_fund)
        f.update(extra)
        r = {n: ret(co, k, ex) for n, ex in EXITS_PX.items()}
        out.append(dict(t=t, part="design" if t < SPLIT else "holdout", family=fam, src=src, m=m, f=f, **r))
    if "fetch" in sys.argv:
        from .m9_test import lookup
        lookup(want)
        return
    OUT.write_text(json.dumps(out))
    SEED_PX.write_text(json.dumps(dict(seen_fund)))               # M9 v2 continues the serial counts from here
    print(f"{len(out)} entries; skipped {dict(skipped)}")
    for part in ("design", "holdout"):
        for fam in ("m1", "m5"):
            g = [e for e in out if e["part"] == part and e["family"] == fam]
            if g:
                print(f"   {part:7s} {fam}: n {len(g):4d} L5 mean {statistics.mean(e['L5'] for e in g):+6.1%} "
                      f"E2 mean {statistics.mean(e['E2'] for e in g):+6.1%} "
                      f"funding known {sum('cluster' in e['f'] for e in g) / len(g):.0%}")


if __name__ == "__main__":
    main()
