"""PX search (vault "2026-10-23 pattern search for M9 (PX)", pre-registered 17:07 PDT).

    uv run python -u -m tradeslave.research.px_search design    -> search design only, write data/m1/px_picks.json
    uv run python -u -m tradeslave.research.px_search holdout   -> judge the saved picks once on holdout
"""

from __future__ import annotations

import itertools
import json
import statistics
import sys

from ..paths import DATA_DIR
from .px_build import OUT as TABLE

PICKS = DATA_DIR / "m1" / "px_picks.json"
POOLS = {"m1": lambda e: e["family"] == "m1", "m5": lambda e: e["family"] == "m5", "all": lambda e: True}
Y = "L5"


def cond(rule):
    """rule = [(var, op, cut), ...]; an entry missing a var fails the rule."""
    def ok(e):
        for var, op, cut in rule:
            v = e["f"].get(var)
            if v is None or (v < cut if op == ">=" else v > cut):
                return False
        return True
    return ok


def mean(v):
    return statistics.mean(v) if v else float("nan")


def label(rule):
    return " & ".join(f"{v} {op} {c:.4g}" for v, op, c in rule)


def design() -> None:
    T = json.loads(TABLE.read_text())
    D = [e for e in T if e["part"] == "design"]
    vars_ = sorted({k for e in D for k, v in e["f"].items() if isinstance(v, (int, float)) and not isinstance(v, bool)} - {"actual"})
    picks = {}
    for pool, inpool in POOLS.items():
        P = sorted((e for e in D if inpool(e)), key=lambda e: e["t"])
        half = P[len(P) // 2]["t"]
        base, base1, base2 = (mean([e[Y] for e in g]) for g in (P, [e for e in P if e["t"] < half], [e for e in P if e["t"] >= half]))

        def score(rule, nmin):
            k = [e for e in P if cond(rule)(e)]
            if len(k) < nmin:
                return None
            a = [e[Y] for e in k if e["t"] < half]
            b = [e[Y] for e in k if e["t"] >= half]
            if not a or not b or mean(a) <= base1 or mean(b) <= base2:
                return None
            return mean([e[Y] for e in k]), len(k), mean(a), mean(b)

        singles = []
        for var in vars_:
            vals = sorted(e["f"][var] for e in P if e["f"].get(var) is not None)
            if len(vals) < 60:
                continue
            cuts = sorted({vals[int(len(vals) * q / 10)] for q in range(1, 10)})
            for c in cuts:
                for op in (">=", "<="):
                    s = score([(var, op, c)], 60)
                    if s:
                        singles.append((s, [(var, op, c)]))
        singles.sort(key=lambda x: -x[0][0])
        top = []                                                 # best 12, one rule per variable and direction
        for s, r in singles:
            if not any(r[0][:2] == t[1][0][:2] for t in top):
                top.append((s, r))
            if len(top) == 12:
                break
        pairs = []
        for (s1, r1), (s2, r2) in itertools.combinations(top, 2):
            if r1[0][0] == r2[0][0]:
                continue
            s = score(r1 + r2, 40)
            if s:
                pairs.append((s, r1 + r2))
        cands = sorted(singles + pairs, key=lambda x: -x[0][0])
        chosen = []
        for s, r in cands:
            if label(r) not in {label(c[1]) for c in chosen}:
                chosen.append((s, r))
            if len(chosen) == 3:
                break
        print(f"\npool {pool}: design n {len(P)} mean {base:+.1%} (halves {base1:+.1%} / {base2:+.1%}); "
              f"{len(singles)} stable single rules, {len(pairs)} stable pairs")
        for s, r in top[:8]:
            print(f"   single {label(r):45s} mean {s[0]:+6.1%} n {s[1]:3d} halves {s[2]:+.1%} / {s[3]:+.1%}")
        for s, r in sorted(pairs, key=lambda x: -x[0][0])[:5]:
            print(f"   pair   {label(r):45s} mean {s[0]:+6.1%} n {s[1]:3d} halves {s[2]:+.1%} / {s[3]:+.1%}")
        picks[pool] = [dict(rule=r, design_mean=s[0], design_n=s[1]) for s, r in chosen]
        for p in picks[pool]:
            print(f"   PICK   {label(p['rule'])}  (design {p['design_mean']:+.1%}, n {p['design_n']})")
    PICKS.write_text(json.dumps(picks))
    print(f"\npicks saved to {PICKS.name}")


def holdout() -> None:
    T = json.loads(TABLE.read_text())
    H = [e for e in T if e["part"] == "holdout"]
    picks = json.loads(PICKS.read_text())
    best = None
    for pool, ps in picks.items():
        P = [e for e in H if POOLS[pool](e)]
        base = mean([e[Y] for e in P])
        print(f"\npool {pool}: holdout n {len(P)} mean {base:+.1%} (E2 {mean([e['E2'] for e in P]):+.1%})")
        for p in ps:
            k = [e for e in P if cond(p["rule"])(e)]
            m = mean([e[Y] for e in k])
            ok = len(k) >= 30 and m > 0 and m >= base + 0.03
            deep = sum(e[Y] <= -0.25 for e in k) / len(k) if k else float("nan")
            print(f"   {label(p['rule']):50s} design {p['design_mean']:+6.1%} (n {p['design_n']}) | holdout {m:+6.1%} "
                  f"n {len(k):3d} E2 {mean([e['E2'] for e in k]):+6.1%} <=-25% {deep:.0%} -> {'PASS' if ok else 'fail'}")
            if ok and (best is None or m > best[0]):
                best = (m, pool, p["rule"], len(k))
    print("\nBEST PASSING:", f"{best[1]}: {label(best[2])} holdout {best[0]:+.1%} n {best[3]}" if best else "none")


if __name__ == "__main__":
    holdout() if "holdout" in sys.argv else design()
