"""RN: what separates coins that double (from an entry we could get) from the rest, at the moment we could buy.

    uv run python -m tradeslave.research.runners

Coins: MV's set A (Oct 21, score >= 7, Helius ticks) and set B (Oct 22 evening, every scored coin, live feed), curve
trades from create. Decision moment = the first trade in the $4-7k band >= 5 s after create; label = the price
reached 2x the fill (1 s later) within 30 min. Features use only trades up to the decision. A feature counts only if
it separates runners in all four parts (A-design, A-check, B-train, B-test).
"""

from __future__ import annotations

import gzip
import json
import math
import statistics
from collections import defaultdict

from ..paths import DATA_DIR
from .m4_bt import OUT as COINS, SOL_USD, part

POSTS = DATA_DIR / "m1" / "post_info.json"


def decision(rows, t0):
    for i, r in enumerate(rows):
        if r[0] >= t0 + 5 and 4000 <= r[1] * 1e9 * SOL_USD <= 7000:
            return i
    return None


def feats(co, info):
    rows, t0 = co["rows"], co["t0"]
    i = decision(rows, t0)
    if i is None:
        return None
    t = rows[i][0]
    pre = rows[:i + 1]
    dev = rows[0][4]
    fill = [r[1] for r in rows if r[0] <= t + 1][-1]
    later = [r[1] for r in rows if t + 1 <= r[0] <= t + 1800]
    if not later:
        return None
    y = max(later) >= 2 * fill
    buys = [r for r in pre if r[2]]
    sells = [r for r in pre if not r[2]]
    w = lambda s, side=None: [r for r in pre if r[0] > t - s and (side is None or r[2] == side)]   # noqa: E731
    bal, first_buy, sold_any = defaultdict(float), {}, set()
    for r in pre:
        bal[r[4]] += r[5] if r[2] else -r[5]
        if r[2]:
            first_buy.setdefault(r[4], r[0])
        else:
            sold_any.add(r[4])
    hser = [r[6] or 0 for r in pre]
    h = hser[-1]
    h_at = lambda s: next((r[6] or 0 for r in reversed(pre) if r[0] <= t - s), 0)   # noqa: E731
    slot0 = {r[4] for r in pre if r[0] <= t0 + 1 and r[2] and r[4] != dev}
    held = sorted((v for k, v in bal.items() if v > 1 and k != dev), reverse=True)
    bs = [r[3] or 0 for r in buys]
    px = [r[1] for r in pre]
    pi = info.get(co["m"], {})
    c = co["c"]
    f = dict(
        secs_to_band=t - t0, trades=len(pre), holders=h, h_max=max(hser), h_gain30=h - h_at(30), h_gain10=h - h_at(10),
        buyers=len(first_buy), sellers=len({r[4] for r in sells}), buyers10=len({r[4] for r in w(10, True)}),
        buyers30=len({r[4] for r in w(30, True)}), sellers10=len({r[4] for r in w(10, False)}),
        new_wallets30=sum(1 for k, ft in first_buy.items() if ft > t - 30),
        trades30=len(w(30)), sell_share=len(sells) / max(1, len(pre)),
        buy_sol=sum(bs), sell_sol=sum(r[3] or 0 for r in sells), flow10=sum((r[3] or 0) * (1 if r[2] else -1) for r in w(10)),
        flow30=sum((r[3] or 0) * (1 if r[2] else -1) for r in w(30)),
        med_buy=statistics.median(bs) if bs else 0, small_buy_share=sum(1 for v in bs if v < 0.2) / max(1, len(bs)),
        big_buy_share=sum(1 for v in bs if v >= 1) / max(1, len(bs)),
        dev_buy=sum(r[3] or 0 for r in pre if r[4] == dev and r[2]), dev_hold=max(0.0, bal[dev]) / 1e7,
        dev_sold=float(dev in sold_any), snipers=len(slot0), sniper_hold=sum(max(0, bal[k]) for k in slot0) / 1e7,
        top1=held[0] / 1e7 if held else 0, top10=sum(held[:10]) / 1e7, flippers=len(sold_any & set(first_buy)) / max(1, len(first_buy)),
        from_ath=px[-1] / max(px), from_open=px[-1] / px[0], score=c.get("score") or 0,
        followers=pi.get("followers") if pi.get("kind") == "post" else None,
        post=float(pi.get("kind") == "post"), quote=float(bool(pi.get("quote"))),
        text_len=len(pi.get("text") or ""), name_len=len(c.get("name") or ""), desc_len=len(c.get("desc") or ""),
        hour=__import__("time").localtime(t0).tm_hour,
    )
    f["buyers_per_holder"] = f["buyers"] / max(1, h)
    f["tx_rate"] = f["trades"] / max(1.0, f["secs_to_band"])
    return f, y


def main() -> None:
    with gzip.open(COINS, "rt", encoding="utf-8") as fh:
        coins = json.load(fh)
    info = json.loads(POSTS.read_text())
    data = []
    for co in coins:
        r = feats(co, info)
        if r:
            data.append((part(co), r[0], r[1]))
    parts = ("A-design", "A-check", "B-train", "B-test")
    print("coins with a decision moment:", {p: (sum(1 for q, _, _ in data if q == p), sum(y for q, _, y in data if q == p))
                                            for p in parts}, "(n, runners)")
    keys = [k for k in data[0][1]]
    out = []
    for k in keys:
        aucs, lifts = [], []
        for p in parts:
            z = [(f[k], y) for q, f, y in data if q == p and f[k] is not None]
            pos = [v for v, y in z if y]
            neg = [v for v, y in z if not y]
            if len(pos) < 3 or len(neg) < 3:
                aucs.append(None)
                continue
            # AUC = P(runner's value > non-runner's value)
            a = sum((pv > nv) + 0.5 * (pv == nv) for pv in pos for nv in neg) / (len(pos) * len(neg))
            aucs.append(a)
        good = [a for a in aucs if a is not None]
        same = good and (all(a > 0.5 for a in good) or all(a < 0.5 for a in good))
        out.append((min(abs(a - 0.5) for a in good) if same and len(good) == 4 else 0, k, aucs))
    print("\nAUC per part (0.5 = no signal; >0.5 runners higher, <0.5 runners lower). * = same direction in all 4 parts")
    for m, k, aucs in sorted(out, key=lambda z: -z[0]):
        mark = "*" if m > 0 else " "
        print(f" {mark} {k:18s} " + "  ".join(f"{p[:1]}{p[-4:]} {a:.2f}" if a is not None else f"{p[:1]}{p[-4:]}  -  " for p, a in zip(parts, aucs))
              + (f"   weakest {0.5 + m:.2f}" if m else ""))
    json.dump([(q, f, y) for q, f, y in data], open(DATA_DIR / "m1" / "rn_features.json", "w"))


if __name__ == "__main__":
    main()
