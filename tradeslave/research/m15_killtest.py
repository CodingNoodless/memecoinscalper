"""M15 fixed 0.90x initial stop kill test (RD design sample only)."""

from __future__ import annotations

import json
import math
import statistics

from ..paths import DATA_DIR
from .rb_model import _cv, _names, _split

OUT = DATA_DIR / "runners" / "rb"
FEE_SIDE = 0.0225
TX_FRACTION = 2 * 0.0003 / 0.05


def factory(f: dict) -> bool:
    return bool(f["template"]) or (math.expm1(f["curve_min"]) < 3 and f["l_buyers"] <= 40)


def replay(c: list[list[float]], k: int, initial_stop: float) -> float | None:
    e = c[k][4]
    if not e:
        return None
    stop, armed, end_t, last = initial_stop * e, False, c[k][0] + 6 * 3600, e
    cost_mult = (1 - FEE_SIDE) / (1 + FEE_SIDE)
    for t, o, h, lo, cl, _ in c[k + 1 :]:
        if t > end_t:
            break
        if lo <= stop:
            return min(o, stop) / e * cost_mult - 1 - TX_FRACTION
        if h >= 2 * e:
            armed = True
        if armed:
            stop = max(stop, 1.25 * e, (math.floor(h / e) - 1) * e if h >= 3 * e else 0)
        last = cl
    return last / e * cost_mult - 1 - TX_FRACTION


def stats(label: str, rets: list[float]) -> None:
    if not rets:
        print(f"{label}: n=0")
        return
    n = len(rets)
    half = n // 2
    ex3 = statistics.mean(sorted(rets)[:-3]) if n > 3 else float("nan")
    print(f"{label}: n={n}, mean={statistics.mean(rets):+.2%}, median={statistics.median(rets):+.2%}, "
          f"win={sum(x > 0 for x in rets) / n:.1%}, halves="
          f"{statistics.mean(rets[:half]):+.2%}/{statistics.mean(rets[half:]):+.2%}, ex-top3={ex3:+.2%}")


def main() -> None:
    rows = json.loads((OUT / "features.json").read_text(encoding="utf-8"))
    design, _, cut = _split(rows)
    sample = {r["t"]: r for r in design if r["sample"]}
    names = _names(rows, False)
    preds = _cv(design, "logit", names)
    threshold = json.loads((OUT / "design_choice.json").read_text(encoding="utf-8"))["thr"]
    flagged = [sample[t] for p, _, t in preds if p >= threshold and t in sample]
    paired = []
    missing = 0
    for r in flagged:
        if factory(r["f"]):
            continue
        path = OUT / "candles1m" / f"{r['m']}.json"
        if not path.exists():
            missing += 1
            continue
        x = json.loads(path.read_text(encoding="utf-8"))
        c = x["c"]
        k = next((i for i, row in enumerate(c) if row[0] >= x["t_mig"]), None)
        if k is None or k >= len(c) - 1:
            continue
        a, b = replay(c, k, 0.80), replay(c, k, 0.90)
        if a is not None and b is not None:
            paired.append((r["t"], a, b))
    paired.sort()
    old = [a for _, a, _ in paired]
    new = [b for _, _, b in paired]
    diffs = [b - a for _, a, b in paired]
    half = len(paired) // 2
    diff_halves = [statistics.mean(diffs[:half]) if half else float("nan"),
                   statistics.mean(diffs[half:]) if len(paired) - half else float("nan")]
    print(f"M15 design-only: cutoff={cut:.0f}, OOF flags={len(flagged)}, missing candles={missing}, paired={len(paired)}")
    stats("0.80x current stop", old)
    stats("0.90x candidate stop", new)
    print(f"paired improvement: mean {statistics.mean(diffs):+.2%}, median {statistics.median(diffs):+.2%}, "
          f"halves {diff_halves[0]:+.2%}/{diff_halves[1]:+.2%}, positive pairs {sum(x > 0 for x in diffs)}/{len(diffs)}")


if __name__ == "__main__":
    main()
