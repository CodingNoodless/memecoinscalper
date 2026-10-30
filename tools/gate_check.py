"""Backtest gates: score a trades file against lab/gates.yaml (locked) and print PASS/FAIL per gate.

    uv run python tools/gate_check.py lab/results/H1/trades.jsonl --id H1      # train/validation (default mode)
    uv run python tools/gate_check.py --final H1                               # the holdout, ONCE per registry id

Trades file: JSONL or CSV, one trade per row, `ret` = net return per trade AFTER tradeslave/cost_model.py costs.
Optional columns: `split` (train | validation | holdout), `gross`, `sol_reserve_in`, `sol_reserve_out`, `stop_fill`,
`size_sol` (for the capacity estimate). Default layout per registry id: lab/results/<id>/trades.jsonl (train +
validation), lab/results/<id>/shift_*.jsonl (the same strategy with each parameter shifted +/-20%, validation rows),
lab/results/<id>/holdout.jsonl (written only by the pre-registered one-shot run).

Default mode scores the validation rows (train rows are shown, never gated) and refuses any holdout rows.
--final scores the holdout: refuses if the id already used it (lab/holdout_log.jsonl), logs the use before scoring,
and prints ALL PASS only if every gate passes. Refuses to run if lab/gates.yaml has uncommitted edits.
These backtest gates are separate from the forward verdict rules in tools/lab.py.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path
from statistics import NormalDist

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
GATES = ROOT / "lab" / "gates.yaml"
HOLDOUT_LOG = ROOT / "lab" / "holdout_log.jsonl"
RESULTS = ROOT / "lab" / "results"
HYP_COUNT = Path("D:/vault/tradeslave/Project/Hypothesis count.md")
N01 = NormalDist()
EULER = 0.5772156649


# ---------- io ----------
def git(*args) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def gates_hash(path: Path = GATES) -> str:
    rel = path.relative_to(ROOT).as_posix()
    if git("status", "--porcelain", "--", rel):
        raise SystemExit(f"REFUSED: {rel} has uncommitted edits (or is untracked). Commit it first; gates are locked.")
    h = git("log", "-1", "--format=%h", "--", rel)
    if not h:
        raise SystemExit(f"REFUSED: {rel} is not committed.")
    return h


def load_trades(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"no trades file: {path}")
    if path.suffix == ".csv":
        with path.open(newline="") as fh:
            rows = list(csv.DictReader(fh))
    else:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for r in rows:
        r["ret"] = float(r["ret"])
    return rows


def lab_module():
    spec = importlib.util.spec_from_file_location("lab", ROOT / "tools" / "lab.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def trials_for(reg_id: str | None, extra: int | None, hyp_count: Path = HYP_COUNT) -> tuple[int, str]:
    """Registry variants in the id's family + the historical tally for that family in Project/Hypothesis count.md
    (a table row `| <family> | <N> |`)."""
    if not reg_id:
        return max(1, extra or 1), f"--trials {extra or 1}"
    lab = lab_module()
    reg = lab.registry()
    if reg_id not in reg:
        raise SystemExit(f"REFUSED: {reg_id} is not in lab/registry.jsonl")
    fam = reg[reg_id].get("family", "")
    n_reg = lab.family_variants(reg, fam)
    n_hist = 0
    if hyp_count.exists():
        for line in hyp_count.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\|\s*`?([^|`]+?)`?\s*\|\s*(\d+)\s*\|", line)
            if m and m.group(1).strip() == fam:
                n_hist = int(m.group(2))
    n = n_reg + n_hist + (extra or 0)
    return max(1, n), f"family {fam}: registry {n_reg} + Hypothesis count {n_hist}" + (f" + extra {extra}" if extra else "")


# ---------- statistics ----------
def profit_factor(r) -> float:
    win = sum(x for x in r if x > 0)
    loss = -sum(x for x in r if x < 0)
    return math.inf if loss == 0 and win > 0 else (win / loss if loss else 0.0)


def bootstrap_lower(r, resamples: int, seed: int) -> float:
    a = np.asarray(r, dtype=float)
    rng = np.random.default_rng(seed)
    means = np.empty(resamples)
    step = max(1, 2_000_000 // max(len(a), 1))
    for i in range(0, resamples, step):
        k = min(step, resamples - i)
        means[i:i + k] = a[rng.integers(0, len(a), size=(k, len(a)))].mean(axis=1)
    return float(np.quantile(means, 0.025))


def max_drawdown(r, bet: float) -> float:
    eq, peak, dd = 1.0, 1.0, 0.0
    for x in r:
        eq *= 1 + bet * max(x, -1.0)
        peak = max(peak, eq)
        dd = max(dd, 1 - eq / peak)
    return dd


def without_top(r, share: float) -> list[float]:
    k = math.ceil(len(r) * share)
    return sorted(r)[:len(r) - k] if k else list(r)


def deflated_sharpe(r, trials: int) -> float:
    """Bailey & Lopez de Prado (2014) deflated Sharpe ratio probability, per-trade Sharpe. The variance of Sharpe
    across trials is unknown, so the null's 1/T variance is used (standard simplification)."""
    t = len(r)
    if t < 3:
        return 0.0
    mu, sd = statistics.mean(r), statistics.pstdev(r)
    if sd == 0:
        return 1.0 if mu > 0 else 0.0
    sr = mu / sd
    a = np.asarray(r)
    skew = float(((a - mu) ** 3).mean() / sd ** 3)
    kurt = float(((a - mu) ** 4).mean() / sd ** 4)
    sr0 = 0.0
    if trials > 1:
        sr0 = math.sqrt(1 / t) * ((1 - EULER) * N01.inv_cdf(1 - 1 / trials) + EULER * N01.inv_cdf(1 - 1 / (trials * math.e)))
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    if denom <= 0:
        return 0.0
    return N01.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(denom))


