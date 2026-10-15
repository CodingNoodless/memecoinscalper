"""Train, test and promote models. Walk-forward, by token creation time:

    oldest 60% of tokens -> train | next 20% -> validation (pick policy + threshold)
    | newest 20% -> holdout (never seen while choosing; decides promotion)

A model trades on paper only when it made money on the holdout. "Proven"
(eligible to be considered for real money, never automatically) needs more.
"""

from __future__ import annotations

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
HISTORY = AUTO_DIR / "history.jsonl"

MIN_VAL_TRADES = 30
MIN_HOLDOUT_TRADES = 20
PROVEN = dict(trades=100, mean=0.01, max_token_share=0.20, t_stat=2.0)
MAX_TOKEN_SHARE = 0.30       # no single token may carry more than this share of a model's gains


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
    scorer: str = "policy"            # "policy" (trained on the exit's return) or "chart60" (60 s forecaster)
    params: dict = field(default_factory=dict)


def split(samples: list[Sample]):
    t0 = sorted({(s.t0, s.mint) for s in samples})
    n = len(t0)
    a, b = t0[int(n * 0.6)] if n else (0, ""), t0[int(n * 0.8)] if n else (0, "")
    train = [s for s in samples if (s.t0, s.mint) < a]
    val = [s for s in samples if a <= (s.t0, s.mint) < b]
    hold = [s for s in samples if (s.t0, s.mint) >= b]
    return train, val, hold


def _X(samples):
    import numpy as np
    if not samples:
        return np.zeros((0, len(FEATURES)), dtype=np.float32)
    return np.vstack([np.asarray(s.x, dtype=np.float32) for s in samples])


def _xy(samples, policy):
    import numpy as np
    y = np.array([min(max(s.y[policy], -0.5), 1.0) for s in samples], dtype=float)
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


def fit(samples, policy, X=None, leaf=400, nodes=15):
    """Learns to ORDER entries by how well they'll do (rank target): robust to the fat tails."""
    X = _X(samples) if X is None else X
    return _regressor(leaf, nodes).fit(X, _rank([s.y[policy] for s in samples]))


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


def trade(samples, preds, policy, threshold) -> list[tuple[str, float]]:
    """Bot-like: at most one open position per token; a token is free again after the hold."""
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
                out.append((mint, y))
                free = t + hold
    return out


def evaluate(trades) -> Eval:
    if not trades:
        return Eval()
    ys = [y for _, y in trades]
    per = collections.Counter()
    for m, y in trades:
        per[m] += y
    gains = sum(v for v in per.values() if v > 0)
    sd = statistics.pstdev(ys) if len(ys) > 1 else 0.0
    t = statistics.mean(ys) / (sd / math.sqrt(len(ys))) if sd > 0 else 0.0
    return Eval(len(ys), len(per), statistics.mean(ys), statistics.median(ys), sum(y > 0 for y in ys) / len(ys),
                sum(ys), (max(per.values()) / gains) if gains > 0 else 1.0, t)


def clean_only(samples: list[Sample], log=print) -> list[Sample]:
    """Train on charts we trust: exact chain data, or recordings made while the feed was up."""
    clean = [s for s in samples if getattr(s, "clean", s.exact)]
    tr, va, ho = split(clean)
    if len(tr) >= 2000 and len(va) >= 500 and len(ho) >= 500:
        log(f"  training on {len(clean)} clean samples (exact or gap-free charts) of {len(samples)}")
        return clean
    log(f"  only {len(clean)} clean samples: using all {len(samples)} (gappy charts mislead the model)")
    return samples


