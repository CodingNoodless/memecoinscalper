"""Strategy U: manage the position like a trader. Learned exits + entries valued under those exits.

    uv run python -m tradeslave.research.managed build     # state at every active 5 s bar (cache)
    uv run python -m tradeslave.research.managed design    # LSM continuation value, choose (th_e, th_x) on validation
    uv run python -m tradeslave.research.managed test      # judge the frozen rule once on launches after the freeze

Rohit (2026-10-18): don't wait to be rugged before selling; a coin that tanks but has strong indicators should be
held. S used fixed price-only exits; here the hold/sell decision at every 5 s bar uses every indicator.

Continuation value C(x) = E[realized exit / p_b | state x_b] (risk-neutral: the decision does not depend on the
entry price). Longstaff-Schwartz style: start from "never sell" (hold to the window end, or to graduation), compute
each path's realized exit under the current policy (sell at bar b when C(x_b) < f_b / p_b), refit C on the value of
holding past b, 4 rounds, cross-fitted over 5 token folds. Entry when C(x)(1-fee)^2 - 2 tx/size - 1 >= th_e, hold
while C(x) >= (f/p)(1 + th_x). Fills as in research.discretion (LAT slots, exact state 1 s after the bar, our bag
in the curve). Pre-registered in the vault note "2026-10-18 learned exits (U)".
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from collections import defaultdict, deque

import numpy as np

from ..paths import DATA_DIR
from . import discretion as D

CACHE = DATA_DIR / "managed_states.npz"
FROZEN = DATA_DIR / "managed_rule.json"
FAST = ["sell5", "buy5", "sell15", "buy15", "max_sell15", "n_sell15", "top10_sold15", "top10_frac15", "snipe_sold15",
        "dev_sold15", "new_buyers15", "since_buy", "since_big_sell", "from_peak30"]
ROUNDS, FOLDS = 4, 5
GRID = [(e, x) for e in (0.0, 0.05, 0.10, 0.20) for x in (0.0, 0.03)]


def fast_features(tk: D.Tok, times):
    """Exit-speed flow features at each bar close in `times` (sorted)."""
    cur = tk.cur
    creator = {x[3] for x in cur if x[0] == cur[0][0]}
    snipe_slots = cur[0][0] + 3
    bal = defaultdict(float)
    snipers, seen = set(), set()
    hist = deque()                 # (t, w, buy, sol, tok, first_buy)
    top10_hist = deque()           # (T, {wallet: balance}) of the top 10 at each bar
    last_buy = last_big_sell = tk.t0
    i, n = 0, len(cur)
    out = {}
    for T in times:
        while i < n and cur[i][2] <= T:
            slot, _, t, w, b, sol, tok, _ = cur[i]
            first = b and w not in seen
            seen.add(w)
            bal[w] += tok if b else -tok
            if b and slot <= snipe_slots and w not in creator:
                snipers.add(w)
            if b:
                last_buy = t
            elif sol >= 0.5:
                last_big_sell = t
            hist.append((t, w, b, sol, tok, first))
            i += 1
        while hist and hist[0][0] <= T - 30:
            hist.popleft()
        top = dict(sorted(((w, v) for w, v in bal.items() if v > 1 and w not in creator), key=lambda z: -z[1])[:10])
        top10_hist.append((T, top))
        while top10_hist and top10_hist[0][0] < T - 15:
            top10_hist.popleft()
        ref = top10_hist[0][1]
        h5 = [h for h in hist if h[0] > T - 5]
        h15 = [h for h in hist if h[0] > T - 15]
        s15 = [h for h in h15 if not h[2]]
        top_sold = sum(h[4] for h in s15 if h[1] in ref)
        k = int(np.searchsorted(tk.t, T, side="right")) - 1
        k30 = int(np.searchsorted(tk.t, T - 30, side="right"))
        vs_now = tk.vs[k] if k >= 0 else D.VS0
        peak = tk.vs[max(k30 - 1, 0):k + 1].max() if k >= 0 else vs_now
        out[T] = [sum(h[3] for h in h5 if not h[2]), sum(h[3] for h in h5 if h[2]), sum(h[3] for h in s15),
                  sum(h[3] for h in h15 if h[2]), max((h[3] for h in s15), default=0.0), len(s15),
                  sum(h[3] for h in s15 if h[1] in ref), top_sold / (sum(ref.values()) or 1.0),
                  sum(h[3] for h in s15 if h[1] in snipers), sum(h[3] for h in s15 if h[1] in creator),
                  sum(1 for h in h15 if h[5]), T - last_buy, T - last_big_sell, (vs_now / peak) ** 2]
    return out


def terminal(tk: D.Tok, ser):
    """Where a never-sold bag ends: (fill price, graduated) at graduation or at the end of the 30-min window."""
    st, sp, sf, curve = ser
    if (~curve).any():
        k = int(np.argmax(~curve))
        return float(sf[k]), bool(sp[k] >= D.P_GRAD * 0.999)
    return float(sf[-1]) if len(sf) else D.price(D.VS0), False


def cmd_build() -> None:
    from .smart_money import CreatorLog, WalletBook
    book, crlog = WalletBook(), CreatorLog()
    X, P, F, LS, TT, M, T0, TERM, TG = [], [], [], [], [], [], [], [], []
    files = sorted(D.FT_OUT.glob("*.json.gz"))
    for j, f in enumerate(files):
        try:
            x = D.load_token(f)
        except Exception:
            continue
        if x is None:
            continue
        tk = D.Tok(*x)
        bars = list(D.bars_and_features(tk, book, crlog))
        if not bars:
            continue
        ff = fast_features(tk, [b[0] for b in bars])
        ser = D.series(tk)
        tf, tg = terminal(tk, ser)
        for T, ls, feats in bars:
            X.append(feats + ff[T])
            P.append(D.price(tk.vs_at_time(T)))
            F.append(D.price(tk.vs_at_time(T + 1)))
            LS.append(ls)
            TT.append(T)
            M.append(tk.m)
            T0.append(tk.t0)
            TERM.append(tf)
            TG.append(tg)
        if j % 250 == 0:
            print(f"{j}/{len(files)} launches, {len(X)} states", flush=True)
    np.savez_compressed(CACHE, X=np.array(X, np.float32), P=np.array(P), F=np.array(F), LS=np.array(LS),
                        TT=np.array(TT), M=np.array(M), T0=np.array(T0), TERM=np.array(TERM), TG=np.array(TG))
    print(f"saved {len(X)} states from {len(set(M))} launches")


# ---------------------------------------------------------------- LSM continuation value

def _paths(M):
    """Index ranges of each token's bars (cache rows are grouped by token, in time order)."""
    cuts = np.flatnonzero(M[1:] != M[:-1]) + 1
    return list(zip(np.r_[0, cuts], np.r_[cuts, len(M)]))


