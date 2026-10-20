"""Strategy W (US venue): earn Kalshi Liquidity Incentive Program (LIP) rewards with a small account.

    uv run python -m tradeslave.research.kalshi_lip snapshot    # our expected reward share per program, right now
    uv run python -m tradeslave.research.kalshi_lip paper       # forward paper test W1 (runs until stopped)
    uv run python -m tradeslave.research.kalshi_lip score       # W1 so far (add "front" for W2)
    uv run python -m tradeslave.research.kalshi_lip history     # W0: maker edge per series from settled trades

Sourced (help.kalshi.com/en/articles/13823851): the book is sampled every second. Per side, the orders that
help reach Target Size (walking from the best bid) qualify. Reference price = first level where cumulative size
reaches Target / 5. Orders at or better than it score size x 1.0, worse ones size x DF^ticks. Share per side =
own score / all qualifying score; snapshot score = yes share + no share; it counts only if BOTH sides reach
Target Size. Reward = period score share x period reward x counted-snapshot fraction. Eligible: regular US
members (SSN on file above IRS thresholds). Payout >= $1 per program.
Our quote: N contracts joined at the best bid on each side (Yes bid at b, No bid at 100 - a), capital N x (b + 100 - a)
cents. period_reward is in 1/100 cents (assumption: 3,000,000 = $300, consistent with the $1-$1,000/day range).
"""

from __future__ import annotations

import json
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

from ..paths import DATA_DIR

API = "https://api.elections.kalshi.com/trade-api/v2"
OUT = DATA_DIR / "kalshi_lip"
N = 20


def ts(x: str) -> float:
    return datetime.fromisoformat(re.sub(r"\.\d+", "", x.replace("Z", "+00:00"))).timestamp()


def programs():
    s, cur, rows = requests.Session(), None, []
    while True:
        p = {"limit": 1000}
        if cur:
            p["cursor"] = cur
        d = s.get(f"{API}/incentive_programs", params=p, timeout=30).json()
        rows += d.get("incentive_programs") or []
        cur = d.get("next_cursor")
        if not cur or not d.get("incentive_programs"):
            return rows


def side_score(levels, target, df, ours):
    """levels: [(price_cents, size)] best first. Returns (our share, target met).

    Kalshi's filed rules (LIP, CFTC filing 2026-02-11): whole price levels are added from the best bid down until
    the cumulative size >= Target; ALL bids at those levels qualify (no time priority inside a level). Score =
    size x DF^(ticks below the reference price). We join the best level (multiplier 1.0). Reference price: the help
    centre version (first level reaching Target / 5); at the best level our multiplier is 1.0 either way.
    """
    cum, ref, inc = 0.0, None, []
    for p, sz in levels:
        cum += sz
        inc.append((p, sz))
        if ref is None and cum >= target / 5:
            ref = p
        if cum >= target:
            break
    if cum < target:
        return 0.0, False
    tot = sum(sz * (1.0 if p >= ref else df ** (ref - p)) for p, sz in inc)
    return ours / (tot + ours), True


def evaluate(book, prog):
    yes = sorted(((round(float(p) * 100), float(q)) for p, q in book.get("yes_dollars") or []), reverse=True)
    no = sorted(((round(float(p) * 100), float(q)) for p, q in book.get("no_dollars") or []), reverse=True)
    if not yes or not no:
        return None
    target, df = float(prog["target_size_fp"]), prog["discount_factor_bps"] / 1e4
    sy, my = side_score(yes, target, df, N)
    sn, mn = side_score(no, target, df, N)
    days = (ts(prog["end_date"]) - ts(prog["start_date"])) / 86400
    per_day = prog["period_reward"] / 1e4 / max(days, 1e-9)
    usd_day = per_day * (sy + sn) / 2 if (my and mn) else 0.0
    cap = N * (yes[0][0] + no[0][0]) / 100
    return dict(bid=yes[0][0], ask=100 - no[0][0], met=my and mn, share=(sy + sn) / 2, pool_day=per_day,
                usd_day=usd_day, capital=cap, per_dollar=usd_day / cap if cap else 0, days=days)


