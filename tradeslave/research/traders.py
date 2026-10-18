"""Who makes money trading pump.fun tokens, does it persist, and what do they do? (strategy N research)

    uv run python -m tradeslave.research.traders

Data: every exact chain trade we have (research.backfill_trades OUT + HISTORY_OUT: first 15 min of
~5k launches). Per wallet per token: SOL out - SOL in + leftover tokens dumped into the final curve
state of the window (fee + impact). Creation-slot wallets (creator + bundle) are excluded: they are
creators, not traders. Skill test: rank wallets on tokens launched in the first half of the period,
then look at the SAME wallets on the second half.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict

from .backfill_trades import HISTORY_OUT, OUT

VS0, VT0 = 30e9, 1073e12
FEE = 0.0125


def load():
    """mint -> (t0, trades sorted)"""
    tr = defaultdict(list)
    for p in (OUT, HISTORY_OUT):
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                if '"trade"' not in line:
                    continue
                r = json.loads(line)
                tr[r["m"]].append(r)
    out = {}
    for m, ts in tr.items():
        ts.sort(key=lambda r: r.get("i", 0))
        if ts[0].get("create") or ts[0].get("i") == 0:
            out[m] = ts
    return out


def positions(ts: list[dict]) -> dict[str, dict]:
    """wallet -> position summary on this token (non-creation-slot wallets only)."""
    s0, tc = ts[0]["slot"], ts[0]["t"]
    grp = {r["w"] for r in ts if r["slot"] == s0}
    vs, vt = VS0, VT0
    pos: dict[str, dict] = {}
    for r in ts:
        vs += r["dv"]
        vt += r["dc"]
        w = r["w"]
        if w in grp:
            continue
        p = pos.setdefault(w, {"in": 0.0, "out": 0.0, "tok": 0.0, "t_first": r["t"] - tc, "slot_first": r["slot"] - s0,
                               "mc_first": vs, "n_buy": 0, "n_sell": 0, "t_last_sell": None, "first_buy_sol": None})
        if r["b"]:
            p["in"] += r["sol"]
            p["tok"] += r["tok"] * 1e6
            p["n_buy"] += 1
            if p["first_buy_sol"] is None:
                p["first_buy_sol"] = r["sol"]
        else:
            p["out"] += r["sol"]
            p["tok"] -= r["tok"] * 1e6
            p["n_sell"] += 1
            p["t_last_sell"] = r["t"] - tc
    for p in pos.values():
        left = max(p["tok"], 0.0)
        p["mark"] = vs * left / (vt + left) / 1e9 * (1 - FEE) if left > 0 and vs > 0 else 0.0
        p["pnl"] = p["out"] + p["mark"] - p["in"]
        p["held_out"] = left > 0
    return pos


def main() -> None:
    data = load()
    t0s = {m: ts[0]["t"] for m, ts in data.items()}
    order = sorted(data, key=lambda m: t0s[m])
    cut = t0s[order[len(order) // 2]]
    print(f"{len(data)} launches with exact trades, split at t={cut:.0f}")
    per = {"A": defaultdict(list), "B": defaultdict(list)}      # wallet -> [(mint, pos)]
    for m in order:
        half = "A" if t0s[m] < cut else "B"
        for w, p in positions(data[m]).items():
            if p["in"] > 0:
                per[half][w].append((m, p))
    # Skill persistence: wallets active in both halves with >= N tokens each
    for n_min in (5, 10, 20):
        ws = [w for w in per["A"] if len(per["A"][w]) >= n_min and len(per["B"].get(w, [])) >= n_min]
        if len(ws) < 20:
            continue
        roi = lambda lst: sum(p["pnl"] for _, p in lst) / sum(p["in"] for _, p in lst)   # noqa: E731
        a = [roi(per["A"][w]) for w in ws]
        b = [roi(per["B"][w]) for w in ws]
        rho = statistics.correlation(a, b)
        ranked = sorted(ws, key=lambda w: -roi(per["A"][w]))
        dec = max(1, len(ws) // 10)
        top, bot = ranked[:dec], ranked[-dec:]
        agg = lambda grp, h: (sum(p["pnl"] for w in grp for _, p in per[h][w]),            # noqa: E731
                              sum(p["in"] for w in grp for _, p in per[h][w]))
        tp, ti = agg(top, "B")
        bp, bi = agg(bot, "B")
        allp, alli = agg(ws, "B")
        print(f"\nwallets with >= {n_min} tokens in both halves: {len(ws)}; corr(ROI A, ROI B) {rho:+.3f}")
        print(f"  top 10% by first-half ROI -> second half ROI {tp / ti:+.1%} on {ti:,.0f} SOL "
              f"({sum(roi(per['B'][w]) > 0 for w in top)}/{len(top)} wallets profitable)")
        print(f"  bottom 10%                -> second half ROI {bp / bi:+.1%}")
        print(f"  all of them               -> second half ROI {allp / alli:+.1%}")
    # Save the persistent winners' trades for behaviour analysis.
    ws = [w for w in per["A"] if len(per["A"][w]) >= 10 and len(per["B"].get(w, [])) >= 10]
    roi = lambda lst: sum(p["pnl"] for _, p in lst) / sum(p["in"] for _, p in lst)          # noqa: E731
    winners = [w for w in ws if roi(per["A"][w]) > 0.05 and roi(per["B"][w]) > 0.05]
    print(f"\npersistent winners (ROI > +5% in BOTH halves, >= 10 tokens each): {len(winners)} of {len(ws)}")
    rows = []
    for w in winners:
        for h in "AB":
            for m, p in per[h][w]:
                rows.append({"w": w, "m": m, **{k: v for k, v in p.items()}})
    (OUT.parent / "winners.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    others = [p for h in "AB" for w in ws if w not in set(winners) for _, p in per[h][w]]
    wins = [r for r in rows]

    def describe(lbl, ps):
        if not ps:
            return
        q = lambda xs, f: sorted(xs)[int(f * (len(xs) - 1))]                              # noqa: E731
        hold = [p["t_last_sell"] - p["t_first"] for p in ps if p["t_last_sell"] is not None]
        print(f"  {lbl:22s} positions {len(ps):6d} | entry age med {q([p['t_first'] for p in ps], .5):6.1f}s "
              f"p25 {q([p['t_first'] for p in ps], .25):5.1f}s | entry slot med {q([p['slot_first'] for p in ps], .5):4.0f} | "
              f"entry curve SOL med {q([p['mc_first'] / 1e9 for p in ps], .5):5.1f} | buy size med {q([p['in'] for p in ps], .5):5.2f} | "
              f"hold med {q(hold, .5) if hold else math.nan:6.1f}s | still holding {sum(p['held_out'] for p in ps) / len(ps):4.0%} | "
              f"ROI {sum(p['pnl'] for p in ps) / sum(p['in'] for p in ps):+.1%}")

    print("\nbehaviour (persistent winners vs other active wallets):")
    describe("persistent winners", wins)
    describe("other active wallets", others)


if __name__ == "__main__":
    main()
