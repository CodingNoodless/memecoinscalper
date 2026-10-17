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
import numpy as np

from ..research.dataset import SLOT_S, load_backfill, load_tokens, slot_ticks, token_from_ticks
from ..research.sim import simulate
from ..scalper.config import ScalpConfig
from ..scalper.core import Scalper
from ..scalper.curve import CurveState
from ..scalper.recorder import TICK_DIR, read_ticks
from .features import FEATURES, features, tradeable

CACHE_DIR = DATA_DIR / "autopilot" / "samples"
CACHE_VERSION = 6   # 4: wallet features (trade events); 5: market regime features; 6: own-impact sells

# Exit policies the model can choose between (net PnL incl. fees, fill delay).
POLICIES = {
    "tp5_sl6_60s": dict(tp=0.05, sl=0.06, hold=60.0),
    "tp8_sl4_30s": dict(tp=0.08, sl=0.04, hold=30.0),
    "tp10_sl10_60s": dict(tp=0.10, sl=0.10, hold=60.0),
    "tp20_sl15_120s": dict(tp=0.20, sl=0.15, hold=120.0),
    "trail20_10_sl15_300s": dict(tp=9.0, sl=0.15, hold=300.0, trail=(0.20, 0.10)),
    "hold30": dict(tp=9.0, sl=0.99, hold=30.0),
}


WALLET_FEED_DELAY_S = 1.0
SNAP_EVERY_S, SNAP_MIN_AGE_S, SNAP_MAX_AGE_S = 2.0, 3.0, 300.0   # when training snapshots (and live re-scores) happen
HORIZONS = (10, 30, 60, 120)   # seconds ahead the model learns to forecast (chart-reading skill)


@dataclass
class Sample:
    mint: str
    t: float           # decision time
    t0: float          # token creation time (used to split train/test by token)
    x: np.ndarray      # float32 features (FEATURES order)
    y: dict[str, float]   # policy -> net return
    exact: bool        # chart + label from exact chain data
    f: dict = None     # horizon (s) -> future price change (no fees): what "reading the chart" must predict
    clean: bool = False   # exact chart, or recorded while the feed was verifiably up (no holes)


class _Snapshotter(Scalper):
    """The scalper's state updates without trading; snapshots features instead."""

    def __init__(self, cfg: ScalpConfig, every_s: float, min_age: float, max_age: float):
        super().__init__(cfg)
        self.every_s, self.min_age, self.max_age = every_s, min_age, max_age
        self.last: dict[str, float] = {}
        self.snaps: list[tuple[str, float, float, list[float]]] = []
        self.tainted: set[str] = set()       # tokens alive while the feed was down: their history has holes
        self.markers = False                 # this recording logs feed up/down (newer engine)
        self.first_marker_t = float("inf")

    def feed_down(self, t: float) -> None:
        self.markers = True
        self.feed_ok = False
        self.tainted.update(m for m, tr in self.trackers.items() if t - tr.created_at <= self.max_age + 60)

    def feed_up(self, t: float) -> None:
        self.markers = True
        self.feed_ok = True

    def _decide(self, tr, now):          # never trade while building data
        return

    def on_curve(self, mint, t, state):
        super().on_curve(mint, t, state)
        tr = self.trackers.get(mint)
        exact = mint in getattr(self, "exact_mints", ())
        if tr is None or not tradeable(tr, t, self.min_age, self.max_age):
            return
        if not exact and (not self.feed_ok or mint in self.tainted):
            return
        if t - self.last.get(mint, -1e18) < self.every_s:
            return
        self.last[mint] = t
        clean = exact or (self.markers and self.feed_ok and tr.created_at >= self.first_marker_t)
        self.snaps.append((mint, t, tr.created_at, features(tr, t, self.heat(t), self.regime(t)), clean))


def _records_with_exact_charts(path: Path, exact: dict):
    """The day's records, with recorded curve updates replaced by exact chain
    charts for backfilled tokens (no gaps from feed outages), in time order."""
    records, created = [], {}
    for rec in read_ticks([path]):
        if rec.get("k") == "curve" and rec.get("m") in exact:
            continue
        if rec.get("k") == "create" and rec.get("m") in exact:
            created[rec["m"]] = float(rec["t"])
        records.append(rec)
    for mint, t_seen in created.items():
        trades = exact[mint]["trades"]
        for t, vs, vt in slot_ticks(trades, t_seen):
            records.append({"k": "curve", "t": t, "m": mint, "vs": vs, "vt": vt, "c": 0, "exact": 1})
        s0 = trades[0].get("slot") or 0 if trades else 0
        for r in trades:      # wallet trades, as the live trade-event feed would see them
            t = t_seen + ((r.get("slot") or s0) - s0) * SLOT_S
            if t < t_seen + WALLET_FEED_DELAY_S:
                continue      # live, the logs subscription starts ~1 s after the launch is seen
            records.append({"k": "trade", "t": t + 0.001, "m": mint, "w": r.get("w"), "b": int(int(r["dv"]) > 0),
                            "sol": abs(int(r["dv"])) / 1e9, "tok": abs(int(r["dc"])) / 1e6, "exact": 1})
    records.sort(key=lambda r: float(r.get("t") or 0))
    return records, created


