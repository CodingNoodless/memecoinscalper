"""Strategy H3: copy public KOL wallets (Kolscan leaderboard) on random launches, with realistic copy delays.

    uv run python -m tradeslave.research.kol_copy design|test <kols.txt>

For each (KOL, launch) in research.full_trades (every trade of the first 30 min): buy when the KOL first buys,
landing DELAY later (slots for a bot, seconds for a human / Telegram alert); exit (a) when the KOL first sells,
(b) the same plus a -40% stop on 5 s closes, (c) when the KOL has sold >= 90% of what it bought; each exit lands
with the same delay. Not exited inside the window: sold at the window-end state. Exact curve with our own bag,
0.5 SOL, 1.25%/side, 0.002 SOL/tx. Also reports the KOLs' own SOL result in the same launches.
Pre-registered in the vault note "2026-10-18 human playbook (H)", round 3.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

from ..paths import DATA_DIR
from . import discretion as D
from .playbook import CUT_T0

DELAYS = [("2 slots", "slot", 2), ("10 slots", "slot", 10), ("+10 s", "sec", 10), ("+30 s", "sec", 30)]
EXITS = ["first_sell", "first_sell_stop40", "sold90"]
FROZEN = DATA_DIR / "kol_copy_rule.json"


def state_after(tk: D.Tok, slot, t, kind, d):
    if kind == "slot":
        vs, _ = tk.vs_after_slot(slot + d)
    else:
        vs = tk.vs_at_time(t + d)
    return vs


def copy_trades(tk: D.Tok, kols: set):
    """Yield (kol, delay label, exit, net return) for every KOL's first buy in this launch."""
    cur = tk.cur
    end_t = tk.t0 + tk.window if tk.grad_t is None else tk.grad_t
    by = defaultdict(list)
    for k, z in enumerate(cur):
        if z[3] in kols:
            by[z[3]].append(k)
    for w, idx in by.items():
        buys = [k for k in idx if cur[k][4]]
        if not buys:
            continue
        b0 = buys[0]
        bought = 0.0
        first_sell = sold90 = None
        for k in idx:
            if k < b0:
                continue
            z = cur[k]
            if z[4]:
                bought += z[6]
            else:
                if first_sell is None:
                    first_sell = k
                sold = sum(cur[j][6] for j in idx if b0 <= j <= k and not cur[j][4])
                if sold90 is None and bought and sold >= 0.9 * bought:
                    sold90 = k
        for lbl, kind, d in DELAYS:
            vs_e = state_after(tk, cur[b0][0], cur[b0][2], kind, d)
            t_e = cur[b0][2] + (d * 0.4 if kind == "slot" else d)
            if t_e >= end_t:
                continue
            net = D.SIZE * (1 - D.FEE) * 1e9
            tokens = (D.K / vs_e) * net / (vs_e + net) / 1e6
            p_e = D.price(vs_e + net)
            for ex in EXITS:
                k_ex = first_sell if ex.startswith("first_sell") else sold90
                t_x = None
                if k_ex is not None:
                    t_x = cur[k_ex][2]
                    vs_x = state_after(tk, cur[k_ex][0], t_x, kind, d)
                if ex == "first_sell_stop40":
                    # 5 s closes between entry and the KOL's sell; stop at -40% of our entry (price incl. our bag)
                    T = t_e + 5
                    stop_hit = None
                    while T < (t_x if t_x is not None else end_t):
                        vs_T = tk.vs_at_time(T)
                        if D.price(vs_T + net) <= p_e * 0.6:
                            stop_hit = T
                            break
                        T += 5
                    if stop_hit is not None:
                        t_x, vs_x = stop_hit + 1, tk.vs_at_time(stop_hit + 1)
                if t_x is None or t_x >= end_t:
                    vs_x = tk.vs_at_time(end_t - 1e-3)
                px = D.price(vs_x)
                got = D.sell_value(tokens, px, net, False)
                yield w, lbl, ex, (got - D.SIZE - 2 * D.PRIO) / D.SIZE
        # the KOL's own SOL result in this launch's window
        spent = sum(cur[k][5] for k in idx if cur[k][4])
        recv = sum(cur[k][5] for k in idx if not cur[k][4])
        yield w, "KOL itself", "own", (recv - spent) / spent if spent else 0.0


def run(which: str, kols: set):
    rows = defaultdict(list)
    for f in sorted(D.FT_OUT.glob("*.json.gz")):
        try:
            x = D.load_token(f)
        except Exception:
            continue
        if x is None or (x[1] < CUT_T0) != (which == "design"):
            continue
        tk = D.Tok(*x)
        for w, lbl, ex, r in copy_trades(tk, kols):
            rows[(lbl, ex)].append((tk.m, w, r))
    return rows


def summary(rs, label):
    xs = [r[2] for r in rs]
    if len(xs) < 5:
        return f"{label}: n={len(xs)}"
    by = defaultdict(float)
    for m, _, x in rs:
        by[m] += x
    v = list(by.values())
    sd = statistics.pstdev(v)
    tcl = statistics.mean(v) / (sd / math.sqrt(len(v))) if sd else 0.0
    trim = sorted(xs)[:-max(1, len(xs) // 50)]
    return (f"{label}: n={len(xs)} launches {len(v)} kols {len({r[1] for r in rs})} mean {statistics.mean(xs):+.2%} "
            f"med {statistics.median(xs):+.2%} win {np.mean(np.array(xs) > 0):.0%} launch-t {tcl:+.2f} "
            f"| w/o top 2% {statistics.mean(trim):+.2%}")


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "design"
    kols = set(Path(sys.argv[2]).read_text().split())
    rows = run(which, kols)
    if which == "design":
        best = None
        for key in sorted(rows):
            print("  ", summary(rows[key], f"{key[0]:10s} {key[1]}"))
            if key[0] in ("2 slots", "10 slots") and len(rows[key]) >= 30:
                m = statistics.mean(r[2] for r in rows[key])
                if best is None or m > best[0]:
                    best = (m, key)
        if best:
            FROZEN.write_text(json.dumps({"delay": best[1][0], "exit": best[1][1]}))
            print("frozen", best[1])
    else:
        fr = json.loads(FROZEN.read_text())
        print("TEST", summary(rows[(fr["delay"], fr["exit"])], f"{fr['delay']} {fr['exit']}"))
        print("     ", summary(rows[("KOL itself", "own")], "KOLs' own result"))


if __name__ == "__main__":
    main()
