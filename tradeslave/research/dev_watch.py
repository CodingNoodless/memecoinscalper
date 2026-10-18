"""Strategy L: ride launches whose creator is still holding, exit when the creator sells.

    uv run python -m tradeslave.research.dev_watch

At decision time tau after the create, if the creation-slot group (creator + bundle) has sold nothing,
at least B outside wallets bought and the price is up >= R vs the creation price, buy (fill at the
first trade >= tau + LAT). Exit at the first trade >= (first creator-group sell + LAT), or at tau + H.
Replay of all real trades with our order inserted (own impact), 1.25% fee per side, PRIO per tx.
Data: the random launch sample (research.creator.launches, first 15 min).
"""

from __future__ import annotations

import itertools
import statistics

from .creator import FEE, VS0, VT0, launches, stats

PRIO = 0.002
LAT = 1.5            # s from a trade's block to our fill (feed ~0.5-1 s + send)


def state_at(d: dict, tau: float):
    ts, tc = d["trades"], d["tc"]
    s0 = ts[0]["slot"]
    grp = {r["w"] for r in ts if r["slot"] == s0}
    vs, sold, buyers = VS0, False, set()
    for r in ts:
        if r["t"] - tc > tau:
            break
        vs += r["dv"]
        if r["w"] in grp and not r["b"]:
            sold = True
        if r["w"] not in grp and r["b"]:
            buyers.add(r["w"])
    vs_create = VS0 + sum(r["dv"] for r in ts if r["slot"] == s0)
    price_ratio = (vs / vs_create) ** 2           # constant product: price ~ vs^2
    return sold, len(buyers), price_ratio, grp


def trade(d: dict, tau: float, hold: float, size: float, grp: set):
    ts, tc = d["trades"], d["tc"]
    vs, vt = VS0, VT0
    bag, entered, proceeds, exit_at = 0.0, False, None, tc + tau + hold
    for r in ts:
        if not entered and r["t"] >= tc + tau + LAT:
            if r["t"] >= exit_at:
                return None
            net = size * (1 - FEE) * 1e9
            bag = vt * net / (vs + net)
            vs, vt, entered = vs + net, vt - bag, True
        if entered and proceeds is None and r["t"] >= exit_at:
            proceeds = vs * bag / (vt + bag) / 1e9 * (1 - FEE)
            vs, vt = vs - proceeds / (1 - FEE) * 1e9, vt + bag
        if r["b"]:
            dv = max(r["dv"], 0)
            got = vt * dv / (vs + dv)
            vs, vt = vs + dv, vt - got
        else:
            tok = r["tok"] * 1e6
            out = vs * tok / (vt + tok)
            vs, vt = vs - out, vt + tok
        if r["w"] in grp and not r["b"] and r["t"] - tc > tau:
            exit_at = min(exit_at, r["t"] + LAT)          # dev sold: we see it and get out
    if not entered:
        return None
    if proceeds is None:
        proceeds = vs * bag / (vt + bag) / 1e9 * (1 - FEE)
    return proceeds - size - 2 * PRIO


def main() -> None:
    ls = launches()
    h = len(ls) // 2
    print(f"{len(ls)} random launches; design = first {h} by time, test = rest\n")
    rows = []
    for tau, b, r, hold in itertools.product((20, 30, 60, 120), (3, 10, 25), (1.0, 1.5, 2.5), (30, 120, 600)):
        res = {}
        for name, part in (("design", ls[:h]), ("test", ls[h:])):
            xs = []
            for d in part:
                sold, nb, pr, grp = state_at(d, tau)
                if not sold and nb >= b and pr >= r:
                    v = trade(d, tau, hold, 1.0, grp)
                    if v is not None:
                        xs.append(v)
            res[name] = xs
        if len(res["design"]) >= 10:
            rows.append((statistics.mean(res["design"]), tau, b, r, hold, res))
    rows.sort(key=lambda x: -x[0])
    print(f"{len(rows)} rules with >= 10 design trades; top 8 by design mean (1 SOL size):")
    for m, tau, b, r, hold, res in rows[:8]:
        print(f"tau={tau:3d}s buyers>={b:2d} price>={r}x hold<={hold:3d}s | design {stats(res['design'])[:60]} | TEST {stats(res['test'])}")


if __name__ == "__main__":
    main()
