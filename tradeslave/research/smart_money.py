"""Online wallet labels ("smart money", GMGN-style) built only from results known BEFORE a given time.

Every launch we hold exact trades for (research.traders set: first 15 min; research.full_trades: first
30 min) closes its book at t0 + window. Each wallet's position on it (SOL out - SOL in + leftover tokens
dumped into the final curve state) becomes known then. WalletBook.stats(w, T) returns the wallet's
record over books closed before T: (positions, ROI, profitable share). No look-ahead.
Also: creator history (earlier creates by the same creator, from the recorder's create log).
"""

from __future__ import annotations

import gzip
import json
import zlib
from bisect import bisect_left
from collections import defaultdict

import numpy as np

from ..scalper.recorder import TICK_DIR
from .full_trades import OUT as FT_OUT
from .traders import FEE, VS0, VT0, load, positions


def _full_positions(tr: list) -> dict[str, tuple[float, float]]:
    """wallet -> (sol_in, pnl) on a full_trades launch (curve trades only, creation-slot wallets excluded)."""
    s0 = tr[0][0]
    grp = {x[3] for x in tr if x[0] == s0}
    vt = VT0
    pos = defaultdict(lambda: [0.0, 0.0, 0.0])
    for slot, _, t, w, b, sol, tok, amm in tr:
        if amm:
            break
        vt += -tok * 1e6 if b else tok * 1e6
        vt = max(vt, 1e12)
        if w in grp:
            continue
        p = pos[w]
        if b:
            p[0] += sol
            p[2] += tok * 1e6
        else:
            p[1] += sol
            p[2] -= tok * 1e6
    vs = VS0 * VT0 / vt
    out = {}
    for w, (i, o, left) in pos.items():
        left = max(left, 0.0)
        mark = vs * left / (vt + left) / 1e9 * (1 - FEE) if left > 0 else 0.0
        if i > 0:
            out[w] = (i, o + mark - i)
    return out


class WalletBook:
    def __init__(self):
        ev = []                                       # (t_done, wallet, sol_in, pnl)
        seen = set()
        for p in FT_OUT.glob("*.json.gz"):
            d = json.load(gzip.open(p, "rt", encoding="utf-8"))
            if not d["trades"]:
                continue
            seen.add(d["m"])
            done = d["t0"] + d.get("window_s", 1800)
            for w, (i, pnl) in _full_positions(d["trades"]).items():
                ev.append((done, w, i, pnl))
        for m, ts in load().items():
            if m in seen:
                continue
            done = ts[0]["t"] + 900
            for w, p in positions(ts).items():
                if p["in"] > 0:
                    ev.append((done, w, p["in"], p["pnl"]))
        ev.sort()
        per = defaultdict(list)
        for t, w, i, pnl in ev:
            per[w].append((t, i, pnl))
        self.t, self.c = {}, {}
        for w, xs in per.items():
            a = np.array(xs)
            self.t[w] = a[:, 0]
            self.c[w] = np.column_stack([np.arange(1, len(a) + 1), np.cumsum(a[:, 1]), np.cumsum(a[:, 2]),
                                         np.cumsum(a[:, 2] > 0)])

    def stats(self, w: str, T: float) -> tuple[int, float, float]:
        ts = self.t.get(w)
        if ts is None:
            return 0, 0.0, 0.0
        k = bisect_left(ts, T)
        if k == 0:
            return 0, 0.0, 0.0
        n, i, pnl, wins = self.c[w][k - 1]
        return int(n), pnl / i if i else 0.0, wins / n


class CreatorLog:
    """Creator -> sorted create times, and mint -> (creator, initial buy SOL, symbol)."""

    def __init__(self):
        self.times = defaultdict(list)
        self.info = {}
        for p in sorted(TICK_DIR.glob("*.jsonl.gz")):
            try:
                with gzip.open(p, "rt", encoding="utf-8") as fh:
                    for line in fh:
                        if '"create"' not in line:
                            continue
                        try:
                            r = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if r.get("k") != "create":
                            continue
                        self.times[r["cr"]].append(r["t"])
                        self.info[r["m"]] = (r["cr"], r.get("ib", 0.0), r.get("s", ""))
            except (OSError, EOFError, zlib.error):           # the recorder's last file can be cut short
                continue
        for v in self.times.values():
            v.sort()

    def prior(self, creator: str, t0: float) -> int:
        return bisect_left(self.times.get(creator, []), t0 - 1)
