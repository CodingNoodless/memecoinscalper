"""Recorded ticks -> per-token arrays for research (lookahead-free analysis)."""

from __future__ import annotations

import bisect
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
