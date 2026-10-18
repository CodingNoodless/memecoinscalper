"""Strategy N5: learn WHEN persistent skilled traders buy (the pattern), then trade it ourselves.

    uv run python -m tradeslave.research.imitate profile          # how the skilled wallets trade
    uv run python -m tradeslave.research.imitate design            # fit + grid on design launches only
    uv run python -m tradeslave.research.imitate test              # judge the frozen rule once on test

Not copy trading: the bot never watches the skilled wallets. A model learns the token state (exact
trade-level features) in which persistent skilled late traders make their first buy, from the
5,140-launch exact-trade set (research.traders). The bot then buys ANY token whose state looks like
that, at our latency, on launches it has never seen: the random sample of research.full_trades
(first 30 min, every trade). Pre-registered in the vault note "2026-10-18 imitation policy (N5)".

Skilled = wallets with >= 8 first buys at age >= 30 s in the training set, top 20% by ROI on those
entries, AND ROI > 0 in both time halves (persistent). Label: state just before a skilled wallet's
first buy on a token (1) vs state before any other trade at age >= 30 s (0, subsampled).

Fill model (exact curve, token-based state): decide at a trade, our buy lands after every trade up to
slot + LAT; exits trigger on the observed state after each trade and fill LAT slots later; 1.25% fee
per side, 0.002 SOL per transaction, our own size inside the curve. Holding at graduation sells at
the last curve state. Design = launches before dip_runner.DESIGN_CUT_T, test = after.
"""

from __future__ import annotations

import gzip
import itertools
import json
import math
import random
import statistics
import sys
from bisect import bisect_right
from collections import defaultdict, deque

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

from .backfill_trades import OUT as BF_OUT
from .dip_runner import DESIGN_CUT_T
from .full_trades import OUT as FT_OUT
from .traders import load, positions

VS0, VT0 = 30e9, 1073e12
K = VS0 * VT0
FEE, PRIO, SIZE = 0.0125, 0.002, 0.25
LAT = 3                         # slots between our decision and our fill (~1.2 s, measured feed + send)
MIN_AGE, MAX_AGE = 30, 1200     # entry window (s after creation)
GRAD_VS = 115e9                 # curve completes around 115 SOL real+virtual (dip_runner.GRAD_PRICE)
MODEL_PATH = BF_OUT.parent / "imitate_model.json"

FEATS = ["age", "curve_sol", "ret_10s", "ret_30s", "ret_120s", "from_peak", "buy_sol_30s", "sell_sol_30s",
         "net_30s", "buyers_30s", "buyers_total", "trades_30s", "sellers_30s", "top_buyer_share", "creator_left",
         "max_buy_30s", "idle_s", "ret_since_first_min"]


# ---------------------------------------------------------------- common trade format

def norm_training() -> dict[str, list[tuple]]:
    """mint -> [(t, slot, wallet, is_buy, sol, tokens_whole)] oldest first (research.traders set)."""
    out = {}
    for m, ts in load().items():
        out[m] = [(r["t"], r["slot"], r["w"], bool(r["b"]), r["sol"], abs(r["tok"])) for r in ts]
    return out


def norm_full() -> dict[str, tuple[float, list[tuple]]]:
    """mint -> (t0, trades) for the random full_trades sample, curve trades only."""
    out = {}
    for p in FT_OUT.glob("*.json.gz"):
        d = json.load(gzip.open(p, "rt", encoding="utf-8"))
        tr = [(x[2], x[0], x[3], x[4], x[5], x[6]) for x in d["trades"]]
        amm_from = next((i for i, x in enumerate(d["trades"]) if x[7]), None)
        if amm_from is not None:
            tr = tr[:amm_from] + [("GRAD",)]
        if tr and tr[0] != ("GRAD",):
            out[d["m"]] = (d["t0"], tr)
    return out


# ---------------------------------------------------------------- streaming state

