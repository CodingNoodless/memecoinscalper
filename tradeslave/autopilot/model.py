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
PROVEN = dict(trades=100, mean=0.01, max_token_share=0.20)


@dataclass
class Eval:
    trades: int = 0
    tokens: int = 0
    mean: float = 0.0
    median: float = 0.0
    win: float = 0.0
    total: float = 0.0           # sum of net returns (x trade size = SOL)
    top_token_share: float = 1.0


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


def split(samples: list[Sample]):
    t0 = sorted({(s.t0, s.mint) for s in samples})
    n = len(t0)
    a, b = t0[int(n * 0.6)] if n else (0, ""), t0[int(n * 0.8)] if n else (0, "")
    train = [s for s in samples if (s.t0, s.mint) < a]
    val = [s for s in samples if a <= (s.t0, s.mint) < b]
    hold = [s for s in samples if (s.t0, s.mint) >= b]
    return train, val, hold


def _xy(samples, policy):
    import numpy as np
    X = np.array([[v if v is not None else math.nan for v in s.x] for s in samples], dtype=float)
    y = np.array([min(max(s.y[policy], -0.5), 1.0) for s in samples], dtype=float)
    return X, y


def fit(samples, policy):
    from sklearn.ensemble import HistGradientBoostingRegressor
    X, y = _xy(samples, policy)
    model = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=15,
                                          min_samples_leaf=100, l2_regularization=1.0, random_state=0)
    return model.fit(X, y)


def predict(model, samples):
    X, _ = _xy(samples, next(iter(POLICIES)))
    return model.predict(X) if len(samples) else []


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
    return Eval(len(ys), len(per), statistics.mean(ys), statistics.median(ys), sum(y > 0 for y in ys) / len(ys),
                sum(ys), (max(per.values()) / gains) if gains > 0 else 1.0)


def train_cycle(samples: list[Sample], log=print) -> tuple[Card | None, object | None, dict]:
    import numpy as np
    train, val, hold = split(samples)
    info = {"n_samples": len(samples), "train": len(train), "val": len(val), "holdout": len(hold)}
    if len(train) < 2000 or len(val) < 500 or len(hold) < 500:
        log(f"  not enough data yet ({info})")
        return None, None, info
    best = None
    for policy in POLICIES:
        model = fit(train, policy)
        p_train = model.predict(_xy(train, policy)[0])
        p_val = predict(model, val)
        thresholds = sorted({float(np.quantile(p_train, q)) for q in (0.80, 0.90, 0.95, 0.98, 0.99)}
                            | {0.0, 0.01, 0.02, 0.05})
        for thr in thresholds:
            ev = evaluate(trade(val, p_val, policy, thr))
            if ev.trades >= MIN_VAL_TRADES and ev.mean > 0 and (best is None or ev.total > best[0].total):
                best = (ev, policy, thr, model)
        base = evaluate(trade(val, [1.0] * len(val), policy, 0.0))
        log(f"  {policy:22s} val: best so far {best[1] + ' ' + format(best[0].total, '+.2f') if best else '-'} "
            f"| take-everything baseline mean {base.mean * 100:+.1f}%")
    if best is None:
        log("  no policy/threshold made money on validation")
        return None, None, info
    val_ev, policy, thr, model = best
    hold_ev = evaluate(trade(hold, predict(model, hold), policy, thr))
    paper_ok = hold_ev.trades >= MIN_HOLDOUT_TRADES and hold_ev.mean > 0
    proven = (paper_ok and hold_ev.trades >= PROVEN["trades"] and hold_ev.mean >= PROVEN["mean"]
              and hold_ev.median > 0 and hold_ev.top_token_share <= PROVEN["max_token_share"])
    card = Card(version=time.strftime("%Y%m%d-%H%M%S", time.gmtime()), created=time.time(), policy=policy,
                threshold=thr, n_tokens=len({s.mint for s in samples}), n_samples=len(samples),
                exact_share=sum(s.exact for s in samples) / len(samples),
                data_from=min(s.t0 for s in samples), data_to=max(s.t0 for s in samples),
                validation=val_ev, holdout=hold_ev, paper_ok=paper_ok, proven=proven)
    info["holdout_samples"] = hold
    return card, model, info


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
    if old_card is None or old_card.features != FEATURES or old_card.policy not in POLICIES:
        return True
    hold = info["holdout_samples"]
    old = evaluate(trade(hold, predict(old_model, hold), old_card.policy, old_card.threshold))
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
