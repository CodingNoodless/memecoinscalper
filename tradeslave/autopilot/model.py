"""Train, test and promote models. Walk-forward, by token creation time:

    oldest 60% of tokens -> train | next 20% -> validation (pick policy + threshold)
    | newest 20% -> holdout (never seen while choosing; decides promotion)

Tokens launched within EMBARGO_S after each boundary are dropped, so no label
window or market-regime feature reaches across from one set into the next.

Candidates (entry scorer x threshold x exit) are judged on validation; only one
that made money in BOTH halves of validation is eligible, and the eligible one
with the best worse-half wins (robust to one lucky stretch). Exits are either a
fixed policy (take profit / stop / time) or LEARNED: the bot re-scores an open
position every time it would take a sample and sells when the score falls below
an exit threshold (plus a hard stop and a time cap).

A model trades on paper only when it made money on the holdout. "Proven"
(eligible to be considered for real money, never automatically) needs more.
Every cycle also saves the best validation candidate as the SHADOW model: it
paper-trades in its own wallet regardless of the holdout, so every idea gets a
real forward test.
"""

from __future__ import annotations

import bisect
import collections
import json
import math
import pickle
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..paths import DATA_DIR
from .features import FEATURES
from .samples import POLICIES, Sample

AUTO_DIR = DATA_DIR / "autopilot"
CHAMPION_PKL = AUTO_DIR / "champion.pkl"
CHAMPION_JSON = AUTO_DIR / "champion.json"
SHADOW_PKL = AUTO_DIR / "shadow.pkl"
HISTORY = AUTO_DIR / "history.jsonl"

MIN_VAL_TRADES = 30
MIN_HOLDOUT_TRADES = 20
PROVEN = dict(trades=100, mean=0.01, max_token_share=0.20, t_stat=2.0)
MAX_TOKEN_SHARE = 0.30       # no single token may carry more than this share of a model's gains
EMBARGO_S = 600.0            # > max entry age + max hold: nothing overlaps across the split
ENTRY_QS = (0.90, 0.95, 0.98, 0.99, 0.995)
ABS_THRESHOLDS = (0.0, 0.01, 0.03)    # expected NET return (fees included) a trade must clear
# Learned exits: (exit scorer, sell when the score drops below this train-quantile, hard stop, max hold)
LEARNED_EXITS = [(xs, q, 0.15, hold) for xs in ("same", "f30") for q in (0.5, 0.8) for hold in (120.0, 300.0)]
DEFAULT_ECON = dict(size=0.15, latency=0.8, fee_bps=125, network_fee=0.0003)


@dataclass
class Eval:
    trades: int = 0
    tokens: int = 0
    mean: float = 0.0
    median: float = 0.0
    win: float = 0.0
    total: float = 0.0           # sum of net returns (x trade size = SOL)
    top_token_share: float = 1.0
    t_stat: float = 0.0          # mean / standard error: how unlikely the result is to be luck
    half_means: tuple = ()       # (earlier half, later half) of the trades, by time


@dataclass
class Card:
    version: str
    created: float
    policy: str
    threshold: float
    n_tokens: int
    n_samples: int
    exact_share: float
    data_from: float
    data_to: float
    validation: Eval
    holdout: Eval
    paper_ok: bool
    proven: bool
    features: list = field(default_factory=lambda: list(FEATURES))
    cols: list | None = None          # feature columns the model uses (None = all)
    scorer: str = "policy"            # "policy" (rank of the exit's return), "abs" (expected net return) or "chart60"
    params: dict = field(default_factory=dict)
    exit: dict | None = None          # learned exit: {"scorer", "threshold", "sl", "hold", "cols"}; None = policy rules
    shadow: bool = False              # saved as the shadow model (forward test), not the champion


def split(samples: list[Sample], embargo_s: float = EMBARGO_S):
    t0 = sorted({(s.t0, s.mint) for s in samples})
    n = len(t0)
    a, b = t0[int(n * 0.6)] if n else (0, ""), t0[int(n * 0.8)] if n else (0, "")
    train = [s for s in samples if (s.t0, s.mint) < a]
    val = [s for s in samples if a <= (s.t0, s.mint) < b and s.t0 >= a[0] + embargo_s]
    hold = [s for s in samples if (s.t0, s.mint) >= b and s.t0 >= b[0] + embargo_s]
    return train, val, hold


