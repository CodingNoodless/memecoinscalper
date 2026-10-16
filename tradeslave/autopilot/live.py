"""The champion (or shadow) model inside the live (paper) scalper."""

from __future__ import annotations

import logging
import math
from pathlib import Path

from ..scalper.config import ScalpConfig
from ..scalper.strategy import Signal
from .features import FEATURES, NAN4, features, tradeable
from .model import CHAMPION_PKL, SHADOW_PKL, card_valid, load_models
from .samples import POLICIES, SNAP_EVERY_S, SNAP_MAX_AGE_S, SNAP_MIN_AGE_S

log = logging.getLogger("autopilot")


class ModelGate:
    """Entry (and learned-exit) decisions of a saved model.

    shadow=True: the forward-test model (data/autopilot/shadow.pkl). It trades its
    own paper wallet whatever its holdout said, so every cycle's best idea gets a
    real-time test on data no model has seen."""

    def __init__(self, cfg: ScalpConfig, path: Path | None = None, shadow: bool = False):
        self.cfg, self.shadow = cfg, shadow
        self.path = path or (SHADOW_PKL if shadow else CHAMPION_PKL)
        self.card, self.model, self.exit_model, self.mtime = None, None, None, None
        self.heat_source = None            # set by the engine: Scalper.heat
        self.regime_source = None          # set by the engine: Scalper.regime
        self.last_exit_score: dict[str, float] = {}
        self.last_entry_score: dict[str, float] = {}
        self.reload()

    @property
    def status(self) -> str:
        if self.card is None:
            return "no shadow model yet" if self.shadow else "learning (no model yet)"
        exits = " with learned exits" if getattr(self.card, "exit", None) else ""
        if self.shadow:
            return f"shadow-testing model {self.card.version}{exits}"
        if not self.card.paper_ok:
            return f"learning (model {self.card.version} not good enough to trade)"
        return f"paper-trading model {self.card.version}{exits}" + (" (passed proof gates)" if self.card.proven else "")

    def reload(self) -> bool:
        """Load the model if it changed on disk. True when a new one was loaded."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False
        if mtime == self.mtime:
            return False
        card, model, exit_model = load_models(self.path)
        self.mtime = mtime
        if not card_valid(card) or (getattr(card, "exit", None) and card.exit["scorer"] != "same"
                                    and exit_model is None):
            log.warning("Ignoring model at %s: unreadable or built for different features", self.path)
            self.card, self.model, self.exit_model = None, None, None
            return False
        self.card, self.model, self.exit_model = card, model, exit_model
        self.last_exit_score.clear()
        self.last_entry_score.clear()
        log.info("Autopilot model loaded: %s", self.status)
        return True

    def _x(self, tr, now: float):
        import numpy as np
        heat = self.heat_source(now) if self.heat_source else (math.nan, math.nan)
        regime = self.regime_source(now) if self.regime_source else NAN4
        x = [[v if v is not None else math.nan for v in features(tr, now, heat, regime)]]
        if len(x[0]) != len(FEATURES):
            return None
        return np.asarray(x, dtype=np.float32)

    def entry(self, tr, now: float) -> Signal:
        if self.card is None or self.model is None:
            return Signal(False, "no_model_yet", {})
        if not self.card.paper_ok and not self.shadow:
            return Signal(False, "model_not_proven", {})
        if not tradeable(tr, now, self.cfg.min_age_s, self.cfg.max_entry_age_s):
            return Signal(False, "outside_model_window", {})
        # Score only where training took snapshots: on a fresh curve update, at most every
        # SNAP_EVERY_S per token. Scoring every update would take the max of many noisy
        # scores, i.e. many more (and worse) entries than the holdout ever saw.
        if tr.last_update_t != now:
            return Signal(False, "not_snapshot_time", {})
        if 0 < now - self.last_entry_score.get(tr.mint, -1e18) < SNAP_EVERY_S:
            return Signal(False, "not_snapshot_time", {})
        self.last_entry_score[tr.mint] = now
        if len(self.last_entry_score) > 20_000:
            self.last_entry_score = {m: t for m, t in self.last_entry_score.items() if now - t < 900}
        X = self._x(tr, now)
        if X is None:
            return Signal(False, "feature_mismatch", {})
        from .model import score
        pred = float(score(self.card, self.model, X)[0])
        if pred < self.card.threshold:
            return Signal(False, "model_says_no", {})
        exit_spec = getattr(self.card, "exit", None)
        if exit_spec:
            rules = {"tp": 9.0, "sl": exit_spec["sl"], "hold": exit_spec["hold"], "learned": True}
        else:
            rules = dict(POLICIES[self.card.policy])
        self.last_exit_score.pop(tr.mint, None)
        return Signal(True, "model", {"model": self.card.version, "policy": self.card.policy, "scorer": self.card.scorer,
                                      "expected": round(pred, 4), "policy_rules": rules,
                                      **({"shadow": True} if self.shadow else {})})

    def should_exit(self, tr, now: float) -> Signal:
        """Learned exit: re-score the open position the way training snapshots were taken
        (on a fresh curve update, at most every SNAP_EVERY_S, inside the snapshot window)
        and sell when the score falls below the exit threshold."""
        exit_spec = getattr(self.card, "exit", None) if self.card is not None else None
        if not exit_spec or tr.last_update_t != now:
            return Signal(False, "hold", {})
        if now - self.last_exit_score.get(tr.mint, -1e18) < SNAP_EVERY_S:
            return Signal(False, "hold", {})
        if not tradeable(tr, now, SNAP_MIN_AGE_S, SNAP_MAX_AGE_S):
            return Signal(False, "hold", {})       # training had no scores here either: stop / time cap only
        X = self._x(tr, now)
        if X is None:
            return Signal(False, "hold", {})
        self.last_exit_score[tr.mint] = now
        from .model import exit_score
        x = float(exit_score(self.card, self.model, self.exit_model, X)[0])
        if x < exit_spec["threshold"]:
            return Signal(True, "model_exit", {"exit_score": round(x, 4), "exit_threshold": round(exit_spec["threshold"], 4)})
        return Signal(False, "hold", {})