class State:
    """Token state from trades seen so far (token-flow reconstruction of the curve)."""

    def __init__(self, first: tuple):
        self.tc, self.s0 = first[0], first[1]
        self.vt = VT0
        self.grp: set = set()
        self.grp_tok = 0.0
        self.grp_bought = 0.0
        self.peak = VS0
        self.path: list[tuple[float, float]] = []
        self.win: deque = deque()
        self.buyers: set = set()
        self.per_w: dict = defaultdict(float)
        self.last_t = first[0]

    @property
    def vs(self) -> float:
        return K / self.vt

    def add(self, r: tuple) -> None:
        t, slot, w, b, sol, tok = r
        raw = tok * 1e6
        self.vt += -raw if b else raw
        self.vt = max(self.vt, 1e12)
        if slot == self.s0:
            self.grp.add(w)
        if w in self.grp:
            self.grp_tok += raw if b else -raw
            if b:
                self.grp_bought += raw
        elif b:
            self.buyers.add(w)
            self.per_w[w] += sol
        vs = self.vs
        self.peak = max(self.peak, vs)
        self.path.append((t, vs))
        self.win.append((t, w, b, sol))
        self.last_t = t

    def features(self, t: float) -> list[float]:
        while self.win and self.win[0][0] < t - 30:
            self.win.popleft()
        vs = self.vs
        times = [p[0] for p in self.path]

        def back(sec):
            i = bisect_right(times, t - sec)
            base = self.path[i - 1][1] if i else VS0
            return (vs / base) ** 2 - 1

        b30 = sum(x[3] for x in self.win if x[2])
        s30 = sum(x[3] for x in self.win if not x[2])
        tot = sum(self.per_w.values()) or 1.0
        i60 = bisect_right(times, self.tc + 60)
        v60 = self.path[i60 - 1][1] if i60 else VS0
        return [t - self.tc, vs / 1e9, back(10), back(30), back(120), (vs / self.peak) ** 2 - 1, b30, s30, b30 - s30,
                len({x[1] for x in self.win if x[2]}), len(self.buyers), len(self.win),
                len({x[1] for x in self.win if not x[2]}), max(self.per_w.values()) / tot if self.per_w else 0.0,
                self.grp_tok / self.grp_bought if self.grp_bought else 0.0,
                max((x[3] for x in self.win if x[2]), default=0.0), t - self.last_t, (vs / v60) ** 2 - 1]


# ---------------------------------------------------------------- skilled wallets and labels

