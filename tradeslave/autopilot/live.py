"""The champion model inside the live (paper) scalper."""

from __future__ import annotations

import logging
import math
from pathlib import Path

from ..scalper.config import ScalpConfig
from ..scalper.strategy import Signal
from .features import FEATURES, features, tradeable
from .model import CHAMPION_PKL, load_champion
from .samples import POLICIES

log = logging.getLogger("autopilot")


class ModelGate:
    def __init__(self, cfg: ScalpConfig, path: Path | None = None):
        self.cfg, self.path = cfg, path or CHAMPION_PKL
        self.card, self.model, self.mtime = None, None, None
        self.heat_source = None            # set by the engine: Scalper.heat
        self.reload()

    @property
    def status(self) -> str:
        if self.card is None:
            return "learning (no model yet)"
        if not self.card.paper_ok:
            return f"learning (model {self.card.version} not good enough to trade)"
        return f"paper-trading model {self.card.version}" + (" (passed proof gates)" if self.card.proven else "")

    def reload(self) -> bool:
        """Load the champion if it changed on disk. True when a new one was loaded."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False
        if mtime == self.mtime:
            return False
        card, model = load_champion(self.path)
        self.mtime = mtime
        if card is not None and not hasattr(card, "cols"):
            card = None                      # built by an older version
        if card is None or card.features != FEATURES or card.policy not in POLICIES:
            log.warning("Ignoring champion at %s: unreadable or built for different features", self.path)
            self.card, self.model = None, None
            return False
        self.card, self.model = card, model
        log.info("Autopilot model loaded: %s", self.status)
        return True

    def entry(self, tr, now: float) -> Signal:
        if self.card is None or self.model is None:
            return Signal(False, "no_model_yet", {})
        if not self.card.paper_ok:
            return Signal(False, "model_not_proven", {})
        if not tradeable(tr, now, self.cfg.min_age_s, self.cfg.max_entry_age_s):
            return Signal(False, "outside_model_window", {})
        heat = self.heat_source(now) if self.heat_source else (math.nan, math.nan)
        x = [[v if v is not None else math.nan for v in features(tr, now, heat)]]
        if len(x[0]) != len(FEATURES):
            return Signal(False, "feature_mismatch", {})
        import numpy as np
        from .model import score
        pred = float(score(self.card, self.model, np.asarray(x, dtype=np.float32))[0])
        if pred < self.card.threshold:
            return Signal(False, "model_says_no", {})
        rules = dict(POLICIES[self.card.policy])
        return Signal(True, "model", {"model": self.card.version, "policy": self.card.policy, "scorer": self.card.scorer,
                                      "expected": round(pred, 4), "policy_rules": rules})
