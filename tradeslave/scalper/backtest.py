"""Replay recorded ticks through the scalper and report results.

    python -m tradeslave.scalper.backtest                      # all days in data/ticks
    python -m tradeslave.scalper.backtest data/ticks/2026-10-13.jsonl.gz --set take_profit_pct=0.04
    python -m tradeslave.scalper.backtest --grid take_profit_pct=0.03,0.05,0.08

Fills use the same pessimistic paper book as live trading (latency, fees,
failed transactions). Results are only as good as the recorded data: tokens
the live engine skipped (watchlist full) are not in it.
"""

from __future__ import annotations

import argparse
import itertools
import json
import time
from dataclasses import fields
from pathlib import Path

from ..paths import PROJECT_ROOT
from .config import ScalpConfig, load_config
from .core import Scalper
from .curve import CurveState
from .recorder import TICK_DIR, read_ticks


def replay(records, cfg: ScalpConfig) -> Scalper:
    scalper = Scalper(cfg)
    last_clock = None
    for rec in records:
        t = float(rec["t"])
        if last_clock is not None and t - last_clock > cfg.max_hold_s:
            # A gap in the recording (engine stopped). Live, the clock keeps
            # running, so let open positions reach their time stop before it.
            for step in range(int((cfg.max_hold_s + 5) / 0.2) + 1):
                scalper.on_clock(last_clock + step * 0.2)
            last_clock = t
            scalper.on_clock(t)
        if last_clock is None or t - last_clock >= 0.2:
            scalper.on_clock(t)
            last_clock = t
        if rec.get("k") == "create":
            state = None
            if rec.get("vt") and rec.get("vs"):
                state = CurveState(int(rec["vt"]), int(rec["vs"]))
            scalper.on_create(rec["m"], rec.get("s", "?"), t, state, rec.get("cr", ""), rec.get("bc", ""),
                              float(rec.get("ib") or 0.0), float(rec.get("dt") or 0.0), bool(rec.get("mh")))
        elif rec.get("k") == "meta":
            scalper.on_metadata(rec["m"], t, {k[2:]: v for k, v in rec.items() if k.startswith("x_")})
        elif rec.get("k") in ("csell", "devsell"):   # "devsell" = older recordings, same meaning
            scalper.on_creator_sell(rec["m"], t)
        elif rec.get("k") == "curve":
            state = CurveState(int(rec["vt"]), int(rec["vs"]), int(rec.get("rt") or 0), int(rec.get("rs") or 0),
                               0, bool(rec.get("c")))
            scalper.on_curve(rec["m"], t, state)
    if last_clock is not None:
        # Let open positions run to their time stop, then force-close at the last state.
        end = last_clock + cfg.max_hold_s + 5
        for step in range(int((end - last_clock) / 0.2) + 1):
            scalper.on_clock(last_clock + step * 0.2)
    return scalper


def summarize(scalper: Scalper) -> dict:
    closed = scalper.book.closed
    n = len(closed)
    pnls = [c["pnl_sol"] for c in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    by_reason: dict[str, dict] = {}
    for c in closed:
        row = by_reason.setdefault(c["exit_reason"], {"n": 0, "pnl_sol": 0.0})
        row["n"] += 1
        row["pnl_sol"] = round(row["pnl_sol"] + c["pnl_sol"], 6)
    safety: dict[str, int] = {}
    for tr in scalper.trackers.values():
        flag = ("dev_sold" if tr.dev_sold else "floor_broken" if tr.floor_broken else
                "serial_launcher" if tr.creator_launches > scalper.cfg.serial_launch_max else None)
        if flag:
            safety[flag] = safety.get(flag, 0) + 1
    by_kind: dict[str, dict] = {}
    for c in closed:
        row = by_kind.setdefault(c["kind"], {"n": 0, "pnl_sol": 0.0, "wins": 0})
        row["n"] += 1
        row["wins"] += c["pnl_sol"] > 0
        row["pnl_sol"] = round(row["pnl_sol"] + c["pnl_sol"], 6)
    return {
        "tokens": len(scalper.trackers),
        "trades": n,
        "win_rate": round(len(wins) / n, 4) if n else None,
        "avg_pnl_pct": round(sum(c["pnl_pct"] for c in closed) / n, 5) if n else None,
        "expectancy_sol": round(sum(pnls) / n, 6) if n else None,
        "total_pnl_sol": round(sum(pnls), 6),
        "profit_factor": round(sum(wins) / -sum(losses), 3) if losses and sum(losses) < 0 else None,
        "max_drawdown_sol": round(max_dd, 6),
        "avg_hold_s": round(sum(c["held_s"] for c in closed) / n, 1) if n else None,
        "fees_paid_sol": round(scalper.book.fees_paid_sol, 6),
        "failed_tx": scalper.book.failed_tx,
        "by_exit_reason": by_reason,
        "by_entry_kind": by_kind,
        "tokens_flagged": safety,
        "top_entry_blocks": dict(sorted(scalper.entry_block_reasons.items(), key=lambda kv: -kv[1])[:8]),
    }


def _parse_value(name: str, raw: str):
    kinds = {f.name: type(f.default) for f in fields(ScalpConfig)}
    if name not in kinds:
        raise SystemExit(f"Unknown setting {name}")
    kind = kinds[name]
    return raw.lower() in {"1", "true", "yes"} if kind is bool else kind(raw)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*", help="tick files (default: data/ticks/*.jsonl.gz)")
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--grid", action="append", default=[], metavar="NAME=V1,V2,...")
    args = parser.parse_args()
    paths = [Path(f) for f in args.files] or sorted(TICK_DIR.glob("*.jsonl.gz"))
    if not paths:
        raise SystemExit("No recorded ticks yet. Run `python scalp.py` first; it records to data/ticks/.")
    base = {}
    for item in args.set:
        name, _, raw = item.partition("=")
        base[name] = _parse_value(name, raw)
    grid_names, grid_values = [], []
    for item in args.grid:
        name, _, raw = item.partition("=")
        grid_names.append(name)
        grid_values.append([_parse_value(name, v) for v in raw.split(",")])
    records = list(read_ticks(paths))
    runs = []
    for combo in itertools.product(*grid_values) if grid_values else [()]:
        overrides = {**base, **dict(zip(grid_names, combo))}
        cfg = load_config(**overrides)
        result = summarize(replay(records, cfg))
        runs.append({"overrides": overrides, **result})
        print(json.dumps({"overrides": overrides, **{k: result[k] for k in (
            "tokens", "trades", "win_rate", "avg_pnl_pct", "expectancy_sol", "total_pnl_sol",
            "profit_factor", "max_drawdown_sol", "avg_hold_s")}}))
    if len(runs) == 1:
        print(json.dumps(runs[0], indent=2))
    out_dir = PROJECT_ROOT / "backtests"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"files": [str(p) for p in paths], "runs": runs}, indent=2), encoding="utf-8")
    print(f"Saved {out}")
    if len(runs) > 1:
        print("Note: picking the best of many settings on the same data overfits. Confirm on a later day.")


if __name__ == "__main__":
    main()