def snapshot():
    OUT.mkdir(parents=True, exist_ok=True)
    now = time.time()
    ps = [p for p in programs() if p["incentive_type"] == "liquidity" and ts(p["start_date"]) <= now < ts(p["end_date"])
          and ts(p["end_date"]) - now > 6 * 3600]          # skip programs ending within 6 h (15-min series etc.)
    print(f"{len(ps)} liquidity programs live now with > 6 h left", flush=True)
    s = requests.Session()

    def one(p):
        for k in range(4):
            try:
                r = s.get(f"{API}/markets/{p['market_ticker']}/orderbook", timeout=20)
                if r.status_code == 429:
                    time.sleep(2 + k)
                    continue
                e = evaluate(r.json().get("orderbook_fp") or {}, p)
                return dict(p, ev=e) if e else None
            except Exception:
                time.sleep(1)
        return None

    with ThreadPoolExecutor(8) as ex:
        rows = [r for r in ex.map(one, ps) if r]
    (OUT / f"snapshot_{int(now)}.json").write_text(json.dumps(rows))
    met = [r for r in rows if r["ev"]["met"]]
    print(f"{len(rows)} with a two-sided book; target met on both sides: {len(met)}; "
          f"pools there ${sum(r['ev']['pool_day'] for r in met):.0f}/day")
    met.sort(key=lambda r: -r["ev"]["per_dollar"])
    for r in met[:25]:
        e = r["ev"]
        print(f"  {r['market_ticker']:34s} pool ${e['pool_day']:6.1f}/d target {float(r['target_size_fp']):5.0f} "
              f"bid/ask {e['bid']}/{e['ask']} share {e['share']:.1%} -> ${e['usd_day']:.2f}/d on ${e['capital']:.0f} "
              f"({e['per_dollar']:.1%}/d)")
    tot = cap = 0.0
    for r in met:
        if cap + r["ev"]["capital"] > 100:
            continue
        cap += r["ev"]["capital"]
        tot += r["ev"]["usd_day"]
    print(f"best $100 portfolio (snapshot, {N} contracts per side, before fills): ${tot:.2f}/day on ${cap:.0f}")
    return rows, len(ps)


# ---------------------------------------------------------------- forward paper test (W1)
# Pre-registered in the vault note "2026-10-19 maker rewards (W)". Every LOOP_S: for each held program, read the
# book and the trades since the last poll. Our resting orders: N contracts joined at the BACK of the queue at the
# best Yes bid and the best No bid. Fill when (a) volume traded at our price since we joined >= the queue ahead of
# us, (b) a trade prints through our price, or (c) the opposite best price crosses ours. Then hold the filled
# contracts to settlement (no more quoting on that side). Rewards accrue each poll: share x pool / (86400 / LOOP_S),
# counted only if the target is met on both sides. Costs: maker fee 0.0175 x C x P x (1 - P) on every fill
# (conservative: not all series charge it). Portfolio: re-chosen every RESELECT_S from a fresh snapshot, top
# programs by reward per $ locked, total locked (quotes + open positions) <= BUDGET.
LOOP_S, RESELECT_S, BUDGET = 60, 3600, 100.0
MM = "mm" in sys.argv[2:]             # W4: W2 that keeps quoting after fills (inventory cap), Yes + No lots net to $1
JOIN = "join" in sys.argv[2:]         # W5: W4 but join the best at the back of the queue (W1 queue tracking)
FRONT = "front" in sys.argv[2:] or (MM and not JOIN)  # W2: improve the best bid by 1c when the spread allows
CAP = 2 * N                           # W4: max net contracts on one side per market
MODE = ("mmjoin" if JOIN else "mm") if MM else "front" if FRONT else ""
STATE = OUT / (f"paper_{MODE}_state.json" if MODE else "paper_state.json")
LEDGER = OUT / (f"paper_{MODE}.jsonl" if MODE else "paper.jsonl")


def _book(s, ticker):
    r = s.get(f"{API}/markets/{ticker}/orderbook", timeout=20)
    b = r.json().get("orderbook_fp") or {}
    yes = sorted(((round(float(p) * 100), float(q)) for p, q in b.get("yes_dollars") or []), reverse=True)
    no = sorted(((round(float(p) * 100), float(q)) for p, q in b.get("no_dollars") or []), reverse=True)
    return yes, no


def _trades(s, ticker, since):
    r = s.get(f"{API}/markets/trades", params={"ticker": ticker, "min_ts": int(since), "limit": 1000}, timeout=20)
    return r.json().get("trades") or []


def _log(ev):
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(dict(ev, t=time.time())) + "\n")


