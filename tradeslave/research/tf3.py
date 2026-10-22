"""TF3: Rohit's M4-note rules (vault "2026-10-22 Rohit M4 notes (TF3)"), pre-registered 16:58 PDT.

    uv run python -m tradeslave.research.tf3
"""

from __future__ import annotations

import heapq
import json
import statistics
from collections import defaultdict

from .tf_replay import BASE, HORIZON, SPLIT, SIZE, load, path, sim


def pre_rules(pre, dev):
    """Entry-time facts from every curve trade since create."""
    bought_sol, bought_tok, sold_tok = defaultdict(float), defaultdict(float), defaultdict(float)
    dev_sold = dev_rebuy = False
    for t, sp, b, sol, w, tok, h in pre:
        if b:
            bought_sol[w] += sol
            bought_tok[w] += tok
            if w == dev and dev_sold:
                dev_rebuy = True
        else:
            sold_tok[w] += tok
            if w == dev:
                dev_sold = True
    top5 = sorted((w for w in bought_sol if w != dev), key=lambda w: -bought_sol[w])[:5]
    bt = sum(bought_tok[w] for w in top5)
    out = sum(min(sold_tok[w], bought_tok[w]) for w in top5) / bt if bt else 0.0
    px = [r[1] for r in pre]
    return dict(top5_out=out, dev_sold=dev_sold, dev_rebuy=dev_rebuy,
                from_open=pre[-1][1] / px[0], from_ath=pre[-1][1] / max(px))


def top10_series(pre, post, dev):
    """Top-10 holders' share of supply (dev excluded) after each post-entry trade, in %."""
    bal = defaultdict(float)
    for r in pre:
        bal[r[4]] += r[5] if r[2] else -r[5]
    out = []
    for r in post:
        bal[r[4]] += r[5] if r[2] else -r[5]
        top = heapq.nlargest(10, (v for w, v in bal.items() if w != dev and v > 0))
        out.append(sum(top) / 1e7)
    return out


def sim_h4(x, pre, post, dev, drop):
    """Baseline exits plus: sell when the top-10 share fell >= drop points within 10 s (polled like the bot)."""
    s10 = top10_series(pre, post, dev)
    t_sig = None
    for i, r in enumerate(post):
        if r[0] > x["t"] + HORIZON:
            break
        j = i
        while j > 0 and post[j - 1][0] > r[0] - 10:
            j -= 1
        if max(s10[j:i + 1]) - s10[i] >= drop:
            t_sig = r[0]
            break
    if t_sig is None:
        return sim(x, pre, post, dict(BASE))[0]
    cut = [r for r in post if r[0] <= t_sig + 2.0]           # the next poll after the signal fills
    base_ret, why, t_exit, _ = sim(x, pre, post, dict(BASE))
    if t_exit <= t_sig:
        return base_ret                                       # the normal rules sold first
    from .c1_curve import sell_curve
    p = cut[-1][1]
    return (sell_curve(x["q"], p) - 2 * 0.0005) / SIZE - 1


def main() -> None:
    trades, ticks, created = load()
    rows = []
    for x in trades:
        tr = ticks.get(x["m"])
        if not tr or x["m"] not in created:
            continue
        pre, post = path(x, tr, created[x["m"]])
        if not pre or pre[-1][6] is None:
            continue
        rows.append((x, pre, post, pre[0][4]))
    half = lambda x: int(x["t"] >= SPLIT)                       # noqa: E731
    base = {id(x): sim(x, pre, post, dict(BASE))[0] for x, pre, post, dev in rows}
    feats = {id(x): pre_rules(pre, dev) for x, pre, post, dev in rows}
    print(f"{len(rows)} trades with full history; baseline design "
          f"{statistics.mean(base[id(x)] for x, *_ in rows if half(x) == 0):+.1%}, check "
          f"{statistics.mean(base[id(x)] for x, *_ in rows if half(x) == 1):+.1%}")

    def judge_skip(name, f):
        out = []
        for k in (0, 1):
            z = [(base[id(x)], f(feats[id(x)])) for x, *_ in rows if half(x) == k]
            take = [b for b, s in z if not s]
            skip = [b for b, s in z if s]
            out.append(dict(n=len(z), take=take, skip=skip, all=[b for b, _ in z]))
        ok = all(h["take"] and statistics.mean(h["take"]) > statistics.mean(h["all"]) and sum(h["take"]) >= sum(h["all"])
                 and sum(h["skip"]) < 0 for h in out)
        for k, h in zip(("design", "check"), out):
            m = statistics.mean(h["take"]) if h["take"] else float("nan")
            sm = statistics.mean(h["skip"]) if h["skip"] else float("nan")
            print(f"   {name:38s} {k:6s} take n {len(h['take']):3d} mean {m:+6.1%} SOL {sum(h['take']) * SIZE:+.3f} | "
                  f"skip n {len(h['skip']):3d} mean {sm:+6.1%} SOL {sum(h['skip']) * SIZE:+.3f} | all {statistics.mean(h['all']):+6.1%}")
        return ok, out

    print("\nSKIP RULES (variant chosen on design by taken SOL)")
    fams = {
        "H1 smart money out": [("top5 sold >= 50%", lambda f: f["top5_out"] >= 0.5), ("top5 sold >= 30%", lambda f: f["top5_out"] >= 0.3)],
        "H2 dev games": [("dev sold anything", lambda f: f["dev_sold"]), ("dev sold then rebought", lambda f: f["dev_rebuy"])],
        "H3 back at launch price": [("<= 1.15x launch and <= 0.8x high", lambda f: f["from_open"] <= 1.15 and f["from_ath"] <= 0.8)],
    }
    accepted = []
    for fam, vs in fams.items():
        res = []
        for lab, f in vs:
            ok, out = judge_skip(f"{fam}: {lab}", f)
            res.append((sum(out[0]["take"]), lab, ok))
        best = max(res)
        print(f"   => {fam}: chosen '{best[1]}' -> {'ACCEPTED' if best[2] else 'rejected'}\n")
        if best[2]:
            accepted.append((fam, best[1]))

    print("EXIT RULE H4 (top-10 share drop)")
    best = None
    for drop in (5.0, 8.0):
        res = {id(x): sim_h4(x, pre, post, dev, drop) for x, pre, post, dev in rows}
        h = []
        for k in (0, 1):
            z = [(res[id(x)], base[id(x)]) for x, *_ in rows if half(x) == k]
            h.append(dict(new=sum(a for a, b in z), old=sum(b for a, b in z), d=statistics.mean(a - b for a, b in z),
                          wn=sum(a for a, b in z if a > 0), wo=sum(b for a, b in z if b > 0)))
        ok = all(q["new"] >= q["old"] for q in h) and h[0]["d"] >= 0.02 and all(q["wn"] >= 0.75 * q["wo"] for q in h)
        print(f"   drop >= {drop:.0f} pts in 10 s: design {(h[0]['new'] - h[0]['old']) * SIZE:+.3f} SOL ({h[0]['d']:+.1%}/trade) | "
              f"check {(h[1]['new'] - h[1]['old']) * SIZE:+.3f} SOL ({h[1]['d']:+.1%}/trade) {'ok' if ok else ''}")
        if best is None or h[0]["new"] - h[0]["old"] > best[0]:
            best = (h[0]["new"] - h[0]["old"], drop, ok)
    print(f"   => H4: chosen drop {best[1]:.0f} -> {'ACCEPTED' if best[2] else 'rejected'}")
    if best[2]:
        accepted.append(("H4 top-10 drop exit", best[1]))
    print("\nACCEPTED:", accepted or "none")


if __name__ == "__main__":
    main()
