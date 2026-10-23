"""CX: coordinated-exit (CX1) and serial-funder creator skip (CX2) on M5 v1 entries
(vault "2026-10-23 coordinated-exit and serial-funder tests (CX)", pre-registered 14:55 PDT).

    uv run python -u -m tradeslave.research.cx_test
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict

from . import er_test
from .fl_test import CACHE, NONSYSTEM, PUBLIC
from .m5_bt import EXITS, ret

E2 = EXITS["E2 M4 exits, 15 s holder grace"]


def summ(v):
    if not v:
        return "n   0"
    return (f"n {len(v):3d} mean {statistics.mean(v):+6.1%} median {statistics.median(v):+6.1%} "
            f"<=-25% {sum(x <= -0.25 for x in v) / len(v):4.0%} win {sum(x > 0 for x in v) / len(v):4.0%}")


def main() -> None:
    er_test.resplit()
    _, byid, ent = er_test.load()
    parts = {"train": er_test.TRAIN, "valid": er_test.HELD}
    print("CX1, coordinated exit (E2 + exit when >= k pre-entry buyers sell at one block time):")
    res = {}
    for k in (None, 2, 3):
        ex = dict(stop=E2["stop"], P=dict(E2["P"], coord=k))
        r = {id(s): (s["r"] if k is None else ret(byid[s["m"]], s["k"], ex)) for s in ent}
        res[k] = r
        line = "  ".join(f"{p} {summ([r[id(s)] for s in ent if s['part'] in ps])}" for p, ps in parts.items())
        print(f"   {'base E2' if k is None else f'k = {k}':8s} {line}")
    deep = lambda v: sum(x <= -0.25 for x in v) / len(v)  # noqa: E731
    ok1 = all(deep([res[2][id(s)] for s in ent if s["part"] in ps]) < deep([res[None][id(s)] for s in ent if s["part"] in ps])
              and statistics.mean([res[2][id(s)] for s in ent if s["part"] in ps])
              >= statistics.mean([res[None][id(s)] for s in ent if s["part"] in ps]) for ps in parts.values())
    print(f"   CX1 (k = 2) verdict: {'PASS' if ok1 else 'FAIL'}")

    fund = json.loads(CACHE.read_text())
    public = set(json.loads(PUBLIC.read_text())) if PUBLIC.exists() else set()
    seen = defaultdict(int)
    flag = {}
    for s in sorted(ent, key=lambda s: byid[s["m"]]["t0"]):
        cr = byid[s["m"]]["rows"][0][4]
        f = (fund.get(cr) or {}).get("funder")
        kind = None if not f or f in NONSYSTEM else ("public" if f in public else "private")
        flag[id(s)] = (kind, seen[f] if f else 0)
        if f:
            seen[f] += 1
    print("\nCX2, creator's funder already funded an earlier creator (time-ordered):")
    for p, ps in parts.items():
        g = [s for s in ent if s["part"] in ps]
        for lab, c in (("private serial funder", lambda s: flag[id(s)] == ("private", flag[id(s)][1]) and flag[id(s)][1] >= 1),
                       ("public serial funder", lambda s: flag[id(s)][0] == "public" and flag[id(s)][1] >= 1),
                       ("any serial funder", lambda s: flag[id(s)][1] >= 1 and flag[id(s)][0] is not None),
                       ("kept (no serial funder)", lambda s: not (flag[id(s)][1] >= 1 and flag[id(s)][0] is not None))):
            print(f"   {p:5s} {lab:24s} {summ([s['r'] for s in g if c(s)])}")
    ok2 = True
    for ps in parts.values():
        g = [s for s in ent if s["part"] in ps]
        kept = [s["r"] for s in g if not (flag[id(s)][1] >= 1 and flag[id(s)][0] is not None)]
        base = [s["r"] for s in g]
        ok2 &= bool(kept) and deep(kept) < deep(base) and statistics.mean(kept) >= statistics.mean(base)
    n_skip = sum(1 for s in ent if s["part"] in er_test.TRAIN and flag[id(s)][1] >= 1 and flag[id(s)][0] is not None)
    ok2 &= n_skip >= 30
    print(f"   CX2 verdict: {'PASS' if ok2 else 'FAIL'} (train trades skipped: {n_skip})")


if __name__ == "__main__":
    main()