def snapshot_day(path: Path, cfg: ScalpConfig, every_s=SNAP_EVERY_S, min_age=SNAP_MIN_AGE_S, max_age=SNAP_MAX_AGE_S,
                 exact: dict | None = None):
    snap = _Snapshotter(cfg, every_s, min_age, max_age)
    records, created = _records_with_exact_charts(path, exact or {})
    snap.exact_mints = set(created)
    for rec in records:
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
        elif kind == "migr":
            snap.on_migration(t)
        elif kind == "trade" and rec.get("w"):
            if rec.get("m") in snap.exact_mints and not rec.get("exact"):
                continue          # live-recorded duplicate of an exact token: keep one source
            snap.on_trade(rec["m"], t, rec["w"], bool(int(rec.get("b") or 0)), float(rec.get("sol") or 0),
                          float(rec.get("tok") or 0) * 1e6)
        elif kind == "feed":
            if not snap.markers:
                snap.first_marker_t = t
            (snap.feed_up if int(rec.get("up") or 0) else snap.feed_down)(t)
        elif kind == "curve":
            snap.on_curve(rec["m"], t, CurveState(int(rec["vt"]), int(rec["vs"]), int(rec.get("rt") or 0),
                                                  int(rec.get("rs") or 0), 0, bool(int(rec.get("c") or 0))))
    return [x[:4] + ((x[4] and x[0] not in snap.tainted) or x[0] in snap.exact_mints,)
            for x in snap.snaps if x[0] not in snap.tainted or x[0] in snap.exact_mints]


def _future(tok, t: float, latency: float) -> dict:
    i = tok.idx_at(t + latency)
    if i < 0 or tok.p[i] <= 0:
        return {}
    out = {}
    for h in HORIZONS:
        j = tok.idx_at(t + latency + h)
        out[h] = tok.p[j] / tok.p[i] - 1
    return out


def compact_path(tok) -> tuple:
    """(t, vs, vt) arrays of a token's price path: what the learned-exit simulator replays."""
    return (np.asarray(tok.t, dtype=np.float64), np.asarray(tok.vs, dtype=np.int64), np.asarray(tok.vt, dtype=np.int64))


def build_day(path: Path, cfg: ScalpConfig, backfill: dict | None = None, paths: dict | None = None) -> list[Sample]:
    """Samples of one recorded day. If `paths` is given, it is filled with mint -> compact_path
    for every token that has samples (the learned-exit simulator needs the whole price path)."""
    backfill = backfill if backfill is not None else load_backfill()
    snaps = snapshot_day(path, cfg, exact=backfill)
    if not snaps:
        return []
    recorded = load_tokens([path])
    t_seen = {m: t0 for m, _, t0, _, _ in snaps}
    exact_tok = {}
    out = []
    for mint, t, t0, x, clean in snaps:
        tok, is_exact = None, False
        if mint in backfill:
            if mint not in exact_tok:
                exact_tok[mint] = token_from_ticks(mint, t_seen[mint], slot_ticks(backfill[mint]["trades"], t_seen[mint]))
            tok, is_exact = exact_tok[mint], True
        elif mint in recorded:
            tok = recorded[mint]
        if tok is None:
            continue
        y = {}
        for name, pol in POLICIES.items():
            r = simulate(tok, t, size=cfg.trade_size_sol, latency=cfg.latency_s, fee_bps=cfg.fee_bps,
                         network_fee=cfg.network_fee_sol, own_impact=cfg.own_impact, **pol)
            if r is None:
                break
            y[name] = r
        if len(y) == len(POLICIES):
            out.append(Sample(mint, t, t0, np.asarray(x, dtype=np.float32), y, is_exact,
                              _future(tok, t, cfg.latency_s), clean or is_exact))
            if paths is not None and mint not in paths:
                paths[mint] = compact_path(tok)
    return out


def _econ(cfg: ScalpConfig) -> tuple:
    """Trade economics baked into the labels: a change means the cache is stale."""
    return (cfg.trade_size_sol, cfg.fee_bps, cfg.network_fee_sol, cfg.latency_s, cfg.own_impact)


def _day_of(path: Path) -> str:
    return path.name.split(".")[0]


def load_samples(cfg: ScalpConfig, days: int = 14, tick_dir: Path | None = None, cache_dir: Path | None = None,
                 log=print, price_paths: dict | None = None) -> list[Sample]:
    """Samples for the most recent `days` recorded days (finished days cached). `price_paths` (optional
    dict) is filled with each sampled token's price path, for the learned-exit simulator."""
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
                        and blob.get("econ") == _econ(cfg) \
                        and blob.get("policies") == list(POLICIES) and blob.get("mtime") == path.stat().st_mtime \
                        and blob.get("n_exact") == len(mints & backfill.keys()):     # new exact data -> rebuild
                    out.extend(blob["samples"])
                    if price_paths is not None:
                        price_paths.update(blob.get("paths") or {})
                    continue
            except Exception:  # noqa: BLE001 - a bad cache is rebuilt
                pass
        started = time.time()
        day_paths: dict = {}
        samples = build_day(path, cfg, backfill, day_paths)
        if price_paths is not None:
            price_paths.update(day_paths)
        log(f"  samples {day}: {len(samples)} ({sum(s.exact for s in samples)} exact) in {time.time() - started:.0f}s")
        if day != today:
            mints = {s.mint for s in samples}
            cache.write_bytes(pickle.dumps({"v": CACHE_VERSION, "features": FEATURES, "policies": list(POLICIES),
                                            "econ": _econ(cfg),
                                            "mtime": path.stat().st_mtime, "samples": samples, "mints": mints, "paths": day_paths,
                                            "n_exact": len(mints & backfill.keys())}))
        out.extend(samples)
    return out