def _X(samples):
    import numpy as np
    if not samples:
        return np.zeros((0, len(FEATURES)), dtype=np.float32)
    return np.vstack([np.asarray(s.x, dtype=np.float32) for s in samples])


def _clip(y):
    return min(max(y, -0.5), 1.0)


def _xy(samples, policy):
    import numpy as np
    y = np.array([_clip(s.y[policy]) for s in samples], dtype=float)
    return _X(samples), y


GRID = [  # (min_samples_leaf, max_leaf_nodes): strong regularization; many samples come from one token
    (400, 15), (1500, 15), (3000, 7),
]


def _regressor(leaf: int = 400, nodes: int = 15):
    from sklearn.ensemble import HistGradientBoostingRegressor
    return HistGradientBoostingRegressor(max_iter=300, learning_rate=0.03, max_leaf_nodes=nodes,
                                         min_samples_leaf=leaf, l2_regularization=1.0, random_state=0)


def _rank(y):
    import numpy as np
    y = np.asarray(y, dtype=float)
    return np.argsort(np.argsort(y)) / max(len(y) - 1, 1)


HALF_LIFE_DAYS = 3.0     # older charts still teach, but the market drifts: their weight halves every 3 days


def recency_weights(samples):
    """Sample weights: newest token = 1, halving every HALF_LIFE_DAYS (floor 0.05)."""
    import numpy as np
    t0 = np.array([s.t0 for s in samples], dtype=float)
    if not len(t0):
        return t0
    age_days = (t0.max() - t0) / 86400.0
    return np.maximum(0.5 ** (age_days / HALF_LIFE_DAYS), 0.05)


def fit(samples, policy, X=None, leaf=400, nodes=15):
    """Learns to ORDER entries by how well they'll do (rank target): robust to the fat tails."""
    X = _X(samples) if X is None else X
    return _regressor(leaf, nodes).fit(X, _rank([s.y[policy] for s in samples]),
                                       sample_weight=recency_weights(samples))


def fit_abs(samples, policy, X=None, leaf=1500, nodes=15):
    """Learns the expected NET return itself (fees included, tails clipped), so a trade can be
    required to clear the cost: the cost gate a rank target can't give."""
    X = _X(samples) if X is None else X
    return _regressor(leaf, nodes).fit(X, [_clip(s.y[policy]) for s in samples],
                                       sample_weight=recency_weights(samples))


def rank_ic(pred, actual) -> float:
    """Spearman correlation between what the model expected and what happened."""
    import numpy as np
    if len(pred) < 30:
        return float("nan")
    a = np.argsort(np.argsort(pred)); b = np.argsort(np.argsort(actual))
    return float(np.corrcoef(a, b)[0, 1])


def chart_skill(train, hold, Xtr, Xhold, log=print) -> dict:
    """How well the bot reads charts: train a forecaster per horizon on the training
    tokens and score it on the unseen holdout (rank correlation, and how much better
    its top 10% did than average). 0 = no skill."""
    import numpy as np
    from .samples import HORIZONS
    out = {}
    for h in HORIZONS:
        tr = [(i, s.f[h]) for i, s in enumerate(train) if s.f and h in s.f]
        ho = [(i, s.f[h]) for i, s in enumerate(hold) if s.f and h in s.f]
        if len(tr) < 1000 or len(ho) < 300:
            continue
        m = _regressor(400, 15).fit(Xtr[[i for i, _ in tr]], _rank([v for _, v in tr]))
        pred = m.predict(Xhold[[i for i, _ in ho]])
        actual = np.array([v for _, v in ho])
        top = actual[pred >= np.quantile(pred, 0.9)]
        out[h] = {"ic": round(rank_ic(pred, actual), 4), "top10_mean": round(float(np.mean(top)), 4),
                  "all_mean": round(float(np.mean(actual)), 4),
                  "top10_up": round(float(np.mean(top > 0)), 3), "all_up": round(float(np.mean(actual > 0)), 3)}
        log(f"  chart reading, {h:3d}s ahead: rank IC {out[h]['ic']:+.3f} | model's top 10%: "
            f"{out[h]['top10_mean'] * 100:+.1f}% avg, up {out[h]['top10_up']:.0%} vs all {out[h]['all_mean'] * 100:+.1f}%, "
            f"up {out[h]['all_up']:.0%}")
    return out


