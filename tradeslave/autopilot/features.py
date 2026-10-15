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
    "launches_10m", "graduations_1h",
] + [f"social_{k}" for k in SOCIAL_KINDS] + [
    # market regime: how recent launches actually did (see Scalper.regime)
    "mkt_med_30m", "mkt_up_30m", "mkt_n_30m", "mkt_med_10m",
]
NAN4 = (math.nan, math.nan, math.nan, math.nan)

# The chart itself, as the model sees it: the last 120 s in 3 s bins and the last
# 300 s in 30 s bins. Prices are log returns relative to the price now (so every
# chart is on the same scale); flows are SOL bought / sold in each bin.
SEQ_BINS, SEQ_STEP = 40, 3.0
LONG_BINS, LONG_STEP = 10, 30.0
CHART = ([f"c{i}_{ch}" for i in range(SEQ_BINS) for ch in ("ret", "buy", "sell", "n")]
         + [f"l{i}_{ch}" for i in range(LONG_BINS) for ch in ("ret", "net")])
BASE = list(FEATURES)
# Who is trading (pump.fun trade events): breadth of buyers, concentration, bots, the dev.
WALLET = ["w_trades", "w_buyers", "w_sellers", "w_unique_ratio", "w_avg_buy", "w_med_buy", "w_big_buys",
          "w_holders", "w_top1", "w_top5", "w_dev_sold_frac", "w_bot_share", "w_buyers_30s", "w_sellers_30s",
          "w_buy_sol_30s", "w_new_buyers_30s"]
FEATURES = BASE + CHART + WALLET


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


def features(tr: TokenTracker, now: float, heat: tuple[float, float] = (math.nan, math.nan),
             regime: tuple = NAN4) -> list[float]:
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
        heat[0], heat[1],
    ] + [1.0 if kind == k else 0.0 for k in SOCIAL_KINDS] + list(regime)
    return vals + chart(tr, now) + wallet_features(tr, now)


def chart(tr: TokenTracker, now: float) -> list[float]:
    """Binned recent chart (see CHART). Bins before the token existed are NaN."""
    price = tr.price
    if price <= 0:
        return [math.nan] * len(CHART)
    lp = math.log(price)
    horizon = max(SEQ_BINS * SEQ_STEP, LONG_BINS * LONG_STEP)
    recent = []
    for tk in reversed(tr.ticks):
        if tk.t < now - horizon:
            break
        recent.append(tk)
    recent.reverse()                                  # oldest first
    before = None                                     # last tick before the window
    n_recent = len(recent)
    if n_recent < len(tr.ticks):
        before = tr.ticks[len(tr.ticks) - n_recent - 1]

    def binned(nbins, step, channels):
        out = []
        start = now - nbins * step
        j = 0
        last_price = before.price if before is not None else None
        for b in range(nbins):
            lo, hi = start + b * step, start + (b + 1) * step
            buy = sell = 0.0
            n = 0
            while j < n_recent and recent[j].t <= hi:
                tk = recent[j]
                if tk.t > lo:
                    n += 1
                    if tk.flow_sol > 0:
                        buy += tk.flow_sol
                    else:
                        sell -= tk.flow_sol
                last_price = tk.price
                j += 1
            alive = hi >= tr.created_at
            ret = math.log(last_price) - lp if (last_price and alive) else math.nan
            if channels == 4:
                out += [ret, buy if alive else math.nan, sell if alive else math.nan, float(n) if alive else math.nan]
            else:
                out += [ret, (buy - sell) if alive else math.nan]
        return out

    return binned(SEQ_BINS, SEQ_STEP, 4) + binned(LONG_BINS, LONG_STEP, 2)


def wallet_features(tr: TokenTracker, now: float) -> list[float]:
    if not tr.wallet_seen:
        return [math.nan] * len(WALLET)
    n = max(tr.w_trades, 1)
    sizes = sorted(tr.w_buy_sizes)
    held = sorted((v for w, v in tr.w_positions.items() if v > 0 and w != tr.creator), reverse=True)
    total_held = sum(held) or 1.0
    bots = sum(c for c in tr.w_counts.values() if c >= 4)
    recent = [r for r in tr.w_recent if r[0] >= now - 30]
    first_seen: dict = {}
    for t, w, buy, _ in tr.w_recent:
        first_seen.setdefault(w, t)
    return [
        float(tr.w_trades), float(len(tr.w_buyers)), float(len(tr.w_sellers)), len(tr.w_counts) / n,
        sum(sizes) / len(sizes) if sizes else 0.0, sizes[len(sizes) // 2] if sizes else 0.0,
        float(sum(1 for x in sizes if x >= 1.0)), float(len(held)),
        held[0] / total_held if held else 0.0, sum(held[:5]) / total_held if held else 0.0,
        (tr.dev_sold_tokens / tr.dev_tokens) if tr.dev_tokens else 0.0, bots / n,
        float(len({w for _, w, b, _ in recent if b})), float(len({w for _, w, b, _ in recent if not b})),
        sum(s for _, _, b, s in recent if b) - sum(s for _, _, b, s in recent if not b),
        float(sum(1 for w, t in first_seen.items() if t >= now - 30)),
    ]


def tradeable(tr: TokenTracker, now: float, min_age: float, max_age: float) -> bool:
    """Hard rules the model can't override."""
    if tr.mayhem or tr.dev_sold or (tr.state is not None and tr.state.complete) or not tr.ticks:
        return False
    return min_age <= now - tr.created_at <= max_age
