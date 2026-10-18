"""Strategy H: the human memecoin playbook from trading guides, mechanised, on 1-minute candles of every launch.

    uv run python -m tradeslave.research.playbook design    # 3 entries x 3 exits on design launches, freeze the best
    uv run python -m tradeslave.research.playbook test      # judge the frozen cell once on test launches

Data: research.long_paths (pump.fun 1-minute candles from creation, curve + PumpSwap, every logged launch; minutes
without trades have no candle). Market cap in SOL = price x 1e9; "KOTH / $30k" = 180 SOL (assumption, SOL ~ $166).
Entry at the next minute's open after the signal close. Take-profits fill at target -1% when a candle's high reaches
them; stops at min(stop, open) -1% when the low does (stop first if both); trailing / structure / time exits at the
next open. Exact curve math with our own bag (1.25%/side), PumpSwap tier fee + pool impact after graduation,
0.5 SOL, 0.002 SOL per transaction (each partial sell is one). One trade per coin.
Pre-registered in the vault note "2026-10-18 human playbook (H)".
"""

from __future__ import annotations

import json
import math
import statistics
import sys

import numpy as np

from ..paths import DATA_DIR
from . import discretion as D
from .long_paths import OUT as LP_OUT
from .shock import K_POOL, fee as pool_fee

SIZE, PRIO, SLIP = 0.5, 0.002, 0.01
KOTH = 180.0
FROZEN = DATA_DIR / "playbook_rule.json"
CUT_T0 = 1792085650.755          # 60th percentile of all 70,256 logged launches (2026-10-15 17:34 UTC), fixed


def load(path):
    d = json.loads(path.read_text())
    c = np.array([x for x in d.get("curve") or [] if x[1] > 0 and x[4] > 0], float)
    if len(c) < 3:
        return None
    return d["m"], float(d["t0"]), c


# ---------------------------------------------------------------- costs

def buy(p: float, graduated: bool):
    """(tokens, our SOL left in the curve in lamports) for SIZE SOL at observed price p."""
    if not graduated:
        vs = D.vs_of(p)
        net = SIZE * (1 - D.FEE) * 1e9
        return (D.K / vs) * net / (vs + net) / 1e6, net
    s = math.sqrt(K_POOL * p)
    net = SIZE * (1 - pool_fee(p * 1e9))
    return (s / p) * net / (s + net), 0.0


def sell(tokens: float, p: float, net: float, graduated: bool, frac_left: float = 1.0) -> float:
    """SOL for `tokens` at observed price p. Our earlier buy is still in the market (curve: net lamports in vs;
    pool: SIZE x fraction still held, in the SOL reserve), so a round trip at an unchanged price costs only fees."""
    if not graduated:
        return D.sell_value(tokens, p, net, False)
    s = math.sqrt(K_POOL * p) + SIZE * frac_left
    pool_tok = K_POOL / s
    return s * tokens / (pool_tok + tokens) * (1 - pool_fee(p * 1e9))


# ---------------------------------------------------------------- entries

def entry(kind: str, t0, c):
    """Index of the signal candle (decision at its close), or None."""
    t, o, h, lo, cl, v = c.T
    if h.max() * 1e9 < KOTH:
        return None
    mc = cl * 1e9
    grad = np.maximum.accumulate(h >= D.P_GRAD * 0.999)
    ath = np.maximum.accumulate(h)
    vol15 = np.array([v[np.searchsorted(t, t[i] - 900, side="right"):i + 1].sum() for i in range(len(t))])
    p0 = D.price(D.VS0)
    gi = int(np.argmax(grad)) if grad.any() else None
    for i in range(2, len(t) - 1):
        age = t[i] + 60 - t0
        green_hl = cl[i] > o[i] and lo[i] > lo[i - 1]
        if kind == "E1":
            if grad[i] or age < 600 or ath[i] * 1e9 < KOTH or vol15[i] < 5:
                continue
            lo_r, hi_r = ath[i] - 0.618 * (ath[i] - p0), ath[i] - 0.382 * (ath[i] - p0)
            if lo_r <= cl[i] <= hi_r and green_hl:
                return i
        elif kind == "E2":
            if grad[i] or age < 300:
                continue
            if mc[i] >= KOTH:
                return i if vol15[i] >= 10 and (mc[:i] < KOTH).all() else None
        elif kind == "E3":
            if gi is None or i <= gi or t[i] - t[gi] < 600:
                continue
            if cl[i] <= 0.7 * h[gi:i + 1].max() and green_hl and vol15[i] >= 20:
                return i
    return None


# ---------------------------------------------------------------- exits

EXITS = {
    "X1": dict(stop=0.40, ladder=[(2, .25), (3, .25), (5, .25), (10, .25)], trail=None, structure=False, hold=6 * 3600),
    "X2": dict(stop=0.40, ladder=[(2, .50)], trail=0.30, structure=False, hold=12 * 3600),
    "X3": dict(stop=0.30, ladder=[], trail=None, structure=True, hold=6 * 3600),
}