def _gbm():
    from sklearn.ensemble import HistGradientBoostingRegressor
    return HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=300,
                                         l2_regularization=1.0, random_state=0)


def hold_targets(P, F, TERM, paths, C):
    """Realized value of holding past each bar (relative to its price) when selling at the first later bar
    with C < f/p; never-sold bags end at the terminal fill."""
    y = np.empty(len(P))
    for a, b in paths:
        w = TERM[a]
        for k in range(b - 1, a - 1, -1):
            y[k] = w / P[k]
            if C is not None and C[k] < F[k] / P[k]:
                w = F[k]
    return np.clip(y, 0, 10)


def fit_lsm(X, P, F, TERM, M, verbose=True):
    """Cross-fitted continuation values for the given rows + the final model fit on all of them."""
    paths = _paths(M)
    toks = np.unique(M)
    fold = {m: k % FOLDS for k, m in enumerate(np.random.default_rng(0).permutation(toks))}
    fi = np.array([fold[m] for m in M])
    C = None
    for r in range(ROUNDS):
        y = hold_targets(P, F, TERM, paths, C)
        C = np.empty(len(y))
        for k in range(FOLDS):
            C[fi == k] = _gbm().fit(X[fi != k], y[fi != k]).predict(X[fi == k])
        if verbose:
            print(f"  round {r + 1}: mean hold value {y.mean():.3f}, sells at {np.mean(C < F / P):.0%} of bars", flush=True)
    y = hold_targets(P, F, TERM, paths, C)
    return _gbm().fit(X, y)


# ---------------------------------------------------------------- exact policy simulation

def simulate(rows, C, P, F, LS, TT, M, TERM, TG, th_e, th_x):
    """Trade the policy on the given token rows. Returns (token, entry t, net return) per trade."""
    trades = []
    cost = 2 * D.PRIO / D.SIZE
    for a, b in rows:
        k = a
        while k < b:
            if C[k] * (1 - D.FEE) ** 2 - cost - 1 < th_e:
                k += 1
                continue
            vs_e = _vs_after(M[k], LS[k] + D.LAT)
            net = D.SIZE * (1 - D.FEE) * 1e9
            tokens = (D.K / vs_e) * net / (vs_e + net) / 1e6
            j = k + 1
            while j < b and C[j] >= F[j] / P[j] * (1 + th_x):
                j += 1
            if j < b:
                px, g = F[j], False
            else:
                px, g = TERM[a], bool(TG[a])
            trades.append((M[k], TT[k], (D.sell_value(tokens, px, net, g) - D.SIZE - 2 * D.PRIO) / D.SIZE))
            k = j + 1
    return trades


