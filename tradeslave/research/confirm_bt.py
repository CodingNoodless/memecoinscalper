"""CF: confirm-first entry (vault "2026-10-22 what separates runners (RN)", pre-registered 23:00 PDT).

    uv run python -m tradeslave.research.confirm_bt

Decide D s after a coin's first band trade (>= 5 s after create); buy only if holders, their 30-s gain and new wallets
in 30 s pass thresholds (grid chosen on A-design + B-train); M4's exits. Judged once on A-check and B-test.
"""

from __future__ import annotations

import gzip
import json
import statistics

from .m4_bt import OUT as COINS, SOL_USD, part, stats, trade
from .runners import decision

GRID = [(H, G, N) for H in (20, 30, 40) for G in (0, 5, 10) for N in (5, 10, 20)]


def facts(co, delay):
    rows, t0 = co["rows"], co["t0"]
    i = decision(rows, t0)
    if i is None:
        return None
    t = rows[i][0] + delay
    k = max(j for j, r in enumerate(rows) if r[0] <= t)
    pre = rows[:k + 1]
    fb = {}
    for r in pre:
        if r[2]:
            fb.setdefault(r[4], r[0])
    h = pre[-1][6] or 0
    h30 = next((r[6] or 0 for r in reversed(pre) if r[0] <= t - 30), 0)
    k1 = k
    while k1 + 1 < len(rows) and rows[k1 + 1][0] <= t + 1:
        k1 += 1
    return dict(k=k1, h=h, g=h - h30, n=sum(1 for ft in fb.values() if ft > t - 30), mc=pre[-1][1] * 1e9 * SOL_USD)


def main() -> None:
    with gzip.open(COINS, "rt", encoding="utf-8") as fh:
        coins = json.load(fh)
    for delay in (30, 15):
        fx = [(co, facts(co, delay)) for co in coins]
        fx = [(co, f) for co, f in fx if f and f["k"] + 1 < len(co["rows"])]
        res = {}
        for H, G, N in GRID:
            out = {}
            for co, f in fx:
                if f["h"] >= H and f["g"] >= G and f["n"] >= N and f["mc"] <= 15000:
                    out.setdefault(part(co), []).append(trade(co, f["k"], {}))
            res[(H, G, N)] = out
        design = lambda o: o.get("A-design", []) + o.get("B-train", [])        # noqa: E731
        held = lambda o: o.get("A-check", []) + o.get("B-test", [])            # noqa: E731
        ok = [(sum(design(o)), c) for c, o in res.items() if len(design(o)) >= 15]
        best = max(ok)[1] if ok else None
        print(f"\n== D = {delay} s; grid on design (A-design + B-train), top 6 by SOL:")
        for s, c in sorted(ok, reverse=True)[:6]:
            print(f"   H {c[0]:2d} G {c[1]:2d} N {c[2]:2d}: design {stats(design(res[c]))}")
        if best:
            o = res[best]
            print(f"   CHOSEN H {best[0]} G {best[1]} N {best[2]} -> held out:")
            for p in ("A-check", "B-test"):
                print(f"      {p:8s} {stats(o.get(p, []))}")
            print(f"      {'pooled':8s} {stats(held(o))}")
            print(f"      (design parts: A-design {stats(o.get('A-design', []))} | B-train {stats(o.get('B-train', []))})")


if __name__ == "__main__":
    main()