def train_cycle(samples: list[Sample], log=print) -> tuple[Card | None, object | None, dict]:
    import numpy as np
    from .features import BASE
    samples = clean_only(samples, log)
    train, val, hold = split(samples)
    info = {"n_samples": len(samples), "n_train": len(train), "n_val": len(val), "n_holdout": len(hold)}
    if len(train) < 2000 or len(val) < 500 or len(hold) < 500:
        log(f"  not enough data yet ({info})")
        return None, None, info
    Xtr, Xval, Xhold = _X(train), _X(val), _X(hold)
    info["chart_skill"] = chart_skill(train, hold, Xtr, Xhold, log=log)
    feature_sets = {"all": None, "chart": list(range(len(BASE), len(FEATURES)))}

    def cols_of(X, cols):
        return X if cols is None else X[:, cols]

    candidates = []          # (val Eval, policy, threshold, model, cols, scorer, params)
    # 1) chart reader: forecast the next 60 s, then try it with every exit policy
    f_idx = [i for i, s in enumerate(train) if s.f and 60 in s.f]
    for fs, cols in feature_sets.items():
        for leaf, nodes in GRID:
            if len(f_idx) < 2000:
                break
            m = _regressor(leaf, nodes).fit(cols_of(Xtr, cols)[f_idx], _rank([train[i].f[60] for i in f_idx]))
            p_tr, p_val = m.predict(cols_of(Xtr, cols)), m.predict(cols_of(Xval, cols))
            for policy in POLICIES:
                for q in (0.90, 0.95, 0.98, 0.99, 0.995):
                    thr = float(np.quantile(p_tr, q))
                    ev = evaluate(trade(val, p_val, policy, thr))
                    candidates.append((ev, policy, thr, m, cols, "chart60", {"leaf": leaf, "nodes": nodes, "fs": fs, "q": q}))
    # 2) policy models: learn each exit's own outcome
    for policy in POLICIES:
        for fs, cols in feature_sets.items():
            for leaf, nodes in GRID:
                m = fit(train, policy, cols_of(Xtr, cols), leaf, nodes)
                p_tr, p_val = m.predict(cols_of(Xtr, cols)), m.predict(cols_of(Xval, cols))
                for q in (0.90, 0.95, 0.98, 0.99, 0.995):
                    thr = float(np.quantile(p_tr, q))
                    ev = evaluate(trade(val, p_val, policy, thr))
                    candidates.append((ev, policy, thr, m, cols, "policy", {"leaf": leaf, "nodes": nodes, "fs": fs, "q": q}))
        base = evaluate(trade(val, [1.0] * len(val), policy, 0.0))
        log(f"  {policy:22s} take-everything baseline on validation: mean {base.mean * 100:+.1f}%")
    good = [c for c in candidates if c[0].trades >= MIN_VAL_TRADES and c[0].mean > 0 and c[0].t_stat >= 1.0
            and c[0].top_token_share <= MAX_TOKEN_SHARE]
    info["candidates_tried"] = len(candidates)
    info["candidates_positive_val"] = len(good)
    log(f"  {len(candidates)} candidates tried; {len(good)} made money on validation "
        f"(mean > 0, t >= 1, no token > {MAX_TOKEN_SHARE:.0%} of gains)")
    enough = [c for c in candidates if c[0].trades >= MIN_VAL_TRADES]
    if enough:
        near = max(enough, key=lambda c: c[0].mean)
        info["closest_val_mean"] = round(near[0].mean, 4)
        log(f"  closest to profitable on validation: {near[1]} via {near[5]} {near[6]}: {near[0].trades} trades, "
            f"mean {near[0].mean * 100:+.2f}%, median {near[0].median * 100:+.2f}%, win {near[0].win:.0%}")
    if not good:
        return None, None, info
    val_ev, policy, thr, model, cols, scorer, params = max(good, key=lambda c: c[0].total)
    hold_ev = evaluate(trade(hold, model.predict(cols_of(Xhold, cols)), policy, thr))
    paper_ok = (hold_ev.trades >= MIN_HOLDOUT_TRADES and hold_ev.mean > 0
                and hold_ev.top_token_share <= MAX_TOKEN_SHARE)
    proven = (paper_ok and hold_ev.trades >= PROVEN["trades"] and hold_ev.mean >= PROVEN["mean"]
              and hold_ev.t_stat >= PROVEN["t_stat"] and hold_ev.top_token_share <= PROVEN["max_token_share"])
    card = Card(version=time.strftime("%Y%m%d-%H%M%S", time.gmtime()), created=time.time(), policy=policy,
                threshold=thr, n_tokens=len({s.mint for s in samples}), n_samples=len(samples),
                exact_share=sum(s.exact for s in samples) / len(samples),
                data_from=min(s.t0 for s in samples), data_to=max(s.t0 for s in samples),
                validation=val_ev, holdout=hold_ev, paper_ok=paper_ok, proven=proven,
                cols=cols, scorer=scorer, params=params)
    info["holdout_samples"] = hold
    return card, model, info


def score(card: Card, model, X):
    """Model scores for rows of the full feature matrix X."""
    if card.cols is not None:
        X = X[:, card.cols]
    return model.predict(X)


def load_champion(path: Path | None = None):
    path = path or CHAMPION_PKL
    if not path.exists():
        return None, None
    try:
        blob = pickle.loads(path.read_bytes())
        return blob["card"], blob["model"]
    except Exception:  # noqa: BLE001
        return None, None


def save_champion(card: Card, model, directory: Path | None = None) -> None:
    directory = directory or AUTO_DIR
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / "champion.pkl.tmp"
    tmp.write_bytes(pickle.dumps({"card": card, "model": model}))
    tmp.replace(directory / "champion.pkl")
    (directory / "champion.json").write_text(json.dumps(asdict(card), indent=2))


def compete(card: Card, model, info: dict, log=print) -> bool:
    """New model vs current champion on the SAME newest holdout. True = new one wins."""
    if not card.paper_ok:
        return False
    old_card, old_model = load_champion()
    if old_card is None or not hasattr(old_card, "cols") or old_card.features != FEATURES \
            or old_card.policy not in POLICIES:
        return True
    hold = info["holdout_samples"]
    old = evaluate(trade(hold, score(old_card, old_model, _X(hold)), old_card.policy, old_card.threshold))
    log(f"  champion {old_card.version} on the new holdout: {old.trades} trades, mean {old.mean * 100:+.2f}%, "
        f"total {old.total:+.2f} | challenger total {card.holdout.total:+.2f}")
    return card.holdout.total > old.total


def record_history(card: Card | None, info: dict, promoted: bool, directory: Path | None = None) -> dict:
    directory = directory or AUTO_DIR
    directory.mkdir(parents=True, exist_ok=True)
    row = {"t": time.time(), "promoted": promoted, **{k: v for k, v in info.items() if k != "holdout_samples"}}
    if card is not None:
        row.update(version=card.version, policy=card.policy, threshold=card.threshold, n_tokens=card.n_tokens,
                   exact_share=round(card.exact_share, 3), paper_ok=card.paper_ok, proven=card.proven,
                   val=asdict(card.validation), holdout=asdict(card.holdout))
    with (directory / "history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    return row
