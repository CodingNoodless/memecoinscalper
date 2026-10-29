"""M14 fixed first-minute confirmation kill test (design sample only).

Run with: uv run python -m tradeslave.research.m14_killtest
This must not read or score the RD holdout outcomes.
"""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

import numpy as np

from ..paths import DATA_DIR
from .rb_model import _cv, _names, _split

OUT = DATA_DIR / "runners" / "rb"
FEE_SIDE = 0.0225  # 1.25% fee + 1% slippage, each side
TX_PER_SIDE_SOL = 0.0003
SIZE_SOL = 0.05


def m13_factory(f: dict) -> bool:
    return bool(f["template"]) or (math.expm1(f["curve_min"]) < 3 and f["l_buyers"] <= 40)


def m12r_return(candles: list[list[float]], entry_i: int) -> float | None:
    """Replay the frozen M13 M12r exit on OHLC candles, then charge fixed TX cost."""
    e = candles[entry_i][4]
    if not e:
        return None
    stop, armed, end_t, last = 0.8 * e, False, candles[entry_i][0] + 6 * 3600, e
    ratio = (1 - FEE_SIDE) / (1 + FEE_SIDE)
    tx_cost = 2 * TX_PER_SIDE_SOL / SIZE_SOL
    for t, o, h, lo, cl, _ in candles[entry_i + 1 :]:
        if t > end_t:
            break
        # Match rb_model.simulate's conservative same-candle ordering.
        if lo <= stop:
            return min(o, stop) / e * ratio - 1 - tx_cost
        if h >= 2 * e:
            armed = True
        if armed:
            stop = max(stop, 1.25 * e, (math.floor(h / e) - 1) * e if h >= 3 * e else 0)
        last = cl
    return last / e * ratio - 1 - tx_cost


def summarize(label: str, rows: list[dict]) -> None:
    if not rows:
        print(f"{label}: n=0")
        return
    rets = [r["ret"] for r in rows]
    n = len(rets)
    half = n // 2
    trimmed = sorted(rets)[:-3] if n > 3 else []
    h1 = statistics.mean(rets[:half]) if half else float("nan")
    h2 = statistics.mean(rets[half:]) if n - half else float("nan")
    ex3 = statistics.mean(trimmed) if trimmed else float("nan")
    print(f"{label}: n={n}, mean={statistics.mean(rets):+.2%}, median={statistics.median(rets):+.2%}, "
          f"win={sum(x > 0 for x in rets) / n:.1%}, halves={h1:+.2%}/{h2:+.2%}, "
          f"ex-top3={ex3:+.2%}, <=-25%={sum(x <= -0.25 for x in rets) / n:.1%}")


def main() -> None:
    rows = json.loads((OUT / "features.json").read_text(encoding="utf-8"))
    design, _, cut = _split(rows)
    train_sample = [r for r in design if r["sample"]]
    model_rows = [r for r in design if r["sample"] or not r["sample"]]
    names = _names(rows, False)
    preds = _cv(model_rows, "logit", names)
    threshold = json.loads((OUT / "design_choice.json").read_text(encoding="utf-8"))["thr"]
    # _cv emits only out-of-fold uniform-sample predictions from the provided design rows.
    scores = {(t, y): p for p, y, t in preds}
    by_time = {r["t"]: r for r in train_sample}
    flagged = [by_time[t] for p, y, t in preds if p >= threshold and t in by_time]
    control, candidate = [], []
    missing_candles = 0
    for r in flagged:
        if m13_factory(r["f"]):
            continue
        cp = OUT / "candles1m" / f"{r['m']}.json"
        if not cp.exists():
            missing_candles += 1
            continue
        x = json.loads(cp.read_text(encoding="utf-8"))
        candles = x["c"]
        i = next((j for j, c in enumerate(candles) if c[0] >= x["t_mig"]), None)
        if i is None or i >= len(candles) - 1:
            continue
        result = m12r_return(candles, i)
        if result is None:
            continue
        rec = {"m": r["m"], "t": r["t"], "ret": result,
               "green": bool(candles[i][4] > candles[i][1])}
        control.append(rec)
        if rec["green"]:
            candidate.append(rec)
    print(f"M14 design-only: cutoff={cut:.0f}, uniform sample={len(train_sample)}, "
          f"OOF flags={len(flagged)}, candles missing={missing_candles}; threshold={threshold:.6f}")
    summarize("M13 control (same trades with OHLC M12r + TX costs)", control)
    summarize("M14 green first-minute candle", candidate)
    # Print raw rule counts by temporal half, based on the registered cutoff.
    for label, xs in (("control", control), ("M14", candidate)):
        halves = [sum((r["t"] < cut) == first for r in xs) for first in (True, False)]
        print(f"{label} trade counts before/after RD holdout cutoff: {halves[0]}/{halves[1]}")


if __name__ == "__main__":
    main()
