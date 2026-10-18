"""Strategy S: trade like a discretionary memecoin trader (screen -> pick horizon -> time entry on the 5s chart).

    uv run python -m tradeslave.research.discretion build     # decision points + labels (cache)
    uv run python -m tradeslave.research.discretion design    # fit on design launches, choose rule on validation
    uv run python -m tradeslave.research.discretion test      # judge the frozen rule once on test launches

What a human does on Axiom/Photon/GMGN, done by a bot:
1) Screener indicators of the coin: market cap, age, volume, buys/sells, holders, top-10 share, dev still
   holding, snipers still holding, largest holder, big buys, ATH and drawdown, VWAP.
2) Horizon: scalp (seconds), swing (minutes, trailing stop) or hold (hours, wide trailing stop). A model
   per exit policy predicts the net return of entering now; the bot picks the best policy for this coin.
3) Entry on the 5-second chart: pullbacks, range position, higher lows, volume spikes, RSI, distance from
   VWAP and the 60 s low/high are features at every 5 s bar, so the model learns WHEN on the chart to buy.

Data: research.full_trades (random launches, every trade of the first 30 min) for the decision and for
exits inside 30 min (exact curve state); research.long_paths (1-minute candles from pump.fun) for exits
after 30 min, up to 6 h, including PumpSwap after graduation (official fee tiers, constant-product impact).
Fill model: decide at a 5 s bar close; our buy lands after every trade up to the bar's last slot + LAT;
exit triggers on 5 s bar closes and fills at the state 1 s later (1-minute candles after 30 min: next
minute's open). Our bag stays in the curve. 1.25% curve fee per side, 0.002 SOL per transaction.
Split by launch time: design < dip_runner.DESIGN_CUT_T (fit on its first 70%, choose on its last 30%),
test >= DESIGN_CUT_T, judged once. Pre-registered in the vault note "2026-10-18 discretionary mimic (S)".
"""

from __future__ import annotations

import gzip
import json
import math
import statistics
import sys
from bisect import bisect_right
from collections import defaultdict

import numpy as np

from ..paths import DATA_DIR
from .dip_runner import DESIGN_CUT_T
from .full_trades import OUT as FT_OUT
from .long_paths import OUT as LP_OUT
from .shock import K_POOL, fee as pool_fee

VS0, VT0 = 30e9, 1073e12
K = VS0 * VT0
FEE, PRIO, SIZE = 0.0125, 0.002, 0.5
LAT = 2                          # slots (measured: Helius websocket sees a trade ~100 ms into its slot)
BAR = 5
WINDOW = 1800
LONG = 6 * 3600
MIN_AGE, MAX_AGE = 15, 1500
P_GRAD = 4.15e-7                 # SOL/token where the curve completes (vs ~ 115 SOL)
CACHE = DATA_DIR / "discretion_points.npz"
FROZEN = DATA_DIR / "discretion_rule.json"

# exit policies: (name, take profit, stop loss, trailing from peak, max hold s)
POLICIES = [("scalp45", 0.20, 0.12, None, 45), ("scalp3m", 0.50, 0.20, None, 180),
            ("swing30m", None, 0.30, 0.25, 1800), ("hold6h", None, None, 0.45, LONG), ("flat6h", None, None, None, LONG)]

FEATS = ["age", "mcap", "ath_ratio", "since_ath", "r5", "r15", "r30", "r60", "r180", "vol30", "vol60", "vol300",
         "buys60", "sells60", "ubuy60", "traders", "holders", "top10", "max_holder", "dev_left", "dev_sold_all",
         "snipe_left", "bsr60", "avg_buy60", "max_buy60", "big_buys60", "vwap_d", "vwap60_d", "b1", "b2", "b3", "b4",
         "b5", "b6", "vol5s12", "range12", "pos_range12", "vol_spike", "greens", "higher_low", "from_low60",
         "from_high60", "rsi14", "idle", "sm_holders", "sm_share", "sm_buys60", "sm_sells60", "dumb_holders",
         "exp_holders", "buyer_roi60", "dev_prior", "dev_ib", "sym_len"]