def predict(model, samples):
    return model.predict(_X(samples)) if len(samples) else []


def trade(samples, preds, policy, threshold) -> list[tuple[str, float, float]]:
    """Bot-like: at most one open position per token; a token is free again after the hold.
    Returns (mint, net return, entry time) per trade."""
    hold = POLICIES[policy]["hold"] + 1.0
    by = collections.defaultdict(list)
    for s, p in zip(samples, preds):
        by[s.mint].append((s.t, p, s.y[policy]))
    out = []
    for mint, rows in by.items():
        rows.sort()
        free = -1e18
        for t, p, y in rows:
            if t >= free and p >= threshold:
                out.append((mint, y, t))
                free = t + hold
    return out


# --- learned exits ------------------------------------------------------------
def simulate_learned(path, t_dec: float, times, scores, exit_thr: float, sl: float, hold: float,
                     size: float, latency: float, fee_bps: int, network_fee: float):
    """(net return, exit decision time) of buying at t_dec (+latency) and selling at the first
    score below exit_thr (scores taken at `times`, as the live bot re-scores), a net loss of `sl`
    on any tick, or after `hold` seconds. Fills like research.sim: exact curve math, fee both
    ways, network fee, fill `latency` after each decision."""
    from ..research.dataset import L
    from ..scalper.curve import CurveState, buy_tokens_out, sell_sol_out
    t_arr, vs, vt = path
    i = bisect.bisect_right(t_arr, t_dec + latency) - 1
    if i < 0:
        return None
    tokens = buy_tokens_out(CurveState(int(vt[i]), int(vs[i])), int(size * L), fee_bps)
    if tokens <= 0:
        return None
    cost = size + network_fee
    t_fill = t_dec + latency
    end = t_fill + hold
    j, k = i + 1, bisect.bisect_right(times, t_fill)
    n_ticks, n_scores = len(t_arr), len(times)
    exit_t = None
    while exit_t is None:
        nt = t_arr[j] if j < n_ticks and t_arr[j] <= end else math.inf
        ns = times[k] if k < n_scores and times[k] <= end else math.inf
        if nt == math.inf and ns == math.inf:
            break
        if nt <= ns:                   # a trade happened: check the hard stop on the new state
            pnl = (sell_sol_out(CurveState(int(vt[j]), int(vs[j])), tokens, fee_bps) / L - network_fee) / cost - 1
            if pnl <= -sl:
                exit_t = float(t_arr[j])
            j += 1
        else:                          # the bot re-scored the position
            if scores[k] < exit_thr:
                exit_t = float(times[k])
            k += 1
    if exit_t is None:
        exit_t = end
    m = max(bisect.bisect_right(t_arr, exit_t + latency) - 1, 0)
    ret = (sell_sol_out(CurveState(int(vt[m]), int(vs[m])), tokens, fee_bps) / L - network_fee) / cost - 1
    return ret, exit_t


def trade_learned(samples, entry_preds, threshold, exit_preds, exit_thr, sl, hold, paths, econ):
    """Like trade(), with learned exits. Tokens without a stored price path are skipped."""
    import numpy as np
    by = collections.defaultdict(list)
    for s, p, x in zip(samples, entry_preds, exit_preds):
        by[s.mint].append((s.t, p, x))
    out = []
    for mint, rows in by.items():
        path = paths.get(mint)
        if path is None:
            continue
        rows.sort()
        times = np.array([r[0] for r in rows])
        xs = np.array([r[2] for r in rows])
        free = -1e18
        for t, p, _ in rows:
            if t >= free and p >= threshold:
                r = simulate_learned(path, t, times, xs, exit_thr, sl, hold, econ["size"], econ["latency"],
                                     econ["fee_bps"], econ["network_fee"])
                if r is None:
                    continue
                out.append((mint, r[0], t))
                free = r[1] + econ["latency"] + 1.0
    return out


