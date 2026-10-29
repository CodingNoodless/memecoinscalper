"""M16: test the same migration model against actual net M12r trade outcomes (design only)."""

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


def replay(c: list[list[float]], k: int) -> float | None:
    """M13's 0.80x/M12r/six-hour exit; None means the candle path is censored."""
    e = c[k][4]
    if not e:
        return None
    stop, armed, end_t = 0.8 * e, False, c[k][0] + 6 * 3600
    cost_mult = (1 - FEE_SIDE) / (1 + FEE_SIDE)
    last_t, last = c[k][0], e
    for t, o, h, lo, cl, _ in c[k + 1 :]:
        if t > end_t:
            break
        last_t, last = t, cl
        # Match the existing conservative stop-before-high OHLC ordering.
        if lo <= stop:
            return min(o, stop) / e * cost_mult - 1 - TX_FRACTION
        if h >= 2 * e:
            armed = True
        if armed:
            stop = max(stop, 1.25 * e, (math.floor(h / e) - 1) * e if h >= 3 * e else 0)
    if last_t >= end_t - 60:
        return last / e * cost_mult - 1 - TX_FRACTION
    return None


def summarize(label: str, xs: list[tuple[int, float]]) -> None:
    xs = sorted(xs)
    rets = [v for _, v in xs]
    if not rets:
        print(f"{label}: n=0")
        return
    n, half = len(rets), len(rets) // 2
    trimmed = sorted(rets)[:-3] if n > 3 else []
    h1 = statistics.mean(rets[:half]) if half else float("nan")
    h2 = statistics.mean(rets[half:]) if n - half else float("nan")
    ex3 = statistics.mean(trimmed) if trimmed else float("nan")
    print(f"{label}: n={n}, mean={statistics.mean(rets):+.2%}, median={statistics.median(rets):+.2%}, "
          f"win={sum(r > 0 for r in rets) / n:.1%}, halves={h1:+.2%}/{h2:+.2%}, "
          f"ex-top3={ex3:+.2%}, <=-25%={sum(r <= -0.25 for r in rets) / n:.1%}")


def main() -> None:
    source = json.loads((OUT / "features.json").read_text(encoding="utf-8"))
    design, _, cut = _split(source)
    sample = [r for r in design if r["sample"] and not factory(r["f"])]
    candles: dict[int, tuple[dict, float]] = {}
    eligible, censored_or_missing = [], 0
    for r in sample:
        p = OUT / "candles1m" / f"{r['m']}.json"
        if not p.exists():
            censored_or_missing += 1
            continue
        x = json.loads(p.read_text(encoding="utf-8"))
        c = x["c"]
        k = next((i for i, row in enumerate(c) if row[0] >= x["t_mig"]), None)
        if k is None or k >= len(c) - 1:
            censored_or_missing += 1
            continue
        yret = replay(c, k)
        if yret is None:
            censored_or_missing += 1
            continue
        eligible.append(r)
        candles[r["t"]] = (x, yret)

    if not eligible:
        raise SystemExit("No complete design candle outcomes; M16 cannot be evaluated.")
    names = _names(source, False)

    # Candidate: expanding-window predictions on the direct, after-cost trade-profit label.
    direct_rows = [dict(r, y=int(candles[r["t"]][1] > 0)) for r in eligible]
    direct_pred = _cv(direct_rows, "logit", names)
    dscore = {t: p for p, _, t in direct_pred}
    dps = sorted(dscore.values(), reverse=True)
    dthr = dps[max(0, int(0.05 * len(dps)) - 1)]

    # Control: existing ATH label/model and its frozen design threshold, restricted to identical rows.
    old_pred = _cv(design, "logit", names)
    old_score = {t: p for p, _, t in old_pred}
    old_thr = json.loads((OUT / "design_choice.json").read_text(encoding="utf-8"))["thr"]
    by_t = {r["t"]: r for r in eligible}
    direct_flags = [t for t, p in dscore.items() if p >= dthr and t in by_t]
    old_flags = [t for t, p in old_score.items() if p >= old_thr and t in by_t]
    candidate_trades = [(t, candles[t][1]) for t in direct_flags]
    control_trades = [(t, candles[t][1]) for t in old_flags]
    print(f"M16 design-only: split={cut:.0f}, nonfactory uniform design={len(sample)}, "
          f"complete tradable outcomes={len(eligible)}, missing/censored={censored_or_missing}")
    print(f"direct label profitable share={sum(r['y'] for r in direct_rows)}/{len(direct_rows)}; "
          f"OOF predictions={len(dscore)}, top5 threshold={dthr:.6f}; frozen M13 threshold={old_thr:.6f}")
    summarize("M13 ATH-label control, restricted to identical rows", control_trades)
    summarize("M16 direct net-profit target, top 5% OOF", candidate_trades)


if __name__ == "__main__":
    main()