def price(vs: float) -> float:
    return vs * vs * 1e6 / (1e9 * K)


def vs_of(p: float) -> float:
    return math.sqrt(p * K * 1e3)


# ---------------------------------------------------------------- per-token data

def load_token(path):
    d = json.load(gzip.open(path, "rt", encoding="utf-8"))
    tr = d["trades"]
    if not tr:
        return None
    t0 = d["t0"]
    lp = LP_OUT / f"{d['m']}.json"
    candles = json.loads(lp.read_text())["curve"] if lp.exists() else []
    return d["m"], t0, tr, candles


def busy_tokens(skip: set):
    """The research.traders exact-trade set (first 15 min, activity-selected): FIT data only, never judged."""
    from .traders import load
    bdir = DATA_DIR / "long_paths_busy"
    for m, ts in load().items():
        if m in skip:
            continue
        lp = LP_OUT / f"{m}.json"
        lp = lp if lp.exists() else bdir / f"{m}.json"
        if not lp.exists():
            continue
        tr = [[r["slot"], r.get("i", 0), r["t"], r["w"], bool(r["b"]), r["sol"], abs(r["tok"]), False] for r in ts]
        yield m, ts[0]["t"], tr, json.loads(lp.read_text())["curve"], 900


class Tok:
    """Exact replay of one launch: observed curve state after each trade, plus long candles."""

    def __init__(self, m, t0, tr, candles, window=WINDOW):
        self.m, self.t0, self.window = m, t0, window
        self.grad_t = next((x[2] for x in tr if x[7]), None)
        cur = [x for x in tr if not x[7]]
        self.t = np.array([x[2] for x in cur], float)
        self.slot = np.array([x[0] for x in cur], np.int64)
        vt, vts = VT0, []
        for x in cur:
            vt += -x[6] * 1e6 if x[4] else x[6] * 1e6
            vts.append(max(vt, 1e12))
        self.vs = K / np.array(vts) if vts else np.array([])
        self.cur = cur
        self.full_c = candles or []
        self.c = [c for c in self.full_c if c[0] >= t0 + window - 60]

    def vs_at_time(self, t):
        i = bisect_right(self.t, t) - 1
        return self.vs[i] if i >= 0 else VS0

    def vs_after_slot(self, s):
        i = int(np.searchsorted(self.slot, s, side="right")) - 1
        return self.vs[i] if i >= 0 else VS0, i


# ---------------------------------------------------------------- features at 5 s bars