def evaluate(trades) -> Eval:
    if not trades:
        return Eval()
    ys = [tr[1] for tr in trades]
    per = collections.Counter()
    for tr in trades:
        per[tr[0]] += tr[1]
    gains = sum(v for v in per.values() if v > 0)
    sd = statistics.pstdev(ys) if len(ys) > 1 else 0.0
    t = statistics.mean(ys) / (sd / math.sqrt(len(ys))) if sd > 0 else 0.0
    halves = ()
    if len(trades) >= 2 and len(trades[0]) > 2:
        by_t = [tr[1] for tr in sorted(trades, key=lambda tr: tr[2])]
        mid = len(by_t) // 2
        halves = (statistics.mean(by_t[:mid]), statistics.mean(by_t[mid:]))
    return Eval(len(ys), len(per), statistics.mean(ys), statistics.median(ys), sum(y > 0 for y in ys) / len(ys),
                sum(ys), (max(per.values()) / gains) if gains > 0 else 1.0, t, halves)


def clean_only(samples: list[Sample], log=print) -> list[Sample]:
    """Train on charts we trust: exact chain data, or recordings made while the feed was up."""
    clean = [s for s in samples if getattr(s, "clean", s.exact)]
    tr, va, ho = split(clean)
    if len(tr) >= 2000 and len(va) >= 500 and len(ho) >= 500:
        log(f"  training on {len(clean)} clean samples (exact or gap-free charts) of {len(samples)}")
        return clean
    log(f"  only {len(clean)} clean samples: using all {len(samples)} (gappy charts mislead the model)")
    return samples


def _cols_of(X, cols):
    return X if cols is None else X[:, cols]


@dataclass
class Candidate:
    val: Eval
    policy: str
    threshold: float
    model: object
    cols: list | None
    scorer: str
    params: dict
    exit: dict | None = None
    exit_model: object | None = None


def run_candidate(c, samples, X, paths, econ):
    """Trades of a candidate (or a loaded card + models) on a sample set."""
    preds = c.model.predict(_cols_of(X, c.cols))
    if not c.exit:
        return trade(samples, preds, c.policy, c.threshold)
    xm = c.model if c.exit["scorer"] == "same" else c.exit_model
    xpreds = preds if c.exit["scorer"] == "same" else xm.predict(_cols_of(X, c.exit.get("cols")))
    return trade_learned(samples, preds, c.threshold, xpreds, c.exit["threshold"], c.exit["sl"], c.exit["hold"],
                         paths or {}, econ)


def eligible(ev: Eval) -> bool:
    """Made money on validation, in both halves, not on one token."""
    return (ev.trades >= MIN_VAL_TRADES and ev.mean > 0 and ev.t_stat >= 1.0 and ev.top_token_share <= MAX_TOKEN_SHARE
            and len(ev.half_means) == 2 and min(ev.half_means) > 0)


def robust_key(c: Candidate) -> float:
    """Rank eligible candidates by their WORSE validation half (total return), not the best number."""
    return min(c.val.half_means) * c.val.trades / 2


SHADOW_LANES = 4             # forward-test this many of each cycle's candidates live (one paper wallet each)
SHADOW_MIN_VAL_TRADES = 60   # a shadow pick needs enough validation trades to trade often enough live to judge


def _family(c: Candidate) -> str:
    return "learned exit" if c.exit else "cost gate (abs)" if c.scorer == "abs" else "fixed exit"


def shadow_picks(good: list[Candidate], enough: list[Candidate], n: int = SHADOW_LANES) -> list[Candidate]:
    """The candidates to forward-test live. Robust first: the best eligible one, then the best
    of each exit family by its WORSE validation half (a big mean from one lucky stretch, or from a
    handful of trades at a 99% threshold, is exactly what the forward test keeps failing on).
    Frequent traders are preferred so each lane collects enough live trades in a day."""
    picks: list[Candidate] = []
    key = lambda c: (min(c.val.half_means) if len(c.val.half_means) == 2 else c.val.mean)
    if good:
        picks.append(max(good, key=robust_key))
    pool = [c for c in enough if c.val.trades >= SHADOW_MIN_VAL_TRADES] or enough
    by_fam = collections.defaultdict(list)
    for c in pool:
        by_fam[_family(c)].append(c)
    ranked = sorted((max(cs, key=key) for cs in by_fam.values()), key=key, reverse=True)
    rest = sorted(pool, key=key, reverse=True)
    for c in ranked + rest:
        if len(picks) >= n:
            break
        if all(c is not p and (c.policy, c.scorer, c.params, c.threshold) != (p.policy, p.scorer, p.params, p.threshold)
               for p in picks):
            picks.append(c)
    return picks