def trade(c, i, rule):
    """Net return of entering after signal candle i under exit rule; None if no next candle."""
    t, o, h, lo, cl, v = c.T
    if i + 1 >= len(t):
        return None
    e = i + 1
    p_in = o[e] if t[e] - t[i] <= 60 else cl[i]
    grad = np.maximum.accumulate(h >= D.P_GRAD * 0.999)
    g_in = bool(grad[i])
    tokens, net = buy(p_in, g_in)
    left, got, n_tx = 1.0, 0.0, 1
    done_levels = set()
    peak = p_in
    stop = p_in * (1 - rule["stop"])
    t_end = t[e] + rule["hold"]

    def sell_frac(frac, px, k):
        nonlocal left, got, n_tx
        got += sell(tokens * frac, px, net * left, bool(grad[k]), left)
        left -= frac
        n_tx += 1

    k = e
    while k < len(t) and t[k] <= t_end and left > 1e-9:
        if lo[k] <= stop:
            sell_frac(left, min(stop, o[k]) * (1 - SLIP), k)
            break
        for mult, frac in rule["ladder"]:
            if mult not in done_levels and h[k] >= p_in * mult:
                done_levels.add(mult)
                sell_frac(min(frac, left), p_in * mult * (1 - SLIP), k)
        if left <= 1e-9:
            break
        peak = max(peak, cl[k])
        nxt = o[k + 1] if k + 1 < len(t) else cl[k]
        if rule["trail"] and done_levels and cl[k] <= peak * (1 - rule["trail"]):
            sell_frac(left, nxt, min(k + 1, len(t) - 1))
            break
        if rule["structure"] and peak >= p_in * 1.2 and k >= e + 5 and cl[k] < lo[k - 5:k].min():
            sell_frac(left, nxt, min(k + 1, len(t) - 1))
            break
        k += 1
    if left > 1e-9:
        kk = min(k, len(t)) - 1
        sell_frac(left, cl[kk], kk)
    return (got - SIZE - PRIO * n_tx) / SIZE


# ---------------------------------------------------------------- run

def run_all(coins):
    res = {}
    for kind in ("E1", "E2", "E3"):
        sig = [(m, t0, c, entry(kind, t0, c)) for m, t0, c in coins]
        sig = [s for s in sig if s[3] is not None]
        for xn, rule in EXITS.items():
            rows = [(m, t0, r) for m, t0, c, i in sig if (r := trade(c, i, rule)) is not None]
            res[(kind, xn)] = rows
    return res


def summary(rows, label):
    xs = [r[2] for r in sorted(rows, key=lambda r: r[1])]
    if len(xs) < 5:
        return f"{label}: n={len(xs)}"
    m, sd = statistics.mean(xs), statistics.pstdev(xs)
    trim = sorted(xs)[:-max(1, len(xs) // 50)]
    h = len(xs) // 2
    return (f"{label}: n={len(xs)} mean {m:+.2%} med {statistics.median(xs):+.2%} win {np.mean(np.array(xs) > 0):.0%} "
            f"t {m / (sd / math.sqrt(len(xs))) if sd else 0:+.2f} | w/o top 2% {statistics.mean(trim):+.2%} | "
            f"halves {statistics.mean(xs[:h]):+.2%} / {statistics.mean(xs[h:]):+.2%} | sum {sum(xs) * SIZE:+.1f} SOL")


def coins_split():
    coins = [x for p in sorted(LP_OUT.glob("*.json")) if (x := load(p)) is not None]
    return [x for x in coins if x[1] < CUT_T0], [x for x in coins if x[1] >= CUT_T0], CUT_T0


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "design"
    design, test, cut = coins_split()
    print(f"{len(design)} design launches, {len(test)} test launches (cut t0 {cut:.0f})", flush=True)
    if cmd == "design":
        res = run_all(design)
        best = None
        for key, rows in res.items():
            print("  ", summary(rows, f"{key[0]}+{key[1]}"), flush=True)
            if len(rows) >= 50:
                m = statistics.mean(r[2] for r in rows)
                if best is None or m > best[0]:
                    best = (m, key)
        if best:
            FROZEN.write_text(json.dumps({"entry": best[1][0], "exit": best[1][1], "cut": cut}))
            print("frozen", best[1])
    else:
        fr = json.loads(FROZEN.read_text())
        test = [x for x in test if x[1] >= fr["cut"]]
        rows = []
        for m, t0, c in test:
            i = entry(fr["entry"], t0, c)
            if i is not None and (r := trade(c, i, EXITS[fr["exit"]])) is not None:
                rows.append((m, t0, r))
        print("TEST", summary(rows, f"{fr['entry']}+{fr['exit']}"))


if __name__ == "__main__":
    main()