def skilled_wallets(data: dict) -> tuple[set, dict]:
    t0s = {m: ts[0][0] for m, ts in data.items()}
    order = sorted(data, key=lambda m: t0s[m])
    cut = t0s[order[len(order) // 2]]
    raw = load()
    hist = defaultdict(lambda: {"A": [0.0, 0.0], "B": [0.0, 0.0], "n": 0})
    pos_all = {}
    for m in order:
        pos = positions(raw[m])
        pos_all[m] = pos
        h = "A" if t0s[m] < cut else "B"
        for w, p in pos.items():
            if p["in"] > 0 and p["t_first"] >= MIN_AGE:
                hist[w][h][0] += p["pnl"]
                hist[w][h][1] += p["in"]
                hist[w]["n"] += 1
    ws = [w for w, x in hist.items() if x["n"] >= 8 and x["A"][1] > 0 and x["B"][1] > 0]
    roi = {w: (hist[w]["A"][0] + hist[w]["B"][0]) / (hist[w]["A"][1] + hist[w]["B"][1]) for w in ws}
    ranked = sorted(ws, key=lambda w: -roi[w])
    top = ranked[: len(ranked) // 5]
    return {w for w in top if hist[w]["A"][0] > 0 and hist[w]["B"][0] > 0}, pos_all


def training_rows(data: dict, skilled: set, rng: random.Random):
    X, y = [], []
    for m, ts in data.items():
        st = State(ts[0])
        seen = set()
        for r in ts:
            t, w, b = r[0], r[2], r[3]
            age = t - st.tc
            if MIN_AGE <= age <= 900 and r[1] != st.s0:
                first_buy = b and w not in seen
                if first_buy and w in skilled:
                    X.append(st.features(t))
                    y.append(1)
                elif rng.random() < 0.15:
                    X.append(st.features(t))
                    y.append(0)
            if b:
                seen.add(w)
            st.add(r)
    return np.array(X), np.array(y)


def fit():
    data = norm_training()
    skilled, _ = skilled_wallets(data)
    X, y = training_rows(data, skilled, random.Random(5))
    clf = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=50,
                                         l2_regularization=1.0, random_state=0)
    clf.fit(X, y)
    return clf, skilled, X, y


# ---------------------------------------------------------------- policy simulation on unseen launches

def scores_for(tr: list, clf) -> dict[int, float]:
    """Model score at each eligible decision point i (state before trade i = what we know then)."""
    if not tr or tr[0] == ("GRAD",):
        return {}
    st = State(tr[0])
    st.add(tr[0])
    idx, X = [], []
    for i in range(1, len(tr)):
        r = tr[i]
        if r == ("GRAD",) or r[0] - st.tc > MAX_AGE:
            break
        if r[0] - st.tc >= MIN_AGE and st.vs < GRAD_VS * 0.85:
            idx.append(i)
            X.append(st.features(r[0]))
        st.add(r)
    if not X:
        return {}
    return dict(zip(idx, clf.predict_proba(np.array(X))[:, 1]))


def simulate(tr: list, sc: dict, thr: float, tp: float, sl: float, hold: float, fee_mult: float = 1.0,
             lat: int = LAT):
    """One entry per token at the first decision point scoring >= thr. Returns net SOL or None."""
    first = next((i for i in sorted(sc) if sc[i] >= thr), None)
    if first is None:
        return None
    st = State(tr[0])
    n = len(tr)
    for r in tr[:first]:
        st.add(r)
    i = first
    # 2) our buy lands after every trade with slot <= decision slot + lat
    dslot = tr[i - 1][1]
    while i < n and tr[i] != ("GRAD",) and tr[i][1] <= dslot + lat:
        st.add(tr[i])
        i += 1
    net = SIZE * (1 - FEE * fee_mult) * 1e9
    vs_e = st.vs + 0.0
    tokens = (K / vs_e) * net / (vs_e + net)
    entry_px = (vs_e + net) ** 2
    t_entry = st.last_t

    def value(vs_obs):
        vs2 = vs_obs + net
        vt2 = K / vs2
        return vs2 * tokens / (vt2 + tokens) / 1e9 * (1 - FEE * fee_mult)

    # 3) exit: trigger on observed state after each trade, fill lat slots later
    trigger = None
    while i < n:
        r = tr[i]
        if r == ("GRAD",):
            return value(st.vs) - SIZE - 2 * PRIO
        st.add(r)
        i += 1
        px = (st.vs + net) ** 2
        if trigger is None and (px >= entry_px * (1 + tp) or px <= entry_px * (1 - sl) or r[0] - t_entry >= hold):
            trigger = r[1]
        if trigger is not None and (i >= n or tr[i] == ("GRAD",) or tr[i][1] > trigger + lat):
            return value(st.vs) - SIZE - 2 * PRIO
    return value(st.vs) - SIZE - 2 * PRIO          # window ends: mark at the last state (dump)


def stats(xs) -> str:
    if len(xs) < 5:
        return f"n={len(xs)}"
    m, sd = statistics.mean(xs), statistics.pstdev(xs)
    gains = sum(x for x in xs if x > 0)
    return (f"n={len(xs):4d} ROI {m / SIZE * 100:+6.1f}%/trade med {statistics.median(xs) / SIZE * 100:+6.1f}% "
            f"win {sum(x > 0 for x in xs) / len(xs):4.0%} t {m / (sd / math.sqrt(len(xs))) if sd else 0:+5.2f} "
            f"top-share {max(xs) / gains if gains > 0 else 1:.0%}")


GRID = list(itertools.product((0.3, 0.6, 1.0, 2.0), (0.2, 0.35), (120, 600, 1500)))
QUANTS = (0.99, 0.97, 0.90)


def split(full: dict, train_mints: set):
    toks = [(t0, m, tr) for m, (t0, tr) in full.items() if m not in train_mints]
    toks.sort()
    return [x for x in toks if x[0] < DESIGN_CUT_T], [x for x in toks if x[0] >= DESIGN_CUT_T]


def cmd_design() -> None:
    clf, skilled, X, y = fit()
    data_mints = set(load())
    full = norm_full()
    design, test = split(full, data_mints)
    print(f"skilled persistent late traders: {len(skilled)}; training states {len(y)} ({y.sum()} skilled entries)")
    print(f"random full-trade launches: design {len(design)}, test {len(test)} (training launches excluded)")
    sc = {m: scores_for(tr, clf) for t0, m, tr in design}
    scores = [v for d in sc.values() for v in d.values()]
    qs = {q: float(np.quantile(scores, q)) for q in QUANTS}
    print("decision-point score quantiles (design):", {q: round(v, 4) for q, v in qs.items()})
    base = [v for t0, m, tr in design if (v := simulate(tr, sc[m], 0.0, 0.6, 0.35, 600)) is not None]
    print(f"baseline (enter every token at first decision point >= 30 s, TP 60/SL 35/10 min): {stats(base)}")
    res = []
    for q, thr in qs.items():
        for tp, sl, hold in GRID:
            xs = [v for t0, m, tr in design if (v := simulate(tr, sc[m], thr, tp, sl, hold)) is not None]
            if len(xs) >= 30:
                res.append((statistics.mean(xs), (q, tp, sl, hold), xs))
    res.sort(key=lambda r: -r[0])
    print("\ntop design rules (score quantile, TP, SL, hold s):")
    for mean, prm, xs in res[:10]:
        print(f"  {prm}: {stats(xs)}")
    if res:
        MODEL_PATH.write_text(json.dumps({"rule": res[0][1], "thr": qs[res[0][1][0]], "design_n": len(design)}))
        print(f"\nfrozen best design rule -> {MODEL_PATH.name}: {res[0][1]}")


def cmd_test() -> None:
    frozen = json.loads(MODEL_PATH.read_text())
    q, tp, sl, hold = frozen["rule"]
    thr = frozen["thr"]
    clf, *_ = fit()
    design, test = split(norm_full(), set(load()))
    sc = {m: scores_for(tr, clf) for t0, m, tr in test}
    xs = [v for t0, m, tr in test if (v := simulate(tr, sc[m], thr, tp, sl, hold)) is not None]
    h = len(xs) // 2
    print(f"TEST rule {frozen['rule']} thr {thr:.4f}: {stats(xs)}")
    print(f"  halves: {stats(xs[:h])} | {stats(xs[h:])}")
    print(f"  2x fees: {stats([v for t0, m, tr in test if (v := simulate(tr, sc[m], thr, tp, sl, hold, 2.0)) is not None])}")
    print(f"  latency 8 slots: {stats([v for t0, m, tr in test if (v := simulate(tr, sc[m], thr, tp, sl, hold, lat=8)) is not None])}")


def cmd_profile() -> None:
    data = norm_training()
    skilled, pos_all = skilled_wallets(data)
    rows = {"skilled": [], "others": []}
    for m, pos in pos_all.items():
        for w, p in pos.items():
            if p["in"] > 0 and p["t_first"] >= MIN_AGE:
                rows["skilled" if w in skilled else "others"].append(p)
    print(f"persistent skilled late traders: {len(skilled)}")
    for k, ps in rows.items():
        r = [p["pnl"] / p["in"] for p in ps]
        wins = [x for x in r if x > 0]
        losses = [x for x in r if x <= 0]
        hold = [p["t_last_sell"] - p["t_first"] for p in ps if p["t_last_sell"] is not None]
        big = sorted(ps, key=lambda p: -p["in"])[: max(1, len(ps) // 5)]
        sized = sum(p["pnl"] for p in big) / sum(p["in"] for p in big)
        print(f"\n{k}: {len(ps)} positions, ROI {sum(p['pnl'] for p in ps) / sum(p['in'] for p in ps):+.1%}, "
              f"win rate {len(wins) / len(r):.0%}, avg win {statistics.mean(wins) if wins else 0:+.1%}, "
              f"avg loss {statistics.mean(losses) if losses else 0:+.1%}, median {statistics.median(r):+.1%}")
        print(f"  entry age med {statistics.median(p['t_first'] for p in ps):.0f} s, buy size med "
              f"{statistics.median(p['in'] for p in ps):.2f} SOL, buys/position {statistics.mean(p['n_buy'] for p in ps):.2f}, "
              f"sells/position {statistics.mean(p['n_sell'] for p in ps):.2f}, hold med {statistics.median(hold) if hold else 0:.0f} s, "
              f"still holding at window end {sum(p['held_out'] for p in ps) / len(ps):.0%}")
        print(f"  ROI on their largest 20% of bets {sized:+.1%}; share of P&L from top 5% positions "
              f"{sum(sorted((p['pnl'] for p in ps), reverse=True)[: max(1, len(ps) // 20)]) / (sum(p['pnl'] for p in ps) or 1):.0%}")
        lt = [x for x, p in zip(r, ps) if p["t_last_sell"] is not None and p["t_last_sell"] - p["t_first"] < 60]
        print(f"  quick exits (< 60 s): {len(lt) / len(ps):.0%} of positions, ROI mean {statistics.mean(lt) if lt else 0:+.1%}")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "design"
    {"profile": cmd_profile, "design": cmd_design, "test": cmd_test}[cmd]()


if __name__ == "__main__":
    main()