def capacity_of(rows) -> str:
    need = ("gross", "sol_reserve_in")
    if not rows or not all(k in rows[0] and rows[0][k] not in ("", None) for k in need):
        return "n/a (trades file needs gross + sol_reserve_in columns)"
    from tradeslave.cost_model import capacity
    g = [float(x["gross"]) for x in rows]
    ri = [float(x["sol_reserve_in"]) for x in rows]
    ro = [float(x["sol_reserve_out"]) for x in rows] if rows[0].get("sol_reserve_out") not in (None, "") else None
    st = [str(x.get("stop_fill", "")).lower() in ("1", "true") for x in rows]
    return f"{capacity(g, ri, ro, st):.2f} SOL"


# ---------- gates ----------
def evaluate(r: list[float], cfg: dict, trials: int, shifts: dict[str, list[float]] | None, final: bool) -> list[tuple]:
    """Rows of (gate, value, threshold, PASS/FAIL/PENDING)."""
    out = []
    n = len(r)
    ok = lambda c: "PASS" if c else "FAIL"  # noqa: E731
    out.append(("min_trades_oos", n, f">= {cfg['min_trades_oos']}", ok(n >= cfg["min_trades_oos"])))
    if n == 0:
        return out
    pf = profit_factor(r)
    out.append(("profit_factor_after_costs", f"{pf:.3f}", f"> {cfg['profit_factor_after_costs']}",
                ok(pf > cfg["profit_factor_after_costs"])))
    lo = bootstrap_lower(r, int(cfg["bootstrap_resamples"]), int(cfg["bootstrap_seed"]))
    out.append(("expectancy_ci95_lower", f"{lo:+.4f}", f"> {cfg['expectancy_ci95_lower']}",
                ok(lo > cfg["expectancy_ci95_lower"])))
    dd = max_drawdown(r, cfg["drawdown_bet_fraction"])
    out.append(("max_drawdown", f"{dd:.1%}", f"< {cfg['max_drawdown']:.0%} @ {cfg['drawdown_bet_fraction']:.0%}/trade",
                ok(dd < cfg["max_drawdown"])))
    if shifts:
        worst = min(shifts, key=lambda k: profit_factor(shifts[k]))
        wpf = profit_factor(shifts[worst])
        out.append(("param_stability", f"min PF {wpf:.3f} ({worst}, {len(shifts)} shifts)",
                    f"> {cfg['param_stability_pf']}", ok(wpf > cfg["param_stability_pf"])))
    else:
        out.append(("param_stability", "no shift files", f"> {cfg['param_stability_pf']}", "FAIL"))
    trimmed = without_top(r, cfg["outlier_top_share"])
    tm = statistics.mean(trimmed) if trimmed else 0.0
    out.append(("outlier_test", f"{tm:+.4f} (n {len(trimmed)})", f"mean > 0 w/o top {cfg['outlier_top_share']:.0%}", ok(tm > 0)))
    dsr = deflated_sharpe(r, trials)
    out.append(("deflated_sharpe_prob", f"{dsr:.3f} (trials {trials})", f"> {cfg['deflated_sharpe_prob']}",
                ok(dsr > cfg["deflated_sharpe_prob"])))
    if final:
        out.append(("holdout_profit_factor", f"{pf:.3f}", f"> {cfg['holdout_profit_factor']}",
                    ok(pf > cfg["holdout_profit_factor"])))
    else:
        out.append(("holdout_profit_factor", "-", f"> {cfg['holdout_profit_factor']}", "PENDING (--final only)"))
    return out


