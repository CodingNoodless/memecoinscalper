"""Strategy W: earn Polymarket liquidity rewards with a small account (two-sided resting orders near the mid).

    uv run python -m tradeslave.research.poly_rewards snapshot      # expected reward share per market, right now

Sourced (docs.polymarket.com/market-makers/liquidity-rewards): each minute the book is sampled; an order at
distance s (cents) from the mid scores S = ((v - s) / v)^2 * size if s < v (v = max spread) and size >= min size.
Q_one = Yes bids + No asks, Q_two = Yes asks + No bids (the No book mirrors the Yes book, so only the Yes book
is read). Q_min = max(min(Q1, Q2), max(Q1, Q2) / 3) for mids in [0.10, 0.90], else min(Q1, Q2). The daily pool is
split by each maker's share of summed Q_min. Payout >= $1/day only.
Our quote: MIN size (the market's min) on both sides at distance D_TICKS ticks... see quote(); the rest of the book
is treated as ONE competitor (sum of everyone's Q_min <= Q_min of the summed book: our share is conservative).
Capital: a Yes bid at mid - d plus a No bid at (1 - mid) - d locks size * (1 - 2d) USDC.
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from ..paths import DATA_DIR

CLOB = "https://clob.polymarket.com"
OUT = DATA_DIR / "poly_rewards"


def pages(path):
    s, cur, out = requests.Session(), "", []
    while True:
        d = s.get(CLOB + path, params={"next_cursor": cur}, timeout=30).json()
        out += d.get("data") or []
        cur = d.get("next_cursor")
        if not d.get("data") or not cur or cur == "LTE=":
            return out


def score(levels, mid, v, side, min_size):
    """Sum of S * size over price levels within v cents of mid (levels: [(price, size)])."""
    q = 0.0
    for p, sz in levels:
        s = (mid - p if side == "bid" else p - mid) * 100
        if 0 <= s < v and sz >= min_size:
            q += ((v - s) / v) ** 2 * sz
    return q


def qmin(q1, q2, mid):
    return max(min(q1, q2), max(q1, q2) / 3) if 0.10 <= mid <= 0.90 else min(q1, q2)


def evaluate(book, rate, v, min_size, d_cents):
    bids = [(float(x["price"]), float(x["size"])) for x in book["bids"]]
    asks = [(float(x["price"]), float(x["size"])) for x in book["asks"]]
    if not bids or not asks:
        return None
    bb, ba = max(p for p, _ in bids), min(p for p, _ in asks)
    mid = (bb + ba) / 2
    others = qmin(score(bids, mid, v, "bid", min_size), score(asks, mid, v, "ask", min_size), mid)
    if d_cents >= v:
        return None
    ours = ((v - d_cents) / v) ** 2 * min_size              # two-sided: Q1 = Q2 = ours
    share = ours / (ours + others)
    capital = min_size * (1 - 2 * d_cents / 100)
    return dict(mid=mid, spread=(ba - bb) * 100, others=others, share=share, usd_day=rate * share,
                capital=capital, per_dollar=rate * share / capital)


def snapshot(d_cents=None):
    OUT.mkdir(parents=True, exist_ok=True)
    rw = {r["condition_id"]: r for r in pages("/rewards/markets/current")}
    sm = {m["condition_id"]: m for m in pages("/sampling-markets") if m.get("accepting_orders")}
    todo = [(c, r, sm[c]) for c, r in rw.items() if c in sm and (r.get("total_daily_rate") or 0) > 0]
    print(f"{len(rw)} reward markets, {len(todo)} open with tokens", flush=True)
    s = requests.Session()

    def one(x):
        c, r, m = x
        yes = m["tokens"][0]["token_id"]                       # Yes (or first outcome); the other book mirrors it
        try:
            b = s.get(CLOB + "/book", params={"token_id": yes}, timeout=20).json()
        except Exception:
            return None
        v, ms, rate = r["rewards_max_spread"], r["rewards_min_size"], r["total_daily_rate"]
        res = {}
        for d in (d_cents,) if d_cents is not None else (0.5, 1.0, 2.0, v / 2):
            e = evaluate(b, rate, v, ms, d)
            if e:
                res[str(d)] = e
        return dict(c=c, q=m["question"], end=m.get("end_date_iso"), rate=rate, v=v, min_size=ms,
                    tick=m.get("minimum_tick_size"), yes=yes, res=res, t=time.time())

    with ThreadPoolExecutor(16) as ex:
        rows = [x for x in ex.map(one, todo) if x and x["res"]]
    path = OUT / f"snapshot_{int(time.time())}.json"
    path.write_text(json.dumps(rows))
    key = "1.0"
    rows = [r for r in rows if key in r["res"]]
    rows.sort(key=lambda r: -r["res"][key]["per_dollar"])
    print(f"{len(rows)} markets evaluated -> {path.name}. Top by reward per $ locked (quote 1c from mid):")
    for r in rows[:30]:
        e = r["res"][key]
        print(f"  ${r['rate']:>6.0f}/d v {r['v']:.1f}c min {r['min_size']:>4.0f} mid {e['mid']:.3f} spr {e['spread']:.1f}c "
              f"share {e['share']:.0%} -> ${e['usd_day']:.2f}/d on ${e['capital']:.0f} ({e['per_dollar']:.1%}/d) {r['q'][:50]}")
    cheap = [r for r in rows if r["res"][key]["capital"] <= 25]
    tot, cap = 0.0, 0.0
    for r in cheap:
        if cap + r["res"][key]["capital"] > 100:
            break
        cap += r["res"][key]["capital"]
        tot += r["res"][key]["usd_day"]
    print(f"best $100 portfolio (snapshot, before fills): ${tot:.2f}/day on ${cap:.0f}")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    if cmd == "snapshot":
        snapshot()


if __name__ == "__main__":
    main()