def train_cycle(samples: list[Sample], log=print, paths: dict | None = None,
                econ: dict | None = None) -> tuple[Card | None, object | None, dict]:
    import numpy as np
    from .features import BASE, CHART
    econ = {**DEFAULT_ECON, **(econ or {})}
    samples = clean_only(samples, log)
    train, val, hold = split(samples)
    info = {"n_samples": len(samples), "n_train": len(train), "n_val": len(val), "n_holdout": len(hold)}
    if len(train) < 2000 or len(val) < 500 or len(hold) < 500:
        log(f"  not enough data yet ({info})")
        return None, None, info
    Xtr, Xval, Xhold = _X(train), _X(val), _X(hold)
    info["chart_skill"] = chart_skill(train, hold, Xtr, Xhold, log=log)
    feature_sets = {"all": None, "chart": list(range(len(BASE), len(BASE) + len(CHART)))}
    w_tr = recency_weights(train)
    candidates: list[Candidate] = []

    # exit scorer for learned exits: forecasts the next 30 s, trained on every training sample
    f30_idx = [i for i, s in enumerate(train) if s.f and 30 in s.f]
    f30 = None
    if paths and len(f30_idx) >= 2000:
        f30 = _regressor(1500, 15).fit(Xtr[f30_idx], _rank([train[i].f[30] for i in f30_idx]),
                                       sample_weight=w_tr[f30_idx])
        f30_tr, f30_val = f30.predict(Xtr), f30.predict(Xval)

    # 1) chart reader: forecast the next 60 s, then try it with every exit policy and learned exits
    f_idx = [i for i, s in enumerate(train) if s.f and 60 in s.f]
    for fs, cols in feature_sets.items():
        for leaf, nodes in GRID:
            if len(f_idx) < 2000:
                break
            m = _regressor(leaf, nodes).fit(_cols_of(Xtr, cols)[f_idx], _rank([train[i].f[60] for i in f_idx]),
                                            sample_weight=w_tr[f_idx])
            p_tr, p_val = m.predict(_cols_of(Xtr, cols)), m.predict(_cols_of(Xval, cols))
            params = {"leaf": leaf, "nodes": nodes, "fs": fs}
            for q in ENTRY_QS:
                thr = float(np.quantile(p_tr, q))
                for policy in POLICIES:
                    ev = evaluate(trade(val, p_val, policy, thr))
                    candidates.append(Candidate(ev, policy, thr, m, cols, "chart60", {**params, "q": q}))
                if not paths:
                    continue
                for xs, xq, sl, xhold in LEARNED_EXITS:
                    if xs == "f30" and f30 is None:
                        continue
                    x_tr, x_val = (p_tr, p_val) if xs == "same" else (f30_tr, f30_val)
                    exit_spec = {"scorer": xs, "threshold": float(np.quantile(x_tr, xq)), "sl": sl, "hold": xhold,
                                 "q": xq, "cols": cols if xs == "same" else None}
                    ev = evaluate(trade_learned(val, p_val, thr, x_val, exit_spec["threshold"], sl, xhold, paths, econ))
                    candidates.append(Candidate(ev, f"learned_{xs}_q{xq}_{int(xhold)}s", thr, m, cols, "chart60",
                                                {**params, "q": q}, exit_spec, None if xs == "same" else f30))
    # 2) policy models: learn each exit's own outcome (rank), and its expected net return (cost gate)
    for policy in POLICIES:
        for fs, cols in feature_sets.items():
            for leaf, nodes in GRID:
                m = fit(train, policy, _cols_of(Xtr, cols), leaf, nodes)
                p_tr, p_val = m.predict(_cols_of(Xtr, cols)), m.predict(_cols_of(Xval, cols))
                for q in ENTRY_QS:
                    thr = float(np.quantile(p_tr, q))
                    ev = evaluate(trade(val, p_val, policy, thr))
                    candidates.append(Candidate(ev, policy, thr, m, cols, "policy",
                                                {"leaf": leaf, "nodes": nodes, "fs": fs, "q": q}))
        m = fit_abs(train, policy, Xtr)
        p_val = m.predict(Xval)
        for thr in ABS_THRESHOLDS:
            ev = evaluate(trade(val, p_val, policy, thr))
            candidates.append(Candidate(ev, policy, thr, m, None, "abs", {"leaf": 1500, "nodes": 15, "fs": "all",
                                                                           "min_expected": thr}))
        base = evaluate(trade(val, [1.0] * len(val), policy, 0.0))
        log(f"  {policy:22s} take-everything baseline on validation: mean {base.mean * 100:+.1f}%")

    good = [c for c in candidates if eligible(c.val)]
    info["candidates_tried"] = len(candidates)
    info["candidates_positive_val"] = len(good)
    hurdle = math.sqrt(2 * math.log(max(len(candidates), 2)))
    info["luck_hurdle_t"] = round(hurdle, 2)
    log(f"  {len(candidates)} candidates tried; {len(good)} made money on validation in both halves "
        f"(mean > 0, t >= 1, no token > {MAX_TOKEN_SHARE:.0%} of gains). Best t from pure luck among this many: ~{hurdle:.1f}")
    enough = [c for c in candidates if c.val.trades >= MIN_VAL_TRADES]
    near = None
    if enough:
        near = max(enough, key=lambda c: c.val.mean)
        info["closest_val_mean"] = round(near.val.mean, 4)
        log(f"  closest to profitable on validation: {near.policy} via {near.scorer} {near.params}: {near.val.trades} "
            f"trades, mean {near.val.mean * 100:+.2f}%, median {near.val.median * 100:+.2f}%, win {near.val.win:.0%}")
    families = collections.defaultdict(list)
    for c in enough:
        families[_family(c)].append(c)
    info["family_best_val"] = {}
    for fam, cs in sorted(families.items()):
        b = max(cs, key=lambda c: c.val.mean)
        info["family_best_val"][fam] = {"trades": b.val.trades, "mean": round(b.val.mean, 4),
                                        "halves": [round(h, 4) for h in b.val.half_means], "policy": b.policy}
        log(f"  best {fam:15s} on validation: {b.policy} via {b.scorer}: {b.val.trades} trades, mean "
            f"{b.val.mean * 100:+.2f}%, halves {', '.join(f'{h * 100:+.1f}%' for h in b.val.half_means)}")
    best = max(good, key=robust_key) if good else None
    info["shadows"] = []
    for i, c in enumerate(shadow_picks(good, enough)):
        sc = _card(c, samples, hold, Xhold, paths, econ, shadow=True)
        sc.version = f"{sc.version}-s{i}"
        info["shadows"].append((sc, c.model, c.exit_model))
    if best is None:
        return None, None, info
    card = _card(best, samples, hold, Xhold, paths, econ)
    if best.val.t_stat < hurdle:
        log(f"  note: validation t {best.val.t_stat:+.1f} is below the luck hurdle {hurdle:.1f}; the holdout decides")
    info["holdout_samples"] = hold
    info["exit_model"] = best.exit_model
    return card, best.model, info