def bars_and_features(tk: Tok, book=None, crlog=None):
    """Yield (bar_end_t, last_slot, features) at every 5 s bar close in the entry window with activity."""
    cur = tk.cur
    if len(cur) < 5:
        return
    wstat = {}

    def ws(w):                                     # wallet record from books closed before this launch
        if w not in wstat:
            wstat[w] = book.stats(w, tk.t0) if book else (0, 0.0, 0.0)
        return wstat[w]

    cr, ib, sym = crlog.info.get(tk.m, ("", 0.0, "")) if crlog else ("", 0.0, "")
    dev_prior = crlog.prior(cr, tk.t0) if crlog and cr else 0
    creator_grp = {x[3] for x in cur if x[0] == cur[0][0]}
    snipe_slots = cur[0][0] + 3
    bal = defaultdict(float)
    grp_bought = grp_held = 0.0
    snipers = set()
    snipe_bought = 0.0
    vol_cum = sol_cum = 0.0
    hist = []            # (t, w, b, sol, tok)
    closes, vols = [], []
    ath, ath_t = VS0, tk.t0
    i, n = 0, len(cur)
    t_end = tk.t0 + min(MAX_AGE, tk.window - BAR)
    T = tk.t0 + BAR
    last_trade_t = cur[0][2]
    while T <= t_end and i < n:
        bar_vol = 0.0
        active = False
        while i < n and cur[i][2] <= T:
            slot, _, t, w, b, sol, tok, _ = cur[i]
            bal[w] += tok if b else -tok
            if w in creator_grp:
                if b:
                    grp_bought += tok
                grp_held += tok if b else -tok
            elif b and slot <= snipe_slots:
                snipers.add(w)
                snipe_bought += tok
            hist.append((t, w, b, sol, tok))
            bar_vol += sol
            vol_cum += tok
            sol_cum += sol
            last_trade_t = t
            active = True
            vs = tk.vs[i]
            if vs > ath:
                ath, ath_t = vs, t
            i += 1
        vs_now = tk.vs[i - 1] if i else VS0
        closes.append(price(vs_now))
        vols.append(bar_vol)
        if tk.grad_t is not None and T >= tk.grad_t:
            return
        age = T - tk.t0
        recent = active or (len(vols) >= 6 and sum(vols[-6:]) > 0)
        if age >= MIN_AGE and recent and vs_now < 0.85 * 115e9:
            p = closes[-1]

            def back(sec):
                k = len(closes) - 1 - sec // BAR
                return p / closes[k] - 1 if k >= 0 else p / price(VS0) - 1

            w60 = [h for h in hist if h[0] > T - 60]
            w30 = [h for h in hist if h[0] > T - 30]
            w300 = [h for h in hist if h[0] > T - 300]
            buys60 = [h for h in w60 if h[2]]
            sells60 = [h for h in w60 if not h[2]]
            pos = sorted((v for w, v in bal.items() if v > 1), reverse=True)
            circ = sum(pos) or 1.0
            snipe_left = sum(max(bal[w], 0) for w in snipers) / snipe_bought if snipe_bought else 0.0
            vwap = sol_cum / vol_cum if vol_cum else p
            v60_sol = sum(h[3] for h in w60)
            v60_tok = sum(h[4] for h in w60)
            vwap60 = v60_sol / v60_tok if v60_tok else p
            cl = np.array(closes[-13:])
            rets = cl[1:] / cl[:-1] - 1 if len(cl) > 1 else np.zeros(1)
            last6 = list(rets[-6:])[::-1] + [0.0] * (6 - min(6, len(rets)))
            c12 = cl[-12:]
            hi12, lo12 = c12.max(), c12.min()
            v12 = vols[-12:]
            greens = 0
            for x in rets[::-1]:
                if x > 0:
                    greens += 1
                else:
                    break
            c60 = closes[-12:]
            gains = rets[-14:][rets[-14:] > 0].sum()
            losses = -rets[-14:][rets[-14:] < 0].sum()
            f = [age, p * 1e9, (vs_now / ath) ** 2, T - ath_t, back(5), back(15), back(30), back(60), back(180),
                 sum(h[3] for h in w30), v60_sol, sum(h[3] for h in w300), len(buys60), len(sells60),
                 len({h[1] for h in buys60}), len(bal), len(pos), sum(pos[:10]) / circ, pos[0] / circ if pos else 0,
                 grp_held / grp_bought if grp_bought else 0.0, float(grp_bought > 0 and grp_held <= grp_bought * 0.01),
                 snipe_left, sum(h[3] for h in buys60) / (v60_sol or 1), statistics.mean(h[3] for h in buys60) if buys60 else 0,
                 max((h[3] for h in buys60), default=0), sum(h[3] >= 1 for h in buys60), p / vwap - 1, p / vwap60 - 1,
                 *last6, float(np.std(rets[-12:])), hi12 / lo12 - 1, (p - lo12) / (hi12 - lo12) if hi12 > lo12 else 0.5,
                 bar_vol / (statistics.mean(v12) or 1), greens,
                 float(len(cl) >= 7 and min(cl[-3:]) > min(cl[-6:-3])), p / min(c60) - 1, p / max(c60) - 1,
                 100 - 100 / (1 + gains / losses) if losses > 0 else 100.0, T - last_trade_t]
            hold_w = [(w, v) for w, v in bal.items() if v > 1 and w not in creator_grp]
            smart = [(w, v) for w, v in hold_w if ws(w)[0] >= 5 and ws(w)[1] > 0.1]
            r60 = [ws(h[1])[1] for h in buys60 if ws(h[1])[0] >= 3]
            f += [len(smart), sum(v for _, v in smart) / circ,
                  sum(1 for h in buys60 if ws(h[1])[0] >= 5 and ws(h[1])[1] > 0.1),
                  sum(1 for h in sells60 if ws(h[1])[0] >= 5 and ws(h[1])[1] > 0.1),
                  sum(1 for w, _ in hold_w if ws(w)[0] >= 5 and ws(w)[1] < -0.3),
                  sum(1 for w, _ in hold_w if ws(w)[0] >= 20), statistics.mean(r60) if r60 else 0.0,
                  dev_prior, ib, len(sym)]
            yield T, int(cur[i - 1][0]), f
        T += BAR


