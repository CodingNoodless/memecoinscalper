"""Training samples: replay each recorded day through the scalper's own state
machine, take a snapshot of the features at regular decision points, and label
each snapshot with what every exit policy would really have returned.

Labels use the exact chain price path when the token was backfilled
(research.backfill_trades) and the recorded curve otherwise. Finished days
are cached in data/autopilot/samples/.
"""

from __future__ import annotations

import pickle
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..paths import DATA_DIR
from ..research.dataset import load_backfill, load_tokens, token_from_trades
from ..research.sim import simulate
from ..scalper.config import ScalpConfig
from ..scalper.core import Scalper
from ..scalper.curve import CurveState
from ..scalper.recorder import TICK_DIR, read_ticks
from .features import FEATURES, features, tradeable

CACHE_DIR = DATA_DIR / "autopilot" / "samples"
CACHE_VERSION = 1

# Exit policies the model can choose between (net PnL incl. fees, fill delay).
POLICIES = {
    "tp5_sl6_60s": dict(tp=0.05, sl=0.06, hold=60.0),
    "tp8_sl4_30s": dict(tp=0.08, sl=0.04, hold=30.0),
    "tp10_sl10_60s": dict(tp=0.10, sl=0.10, hold=60.0),
    "tp20_sl15_120s": dict(tp=0.20, sl=0.15, hold=120.0),
    "trail20_10_sl15_300s": dict(tp=9.0, sl=0.15, hold=300.0, trail=(0.20, 0.10)),
    "hold30": dict(tp=9.0, sl=0.99, hold=30.0),
}


@dataclass
class Sample:
    mint: str
    t: float           # decision time
    t0: float          # token creation time (used to split train/test by token)
    x: list[float]
    y: dict[str, float]   # policy -> net return
    exact: bool        # label from exact chain data


class _Snapshotter(Scalper):
    """The scalper's state updates without trading; snapshots features instead."""

    def __init__(self, cfg: ScalpConfig, every_s: float, min_age: float, max_age: float):
        super().__init__(cfg)
        self.every_s, self.min_age, self.max_age = every_s, min_age, max_age
        self.last: dict[str, float] = {}
        self.snaps: list[tuple[str, float, float, list[float]]] = []

    def _decide(self, tr, now):          # never trade while building data
        return

    def on_curve(self, mint, t, state):
        super().on_curve(mint, t, state)
        tr = self.trackers.get(mint)
        if tr is None or not tradeable(tr, t, self.min_age, self.max_age):
            return
        if t - self.last.get(mint, -1e18) < self.every_s:
            return
        self.last[mint] = t
        self.snaps.append((mint, t, tr.created_at, features(tr, t)))


def snapshot_day(path: Path, cfg: ScalpConfig, every_s=2.0, min_age=3.0, max_age=300.0):
    snap = _Snapshotter(cfg, every_s, min_age, max_age)
    for rec in read_ticks([path]):
        kind, t = rec.get("k"), float(rec.get("t") or 0)
        if kind == "create":
            state = CurveState(int(rec["vt"]), int(rec["vs"])) if rec.get("vt") and rec.get("vs") else None
            snap.on_create(rec["m"], rec.get("s", "?"), t, state, rec.get("cr", ""), rec.get("bc", ""),
                           float(rec.get("ib") or 0.0), float(rec.get("dt") or 0.0),
                           str(rec.get("mh")) in ("1", "True", "true"))
        elif kind == "meta":
            snap.on_metadata(rec["m"], t, {k[2:]: v for k, v in rec.items() if k.startswith("x_")})
        elif kind in ("csell", "devsell"):
            snap.on_creator_sell(rec["m"], t)
        elif kind == "curve":
            snap.on_curve(rec["m"], t, CurveState(int(rec["vt"]), int(rec["vs"]), int(rec.get("rt") or 0),
                                                  int(rec.get("rs") or 0), 0, bool(int(rec.get("c") or 0))))
    return snap.snaps


def build_day(path: Path, cfg: ScalpConfig, backfill: dict | None = None) -> list[Sample]:
    snaps = snapshot_day(path, cfg)
    if not snaps:
        return []
    recorded = load_tokens([path])
    backfill = backfill if backfill is not None else load_backfill()
    exact = {}
    out = []
    for mint, t, t0, x in snaps:
        tok, is_exact = None, False
        if mint in backfill:
            if mint not in exact:
                exact[mint] = token_from_trades(mint, backfill[mint]["t0"], backfill[mint]["trades"])
            tok, is_exact = exact[mint], True
        elif mint in recorded:
            tok = recorded[mint]
        if tok is None:
            continue
        y = {}
        for name, pol in POLICIES.items():
            r = simulate(tok, t, size=cfg.trade_size_sol, latency=cfg.latency_s, fee_bps=cfg.fee_bps,
                         network_fee=cfg.network_fee_sol, **pol)
            if r is None:
                break
            y[name] = r
        if len(y) == len(POLICIES):
            out.append(Sample(mint, t, t0, x, y, is_exact))
    return out


def _day_of(path: Path) -> str:
    return path.name.split(".")[0]


def load_samples(cfg: ScalpConfig, days: int = 14, tick_dir: Path | None = None, cache_dir: Path | None = None,
                 log=print) -> list[Sample]:
    """Samples for the most recent `days` recorded days (finished days cached)."""
    tick_dir, cache_dir = tick_dir or TICK_DIR, cache_dir or CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    paths = sorted(tick_dir.glob("*.jsonl.gz"))[-days:]
    today = datetime.now(timezone.utc).date().isoformat()
    backfill = load_backfill()
    out: list[Sample] = []
    for path in paths:
        day = _day_of(path)
        cache = cache_dir / f"{day}.pkl"
        if day != today and cache.exists():
            try:
                blob = pickle.loads(cache.read_bytes())
                mints = blob.get("mints", set())
                if blob.get("v") == CACHE_VERSION and blob.get("features") == FEATURES \
                        and blob.get("policies") == list(POLICIES) and blob.get("mtime") == path.stat().st_mtime \
                        and blob.get("n_exact") == len(mints & backfill.keys()):     # new exact data -> rebuild
                    out.extend(blob["samples"])
                    continue
            except Exception:  # noqa: BLE001 - a bad cache is rebuilt
                pass
        started = time.time()
        samples = build_day(path, cfg, backfill)
        log(f"  samples {day}: {len(samples)} ({sum(s.exact for s in samples)} exact) in {time.time() - started:.0f}s")
        if day != today:
            mints = {s.mint for s in samples}
            cache.write_bytes(pickle.dumps({"v": CACHE_VERSION, "features": FEATURES, "policies": list(POLICIES),
                                            "mtime": path.stat().st_mtime, "samples": samples, "mints": mints,
                                            "n_exact": len(mints & backfill.keys())}))
        out.extend(samples)
    return out