def _card(c: Candidate, samples, hold, Xhold, paths, econ, shadow: bool = False) -> Card:
    hold_ev = evaluate(run_candidate(c, hold, Xhold, paths, econ))
    paper_ok = (hold_ev.trades >= MIN_HOLDOUT_TRADES and hold_ev.mean > 0
                and hold_ev.top_token_share <= MAX_TOKEN_SHARE)
    proven = (paper_ok and hold_ev.trades >= PROVEN["trades"] and hold_ev.mean >= PROVEN["mean"]
              and hold_ev.t_stat >= PROVEN["t_stat"] and hold_ev.top_token_share <= PROVEN["max_token_share"])
    exit_spec = None if c.exit is None else {k: v for k, v in c.exit.items()}
    return Card(version=time.strftime("%Y%m%d-%H%M%S", time.gmtime()), created=time.time(),
                policy=c.policy if c.exit is None else "learned", threshold=c.threshold,
                n_tokens=len({s.mint for s in samples}), n_samples=len(samples),
                exact_share=sum(s.exact for s in samples) / len(samples),
                data_from=min(s.t0 for s in samples), data_to=max(s.t0 for s in samples),
                validation=c.val, holdout=hold_ev, paper_ok=paper_ok, proven=proven,
                cols=c.cols, scorer=c.scorer, params=c.params, exit=exit_spec, shadow=shadow)


def score(card: Card, model, X):
    """Model scores for rows of the full feature matrix X."""
    if card.cols is not None:
        X = X[:, card.cols]
    return model.predict(X)


def exit_score(card: Card, model, exit_model, X):
    """Learned-exit scores for rows of X (the entry model itself, or the 30 s forecaster)."""
    if card.exit["scorer"] == "same":
        return score(card, model, X)
    cols = card.exit.get("cols")
    return exit_model.predict(X if cols is None else X[:, cols])