_TOK = {}


def _vs_after(m, slot):
    return _TOK[m].vs_after_slot(slot)[0]


def _load_toks(ms):
    names = {f.name.split(".")[0]: f for f in D.FT_OUT.glob("*.json.gz")}
    for m in ms:
        if m not in _TOK and m in names:
            _TOK[m] = D.Tok(*D.load_token(names[m]))


def summary(tr, label):
    xs = [x[2] for x in tr]
    if len(xs) < 5:
        return f"{label}: n={len(xs)}"
    by = defaultdict(float)
    for m, _, x in tr:
        by[m] += x
    v = list(by.values())
    sd = statistics.pstdev(v)
    tcl = statistics.mean(v) / (sd / math.sqrt(len(v))) if sd else 0.0
    trim = sorted(xs)[:-max(1, len(xs) // 50)]
    return (f"{label}: n={len(xs)} tokens {len(v)} mean {statistics.mean(xs):+.2%} med {statistics.median(xs):+.2%} "
            f"win {np.mean(np.array(xs) > 0):.0%} token-t {tcl:+.2f} | w/o top 2% {statistics.mean(trim):+.2%} "
            f"| sum {sum(xs) * D.SIZE:+.2f} SOL")


def load_cache():
    z = np.load(CACHE)
    return [z[k] for k in ("X", "P", "F", "LS", "TT", "M", "T0", "TERM", "TG")]


def cmd_design() -> None:
    X, P, F, LS, TT, M, T0, TERM, TG = load_cache()
    fr = json.loads(FROZEN.read_text()) if FROZEN.exists() else {}
    design = np.isin(M, fr["design_mints"]) if fr else np.ones(len(M), bool)
    ut = np.sort(np.unique(T0[design]))
    cut_f = ut[int(len(ut) * 0.7)]
    fm, vm = design & (T0 < cut_f), design & (T0 >= cut_f)
    print(f"fit {fm.sum()} states / {len(set(M[fm]))} launches, val {vm.sum()} / {len(set(M[vm]))}")
    model = fit_lsm(X[fm], P[fm], F[fm], TERM[fm], M[fm])
    C = model.predict(X)
    _load_toks(set(M[vm]))
    vrows = [(a, b) for a, b in _paths(M) if vm[a]]
    res = []
    for th_e, th_x in GRID:
        tr = simulate(vrows, C, P, F, LS, TT, M, TERM, TG, th_e, th_x)
        print("  ", summary(tr, f"th_e {th_e:.2f} th_x {th_x:.2f}"), flush=True)
        if len(tr) >= 40:
            res.append((statistics.mean(x[2] for x in tr), th_e, th_x))
    if res:
        res.sort(reverse=True)
        FROZEN.write_text(json.dumps({"th_e": res[0][1], "th_x": res[0][2],
                                      "design_mints": sorted(set(M[design].tolist()))}))
        print(f"frozen th_e {res[0][1]} th_x {res[0][2]}; test = launches not in the {len(set(M[design]))} design mints")


def cmd_test() -> None:
    X, P, F, LS, TT, M, T0, TERM, TG = load_cache()
    fr = json.loads(FROZEN.read_text())
    design = np.isin(M, fr["design_mints"])
    test = ~design
    print(f"design {len(set(M[design]))} launches, test {len(set(M[test]))} launches")
    model = fit_lsm(X[design], P[design], F[design], TERM[design], M[design])
    C = model.predict(X)
    _load_toks(set(M[test]))
    trows = [(a, b) for a, b in _paths(M) if test[a]]
    tr = sorted(simulate(trows, C, P, F, LS, TT, M, TERM, TG, fr["th_e"], fr["th_x"]), key=lambda r: r[1])
    h = len(tr) // 2
    print("TEST", summary(tr, f"th_e {fr['th_e']} th_x {fr['th_x']}"))
    print("  halves", summary(tr[:h], "1st"), "|", summary(tr[h:], "2nd"))


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "design"
    {"build": cmd_build, "design": cmd_design, "test": cmd_test}[cmd]()


if __name__ == "__main__":
    main()