def print_table(rows, mean, cfg, ghash, capacity, header):
    print(header)
    print(f"{'gate':<27} {'value':<34} {'threshold':<28} result")
    for g, v, th, res in rows:
        print(f"{g:<27} {str(v):<34} {th:<28} {res}")
    print(f"{'capacity':<27} {capacity:<34} {'report only':<28} -")
    bm = cfg.get("benchmark_net_per_trade", 0.05)
    print(f"avg net return per trade {mean:+.2%} vs benchmark {bm:+.0%} ({'above' if mean >= bm else 'below'})")
    print(f"lab/gates.yaml @ {ghash}")


def holdout_used(reg_id: str, log: Path | None = None) -> bool:
    log = log or HOLDOUT_LOG
    if not log.exists():
        return False
    return any(json.loads(line).get("id") == reg_id for line in log.read_text(encoding="utf-8").splitlines() if line.strip())


def load_shifts(d: Path, split: str | None) -> dict[str, list[float]]:
    out = {}
    for p in sorted(d.glob("shift_*.jsonl")) + sorted(d.glob("shift_*.csv")):
        rows = load_trades(p)
        if split and "split" in rows[0]:
            rows = [x for x in rows if x.get("split") == split]
        out[p.stem] = [x["ret"] for x in rows]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("trades", nargs="?", type=Path)
    ap.add_argument("--id", help="registry id (for trials and default paths)")
    ap.add_argument("--final", metavar="ID", help="score the holdout once for this registry id")
    ap.add_argument("--shift", nargs="*", type=Path, default=None, help="parameter-shifted trades files")
    ap.add_argument("--trials-extra", type=int, default=None, help="extra trials not in the registry or tally")
    a = ap.parse_args(argv)

    cfg = yaml.safe_load(GATES.read_text(encoding="utf-8"))
    ghash = gates_hash()
    reg_id = a.final or a.id
    trials, trials_src = trials_for(reg_id, a.trials_extra)
    d = RESULTS / reg_id if reg_id else None

    if a.final:
        path = a.trades or d / "holdout.jsonl"
        if holdout_used(a.final):
            raise SystemExit(f"REFUSED: {a.final} already used its one holdout shot (lab/holdout_log.jsonl).")
        rows = load_trades(path)
        with HOLDOUT_LOG.open("a", encoding="utf-8") as fh:  # logged before scoring: a crash still spends the shot
            fh.write(json.dumps({"id": a.final, "t": time.strftime("%Y-%m-%d %H:%M"), "file": str(path),
                                 "n": len(rows), "gates": ghash}) + "\n")
        r = [x["ret"] for x in rows]
        shifts = {p.stem: [x["ret"] for x in load_trades(p)] for p in a.shift} if a.shift else load_shifts(d, "validation")
        res = evaluate(r, cfg, trials, shifts, final=True)
        print_table(res, statistics.mean(r) if r else 0.0, cfg, ghash, capacity_of(rows),
                    f"FINAL (holdout, one shot) {a.final}: {path} | trials: {trials_src}")
        all_pass = all(x[3] == "PASS" for x in res)
        print("ALL PASS" if all_pass else f"FAIL ({sum(x[3] != 'PASS' for x in res)} gate(s) failed)")
        return 0 if all_pass else 1

    path = a.trades or (d / "trades.jsonl" if d else None)
    if path is None:
        ap.error("give a trades file or --id")
    if "holdout" in path.as_posix().lower():
        raise SystemExit("REFUSED: default mode never reads holdout files; use --final <id> (once).")
    rows = load_trades(path)
    if any(x.get("split") == "holdout" for x in rows):
        raise SystemExit("REFUSED: trades file contains holdout rows; default mode scores train/validation only.")
    has_split = rows and "split" in rows[0]
    train = [x["ret"] for x in rows if x.get("split") == "train"] if has_split else []
    oos_rows = [x for x in rows if x.get("split") == "validation"] if has_split else rows
    r = [x["ret"] for x in oos_rows]
    if a.shift:
        shifts = {p.stem: [x["ret"] for x in load_trades(p) if not has_split or x.get("split", "validation") == "validation"]
                  for p in a.shift}
    else:
        shifts = load_shifts(d, "validation") if d and d.exists() else {}
    res = evaluate(r, cfg, trials, shifts, final=False)
    note = f"train n {len(train)} mean {statistics.mean(train):+.2%} (not gated)" if train else (
        "no split column: all rows treated as out-of-sample" if not has_split else "no train rows")
    print_table(res, statistics.mean(r) if r else 0.0, cfg, ghash, capacity_of(oos_rows),
                f"VALIDATION {reg_id or ''}: {path} | {note} | trials: {trials_src}")
    passed = all(x[3] == "PASS" for x in res if not x[3].startswith("PENDING"))
    print("validation gates PASS (holdout still pending)" if passed else
          f"FAIL ({sum(x[3] == 'FAIL' for x in res)} gate(s) failed)")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
