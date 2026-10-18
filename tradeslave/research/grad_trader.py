"""Strategy S2: the discretionary-trader model (screen -> pick horizon -> time entry) on GRADUATED coins.

    uv run python -m tradeslave.research.grad_trader design
    uv run python -m tradeslave.research.grad_trader test

Same idea as research.discretion (S), on PumpSwap after graduation, where fees fall with market cap
(official tiers, research.shock.fee) and liquidity is deeper. Data: research.candles (1-minute PumpSwap
candles in SOL from graduation, every graduation the recorder saw, Oct 6-17). Decision at each minute
close with market cap >= MIN_MC SOL; buy at the next minute's open (at this close if nobody trades the next
minute); exits on minute closes, filled the same way. SIZE SOL, tiered fee + constant-product impact (shock.round_trip logic), TX SOL per
transaction. Split by graduation time: design = first 60% (fit on its first 70%, choose on the rest),
test = last 40%, judged once. Vault: "2026-10-18 discretionary mimic (S)", section S2.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from collections import defaultdict

import numpy as np

from ..paths import DATA_DIR
from .candles import CANDLE_DIR
from .shock import K_POOL, fee

SIZE, TX = 1.0, 0.001
MIN_MC = 420
MIN_V15 = 3000.0                 # USD traded in the last 15 min (~15x our size): skip thin / odd pools
FROZEN = DATA_DIR / "grad_trader_rule.json"
# (name, take profit, stop, trailing, max hold minutes)
POLICIES = [("scalp15m", 0.15, 0.08, None, 15), ("swing2h", None, 0.20, 0.20, 120), ("hold12h", None, None, 0.35, 720),
            ("flat1h", None, None, None, 60), ("flat4h", None, None, None, 240)]
FEATS = ["age_m", "mcap", "r1", "r5", "r15", "r60", "r240", "v1", "v5", "v15", "v60", "vspike", "ath", "since_ath",
         "lo60", "hi60", "volat15", "greens", "rsi14", "active60", "idle"]


def round_trip(p_in: float, p_out: float) -> float:
    s_in = math.sqrt(K_POOL * p_in)
    t_in = s_in / p_in
    net = SIZE * (1 - fee(p_in * 1e9))
    tokens = t_in * net / (s_in + net)
    s_out = math.sqrt(K_POOL * p_out)
    t_out = s_out / p_out
    got = s_out * tokens / (t_out + tokens) * (1 - fee(p_out * 1e9))
    return (got - SIZE - 2 * TX) / SIZE


def load():
    toks = []
    for p in CANDLE_DIR.glob("*.json"):
        d = json.loads(p.read_text())
        c = [x for x in d.get("c") or [] if x[1] > 0 and x[4] > 0]
        if len(c) >= 30:
            toks.append((d.get("t_grad") or c[0][0], d["m"], np.array(c, float)))
    toks.sort(key=lambda x: x[0])
    return toks


def points(tg, c):
    """(features, labels per policy, t) at each minute close with mcap >= MIN_MC and a next-minute candle."""
    t, o, h, lo, cl, v = c.T
    n = len(c)
    out = []
    ath = np.maximum.accumulate(h)
    ath_i = np.zeros(n, int)
    for i in range(1, n):
        ath_i[i] = i if h[i] >= ath[i - 1] else ath_i[i - 1]
    for i in range(5, n - 1):
        if cl[i] * 1e9 < MIN_MC:
            continue

        def back(mins):
            j = np.searchsorted(t, t[i] - 60 * mins, side="right") - 1
            return cl[i] / cl[j] - 1 if j >= 0 else cl[i] / o[0] - 1

        def vol(mins):
            j = np.searchsorted(t, t[i] - 60 * mins, side="right")
            return v[j:i + 1].sum()

        if vol(15) < MIN_V15:
            continue
        j60 = np.searchsorted(t, t[i] - 3600, side="right")
        r = cl[max(0, i - 14):i + 1]
        rr = r[1:] / r[:-1] - 1 if len(r) > 1 else np.zeros(1)
        greens = 0
        for x in rr[::-1]:
            if x <= 0:
                break
            greens += 1
        gains, losses = rr[rr > 0].sum(), -rr[rr < 0].sum()
        f = [(t[i] - tg) / 60, cl[i] * 1e9, back(1), back(5), back(15), back(60), back(240), v[i], vol(5), vol(15),
             vol(60), v[i] / (vol(60) / max(1, i + 1 - j60)), cl[i] / ath[i], (t[i] - t[ath_i[i]]) / 60,
             cl[i] / lo[j60:i + 1].min() - 1, cl[i] / h[j60:i + 1].max() - 1, float(np.std(rr)), greens,
             100 - 100 / (1 + gains / losses) if losses > 0 else 100.0, i + 1 - j60, (t[i] - t[i - 1]) / 60]
        p_in = o[i + 1] if t[i + 1] - t[i] <= 60 else cl[i]      # no trade next minute: pool still at this close
        ys = []
        for _, tp, sl, trail, hold in POLICIES:
            end = t[i] + 60 + hold * 60
            peak, p_out = p_in, None
            k = i + 1
            while k < n and t[k] <= end:
                peak = max(peak, cl[k])
                if (tp and cl[k] >= p_in * (1 + tp)) or (sl and cl[k] <= p_in * (1 - sl)) or \
                        (trail and cl[k] <= peak * (1 - trail)):
                    p_out = o[k + 1] if k + 1 < n and t[k + 1] - t[k] <= 60 else cl[k]
                    break
                k += 1
            if p_out is None:
                p_out = cl[min(k, n) - 1]
            ys.append(round_trip(p_in, p_out))
        out.append((f, ys, t[i]))
    return out


def build():
    toks = load()
    X, Y, M, TG, TT = [], [], [], [], []
    for tg, m, c in toks:
        for f, ys, tt in points(tg, c):
            X.append(f)
            Y.append(ys)
            M.append(m)
            TG.append(tg)
            TT.append(tt)
    X, Y, M, TG, TT = np.array(X), np.array(Y), np.array(M), np.array(TG), np.array(TT)
    ut = np.sort(np.unique(TG))
    cut_d = ut[int(len(ut) * 0.6)]
    cut_f = ut[int(len(ut) * 0.42)]
    return X, Y, M, TG, TT, TG < cut_f, (TG >= cut_f) & (TG < cut_d), TG >= cut_d


def fit(X, Y):
    from sklearn.ensemble import HistGradientBoostingRegressor
    return [HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=200,
                                          l2_regularization=1.0, random_state=0).fit(X, np.clip(Y[:, k], -1, 3))
            for k in range(Y.shape[1])]


def trade_once(P, Y, M, TT, thr, pols, gap=3600):
    """At most one open position per token, re-entry allowed GAP s after the previous entry."""
    best = np.array([max(pols, key=lambda k: P[i, k]) for i in range(len(P))])
    bp = P[np.arange(len(P)), best]
    last, out = {}, []
    for i in np.argsort(TT, kind="stable"):
        if bp[i] < thr or TT[i] - last.get(M[i], -1e18) < gap:
            continue
        last[M[i]] = TT[i]
        out.append((M[i], TT[i], int(best[i]), float(Y[i, best[i]])))
    return out


def summary(rows, label):
    xs = [r[3] for r in rows]
    if len(xs) < 5:
        return f"{label}: n={len(xs)}"
    m, sd = statistics.mean(xs), statistics.pstdev(xs)
    mix = defaultdict(int)
    for r in rows:
        mix[POLICIES[r[2]][0]] += 1
    trim = sorted(xs)[:-max(1, len(xs) // 50)]
    return (f"{label}: n={len(xs)} tokens {len({r[0] for r in rows})} mean {m:+.2%} med {statistics.median(xs):+.2%} "
            f"win {np.mean(np.array(xs) > 0):.0%} t {m / (sd / math.sqrt(len(xs))) if sd else 0:+.2f} "
            f"| w/o top 2% {statistics.mean(trim):+.2%} | {dict(mix)}")


def main() -> None:
    X, Y, M, TG, TT, fm, vm, tm = build()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "design"
    print(f"points fit {fm.sum()} ({len(set(M[fm]))} tok), val {vm.sum()} ({len(set(M[vm]))}), "
          f"test {tm.sum()} ({len(set(M[tm]))})")
    if cmd == "design":
        for k, pol in enumerate(POLICIES):
            print(f"  every point {pol[0]}: fit {Y[fm, k].mean():+.2%} val {Y[vm, k].mean():+.2%}")
        ms = fit(X[fm], Y[fm])
        P = np.column_stack([m.predict(X[vm]) for m in ms])
        res = []
        for pols in ([0], [1], [2], [3], [4], [0, 1, 2, 3, 4]):
            for thr in (0.0, 0.02, 0.05, 0.10):
                rows = trade_once(P, Y[vm], M[vm], TT[vm], thr, pols)
                if len(rows) >= 30:
                    res.append((statistics.mean(r[3] for r in rows), (pols, thr), rows))
        res.sort(key=lambda r: -r[0])
        for mean, prm, rows in res[:10]:
            print("  ", summary(rows, str(prm)))
        if res:
            FROZEN.write_text(json.dumps({"pols": res[0][1][0], "thr": res[0][1][1]}))
            print("frozen", res[0][1])
    else:
        fr = json.loads(FROZEN.read_text())
        dm = fm | vm
        ms = fit(X[dm], Y[dm])
        P = np.column_stack([m.predict(X[tm]) for m in ms])
        rows = sorted(trade_once(P, Y[tm], M[tm], TT[tm], fr["thr"], fr["pols"]), key=lambda r: r[1])
        h = len(rows) // 2
        print("TEST", summary(rows, str(fr)))
        print("  halves", summary(rows[:h], "1st"), "|", summary(rows[h:], "2nd"))


if __name__ == "__main__":
    main()