# ---------------------------------------------------------------- exit simulation

def series(tk: Tok):
    """Whole-token observed path after launch: (t, price, fill price, is_curve_bar).

    5 s bar closes inside the window (fill = exact state 1 s later), then 1-minute candle closes up to
    6 h (fill = next minute's open). Graduation inside the window hands over to the candles."""
    end_w = tk.t0 + tk.window if tk.grad_t is None else min(tk.grad_t, tk.t0 + tk.window)
    gt = np.arange(tk.t0 + BAR, end_w + 1e-9, BAR)
    idx = np.searchsorted(tk.t, gt, side="right") - 1
    idx1 = np.searchsorted(tk.t, gt + 1, side="right") - 1
    vs = np.where(idx >= 0, tk.vs[np.maximum(idx, 0)], VS0)
    vs1 = np.where(idx1 >= 0, tk.vs[np.maximum(idx1, 0)], VS0)
    gp, gf = vs * vs * 1e6 / (1e9 * K), vs1 * vs1 * 1e6 / (1e9 * K)
    cs = [c for c in tk.c if c[0] + 60 > end_w and c[0] + 60 <= tk.t0 + LONG + MAX_AGE]
    if tk.grad_t is not None and tk.grad_t < tk.t0 + tk.window:
        cs = [c for c in (tk.full_c or []) if c[0] + 60 > tk.grad_t and c[0] + 60 <= tk.t0 + LONG + MAX_AGE]
    ct = np.array([c[0] + 60 for c in cs], float)
    cp = np.array([c[4] for c in cs], float)
    cf = np.array([cs[k + 1][1] if k + 1 < len(cs) else cs[k][4] for k in range(len(cs))], float)
    if tk.grad_t is not None and tk.grad_t < tk.t0 + tk.window:
        ct, cp, cf = np.r_[tk.grad_t, ct], np.r_[P_GRAD, cp], np.r_[cp[0] if len(cp) else P_GRAD, cf]
    return (np.r_[gt, ct], np.r_[gp, cp], np.r_[gf, cf], np.r_[np.ones(len(gt), bool), np.zeros(len(ct), bool)])


def sell_value(tokens: float, p: float, net: float, graduated: bool) -> float:
    """SOL received for our whole-token bag at observed price p (our own SOL still inside the curve)."""
    if not graduated and p < P_GRAD:
        vs2 = vs_of(p) + net
        vt2 = K / vs2
        raw = tokens * 1e6
        return vs2 * raw / (vt2 + raw) / 1e9 * (1 - FEE)
    s = math.sqrt(K_POOL * p)
    tp = s / p
    return s * tokens / (tp + tokens) * (1 - pool_fee(p * 1e9))


