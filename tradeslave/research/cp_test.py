"""CP: copy-trading the best wallets (ranked on the feed before Oct 23 06:00, copied after, 1 s late).
Vault "2026-10-23 copy-trading the best wallets (CP)", pre-registered 23:37 PDT.

    uv run python -u -m tradeslave.research.cp_test
"""

from __future__ import annotations

import bisect
import sqlite3
import statistics
import time
from collections import defaultdict

from ..paths import DATA_DIR
from .hs_test import L5
from .m5_bt import ret

SPLIT = time.mktime(time.strptime("2026-10-23 06:00", "%Y-%m-%d %H:%M"))
FEE, TXR = 0.0125, 2 * 0.0005 / 0.1


def load():
    db = sqlite3.connect(DATA_DIR / "feed" / "trades.sqlite")
    coins = defaultdict(list)
    for m, t, w, b, sol, tok, vs, vt in db.execute("SELECT m, t, w, b, sol, tok, vs, vt FROM tr ORDER BY m, slot, seq"):
        if vt and vs and vs > 0 and vt > 0:
            coins[m].append((t, vs / vt, bool(b), sol or 0.0, w, tok or 0.0))
    return coins


def rank(coins):
    """wallet -> stats over round trips that started before SPLIT."""
    st = defaultdict(lambda: dict(n=0, won=0, spent=0.0, prof=0.0, first=0, early=0))
    for m, rows in coins.items():
        t0 = rows[0][0]
        per = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, None])   # sol in, sol out, tok in, tok out, first buy t
        for t, p, b, sol, w, tok in rows:
            x = per[w]
            if b:
                x[0] += sol
                x[2] += tok
                if x[4] is None:
                    x[4] = t
            else:
                x[1] += sol
                x[3] += tok
        for w, (si, so, ti, to, tf) in per.items():
            if tf is None or tf >= SPLIT or si <= 0:
                continue
            s = st[w]
            s["first"] += 1
            s["early"] += tf - t0 <= 2
            if ti > 0 and to >= 0.9 * ti:
                s["n"] += 1
                s["spent"] += si
                s["prof"] += so - si
                s["won"] += so > si
    return st


def good(st):
    out = set()
    for w, s in st.items():
        if s["n"] >= 5 and s["prof"] >= 1.0 and s["spent"] > 0 and s["prof"] / s["spent"] >= 0.30 and s["won"] / s["n"] >= 0.5 \
                and s["early"] / max(1, s["first"]) < 0.5:
            out.add(w)
    return out


def px_at(ts, rows, t):
    i = bisect.bisect_right(ts, t) - 1
    return rows[max(i, 0)][1], max(i, 0)


def main() -> None:
    coins = load()
    t_end = max(r[-1][0] for r in coins.values())
    st = rank(coins)
    G = good(st)
    top20 = set(sorted((w for w, s in st.items() if s["n"] >= 3), key=lambda w: -st[w]["prof"])[:20])
    print(f"{len(coins)} coins; wallets with ranking-period round trips: {sum(1 for s in st.values() if s['n'])}; good wallets: {len(G)}; "
          f"top-20 by profit: {len(top20)}", flush=True)
    for label, S in (("GOOD (pre-registered)", G), ("TOP20 by profit (report)", top20)):
        res = defaultdict(list)
        own = []
        for m, rows in coins.items():
            ts = [r[0] for r in rows]
            fb = None
            for t, p, b, sol, w, tok in rows:
                if b and w in S and t >= SPLIT:
                    fb = (t, w)
                    break
            if not fb:
                continue
            t_b, w = fb
            if t_b + 1800 > t_end:
                continue
            sells = [r for r in rows if r[4] == w and not r[2] and r[0] > t_b]
            t_s = sells[0][0] if sells and sells[0][0] <= t_b + 1800 else t_b + 1800
            si = sum(r[3] for r in rows if r[4] == w and r[2] and r[0] >= SPLIT)
            so = sum(r[3] for r in rows if r[4] == w and not r[2] and r[0] >= SPLIT)
            if si > 0:
                own.append((so - si) / si)
            half = "h1" if t_b < (SPLIT + t_end) / 2 else "h2"
            for d in (0, 1, 3):
                pe, ke = px_at(ts, rows, t_b + d)
                px, _ = px_at(ts, rows, t_s + d)
                r = px / pe * (1 - FEE) ** 2 - 1 - TXR
                res[f"mirror+{d}"].append((half, r))
            pe, ke = px_at(ts, rows, t_b + 1)
            rr = [[r[0], r[1], r[2], r[3], r[4], r[5], None] for r in rows]
            res["L5+1"].append((half, ret(dict(m=m, rows=rr), ke, L5)))
        print(f"\n{label}: copies {len(res['mirror+1'])}")
        for k, v in res.items():
            vals = [r for _, r in v]
            h = {hh: [r for q, r in v if q == hh] for hh in ("h1", "h2")}
            print(f"   {k:9s} n {len(vals):4d} mean {statistics.mean(vals):+6.1%} med {statistics.median(vals):+6.1%} "
                  f"win {sum(r > 0 for r in vals) / len(vals):4.0%} | halves {statistics.mean(h['h1']):+.1%} / {statistics.mean(h['h2']):+.1%}"
                  if vals and h["h1"] and h["h2"] else f"   {k} n {len(vals)}")
        if own:
            print(f"   the wallets' own copy-period ROI per coin: mean {statistics.mean(own):+.1%}, median {statistics.median(own):+.1%} (n {len(own)})")
        if label.startswith("GOOD"):
            v = res["mirror+1"]
            vals = [r for _, r in v]
            ok = len(vals) >= 60 and statistics.mean(vals) > 0 and all(statistics.mean([r for q, r in v if q == hh]) > 0 for hh in ("h1", "h2"))
            print(f"   CP VERDICT: {'PASS' if ok else 'FAIL'}")


if __name__ == "__main__":
    main()
