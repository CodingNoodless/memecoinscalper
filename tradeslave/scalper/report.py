"""What happens after a token returns to its opening price? (no lookahead)

    uv run python -m tradeslave.scalper.report            # all recorded days
    uv run python -m tradeslave.scalper.report --up 0.09 --down 0.10 --horizon 60

Replays the recording WITHOUT trading. At the first moment each token is in
the buy zone after its launch spike, it notes what the bot could know right
then (rug-filter verdict, dev bag, sniper share, selling pressure, age...),
then looks forward `horizon` seconds: did price rise `up` before it fell
`down`? Features are compared between bounces and failures so entry rules can
be judged on evidence. Only information available at the touch is used.
"""

from __future__ import annotations

import argparse
import statistics
from collections import Counter
from pathlib import Path

from .config import load_config
from .core import Scalper
from .curve import CurveState
from .recorder import TICK_DIR, read_ticks
from .strategy import safety_block

FEATURES = ("rise", "since_peak_s", "age_s", "volume_30s", "net_flow_10s", "updates_10s", "dev_pct", "sniper_pct")


def collect(records, cfg, up: float, down: float, horizon: float) -> list[dict]:
    scalper = Scalper(cfg)
    scalper._decide = lambda tr, now: None   # observe only
    touches: dict[str, dict] = {}
    for rec in records:
        t = float(rec["t"])
        kind = rec.get("k")
        if kind == "create":
            state = CurveState(int(rec["vt"]), int(rec["vs"])) if rec.get("vt") and rec.get("vs") else None
            scalper.on_create(rec["m"], rec.get("s", "?"), t, state, rec.get("cr", ""), rec.get("bc", ""),
                              float(rec.get("ib") or 0.0), float(rec.get("dt") or 0.0), bool(rec.get("mh")))
        elif kind in ("csell", "devsell"):
            scalper.on_creator_sell(rec["m"], t)
        elif kind == "curve":
            scalper.on_curve(rec["m"], t, CurveState(int(rec["vt"]), int(rec["vs"]), int(rec.get("rt") or 0),
                                                     int(rec.get("rs") or 0), 0, bool(rec.get("c"))))
            tr = scalper.trackers.get(rec["m"])
            if tr is None or rec["m"] in touches or tr.zone_touch_t is None or tr.age(t) < cfg.min_age_s:
                continue
            w10, w30 = tr.window(t, 10), tr.window(t, 30)
            touches[rec["m"]] = {
                "mint": tr.mint, "symbol": tr.symbol, "t": t, "price": tr.price,
                "verdict": safety_block(tr, t, cfg) or "clean",
                "rise": tr.peak_price / tr.launch_price - 1.0, "since_peak_s": t - tr.peak_t, "age_s": tr.age(t),
                "volume_30s": sum(abs(x.flow_sol) for x in w30), "net_flow_10s": sum(x.flow_sol for x in w10),
                "updates_10s": len(w10), "dev_pct": tr.dev_supply_pct,
                "sniper_pct": tr.early_supply_pct(cfg.early_window_s) or 0.0,
            }
    out = []
    for mint, row in touches.items():
        path = [x.price / row["price"] - 1.0 for x in scalper.trackers[mint].ticks
                if row["t"] < x.t <= row["t"] + horizon]
        hit_up = next((i for i, x in enumerate(path) if x >= up), None)
        hit_dn = next((i for i, x in enumerate(path) if x <= -down), None)
        row["outcome"] = ("bounce" if hit_up is not None and (hit_dn is None or hit_up < hit_dn)
                          else "fail" if hit_dn is not None else "neither")
        row["best"] = max(path, default=0.0)
        row["end"] = path[-1] if path else 0.0
        out.append(row)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*")
    parser.add_argument("--up", type=float, default=0.09, help="price rise counted as a bounce (0.09 ~ +5%% net)")
    parser.add_argument("--down", type=float, default=0.10, help="price fall counted as a failure")
    parser.add_argument("--horizon", type=float, default=60.0, help="seconds to look forward")
    args = parser.parse_args()
    paths = [Path(f) for f in args.files] or sorted(TICK_DIR.glob("*.jsonl.gz"))
    if not paths:
        raise SystemExit("No recorded ticks yet.")
    rows = collect(read_ticks(paths), load_config(), args.up, args.down, args.horizon)
    print(f"{len(rows)} tokens returned to their opening price after a spike "
          f"(bounce = +{args.up:.0%} before -{args.down:.0%} within {args.horizon:.0f}s)\n")
    for label, subset in (("all", rows), ("passed rug filters at that moment", [r for r in rows if r["verdict"] == "clean"])):
        c = Counter(r["outcome"] for r in subset)
        if not subset:
            continue
        print(f"{label}: n={len(subset)}  bounce {c['bounce']}  fail {c['fail']}  neither {c['neither']}  | "
              f"median best {statistics.median(r['best'] for r in subset):+.1%}  "
              f"median after {args.horizon:.0f}s {statistics.median(r['end'] for r in subset):+.1%}")
    print("\nverdict at touch:", dict(Counter(r["verdict"] for r in rows).most_common()))
    bounce = [r for r in rows if r["outcome"] == "bounce"]
    fail = [r for r in rows if r["outcome"] == "fail"]
    if bounce and fail:
        print(f"\nfeature medians      bounce (n={len(bounce)})   fail (n={len(fail)})")
        for f in FEATURES:
            print(f"  {f:14s} {statistics.median(r[f] for r in bounce):12.3f}   {statistics.median(r[f] for r in fail):12.3f}")
    print("\nSmall samples mislead: confirm any pattern on a later day before changing rules.")


if __name__ == "__main__":
    main()
