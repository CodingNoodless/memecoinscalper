"""M6: M5 v1 plus bad-buy skip filters (vault "2026-10-23 M6 bad-buy filters (M6)", pre-registered 13:10 PDT).

    uv run python -u -m tradeslave.research.m6          # features, single filters, greedy selection on train
    uv run python -u -m tradeslave.research.m6 held     # the selected set, judged once on validation

`features(rows, k)` is shared with the live bot, so the backtest and M6 compute the same numbers.
Rows are curve trades [t, price, is_buy, sol, wallet, tokens, holders].
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict

from ..paths import DATA_DIR

SUPPLY = 1e9
MIN_N, BAR, STEP = 100, 0.20, 0.01
PICK = DATA_DIR / "m1" / "m6_pick.json"


def features(rows, k):
    """Bad-buy features at row k, from the coin's trades up to and including k."""
    t_k, t_first, creator = rows[k][0], rows[0][0], rows[0][4]
    bal, first_buy = defaultdict(float), {}
    xfer = buy_sol = 0.0
    buy_n = 0
    sizes = []
    for t, _p, b, sol, w, tok, _h in rows[:k + 1]:
        tok = tok or 0.0
        if b:
            bal[w] += tok
            first_buy.setdefault(w, t)
            buy_sol += sol or 0.0
            buy_n += 1
            if w != creator and (sol or 0) >= 0.05 and len(sizes) < 20:
                sizes.append((w, sol))
        else:
            xfer += max(0.0, tok - max(bal[w], 0.0))                # sold more than it ever bought: arrived by transfer
            bal[w] -= tok
    held = {w: v for w, v in bal.items() if v > 1}
    tot = sum(held.values()) or 1.0
    early = {w for w, t in first_buy.items() if t <= t_first + 1.0}
    same = sum(1 for i, (w, s) in enumerate(sizes)
               if any(j != i and w2 != w and abs(s2 - s) <= 0.01 * s for j, (w2, s2) in enumerate(sizes)))
    h_then = next((r[6] for r in reversed(rows[:k + 1]) if r[0] <= t_k - 60), rows[0][6])
    return dict(holders=rows[k][6] or 0,
                early_share=sum(held.get(w, 0.0) for w in early) / tot,
                xfer_sold=xfer / SUPPLY,
                same_size=same / len(sizes) if len(sizes) >= 5 else 0.0,
                holder_trend=(rows[k][6] or 0) - (h_then or 0),
                top10=sum(sorted(held.values(), reverse=True)[:10]) / tot,
                avg_buy=buy_sol / buy_n if buy_n else 0.0)


def filters(avg_buy_med):
    """Pre-registered keep conditions. f = features + score + creator_repeat."""
    return {"F1 holders >= 60": lambda f: f["holders"] >= 60,
            "F2 early_share < 10%": lambda f: f["early_share"] < 0.10,
            "F3 xfer_sold <= 1%": lambda f: f["xfer_sold"] <= 0.01,
            "F4 same_size < 30%": lambda f: f["same_size"] < 0.30,
            "F5 holder_trend >= 0": lambda f: f["holder_trend"] >= 0,
            "F6 top10 < 50%": lambda f: f["top10"] < 0.50,
            "F7 score >= 5": lambda f: f["score"] >= 5,
            "F8 creator_repeat = 0": lambda f: f["creator_repeat"] == 0,
            f"F9 avg_buy >= {avg_buy_med:.3f}": lambda f: f["avg_buy"] >= avg_buy_med}


def keep(f, names, fl):
    return all(fl[n](f) for n in names)


def summ(v):
    if not v:
        return "n   0"
    return (f"n {len(v):4d} mean {statistics.mean(v):+6.1%} median {statistics.median(v):+6.1%} "
            f"win {sum(x > 0 for x in v) / len(v):4.0%} <=-25% {sum(x <= -0.25 for x in v) / len(v):4.0%}")


def main() -> None:
    from . import er_test
    er_test.resplit()
    coins, byid, ent = er_test.load()
    seen = defaultdict(int)
    repeat = {}
    for co in sorted(coins, key=lambda c: c["t0"]):
        w = co["rows"][0][4] if co["rows"] else None
        repeat[co["m"]] = seen[w]
        seen[w] += 1
    for s in ent:
        co = byid[s["m"]]
        s["f6"] = dict(features(co["rows"], s["k"]), score=co["c"].get("score") or 0, creator_repeat=repeat[s["m"]])
    traded = {json.loads(line)["m"] for line in open(DATA_DIR / "m5" / "trades.jsonl") if line.strip()}
    tr = [s for s in ent if s["part"] in er_test.TRAIN]
    va = [s for s in ent if s["part"] in er_test.HELD and s["m"] not in traded]
    print(f"{len(ent)} entries: train {len(tr)}, validation {len(va)} "
          f"(after dropping {sum(s['part'] in er_test.HELD for s in ent) - len(va)} coins M5 traded live)\n")
    med = statistics.median(s["f6"]["avg_buy"] for s in tr)
    fl = filters(med)
    print("feature medians on train:", {k: round(statistics.median(s["f6"][k] for s in tr), 3) for k in tr[0]["f6"]})
    print(f"\nTRAIN, base:  {summ([s['r'] for s in tr])}")
    for n, f in fl.items():
        print(f"   {n:28s} kept {summ([s['r'] for s in tr if f(s['f6'])])}   | skipped {summ([s['r'] for s in tr if not f(s['f6'])])}")
    chosen, cur = [], statistics.mean(s["r"] for s in tr)
    while True:
        best = None
        for n in fl:
            if n in chosen:
                continue
            v = [s["r"] for s in tr if keep(s["f6"], chosen + [n], fl)]
            if len(v) >= MIN_N and statistics.mean(v) >= cur + STEP and (best is None or statistics.mean(v) > best[0]):
                best = (statistics.mean(v), n)
        if not best:
            break
        cur = best[0]
        chosen.append(best[1])
        print(f"   + {best[1]:28s} -> train {summ([s['r'] for s in tr if keep(s['f6'], chosen, fl)])}")
    v = [s["r"] for s in tr if keep(s["f6"], chosen, fl)]
    ok = len(v) >= MIN_N and statistics.mean(v) >= BAR
    print(f"\nM6 filter set: {chosen or 'none'}\n   train {summ(v)}   bar (>= {MIN_N}, >= {BAR:+.0%}): {'PASS' if ok else 'FAIL'}")
    PICK.write_text(json.dumps({"filters": chosen, "avg_buy_med": med, "train_mean": statistics.mean(v) if v else None,
                                "train_n": len(v), "bar": ok}, indent=1))
    if "held" in sys.argv:
        print("\nVALIDATION (judged once, coins M5 traded live excluded):")
        for p in sorted({s["part"] for s in va}):
            g = [s for s in va if s["part"] == p]
            print(f"   {p:5s} M6 {summ([s['r'] for s in g if keep(s['f6'], chosen, fl)])}   | base {summ([s['r'] for s in g])}")
        print(f"   all   M6 {summ([s['r'] for s in va if keep(s['f6'], chosen, fl)])}   | base {summ([s['r'] for s in va])}")


if __name__ == "__main__":
    main()
