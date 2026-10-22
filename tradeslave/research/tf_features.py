"""TF2: what separates winning trades from losing ones, measured at the moment of each M-bot buy.

    uv run python -m tradeslave.research.tf_features        -> data/m1/tf_features.json, design-half screen

Every closed M1-M4 trade with exact curve trades from the coin's create (data/m1/ticks). Features use only trades
up to the bot's entry. Exploration is on the design half (before 2026-10-22 00:00 PDT); the check half is read only
for the rules written down after that (vault note "2026-10-22 trade patterns (TF2)").
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict

from ..paths import DATA_DIR
from .tf_replay import BASE, SPLIT, load, path, sim


def feats(x, pre, created):
    """Pre-entry features from every curve trade since create; None if the window misses the create."""
    if not pre or pre[-1][6] is None:
        return None
    t = x["t"]
    dev = pre[0][4]
    bal, first_buy, buys_of, sells_of, sol_in = defaultdict(float), {}, defaultdict(int), defaultdict(int), defaultdict(float)
    slot0_w = set()
    t0 = pre[0][0]
    for i, (tt, sp, b, sol, w, tok, h) in enumerate(pre):
        bal[w] += tok if b else -tok
        if b:
            buys_of[w] += 1
            sol_in[w] += sol
            first_buy.setdefault(w, tt)
            if tt <= t0 + 1 and w != dev:
                slot0_w.add(w)
        else:
            sells_of[w] += 1
    buys = [r for r in pre if r[2]]
    sells = [r for r in pre if not r[2]]
    bs = [r[3] for r in buys]
    hser = [(r[0], r[6]) for r in pre]
    h_at = lambda s: next((h for tt, h in reversed(hser) if tt <= s), 0)   # noqa: E731
    holders = hser[-1][1]
    px = [r[1] for r in pre]
    ath = max(px)
    t_ath = next(r[0] for r in pre if r[1] == ath)
    held = {w: v for w, v in bal.items() if v > 1}
    top10 = sum(sorted((v for w, v in held.items() if w != dev), reverse=True)[:10]) / 1e7
    f = dict(
        age=t - created, n_trades=len(pre), holders=holders,
        h_growth30=holders - h_at(t - 30), h_growth10=holders - h_at(t - 10),
        h_off_max=max(h for _, h in hser) - holders,
        buyers=len(buys_of), sellers=len(sells_of),
        buyers30=len({r[4] for r in buys if r[0] > t - 30}), sellers30=len({r[4] for r in sells if r[0] > t - 30}),
        buyers10=len({r[4] for r in buys if r[0] > t - 10}), sellers10=len({r[4] for r in sells if r[0] > t - 10}),
        sell_ratio=len(sells) / max(1, len(pre)),
        buy_sol=sum(bs), sell_sol=sum(r[3] for r in sells),
        med_buy=statistics.median(bs) if bs else 0, small_buys=sum(1 for v in bs if v < 0.1) / max(1, len(bs)),
        big_buys=sum(1 for v in bs if v >= 1) / max(1, len(bs)),
        flippers=sum(1 for w in sells_of if w in buys_of) / max(1, len(buys_of)),
        sold_out=sum(1 for w in buys_of if bal[w] <= 1) / max(1, len(buys_of)),
        snipers=len(slot0_w), sniper_hold=sum(max(0.0, bal[w]) for w in slot0_w) / 1e7,
        dev_hold=max(0.0, bal[dev]) / 1e7, top10=top10,
        from_ath=pre[-1][1] / ath, since_ath=t - t_ath, from_open=pre[-1][1] / pre[0][1],
        flow10=sum(r[3] if r[2] else -r[3] for r in pre if r[0] > t - 10),
        flow30=sum(r[3] if r[2] else -r[3] for r in pre if r[0] > t - 30),
        new_wallets30=sum(1 for w, ft in first_buy.items() if ft > t - 30),
        repeat_buyers=sum(1 for w in buys_of if buys_of[w] >= 2) / max(1, len(buys_of)),
        score=x.get("score") or 0, hour=__import__("time").localtime(t).tm_hour, mc=x.get("mc") or 0,
    )
    return f


def main() -> None:
    trades, ticks, created = load()
    m4 = DATA_DIR / "m4" / "trades.jsonl"
    rows = []
    for x in trades:
        tr = ticks.get(x["m"])
        if not tr or x["m"] not in created:
            continue
        pre, post = path(x, tr, created[x["m"]])
        f = feats(x, pre, created[x["m"]])
        if f is None:
            continue
        pk10 = max((r[1] for r in post if r[0] <= x["t"] + 600), default=x["p0"]) / x["p0"]
        rows.append(dict(f, m=x["m"], v=x["v"], name=x.get("name"), t=x["t"], ret=x["ret"], exit=x["exit"],
                         base=sim(x, pre, post, dict(BASE))[0], pk10=pk10, half=int(x["t"] >= SPLIT)))
    (DATA_DIR / "m1" / "tf_features.json").write_text(json.dumps(rows))
    D = [r for r in rows if r["half"] == 0]
    print(len(rows), "trades with features;", len(D), "in design")
    if "check" in sys.argv:
        return
    keys = [k for k, v in rows[0].items() if isinstance(v, (int, float)) and k not in
            ("t", "ret", "base", "pk10", "half")]
    print("\nDESIGN ONLY: mean return by feature quintile (low -> high), and win rate")
    out = []
    for k in keys:
        v = sorted(r[k] for r in D)
        cuts = [v[int(len(v) * q)] for q in (0.2, 0.4, 0.6, 0.8)]
        if len(set(cuts)) < 2:
            continue
        g = [[r for r in D if (i == 0 or r[k] >= cuts[i - 1]) and (i == 4 or r[k] < cuts[i])] for i in range(5)]
        ms = [statistics.mean(r["ret"] for r in z) if z else float("nan") for z in g]
        ws = [sum(r["ret"] > 0 for r in z) / len(z) if z else 0 for z in g]
        spread = max(m for m in ms if m == m) - min(m for m in ms if m == m)
        out.append((spread, k, cuts, ms, ws, [len(z) for z in g]))
    for spread, k, cuts, ms, ws, ns in sorted(out, reverse=True):
        print(f"  {k:14s} cuts {', '.join(f'{c:.3g}' for c in cuts):34s} " +
              " ".join(f"{m:+6.1%}" for m in ms) + "   win " + " ".join(f"{w:3.0%}" for w in ws) + f"   n {ns}")


if __name__ == "__main__":
    main()