def _select(st):
    """Fresh snapshot -> re-choose the quoted programs within budget (open positions stay until settlement)."""
    snap, asked = snapshot()                                  # this process's own snapshot, not the newest file
    if len(snap) < 0.5 * asked:                               # rate-limited / failed scan: keep the current set
        _log({"k": "skip_select", "rows": len(snap), "asked": asked})
        return
    locked = sum(p["cost"] for p in st["pos"])
    cands = sorted((r for r in snap if r["ev"]["met"] and r["ev"]["usd_day"] >= 0.25), key=lambda r: -r["ev"]["per_dollar"])
    want = {}
    for r in cands:
        if locked + r["ev"]["capital"] > BUDGET:
            continue
        want[r["market_ticker"]] = r
        locked += r["ev"]["capital"]
    for tk in list(st["quotes"]):
        if tk in want:
            st["quotes"][tk]["prog"] = want[tk]
            continue
        q = st["quotes"][tk]
        if any(p["tk"] == tk for p in st["pos"]):           # stop quoting, keep the entry until the position settles
            for side, o in q["sides"].items():
                if not (o and o.get("filled")):
                    q["sides"][side] = {"filled": True, "dropped": True}
        else:
            del st["quotes"][tk]
        _log({"k": "drop", "tk": tk})
    for tk, r in want.items():
        if tk not in st["quotes"]:
            st["quotes"][tk] = dict(prog=r, sides={"yes": None, "no": None}, last=time.time())
            _log({"k": "select", "tk": tk, "pool_day": r["ev"]["pool_day"], "est_usd_day": r["ev"]["usd_day"]})


def _net(st, tk):
    """W4: a Yes lot and a No lot in the same market pay exactly $1 per pair, so Kalshi nets them into cash."""
    while True:
        y = next((p for p in st["pos"] if p["tk"] == tk and p["side"] == "yes"), None)
        n = next((p for p in st["pos"] if p["tk"] == tk and p["side"] == "no"), None)
        if not (y and n):
            return
        k = min(y["n"], n["n"])
        cy, cn = y["cost"] * k / y["n"], n["cost"] * k / n["n"]
        st["pnl"] += k - cy - cn
        _log({"k": "net", "tk": tk, "n": k, "pnl": k - cy - cn})
        for p, c in ((y, cy), (n, cn)):
            p["n"] -= k
            p["cost"] -= c
        st["pos"] = [p for p in st["pos"] if p["n"] > 0]


def _inv(st, tk):
    return sum(p["n"] * (1 if p["side"] == "yes" else -1) for p in st["pos"] if p["tk"] == tk)


def _step(s, st, tk, q):
    prog = q["prog"]
    try:
        yes, no = _book(s, tk)
        trades = _trades(s, tk, q["last"] - 2)
    except Exception:
        return
    now = time.time()
    seen = set(q.get("seen", []))
    trades = [x for x in trades if x["trade_id"] not in seen]
    q["seen"] = list(seen | {x["trade_id"] for x in trades})[-3000:]
    books = {"yes": yes, "no": no}
    for side in ("yes", "no"):
        o = q["sides"][side]
        if o and o.get("filled"):
            continue
        lv = books[side]
        opp = books["no" if side == "yes" else "yes"]
        if o:
            # trades hitting our side: taker bought the other outcome at (100 - our price) or lower for us
            hit = [x for x in trades if x["taker_side"] != side]
            px_of = lambda x: round(float(x["yes_price_dollars" if side == "yes" else "no_price_dollars"]) * 100)
            at = sum(float(x["count_fp"]) for x in hit if px_of(x) == o["px"])
            through = any(px_of(x) < o["px"] for x in hit)
            crossed = bool(opp) and (100 - opp[0][0]) <= o["px"]
            o["ahead"] -= at
            if o["ahead"] < 0 or through or crossed:
                fee = math.ceil(0.0175 * o["n"] * o["px"] / 100 * (1 - o["px"] / 100) * 100) / 100
                o["filled"] = True
                st["pos"].append(dict(tk=tk, side=side, n=o["n"], px=o["px"], cost=o["n"] * o["px"] / 100 + fee, t=now))
                _log({"k": "fill", "tk": tk, "side": side, "px": o["px"], "n": o["n"], "fee": fee,
                      "why": "queue" if o["ahead"] < 0 else "through" if through else "crossed"})
                if not MM:
                    continue
                _net(st, tk)
                q["sides"][side] = o = None                    # W4: re-quote this side (inventory permitting)
        if MM and _inv(st, tk) * (1 if side == "yes" else -1) >= CAP:
            q["sides"][side] = None                            # W4: inventory cap reached on this side
            continue
        if not lv:
            q["sides"][side] = None
            continue
        best = lv[0][0]
        if FRONT:
            ask = 100 - opp[0][0] if opp else 100
            if o and o["px"] == best and o["ahead"] <= 0:      # we are (still) the best bid, alone at the front
                continue
            if ask - best >= 2:
                q["sides"][side] = dict(px=best + 1, n=N, ahead=0.0)
            elif not o or o["px"] != best:
                q["sides"][side] = dict(px=best, n=N, ahead=lv[0][1])
        elif not o or o["px"] != best:                       # join (or re-join) the back of the best-bid queue
            q["sides"][side] = dict(px=best, n=N, ahead=lv[0][1])
    # rewards for this interval: only quoting sides that are still resting at the best price
    ev = evaluate({"yes_dollars": [[p / 100, z] for p, z in yes], "no_dollars": [[p / 100, z] for p, z in no]}, prog)
    if ev and ev["met"]:
        target, df = float(prog["target_size_fp"]), prog["discount_factor_bps"] / 1e4
        share = cons = 0.0
        for side, lv in books.items():
            o = q["sides"][side]
            if o and not o.get("filled") and lv and o["px"] >= lv[0][0]:   # W2 may sit 1c above the others
                sh = side_score(lv, target, df, N)[0] / 2
                share += sh
                if max(o["ahead"], 0) < target:        # conservative: time priority inside the level, so we count
                    cons += sh                         # only while the queue ahead of us is below Target Size
        dt = min(now - q["last"], 3 * LOOP_S)
        rew = ev["pool_day"] * share * dt / 86400
        st["rewards_cons"] = st.get("rewards_cons", 0.0) + ev["pool_day"] * cons * dt / 86400
        st["rewards"] += rew
        st["by_tk"][tk] = st["by_tk"].get(tk, 0.0) + rew
    q["last"] = now


