"""Recorded ticks -> per-token arrays for research (lookahead-free analysis)."""

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..scalper.curve import CurveState
from ..scalper.recorder import TICK_DIR, read_ticks

L = 1_000_000_000


def price(vs: int, vt: int) -> float:
    return (vs / L) / (vt / 1e6)


@dataclass
class Token:
    mint: str
    t0: float
    symbol: str = "?"
    name: str = ""
    creator: str = ""
    dev_tokens: float = 0.0
    mayhem: bool = False
    uri: str = ""
    meta: dict | None = None
    creator_sells: list = field(default_factory=list)
    t: list = field(default_factory=list)
    vs: list = field(default_factory=list)
    vt: list = field(default_factory=list)
    p: list = field(default_factory=list)

    def idx_at(self, x: float) -> int:
        return bisect.bisect_right(self.t, x) - 1

    def state(self, i: int) -> CurveState:
        return CurveState(self.vt[i], self.vs[i])


def load_tokens(paths: list[Path] | None = None) -> dict[str, Token]:
    paths = paths or sorted(TICK_DIR.glob("*.jsonl.gz"))
    toks: dict[str, Token] = {}
    for r in read_ticks(paths):
        kind, mint = r.get("k"), r.get("m")
        if kind == "create":
            tok = Token(mint, float(r["t"]), r.get("s") or "?", r.get("nm") or "", r.get("cr") or "",
                        float(r.get("dt") or 0.0), bool(r.get("mh")), r.get("uri") or "")
            if r.get("vs") and r.get("vt"):
                tok.t.append(float(r["t"])); tok.vs.append(int(r["vs"])); tok.vt.append(int(r["vt"]))
            toks[mint] = tok
        elif mint in toks:
            tok = toks[mint]
            if kind == "curve":
                tok.t.append(float(r["t"])); tok.vs.append(int(r["vs"])); tok.vt.append(int(r["vt"]))
            elif kind == "meta":
                tok.meta = {k[2:]: v for k, v in r.items() if k.startswith("x_")}
            elif kind in ("csell", "devsell"):
                tok.creator_sells.append(float(r["t"]))
    for tok in toks.values():
        tok.p = [price(a, b) for a, b in zip(tok.vs, tok.vt)]
    return toks


START_VS = 30_000_000_000            # lamports, a fresh pump.fun curve
START_VT = 1_073_000_000_000_000     # raw token units (6 decimals)


def load_backfill(path: Path | None = None) -> dict[str, dict]:
    """data/trades/backfill.jsonl -> mint -> {"t0", "trades": [rows in chain order]} for fully fetched tokens."""
    from .backfill_trades import OUT, VERSION, normalize
    path = path or OUT
    rows, done = {}, {}
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("k") == "trade":
                r = normalize(r)
                if r is not None:
                    rows.setdefault(r["m"], []).append(r)
            elif r.get("k") == "done" and r.get("v") == VERSION:
                done[r["m"]] = r
    out = {}
    for mint, d in done.items():
        trades = sorted(rows.get(mint, []), key=lambda r: (r.get("slot") or 0, r.get("i", 0)))
        out[mint] = {"t0": float(d["t0"]), "window": float(d.get("window") or 0), "trades": trades}
    return out


def token_from_trades(mint: str, t0: float, trades: list[dict], base: Token | None = None) -> Token:
    """Full price path by replaying exact curve deltas from the starting reserves."""
    tok = Token(mint, t0)
    if base is not None:
        tok.symbol, tok.name, tok.creator, tok.dev_tokens = base.symbol, base.name, base.creator, base.dev_tokens
        tok.mayhem, tok.uri, tok.meta, tok.creator_sells = base.mayhem, base.uri, base.meta, base.creator_sells
    vs, vt = START_VS, START_VT
    tok.t.append(t0); tok.vs.append(vs); tok.vt.append(vt)
    for r in trades:
        vs += int(r["dv"]); vt += int(r["dc"])
        if vs <= 0 or vt <= 0:
            break
        tok.t.append(max(float(r["t"]), tok.t[-1])); tok.vs.append(vs); tok.vt.append(vt)
    tok.p = [price(a, b) for a, b in zip(tok.vs, tok.vt)]
    return tok


def replay_error(recorded: Token, replayed: Token) -> float | None:
    """Median relative gap between recorded and replayed virtual SOL at recorded tick times (1 s block-time slack)."""
    gaps = []
    for x, vs in zip(recorded.t[1:], recorded.vs[1:]):
        cands = [replayed.vs[i] for i in (replayed.idx_at(x - 1), replayed.idx_at(x), replayed.idx_at(x + 1)) if i >= 0]
        if cands:
            gaps.append(min(abs(c - vs) for c in cands) / vs)
    if not gaps:
        return None
    gaps.sort()
    return gaps[len(gaps) // 2]
