"""PB / NW (vault "2026-10-22 what separates runners (RN)", pre-registered 22:55 PDT): on CF-confirmed coins, a
pullback entry and a new-wallet stop.

    uv run python -m tradeslave.research.pb_nw
"""

from __future__ import annotations

import gzip
import json

from .m4_bt import OUT as COINS, SIZE, part, stats, trade
from .runners import decision

PARTS = ("A-design", "A-check", "B-train", "B-test")


def confirmed(co):
    """Decision index D = 30 s after the first band trade, if the coin passes CF (H 40, G 5, mc <= $15k)."""
    rows, t0 = co["rows"], co["t0"]
    i = decision(rows, t0)
    if i is None:
        return None
    t = rows[i][0] + 30
    k = max(j for j, r in enumerate(rows) if r[0] <= t)
    h = rows[k][6] or 0
    h30 = next((r[6] or 0 for r in reversed(rows[:k + 1]) if r[0] <= t - 30), 0)
    if h >= 40 and h - h30 >= 5 and rows[k][1] * 1e9 * 105 <= 15000:
        return k, t
    return None


def fill_after(rows, k, t):
    while k + 1 < len(rows) and rows[k + 1][0] <= t + 1:
        k += 1
    return k


def pullback(co, k, t, X, W):
    rows = co["rows"]
    hi, hmax = rows[k][1], max(r[6] or 0 for r in rows[:k + 1])
    for j in range(k + 1, len(rows)):
        r = rows[j]
        if r[0] > t + W:
            return None
        hi = max(hi, r[1])
        hmax = max(hmax, r[6] or 0)
        if r[1] <= (1 - X) * hi and (r[6] or 0) >= 0.9 * hmax:
            return fill_after(rows, j, r[0])
    return None


def main() -> None:
    with gzip.open(COINS, "rt", encoding="utf-8") as fh:
        coins = json.load(fh)
    base = [(co, c) for co in coins for c in [confirmed(co)] if c]
    print(len(base), "confirmed coins")
    entries = {"CF at confirmation": lambda co, k, t: fill_after(co["rows"], k, t)}
    for X in (0.10, 0.20):
        for W in (60, 120):
            entries[f"PB pullback {int(X * 100)}% within {W} s"] = lambda co, k, t, X=X, W=W: pullback(co, k, t, X, W)
    exits = {"M4 exits": {}}
    for S in (15, 30):
        for K in (1, 3):
            exits[f"NW <{K} new wallets in {S} s"] = {"hdecl": None, "nw": (S, K, 10)}
    res = {}
    for en, ef in entries.items():
        for xn, over in exits.items():
            out = {}
            for co, (k, t) in base:
                j = ef(co, k, t)
                if j is not None and j + 1 < len(co["rows"]):
                    out.setdefault(part(co), []).append(trade(co, j, over))
            res[(en, xn)] = out
    design = lambda o: o.get("A-design", []) + o.get("B-train", [])      # noqa: E731
    held = lambda o: o.get("A-check", []) + o.get("B-test", [])          # noqa: E731
    fams = {"PB alone": [k for k in res if k[0].startswith("PB") and k[1] == "M4 exits"],
            "NW alone": [k for k in res if k[0].startswith("CF") and k[1].startswith("NW")],
            "PB + NW": [k for k in res if k[0].startswith("PB") and k[1].startswith("NW")]}
    print("\nreference, CF at confirmation + M4 exits:")
    o = res[("CF at confirmation", "M4 exits")]
    for p in PARTS:
        print(f"   {p:8s} {stats(o.get(p, []))}")
    for fam, keys in fams.items():
        ok = [(sum(design(res[k])), k) for k in keys if len(design(res[k])) >= 15]
        print(f"\n== {fam}: design SOL by cell")
        for sol, k in sorted(ok, reverse=True):
            print(f"   {k[0]:30s} {k[1]:24s} design {stats(design(res[k]))}")
        if not ok:
            print("   no cell with >= 15 design trades")
            continue
        k = max(ok)[1]
        o = res[k]
        print(f"   CHOSEN {k[0]} + {k[1]}")
        for p in PARTS:
            print(f"      {p:8s} {stats(o.get(p, []))}")
        print(f"      held-out {stats(held(o))}")
        print(f"      ALL      {stats(design(o) + held(o))}")


if __name__ == "__main__":
    main()