def _settle(s, st):
    keep = []
    for p in st["pos"]:
        try:
            m = s.get(f"{API}/markets/{p['tk']}", timeout=20).json()["market"]
        except Exception:
            keep.append(p)
            continue
        if m.get("result") in ("yes", "no"):
            pay = p["n"] * (1.0 if m["result"] == p["side"] else 0.0)
            st["pnl"] += pay - p["cost"]
            _log({"k": "settle", "tk": p["tk"], "side": p["side"], "result": m["result"], "pnl": pay - p["cost"]})
        else:
            keep.append(p)
    st["pos"] = keep
    for tk in list(st["quotes"]):
        q = st["quotes"][tk]
        if ts(q["prog"]["end_date"]) <= time.time() or all(o and o.get("filled") for o in q["sides"].values()):
            if not any(p["tk"] == tk for p in st["pos"]):
                del st["quotes"][tk]


def paper():
    OUT.mkdir(parents=True, exist_ok=True)
    st = json.loads(STATE.read_text()) if STATE.exists() else dict(quotes={}, pos=[], rewards=0.0, pnl=0.0, by_tk={},
                                                                     started=time.time(), last_sel=0)
    st.setdefault("cons_from", time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()))
    if "fix_from" not in st:                       # share formula corrected to the filed rules (whole levels count)
        st["fix_from"], st["rewards_at_fix"] = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), st["rewards"]
    s = requests.Session()
    while True:
        if time.time() - st["last_sel"] > RESELECT_S:
            try:
                _select(st)
            except Exception as e:
                print("select failed:", e, flush=True)
            st["last_sel"] = time.time()
        for tk, q in list(st["quotes"].items()):
            _step(s, st, tk, q)
        _settle(s, st)
        STATE.write_text(json.dumps(st))
        hrs = (time.time() - st["started"]) / 3600
        print(f"{time.strftime('%H:%M')} {hrs:.1f} h: rewards ${st['rewards']:.2f}, settled P&L ${st['pnl']:+.2f}, "
              f"open positions {len(st['pos'])}, quoting {len(st['quotes'])}", flush=True)
        time.sleep(LOOP_S)


def score():
    st = json.loads(STATE.read_text())
    ev = [json.loads(line) for line in LEDGER.open(encoding="utf-8")]
    hrs = (time.time() - st["started"]) / 3600
    open_cost = sum(p["cost"] for p in st["pos"])
    net = st["rewards"] + st["pnl"]
    sess, mtm = requests.Session(), 0.0                 # open positions marked at our side's best bid (exit value)
    for p in st["pos"]:
        try:
            yes, no = _book(sess, p["tk"])
            lv = yes if p["side"] == "yes" else no
            mtm += p["n"] * (lv[0][0] / 100 if lv else 0.0) - p["cost"]
        except Exception:
            pass
    if "fix_from" in st:
        rf = st["rewards"] - st["rewards_at_fix"]
        hf = (time.time() - time.mktime(time.strptime(st["fix_from"], "%Y-%m-%d %H:%M UTC")) + time.timezone) / 3600
        print(f"rewards under the filed rules since {st['fix_from']} ({hf:.1f} h): ${rf:.2f} "
              f"(= ${rf / max(hf, 1e-9) * 24:.2f}/day); fills/positions count from the start")
    print(f"{hrs:.1f} h: rewards ${st['rewards']:.2f}, settled trading P&L ${st['pnl']:+.2f}, open positions "
          f"{len(st['pos'])} (cost ${open_cost:.2f}), fills {sum(e['k'] == 'fill' for e in ev)}, "
          f"settled {sum(e['k'] == 'settle' for e in ev)}; net settled ${net:+.2f} "
          f"= {net / BUDGET / max(hrs / 24, 1e-9):+.1%}/day on ${BUDGET:.0f}; open marked {mtm:+.2f} -> "
          f"net incl. open ${net + mtm:+.2f} ({(net + mtm) / BUDGET / max(hrs / 24, 1e-9):+.1%}/day)")



