"""Research datasets: memecoin OHLCV + derived pool reserves, rugged/dead tokens included, split by creation time.

    uv run python -m tradeslave.research.research_data inventory   # coverage of the existing data/ sets (read-only)
    uv run python -m tradeslave.research.research_data build       # writes data/research/{train,validation,holdout}/

Universe: data/long_paths (RANDOM sample of every pump.fun create the recorder logged, mayhem excluded; no survivor
or activity bias) plus data/long_paths_busy (a busier sample, flagged sample="busy"); de-duplicated by mint, the
random copy wins. Candles are pump.fun's 1-minute candles in SOL from launch, covering the bonding curve and PumpSwap.

Reserves (SOL side) are DERIVED from each candle's close, since constant-product pools fix reserves by price:
  curve: virtual SOL V = sqrt(p * k_curve), k_curve = 30 SOL x 1.073e9 tokens (exact on the pump.fun curve)
  pool:  real SOL R = sqrt(p * k_pool), k_pool = 85 SOL x 206.9e6 tokens (*assumption*: no LP adds/removes after
         migration; accrued LP fees would make the true reserve a bit larger, so this errs toward more slippage)
Phase switches from curve to pool at the first candle whose high reaches the graduation price (~4.11e-7 SOL).

Labels per token (from the token's own recorded window): graduated (reached the graduation price), rugged (any
candle closing <= 30% of the previous close or of its own open), died (last close <= 20% of the max high, or no
candle in the last 24 h of the window for a token older than 24 h).

Split: by token creation time t0. Boundaries at the 60% / 80% quantiles; tokens created within 24 h AFTER a boundary
are dropped (the gap), and each split's candles are cut at the start of the next split, so no candle of an earlier
split overlaps a later one. Holdout = the most recent 20%. This module writes the holdout; nothing else may read it
except a pre-registered one-shot test (tools/gate_check.py --final).
Output: data/research/<split>/tokens.jsonl.gz, one token per line:
  {"m", "t0", "sample", "graduated", "rugged", "died", "c": [[t, o, h, l, c, vol_sol, sol_reserve, phase], ...]}
plus data/research/manifest.json.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import time
from pathlib import Path

from ..paths import DATA_DIR

OUT = DATA_DIR / "research"
SOURCES = (("long_paths", "random"), ("long_paths_busy", "busy"))
K_CURVE = 30.0 * 1.073e9
K_POOL = 85.0 * 206.9e6
P_GRAD = 115.005 / 279.9e6           # curve price when real tokens sold reach 793.1M (V = k / T)
GAP_S = 24 * 3600
SPLITS = (("train", 0.0, 0.6), ("validation", 0.6, 0.8), ("holdout", 0.8, 1.0))


def reserve(price: float, phase: str) -> float:
    return math.sqrt(max(price, 0.0) * (K_POOL if phase == "pool" else K_CURVE))


def load_token(path: Path, sample: str) -> dict | None:
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    c = [x for x in d.get("curve") or [] if len(x) >= 6 and x[4] and x[4] > 0]
    if not c:
        return None
    return {"m": d["m"], "t0": float(d["t0"]), "sample": sample, "c": c, "mtime": path.stat().st_mtime}


CREATES_LOG_FROM = 1792540800.0     # 2026-10-21 00:00 UTC: long_paths files from here on come from data/c1/creates.jsonl


def label(tok: dict) -> dict:
    if tok["sample"] == "random" and tok["t0"] >= CREATES_LOG_FROM:
        tok["sample"] = "createlog"     # launch_watch create log: misses many low-dev-buy launches (vault Issue 38)
    c, phase, out = tok["c"], "curve", []
    graduated, rugged = False, False
    prev = None
    for t, o, h, lo, cl, v in (x[:6] for x in c):
        if phase == "curve" and h >= P_GRAD * 0.99:
            phase, graduated = "pool", True
        if (prev and cl <= 0.3 * prev) or (o and cl <= 0.3 * o):
            rugged = True
        out.append([t, o, h, lo, cl, v, round(reserve(cl, phase), 4), phase])
        prev = cl
    max_h = max(x[2] for x in c)
    end = tok["mtime"]
    old = end - tok["t0"] > GAP_S
    died = c[-1][4] <= 0.2 * max_h or (old and c[-1][0] < end - GAP_S)
    return {"m": tok["m"], "t0": tok["t0"], "sample": tok["sample"], "graduated": graduated, "rugged": rugged,
            "died": died, "c": out}


def scan(dirname: str):
    d = DATA_DIR / dirname
    if not d.exists():
        return
    with os.scandir(d) as it:
        for e in it:
            if e.name.endswith(".json"):
                yield Path(e.path)


def shares(tokens) -> dict:
    n = len(tokens)
    if not n:
        return {"tokens": 0}
    t0 = [x["t0"] for x in tokens]
    f = lambda k: round(sum(x[k] for x in tokens) / n, 4)  # noqa: E731
    return {"tokens": n, "t0_min": time.strftime("%Y-%m-%d %H:%M", time.gmtime(min(t0))),
            "t0_max": time.strftime("%Y-%m-%d %H:%M", time.gmtime(max(t0))), "graduated_share": f("graduated"),
            "rugged_share": f("rugged"), "died_share": f("died"),
            "rugged_or_died_share": round(sum(x["rugged"] or x["died"] for x in tokens) / n, 4),
            "busy_share": round(sum(x["sample"] == "busy" for x in tokens) / n, 4),
            "candles": sum(len(x["c"]) for x in tokens)}


def build(gap_s: float = GAP_S) -> dict:
    seen, toks = set(), []
    for dirname, sample in SOURCES:
        for p in scan(dirname):
            if p.stem in seen:
                continue
            t = load_token(p, sample)
            if t:
                seen.add(t["m"])
                toks.append(label(t))
    toks.sort(key=lambda x: x["t0"])
    n = len(toks)
    b1, b2 = toks[int(n * 0.6)]["t0"], toks[int(n * 0.8)]["t0"]
    bounds = {"train": (-math.inf, b1, b1 + gap_s), "validation": (b1 + gap_s, b2, b2 + gap_s),
              "holdout": (b2 + gap_s, math.inf, math.inf)}
    manifest = {"built": time.strftime("%Y-%m-%d %H:%M"), "sources": [s for s, _ in SOURCES],
                "universe": "random sample of all pump.fun creates (+ busy sample), rugged/dead included",
                "reserves": "derived from close price (curve exact; pool assumes no LP changes)",
                "gap_hours": gap_s / 3600, "dropped_in_gaps": 0, "all": shares(toks), "splits": {}}
    for name, (lo, hi, cut) in bounds.items():
        part = [x for x in toks if lo <= x["t0"] < hi]
        for x in part:
            x["c"] = [k for k in x["c"] if k[0] < cut]
        part = [x for x in part if x["c"]]
        d = OUT / name
        d.mkdir(parents=True, exist_ok=True)
        f = d / "tokens.jsonl.gz"
        with gzip.open(f, "wt", encoding="utf-8") as fh:
            for x in part:
                fh.write(json.dumps(x, separators=(",", ":")) + "\n")
        info = shares(part)
        info.update(file=f.relative_to(DATA_DIR.parent).as_posix(), sha256=hashlib.sha256(f.read_bytes()).hexdigest(),
                    t0_from=None if lo == -math.inf else time.strftime("%Y-%m-%d %H:%M", time.gmtime(lo)),
                    t0_before=None if hi == math.inf else time.strftime("%Y-%m-%d %H:%M", time.gmtime(hi)))
        manifest["splits"][name] = info
    manifest["dropped_in_gaps"] = n - sum(s["tokens"] for s in manifest["splits"].values())
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def inventory() -> None:
    """Coverage of the existing sets (counts, date range); read-only."""
    for name in ("long_paths", "long_paths_busy", "candles", "m13_candles", "runners", "survivors", "c1", "full_trades"):
        d = DATA_DIR / name
        if not d.exists():
            print(f"{name:<16} missing")
            continue
        n, size = 0, 0
        for root, _, files in os.walk(d):
            n += len(files)
            size += sum(os.path.getsize(os.path.join(root, f)) for f in files)
        print(f"{name:<16} {n:>6} files {size / 1e6:>7.0f} MB")
    for f in ("trades/history.jsonl", "trades/backfill.jsonl"):
        p = DATA_DIR / f
        if p.exists():
            mints, lines = set(), 0
            with p.open(encoding="utf-8") as fh:
                for line in fh:
                    lines += 1
                    try:
                        mints.add(json.loads(line).get("mint") or json.loads(line).get("m"))
                    except ValueError:
                        pass
            print(f"{f:<22} {lines:>8} rows {len(mints):>6} mints {p.stat().st_size / 1e6:.0f} MB")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("inventory", "build"))
    ap.add_argument("--gap-hours", type=float, default=GAP_S / 3600,
                    help="gap after each split boundary (24 h asked; the Oct 13-17 data only fits a 6 h gap)")
    a = ap.parse_args()
    if a.cmd == "inventory":
        inventory()
    else:
        m = build(a.gap_hours * 3600)
        print(json.dumps({k: m[k] for k in ("all", "dropped_in_gaps")}, indent=1))
        for k, v in m["splits"].items():
            print(k, {x: v[x] for x in ("tokens", "t0_min", "t0_max", "graduated_share", "rugged_or_died_share",
                                        "busy_share")} if v.get("tokens") else v)


if __name__ == "__main__":
    main()