def simulate_all(tk: Tok, T: float, last_slot: int, ser, lat: int = LAT) -> list[float]:
    """Net return (fraction of SIZE) of buying at bar close T, for every exit policy."""
    vs_e, _ = tk.vs_after_slot(last_slot + lat)
    net = SIZE * (1 - FEE) * 1e9
    tokens = (K / vs_e) * net / (vs_e + net) / 1e6
    p_e = price(vs_e + net)
    st, sp, sf, _ = ser
    a = int(np.searchsorted(st, T, side="right"))
    t, p, f = st[a:], sp[a:], sf[a:]
    grad = np.maximum.accumulate(p >= P_GRAD * 0.999) if len(p) else p.astype(bool)
    vso = np.sqrt(np.maximum(p, 0) * K * 1e3)
    pw = np.where(grad, p, (vso + net) ** 2 * 1e6 / (1e9 * K))      # price as WE see it (our bag in the curve)
    peak = np.maximum.accumulate(np.maximum(pw, p_e)) if len(p) else p
    out = []
    for _, tp, sl, trail, hold in POLICIES:
        m = int(np.searchsorted(t, T + hold, side="right"))
        hit = np.zeros(m, bool)
        if tp is not None:
            hit |= pw[:m] >= p_e * (1 + tp)
        if sl is not None:
            hit |= pw[:m] <= p_e * (1 - sl)
        if trail is not None:
            hit |= pw[:m] <= peak[:m] * (1 - trail)
        if hit.any():
            k = int(hit.argmax())
            px, g = f[k], bool(grad[k])
        elif m:
            px, g = p[m - 1], bool(grad[m - 1])
        else:
            px, g = price(tk.vs_at_time(T + 1)), False
        out.append((sell_value(tokens, px, net, g) - SIZE - 2 * PRIO) / SIZE)
    return out


# ---------------------------------------------------------------- build cache

def cmd_build() -> None:
    X, Y, M, T0, TT, SRC = [], [], [], [], [], []
    from .smart_money import CreatorLog, WalletBook
    book, crlog = WalletBook(), CreatorLog()
    print(f"wallet book: {len(book.t)} wallets; creator log: {len(crlog.info)} creates", flush=True)
    files = sorted(FT_OUT.glob("*.json.gz"))
    rand = [x for f in files if (x := load_token(f)) is not None]
    skip = {x[0] for x in rand}
    toks = [(0, x) for x in rand] + [(1, x) for x in busy_tokens(skip)]
    print(f"{len(rand)} random launches, {len(toks) - len(rand)} busy launches (fit only)", flush=True)
    for j, (src, x) in enumerate(toks):
        tk = Tok(*x)
        ser = series(tk)
        for T, ls, feats in bars_and_features(tk, book, crlog):
            Y.append(simulate_all(tk, T, ls, ser) + simulate_all(tk, T, ls, ser, lat=0))
            X.append(feats)
            M.append(tk.m)
            T0.append(tk.t0)
            TT.append(T)
            SRC.append(src)
        if j % 500 == 0:
            print(f"{j}/{len(toks)} launches, {len(X)} points", flush=True)
    np.savez_compressed(CACHE, X=np.array(X, np.float32), Y=np.array(Y, np.float32), M=np.array(M),
                        T0=np.array(T0), TT=np.array(TT), SRC=np.array(SRC))
    print(f"saved {len(X)} points from {len(toks)} launches")


# ---------------------------------------------------------------- model, rule, evaluation

def load_cache():
    z = np.load(CACHE)
    return z["X"], z["Y"], z["M"], z["T0"], z["TT"], z["SRC"]


def fit_models(X, Y):
    from sklearn.ensemble import HistGradientBoostingRegressor
    Y = Y[:, :len(POLICIES)]
    ms = []
    for k in range(len(POLICIES)):
        y = np.clip(Y[:, k], -1, 5)                  # winsorize so one 50x does not drive the fit
        ms.append(HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=31,
                                                min_samples_leaf=300, l2_regularization=1.0,
                                                random_state=0).fit(X, y))
    return ms


def trade_once(P, Y, M, TT, thr, policies=None):
    """One position per token: the first bar whose best predicted policy clears thr. Returns per-trade rows."""
    pols = range(P.shape[1]) if policies is None else policies
    best_k = np.array([max(pols, key=lambda k: P[i, k]) for i in range(len(P))])
    best_p = P[np.arange(len(P)), best_k]
    out, done = [], set()
    for i in np.argsort(TT, kind="stable"):
        if M[i] in done or best_p[i] < thr:
            continue
        done.add(M[i])
        out.append((M[i], TT[i], int(best_k[i]), float(Y[i, best_k[i]])))
    return out