# ---------------------------------------------------------------- maker edge from history (W0)
# For the series with the biggest live pools: every settled market of the last HIST_DAYS days and all its trades.
# Each trade has a maker on the other side: taker bought Yes -> maker bought No at no_price (P&L = [no] - no_price),
# taker bought No -> maker bought Yes at yes_price. "Adverse" = the next trade within 10 min prints >= 1c worse for
# that maker (the fills a back-of-queue order actually gets). Output: data/kalshi_lip/history.json.
HIST_DAYS, HIST_SERIES = 21, 40


def history():
    import collections
    from .kalshi_data import Http
    snap = json.loads(sorted(OUT.glob("snapshot_*.json"))[-1].read_text())
    pools = collections.defaultdict(float)
    for r in snap:
        if r["ev"]["met"]:
            pools[r["market_ticker"].split("-")[0]] += r["ev"]["pool_day"]
    series = [s for s, _ in sorted(pools.items(), key=lambda x: -x[1])[:HIST_SERIES]]
    h = Http(10.0)
    since = time.time() - HIST_DAYS * 86400
    out = {}
    for s in series:
        ms, cur = [], None
        while True:
            p = {"series_ticker": s, "status": "settled", "limit": 1000, "min_close_ts": int(since)}
            if cur:
                p["cursor"] = cur
            d = h.get(f"{API}/markets", p)
            ms += [m for m in d.get("markets") or [] if m.get("result") in ("yes", "no")]
            cur = d.get("cursor")
            if not cur or not d.get("markets"):
                break
        ms = ms[:300]
        pnl, adv, vol = [], [], 0.0
        for m in ms:
            tr, cur = [], None
            while True:
                p = {"ticker": m["ticker"], "limit": 1000}
                if cur:
                    p["cursor"] = cur
                d = h.get(f"{API}/markets/trades", p)
                tr += d.get("trades") or []
                cur = d.get("cursor")
                if not cur or not d.get("trades") or len(tr) > 20000:
                    break
            tr.sort(key=lambda x: x["created_time"])
            yes_won = m["result"] == "yes"
            for k, x in enumerate(tr):
                n = float(x["count_fp"])
                yp = float(x["yes_price_dollars"])
                if x["taker_side"] == "yes":           # maker bought No at 1 - yp
                    e = (0.0 if yes_won else 1.0) - (1 - yp)
                    worse = lambda y: y >= yp + 0.01
                else:                                  # maker bought Yes at yp
                    e = (1.0 if yes_won else 0.0) - yp
                    worse = lambda y: y <= yp - 0.01
                pnl.append((e, n))
                vol += n
                t0 = ts(x["created_time"])
                for z in tr[k + 1:k + 30]:
                    if ts(z["created_time"]) - t0 > 600:
                        break
                    if worse(float(z["yes_price_dollars"])):
                        adv.append((e, n))
                        break
        f = lambda xs: sum(e * n for e, n in xs) / max(sum(n for _, n in xs), 1e-9)
        out[s] = dict(pool_day=pools[s], markets=len(ms), contracts_day=vol / HIST_DAYS, edge=f(pnl), edge_adverse=f(adv),
                      adverse_share=sum(n for _, n in adv) / max(vol, 1e-9))
        r = out[s]
        print(f"  {s:22s} pool ${r['pool_day']:6.0f}/d  {r['markets']:3d} mkts  {r['contracts_day']:9.0f} contracts/d  "
              f"maker edge {r['edge'] * 100:+.2f}c  adverse fills {r['edge_adverse'] * 100:+.2f}c ({r['adverse_share']:.0%})", flush=True)
        (OUT / "history.json").write_text(json.dumps(out))

def main() -> None:
    {"snapshot": snapshot, "paper": paper, "score": score, "history": history}[sys.argv[1] if len(sys.argv) > 1 else "snapshot"]()


if __name__ == "__main__":
    main()
