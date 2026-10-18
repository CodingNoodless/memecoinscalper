"""Strategy O: does a big buy on a young token pull a bot wave that the big buyer can sell into?

    uv run python -m tradeslave.research.whale_wave

Event study on UNBIASED launches (research.creator history sample + research.full_trades random
sample). Event = a buy >= B SOL by a wallet outside the creation slot at age A_MIN..A_MAX. We take the
big buyer's place: same SOL, same fill, then everyone ELSE's later trades replayed in order (token-flow
curve), and we sell the whole bag at the first trade >= T s later. Curve fee 1.25% per side, 0.002 SOL
per transaction. Buyers are split by what we know about them BEFORE the event (prior launches in the
sample): new wallets are the closest stand-in for us (no private information).
"""

from __future__ import annotations

import gzip
import json
import math
import statistics
from collections import defaultdict

from .creator import launches
from .full_trades import OUT as FT_OUT

VS0, VT0 = 30e9, 1073e12
K = VS0 * VT0
FEE, PRIO = 0.0125, 0.002
TS = (5, 10, 15, 30, 60)


def unbiased() -> list[tuple[float, str, list[tuple]]]:
    out = {}
    for d in launches():
        out[d["m"]] = (d["t0"], [(r["t"], r["slot"], r["w"], bool(r["b"]), r["sol"], abs(r["tok"])) for r in d["trades"]])
    for p in FT_OUT.glob("*.json.gz"):
        d = json.load(gzip.open(p, "rt", encoding="utf-8"))
        tr = []
        for x in d["trades"]:
            if x[7]:
                break                                  # graduated: curve trades only
            tr.append((x[2], x[0], x[3], x[4], x[5], x[6]))
        if tr:
            out.setdefault(d["m"], (d["t0"], tr))
    return sorted((t0, m, tr) for m, (t0, tr) in out.items())


def events(tr: list[tuple], b_min: float, a_min: float, a_max: float):
    s0, tc = tr[0][1], tr[0][0]
    grp = {r[2] for r in tr if r[1] == s0}
    for i, r in enumerate(tr):
        if r[3] and r[4] >= b_min and r[2] not in grp and a_min <= r[0] - tc <= a_max:
            yield i


def take_place(tr: list[tuple], i: int, hold: float) -> tuple[float, float]:
    """(net SOL, SOL in) if we had made trade i and sold `hold` s later."""
    vt = VT0
    for r in tr[:i]:
        vt += -r[5] * 1e6 if r[3] else r[5] * 1e6
    t_e, w, sol, tok = tr[i][0], tr[i][2], tr[i][4], tr[i][5] * 1e6
    vt -= tok
    for r in tr[i + 1:]:
        if r[0] - t_e >= hold:
            break
        if r[2] == w:
            continue
        vt += -r[5] * 1e6 if r[3] else r[5] * 1e6
        vt = max(vt, 1e12)
    vs = K / vt
    out = vs * tok / (vt + tok) / 1e9
    paid = sol * (1 + FEE)
    return out * (1 - FEE) - paid - 2 * PRIO, paid


def stats(rows) -> str:
    if len(rows) < 5:
        return f"n={len(rows)}"
    xs = [a / b for a, b in rows]
    m, sd = statistics.mean(xs), statistics.pstdev(xs)
    return (f"n={len(xs):4d} ROI {m:+6.1%} med {statistics.median(xs):+6.1%} win {sum(x > 0 for x in xs) / len(xs):4.0%} "
            f"t {m / (sd / math.sqrt(len(xs))) if sd else 0:+5.2f} | SOL {sum(a for a, _ in rows):+8.2f}")


def main() -> None:
    toks = unbiased()
    print(f"{len(toks)} unbiased launches")
    seen = defaultdict(int)                      # wallet -> launches seen before (time order)
    rows = defaultdict(list)
    for t0, m, tr in toks:
        for b_min in (1, 2, 5):
            for i in events(tr, b_min, 3, 600):
                new = seen[tr[i][2]] == 0
                for T in TS:
                    rows[(b_min, T, new)].append(take_place(tr, i, T))
        for w in {r[2] for r in tr}:
            seen[w] += 1
    for b_min in (1, 2, 5):
        for new in (True, False):
            print(f"\nbuy >= {b_min} SOL at 3 s-10 min, buyer {'NEW to the sample' if new else 'seen before'}:")
            for T in TS:
                print(f"  sell {T:3d} s later: {stats(rows[(b_min, T, new)])}")


if __name__ == "__main__":
    main()