def card_valid(card) -> bool:
    """A card this code can run: right features, a known exit."""
    if card is None or not hasattr(card, "cols") or card.features != FEATURES:
        return False
    if getattr(card, "exit", None):
        return card.exit.get("scorer") in ("same", "f30")
    return card.policy in POLICIES


def load_champion(path: Path | None = None):
    card, model, _ = load_models(path)
    return card, model


def load_models(path: Path | None = None):
    """(card, model, exit_model) from a saved champion / shadow file."""
    path = path or CHAMPION_PKL
    if not path.exists():
        return None, None, None
    try:
        blob = pickle.loads(path.read_bytes())
        return blob["card"], blob["model"], blob.get("exit_model")
    except Exception:  # noqa: BLE001
        return None, None, None


def save_champion(card: Card, model, directory: Path | None = None, exit_model=None, name: str = "champion") -> None:
    directory = directory or AUTO_DIR
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f"{name}.pkl.tmp"
    tmp.write_bytes(pickle.dumps({"card": card, "model": model, "exit_model": exit_model}))
    tmp.replace(directory / f"{name}.pkl")
    (directory / f"{name}.json").write_text(json.dumps(asdict(card), indent=2))


def shadow_name(lane: int) -> str:
    """File name of a shadow lane's model: shadow.pkl, shadow_1.pkl, ..."""
    return "shadow" if lane == 0 else f"shadow_{lane}"


def save_shadow(info: dict, directory: Path | None = None, log=print) -> bool:
    """Save this cycle's shadow picks, one per lane, for forward paper testing."""
    shadows = info.get("shadows") or []
    for lane, (card, model, exit_model) in enumerate(shadows):
        save_champion(card, model, directory, exit_model=exit_model, name=shadow_name(lane))
        v, h = card.validation, card.holdout
        log(f"  shadow lane {lane}: model {card.version} ({card.policy} via {card.scorer} {card.params}) | "
            f"validation {v.trades} trades {v.mean * 100:+.2f}% (halves "
            f"{', '.join(f'{x * 100:+.1f}%' for x in v.half_means)}) | holdout {h.trades} trades {h.mean * 100:+.2f}%")
    return bool(shadows)


def compete(card: Card, model, info: dict, log=print, paths: dict | None = None, econ: dict | None = None) -> bool:
    """New model vs current champion on the SAME newest holdout. True = new one wins."""
    if not card.paper_ok:
        return False
    old_card, old_model, old_exit = load_models()
    if not card_valid(old_card):
        return True
    hold = info["holdout_samples"]
    econ = {**DEFAULT_ECON, **(econ or {})}
    old_c = Candidate(old_card.validation, old_card.policy, old_card.threshold, old_model, old_card.cols,
                      old_card.scorer, old_card.params, getattr(old_card, "exit", None), old_exit)
    old = evaluate(run_candidate(old_c, hold, _X(hold), paths, econ))
    log(f"  champion {old_card.version} on the new holdout: {old.trades} trades, mean {old.mean * 100:+.2f}%, "
        f"total {old.total:+.2f} | challenger total {card.holdout.total:+.2f}")
    return card.holdout.total > old.total


def record_history(card: Card | None, info: dict, promoted: bool, directory: Path | None = None) -> dict:
    directory = directory or AUTO_DIR
    directory.mkdir(parents=True, exist_ok=True)
    skip = ("holdout_samples", "shadows", "exit_model")
    row = {"t": time.time(), "promoted": promoted, **{k: v for k, v in info.items() if k not in skip}}
    if card is not None:
        row.update(version=card.version, policy=card.policy, threshold=card.threshold, n_tokens=card.n_tokens,
                   exact_share=round(card.exact_share, 3), paper_ok=card.paper_ok, proven=card.proven,
                   val=asdict(card.validation), holdout=asdict(card.holdout), exit=card.exit)
    shadows = [{"version": sh.version, "policy": sh.policy, "scorer": sh.scorer, "exit": sh.exit, "params": sh.params,
                "val": asdict(sh.validation), "holdout": asdict(sh.holdout)} for sh, _, _ in info.get("shadows") or []]
    if shadows:
        row["shadow"], row["shadows"] = shadows[0], shadows
    with (directory / "history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    return row
