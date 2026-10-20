"""Strategy Y15: long-only memecoin rotation by trailing return, momentum or reversal (pre-registered; vault:
"2026-10-20 aged memecoin survivors (Y)", section Y15).

    uv run python -m tradeslave.research.rotation design
    uv run python -m tradeslave.research.rotation test

Prices: research.perps_data hourly closes (Binance perps as the price source). Every R hours hold the K coins with the
lowest (REV) or highest (MOM) trailing L-hour return among coins listed >= 30 days, equal weight. Cost per side on
coins that enter or leave. Excess = strategy minus the equal-weight basket of all eligible coins.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from itertools import product

from .perps_data import OUT as PERPS, REFERENCE

FROZEN = PERPS.parent / "rotation_rule.json"
K, COST, COST2, AGE = 3, 0.003, 0.005, 30 * 86400
DESIGN, TEST = (1672531200, 1751328000), (1751328000, 1800000000)    # 2023-01..2025-07 / 2025-07..
GRID = list(product(("REV", "MOM"), (24, 72, 168), (24, 72, 168)))


def closes():
    out = {}
    for f in PERPS.glob("*.json"):
        d = json.loads(f.read_text())
        if d["sym"] in REFERENCE:
            continue
        out[d["sym"]] = {int(k[0]): k[4] for k in d["k"]}
    return out


def run(px, mode, L, R, lo, hi, cost=COST):
    first = {s: min(v) for s, v in px.items()}
    t0 = lo - lo % 3600
    held, out = set(), []
    t = t0
    while t + R * 3600 < hi:
        elig = [s for s, v in px.items() if t - first[s] >= AGE and t in v and t - L * 3600 in v and t + R * 3600 in v]
        if len(elig) >= 6:
            past = {s: px[s][t] / px[s][t - L * 3600] - 1 for s in elig}
            fwd = {s: px[s][t + R * 3600] / px[s][t] - 1 for s in elig}
            pick = sorted(elig, key=lambda s: past[s], reverse=(mode == "MOM"))[:K]
            turn = len(set(pick) ^ held) / K                        # fraction of the book traded (each side)
            r = statistics.mean(fwd[s] for s in pick) - cost * turn
            out.append((t, r, r - statistics.mean(fwd.values())))
            held = set(pick)
        else:
            held = set()
        t += R * 3600
    return out


def fmt(xs):
    if len(xs) < 3:
        return f"n {len(xs)}"
    a, e = [x[1] for x in xs], [x[2] for x in xs]
    te = statistics.mean(e) / statistics.stdev(e) * math.sqrt(len(e))
    return f"n {len(xs):4d} abs {statistics.mean(a):+.2%} excess {statistics.mean(e):+.2%} t {te:+.2f}"


def design():
    px = closes()
    best = None
    for mode, L, R in GRID:
        xs = run(px, mode, L, R, *DESIGN)
        print(f"{mode} L {L:3d} R {R:3d}: {fmt(xs)}", flush=True)
        if len(xs) >= 30:
            m = statistics.mean(x[2] for x in xs)
            if best is None or m > best[0]:
                best = (m, dict(mode=mode, L=L, R=R))
    FROZEN.write_text(json.dumps(best[1]))
    print("FROZEN", best[1], f"design excess {best[0]:+.2%}")


def test():
    px, p = closes(), json.loads(FROZEN.read_text())
    xs = run(px, lo=TEST[0], hi=TEST[1], **p)
    h = len(xs) // 2
    print(f"TEST {p}: {fmt(xs)}\n   halves {fmt(xs[:h])} / {fmt(xs[h:])}\n   0.5%/side "
          f"{fmt(run(px, lo=TEST[0], hi=TEST[1], cost=COST2, **p))}")


def main() -> None:
    {"design": design, "test": test}[sys.argv[1]]()


if __name__ == "__main__":
    main()