def summary(rows, label):
    xs = [r[3] for r in rows]
    if len(xs) < 5:
        return f"{label}: n={len(xs)}"
    m, sd = statistics.mean(xs), statistics.pstdev(xs)
    gains = sum(x for x in xs if x > 0)
    mix = defaultdict(int)
    for r in rows:
        mix[POLICIES[r[2]][0]] += 1
    xs_trim = sorted(xs)[:-max(1, len(xs) // 50)]
    return (f"{label}: n={len(xs)} mean {m:+.1%} med {statistics.median(xs):+.1%} win {np.mean(np.array(xs) > 0):.0%} "
            f"t {m / (sd / math.sqrt(len(xs))) if sd else 0:+.2f} | without top 2%: {statistics.mean(xs_trim):+.1%} | "
            f"top-trade share {max(xs) / gains if gains > 0 else 1:.0%} | mix {dict(mix)}")


def split():
    X, Y, M, T0, TT, SRC = load_cache()
    rnd = SRC == 0
    d = T0 < DESIGN_CUT_T
    dt = np.sort(np.unique(T0[d & rnd]))
    cut = dt[int(len(dt) * 0.7)]
    fit_m, val_m, test_m = d & (T0 < cut), rnd & d & (T0 >= cut), rnd & ~d
    busy_design = ~rnd & d                          # busy launches: fit only, never in val/test
    return X, Y, M, T0, TT, fit_m, val_m, test_m, busy_design


THRS = (0.0, 0.05, 0.10, 0.20, 0.30, 0.50)


def cmd_design() -> None:
    X, Y, M, T0, TT, fm, vm, tm, _ = split()
    print(f"points: fit {fm.sum()} ({len(set(M[fm]))} launches), val {vm.sum()} ({len(set(M[vm]))}), "
          f"test {tm.sum()} ({len(set(M[tm]))})")
    for k, pol in enumerate(POLICIES):
        print(f"  every point, policy {pol[0]}: fit mean {Y[fm, k].mean():+.1%}, val {Y[vm, k].mean():+.1%}")
    ms = fit_models(X[fm], Y[fm])
    P = np.column_stack([m.predict(X[vm]) for m in ms])
    res = []
    for pset in ([0], [1], [2], [3], [4], None):
        for thr in THRS:
            rows = trade_once(P, Y[vm], M[vm], TT[vm], thr, pset)
            if len(rows) >= 30:
                res.append((statistics.mean(r[3] for r in rows), (pset, thr), rows))
    res.sort(key=lambda r: -r[0])
    print("\nvalidation (one entry per token), best rules (policies, predicted-return threshold):")
    for mean, prm, rows in res[:10]:
        print("  ", summary(rows, str(prm)))
    rnd = trade_once(np.random.default_rng(0).random(P.shape), Y[vm], M[vm], TT[vm], 0.999)
    print("  ", summary(rnd, "random bar, random policy (top 0.1% random draw)"))
    if res:
        FROZEN.write_text(json.dumps({"policies": res[0][1][0], "thr": res[0][1][1]}))
        print(f"\nfrozen -> {FROZEN.name}: {res[0][1]}")


def cmd_test() -> None:
    fr = json.loads(FROZEN.read_text())
    X, Y, M, T0, TT, fm, vm, tm, bd = split()
    dm = fm | vm | bd
    ms = fit_models(X[dm], Y[dm])                   # refit on all design launches, same settings
    P = np.column_stack([m.predict(X[tm]) for m in ms])
    rows = trade_once(P, Y[tm], M[tm], TT[tm], fr["thr"], fr["policies"])
    rows.sort(key=lambda r: r[1])
    h = len(rows) // 2
    print("TEST", summary(rows, str((fr["policies"], fr["thr"]))))
    print("  first half ", summary(rows[:h], ""))
    print("  second half", summary(rows[h:], ""))


def main() -> None:
    {"build": cmd_build, "design": cmd_design, "test": cmd_test}[sys.argv[1] if len(sys.argv) > 1 else "design"]()


if __name__ == "__main__":
    main()
