"""Model inputs computed from a live TokenTracker.

The same function runs when building training data (replaying recordings)
and when trading live, so the model always sees exactly what the bot sees.
Everything here is known at decision time: no future data.
"""

from __future__ import annotations

import math

from ..scalper.tracker import TokenTracker

SOCIAL_KINDS = ("unknown", "none", "tweet", "profile", "community", "other")

FEATURES = [
    "age", "n_updates", "vs_open", "dd_peak", "rise", "since_peak", "vsol",
    "net5", "net10", "net30", "net60", "upd5", "upd10", "upd30",
    "buys10", "sells10", "buys30", "sells30", "max_sell30", "max_buy30",
    "quiet", "since_sell", "chg5", "chg10", "chg30",
    "early_supply", "dev_supply", "dev_buy_sol", "creator_launches", "creator_sells",
    "twitter_reuse", "desc_len", "has_website", "has_telegram", "hour_sin", "hour_cos",
] + [f"social_{k}" for k in SOCIAL_KINDS]


def _social_kind(tr: TokenTracker) -> str:
    s = tr.socials
    if not s or str(s.get("fetched")) not in ("True", "true", "1"):
        return "unknown"
    kind = s.get("twitter_kind")
    if kind in (None, "", "None"):
        return "none"
    return kind if kind in SOCIAL_KINDS else "other"


def _truthy(v) -> bool:
    return v not in (None, "", "None", False, "False", 0, "0")


def features(tr: TokenTracker, now: float) -> list[float]:
    ticks = tr.ticks
    price = tr.price
    launch = tr.launch_price or price or 1e-18
    peak = tr.peak_price or price or 1e-18

    def window(seconds):
        out = []
        for tk in reversed(ticks):
            if tk.t < now - seconds:
                break
            out.append(tk)
        return out

    w5, w10, w30, w60 = window(5), window(10), window(30), window(60)

    def chg(seconds):
        ref = None
        for tk in reversed(ticks):
            if tk.t <= now - seconds:
                ref = tk
                break
        return price / ref.price - 1 if ref and ref.price > 0 else 0.0

    last_sell = next((tk.t for tk in reversed(ticks) if tk.flow_sol < 0), None)
    early = tr.early_supply_pct(3.0)
    hour = (now % 86400) / 3600.0
    s = tr.socials or {}
    kind = _social_kind(tr)
    vals = [
        now - tr.created_at, float(len(ticks)), price / launch - 1, 1 - price / peak, peak / launch - 1,
        now - (tr.peak_t or now), tr.state.virtual_sol if tr.state else 0.0,
        sum(t.flow_sol for t in w5), sum(t.flow_sol for t in w10), sum(t.flow_sol for t in w30),
        sum(t.flow_sol for t in w60), float(len(w5)), float(len(w10)), float(len(w30)),
        float(sum(t.flow_sol > 0 for t in w10)), float(sum(t.flow_sol < 0 for t in w10)),
        float(sum(t.flow_sol > 0 for t in w30)), float(sum(t.flow_sol < 0 for t in w30)),
        -min([t.flow_sol for t in w30] + [0.0]), max([t.flow_sol for t in w30] + [0.0]),
        now - (tr.last_update_t or now), (now - last_sell) if last_sell is not None else 999.0,
        chg(5), chg(10), chg(30),
        early if early is not None else math.nan, tr.dev_supply_pct, tr.initial_buy_sol,
        float(tr.creator_launches), float(len(tr.creator_sell_times)),
        float(tr.twitter_reuse), float(s.get("desc_len") or 0) if str(s.get("desc_len", "0")).isdigit() else 0.0,
        float(_truthy(s.get("website"))), float(_truthy(s.get("telegram"))),
        math.sin(2 * math.pi * hour / 24), math.cos(2 * math.pi * hour / 24),
    ] + [1.0 if kind == k else 0.0 for k in SOCIAL_KINDS]
    return vals


def tradeable(tr: TokenTracker, now: float, min_age: float, max_age: float) -> bool:
    """Hard rules the model can't override."""
    if tr.mayhem or tr.dev_sold or (tr.state is not None and tr.state.complete) or not tr.ticks:
        return False
    return min_age <= now - tr.created_at <= max_age
