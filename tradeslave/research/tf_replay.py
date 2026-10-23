"""TF: replay every closed M1/M2/M3 paper trade on exact curve trades, find failure patterns, test fixes.

    uv run python -m tradeslave.research.tf_replay            # calibration, taxonomy, fix sweep, final backtest

Pre-registered 2026-10-22 14:35 (vault note "2026-10-22 trade forensics and fix sweep (TF)").
Curve state: rebuilt from the bot's own entry price (virtual SOL = sqrt(p0 * V0 * T0)), then moved by each curve
trade's SOL change (data/m1/ticks, Helius). The bots' exit code (m1_bot.manage) is simulated on polls every 2 s
(M1) or 1 s (M2/M3): an exit decided on one poll fills at the next poll's state via sell_curve.
"""

from __future__ import annotations

import gzip
import json
import math
import statistics
import sys
import time
from collections import defaultdict

from ..paths import DATA_DIR
from .c1_curve import T0, V0, sell_curve
from .ticks import OUT as TICKS

K = V0 * T0
SPLIT = time.mktime(time.strptime("2026-10-22 00:00", "%Y-%m-%d %H:%M"))
SIZE, TX = 0.1, 0.0005
BASE = dict(tp=2.0, trend_arm=1.3, trend_give=0.8, stall_s=120, stall_act=0.5, max_hold=600,
            hi_arm=1.5, hi_give=0.6, hi_dead=0.2, hi_hold=7200, high=True,
            lock_arm=None, lock_give=None, fade=None, be_arm=None, half15=False, early=None, m1_stop=None,
            actfade=None, hdecl=None, hflow=None, hgrace=0)
HORIZON = 900


def load():
    trades = []
    for v in ("m1", "m2", "m3"):
        for line in (DATA_DIR / v / "trades.jsonl").open(encoding="utf-8"):
            x = json.loads(line)
            if "ret" in x and x.get("p0") and x.get("q"):
                x["v"] = v
                trades.append(x)
    ticks = {}
    for x in trades:
        p = TICKS / f"{x['m']}.json.gz"
        if x["m"] not in ticks and p.exists():
            with gzip.open(p, "rt", encoding="utf-8") as fh:
                d = json.load(fh)
            ticks[x["m"]] = sorted(d["trades"], key=lambda r: (r[0], r[1]))     # chain order: slot, then time
    created = {}
    for v in ("m1", "m2", "m3"):
        for line in (DATA_DIR / v / "cands.jsonl").open(encoding="utf-8"):
            c = json.loads(line)
            if "age" in c and c.get("m") in ticks:
                created[c["m"]] = min(created.get(c["m"], 9e12), c["t"] - c["age"])
    return trades, ticks, created


def implied_v(b, sol, tok):
    """Virtual SOL reserve before a trade, from its own SOL and token amounts (constant product)."""
    c = K * sol / tok
    return (-sol + math.sqrt(sol * sol + 4 * c)) / 2 if b else (sol + math.sqrt(sol * sol + 4 * c)) / 2


def path(x, tr, created=None):
    """Spot price after each curve trade. Token changes are exact for every transaction (the SOL change is hidden
    for some routers), so the token reserve is walked by token deltas and its level is the median of the levels
    implied by the trades whose SOL is known. The entry boundary within the entry's second is the split whose
    price is closest to the bot's own p0; the path is then scaled to p0 (a small correction).
    Holders (wallets holding > 1 token, curve excluded) are counted only when the window starts at the create.
    Returns (pre, post): lists of [t, spot, is_buy, sol, wallet, tokens, holders|None], before and after our entry."""
    d, cum = 0.0, []
    for s, t, w, b, sol, tok in tr:
        cum.append(d)                                           # token delta before this trade
        d += -tok if b else tok
    est = [K / implied_v(b, sol, tok) - cum[i] for i, (s, t, w, b, sol, tok) in enumerate(tr)
           if sol and tok and sol > 1e-6]
    if not est:
        return [], []
    t_start = statistics.median(est)
    rows, prev_v = [], K / t_start
    full = created is not None and tr[0][1] <= created + 3
    bal, hold = {}, 0
    for i, (s, t, w, b, sol, tok) in enumerate(tr):
        tk = max(1.0, t_start + cum[i] + (-tok if b else tok))
        v = K / tk
        b0 = bal.get(w, 0.0)
        b1 = b0 + (tok if b else -tok)
        bal[w] = b1
        hold += (b0 <= 1 < b1) - (b1 <= 1 < b0)
        rows.append([t, v / tk, b, sol if sol is not None else abs(v - prev_v), w, tok, hold if full else None])
        prev_v = v
    sec = math.floor(x["t"])
    lo = next((i for i, r in enumerate(rows) if r[0] >= sec - 1), len(rows))
    hi = next((i for i, r in enumerate(rows) if r[0] > sec + 1), len(rows))
    p_before = lambda j: rows[j - 1][1] if j > 0 else K / (t_start ** 2)
    j = min(range(lo, hi + 1), key=lambda j: abs(math.log(p_before(j) / x["p0"]))) if hi >= lo else lo
    k = x["p0"] / p_before(j)
    if abs(math.log(k)) < 0.15:
        for r in rows:
            r[1] *= k
    return rows[:j], rows[j:]


class Win:
    """Rolling views over the post-entry trades up to a time."""

    def __init__(self, post):
        self.p, self.i = post, 0

    def upto(self, t):
        while self.i < len(self.p) and self.p[self.i][0] <= t:
            self.i += 1
        return self.i

    def since(self, a, b):
        j = self.upto(b)
        out = []
        for k in range(j - 1, -1, -1):
            if self.p[k][0] <= a:
                break
            out.append(self.p[k])
        return out


def sim(x, pre, post, P):
    """Replay one trade with exit parameters P. Returns (ret, why, t_exit, peak_x)."""
    v, t0, p0, q = x["v"], x["t"], x["p0"], x["q"]
    poll = 2.0 if v == "m1" else 1.0
    stop = x.get("stop", 0.7 if v == "m1" else 0.9)
    if v == "m1" and P["m1_stop"]:
        stop = P["m1_stop"]
    high = x.get("high", False) and P["high"] and v != "m3"
    w = Win(post)
    p, peak, t_peak, act, amax, t_act = p0, p0, t0, None, 0, -99
    pending, cash, qq, run, half_done = None, 0.0, q, None, False
    t_end = t0 + HORIZON
    a30max, hmax = 0, (pre[-1][6] if pre and pre[-1][6] is not None else None)
    pre_t = [r for r in pre if r[0] > t0 - 60]
    act_pre = len(pre_t)
    tick_t = t0
    while tick_t < t_end:
        tick_t += poll
        n = w.upto(tick_t)
        p = post[n - 1][1] if n else p0
        if pending:
            out = sell_curve(qq, p) + cash
            return (out - 2 * TX) / SIZE - 1, pending, tick_t, peak / p0
        if p > peak:
            peak, t_peak = p, tick_t
        if tick_t - t_act >= 6.0:
            act = len(w.since(tick_t - 60, tick_t)) + (act_pre if tick_t - t0 < 60 else 0)
            amax, t_act = max(amax, act), tick_t
        r, held = p / p0, tick_t - t0
        a30max = max(a30max, len({y[4] for y in w.since(tick_t - 30, tick_t)}))
        if hmax is not None and n and post[n - 1][6] is not None:
            hmax = max(hmax, post[n - 1][6])
        if run is not None:                                     # M3 ride
            if tick_t - run["t_sup"] >= 10:
                run["sup"], run["flow"] = support(w, tick_t)
                run["sup_max"], run["t_sup"] = max(run["sup_max"], run["sup"]), tick_t
            fade = run["sup"] < 0.40 * run["sup_max"] and run["flow"] < 1.0
            pending = ("FADE" if fade else "FLOOR" if r <= run["floor"] else "CRASH" if p <= peak * 0.5 else None)
            continue
        if P["half15"] and not half_done and r >= 1.5:
            cash, qq, half_done = cash + sell_curve(qq / 2, p) - TX, qq / 2, True
        if r <= stop:
            pending = "STOP"
        elif P["be_arm"] and peak >= p0 * P["be_arm"] and r <= 1.0:
            pending = "BE"
        elif P["lock_arm"] and peak >= p0 * P["lock_arm"] and p <= peak * (1 - P["lock_give"]):
            pending = "LOCK"
        elif P["fade"] and peak >= p0 * 1.2 and r > stop:
            last = w.since(tick_t - 20, tick_t)
            flow = sum(y[3] if y[2] else -y[3] for y in last if y[0] > tick_t - 15)
            buyers = len({y[4] for y in last if y[2]})
            run_max = P.setdefault("_bmax", {}).get(id(x), 0)
            run_max = max(run_max, buyers)
            P["_bmax"][id(x)] = run_max
            if flow < 0 and buyers < P["fade"] * run_max:
                pending = "FADE"
        elif P["actfade"] and len(post) and (lambda a30: a30 < P["actfade"] * a30max and p < 0.9 * peak)(
                len({y[4] for y in w.since(tick_t - 30, tick_t)})):
            pending = "AFADE"
        elif (P["hdecl"] and held >= P["hgrace"] and hmax and n and post[n - 1][6] is not None
              and post[n - 1][6] <= (1 - P["hdecl"]) * hmax
              and (not P["hflow"] or sum(y[3] if y[2] else -y[3] for y in w.since(tick_t - P["hflow"], tick_t)) < 0)):
            pending = "HDECL"                                   # hflow (MV idea 2): only while net SOL flow is negative
        elif P["early"] and held >= P["early"] and peak < p0 * 1.05:
            last = w.since(tick_t - 15, tick_t)
            if sum(y[3] if y[2] else -y[3] for y in last) < 0:
                pending = "EARLY"
        if pending:
            continue
        if high:
            pending = ("TREND" if peak >= p0 * P["hi_arm"] and p <= peak * P["hi_give"] else
                       "DEAD" if act is not None and held > 60 and act < P["hi_dead"] * amax and r < 1 else
                       "TIME" if held > P["hi_hold"] else None)
        else:
            pending = ("TP" if r >= P["tp"] else
                       "TREND" if peak >= p0 * P["trend_arm"] and p <= peak * P["trend_give"] else
                       "STALL" if act is not None and tick_t - t_peak > P["stall_s"] and act < P["stall_act"] * amax else
                       "TIME" if held > P["max_hold"] else None)
            if v == "m3" and pending in ("TP", "TREND", "STALL", "TIME") and r >= 1.3:
                b, f = support(w, tick_t)
                if b >= 25 and f >= 1.5:
                    cash, qq = cash + sell_curve(qq / 2, p) - TX, qq / 2
                    run = dict(sup=b, flow=f, sup_max=b, t_sup=tick_t, floor=min(1.4, 0.8 * r))
                    pending = None
    n = w.upto(t_end)
    p = post[n - 1][1] if n else p0
    return (sell_curve(qq, p) + cash - 2 * TX) / SIZE - 1, "END", t_end, peak / p0


def support(w, t):
    last = w.since(t - 60, t)
    buy = sum(y[3] for y in last if y[2])
    sell = sum(y[3] for y in last if not y[2])
    return len({y[4] for y in last if y[2]}), (buy / sell if sell else 99.0)


def pre_feats(x, pre):
    t = x["t"]
    flow10 = sum(y[3] if y[2] else -y[3] for y in pre if y[0] > t - 10)
    lo60 = min((y[1] for y in pre if y[0] > t - 60), default=x["p0"])
    return dict(flow10=flow10, from_lo60=x["p0"] / lo60 if lo60 else 1.0)


def stats(v):
    if not v:
        return "n 0"
    m = statistics.mean(v)
    t = m / (statistics.stdev(v) / len(v) ** 0.5) if len(v) > 2 and statistics.stdev(v) else 0
    srt = sorted(v)
    return (f"n {len(v):3d} mean {m:+6.1%} t {t:+5.2f} win {sum(x > 0 for x in v) / len(v):4.0%} "
            f"SOL {sum(v) * SIZE:+.3f} (ex best 2 {sum(srt[:-2]) * SIZE:+.3f})")


def entry_at(x, pre, post, H):
    """R4 watchlist: the first moment from the bot's entry (up to 15 min) with holders >= H and mc in the buy band.
    Returns a trade dict entered there (curve fill for 0.1 SOL), the bot's own trade if it already qualified, or None.
    Unknown holders -> the bot's own trade (no change)."""
    h0 = pre[-1][6] if pre else None
    if h0 is None:
        return x
    if h0 >= H:
        return x
    top = 10000.0 if (x.get("score") or 0) >= 9 else 7000.0
    usd = 1e9 * (x.get("sol_usd") or 105.0)
    for r in post:
        if r[0] > x["t"] + HORIZON:
            return None
        if r[6] is not None and r[6] >= H and 4000.0 <= r[1] * usd <= top:
            v = math.sqrt(r[1] * K)
            q = K / v - K / (v + SIZE * (1 - 0.0125))
            return dict(x, t=r[0] + 1.0, p0=r[1], q=q, peak=r[1])
    return None


def resplit(x, pre, post):
    rows = pre + post
    j = next((i for i, r in enumerate(rows) if r[0] > x["t"]), len(rows))
    return rows[:j], rows[j:]


def main() -> None:
    trades, ticks, created = load()
    rows = []
    for x in trades:
        tr = ticks.get(x["m"])
        if not tr:
            continue
        pre, post = path(x, tr, created.get(x["m"]))
        if post or pre:
            rows.append((x, pre, post))
    print(f"{len(rows)} of {len(trades)} trades have exact curve trades; "
          f"{sum(1 for x, pre, _ in rows if pre and pre[-1][6] is not None)} with holder counts from create")
    half = lambda x: int(x["t"] >= SPLIT)                       # noqa: E731

    # 1. calibration
    base = {id(x): sim(x, pre, post, dict(BASE))[0] for x, pre, post in rows}
    act = [x["ret"] for x, _, _ in rows]
    rep = [base[id(x)] for x, _, _ in rows]
    corr = statistics.correlation(act, rep) if len(rows) > 2 else 0
    same = sum(sim(x, pre, post, dict(BASE))[1] == x["exit"] for x, pre, post in rows) / len(rows)
    print(f"\n1. CALIBRATION  actual mean {statistics.mean(act):+.1%}  replay mean {statistics.mean(rep):+.1%}  "
          f"corr {corr:.2f}  same exit {same:.0%}")
    for v in ("m1", "m2", "m3"):
        z = [(x["ret"], base[id(x)]) for x, _, _ in rows if x["v"] == v]
        if z:
            print(f"   {v}: actual {statistics.mean(a for a, b in z):+.1%} replay {statistics.mean(b for a, b in z):+.1%} n {len(z)}")
    ok = abs(statistics.mean(act) - statistics.mean(rep)) <= 0.03 and corr >= 0.8
    print("   gate:", "PASS" if ok else "FAIL (sweep results are unreliable)")

    # 2. taxonomy (actual results)
    cat = defaultdict(list)
    for x, pre, post in rows:
        p0, te = x["p0"], x["t_exit"]
        after = max((y[1] for y in post if te < y[0] <= x["t"] + HORIZON), default=0) / p0
        pk = x["peak"] / p0
        h = pre[-1][6] if pre else None
        if x["ret"] > 0:
            c = "win"
        elif pk >= 1.3:
            c = "gave back (peak >= 1.3x)"
        elif x["exit"] == "STOP" and x["p_exit"] / p0 < x.get("stop", 0.7) - 0.10:
            c = "rug gap (stop filled > 10 pts low)"
        elif x["exit"] == "STOP" and after >= 2:
            c = "shaken out (stopped, then 2x)"
        elif x["exit"] in ("STALL", "DEAD", "TREND", "TIME") and after >= 2:
            c = "cut winner (exited, then 2x)"
        elif pk < 1.05:
            c = "instant dump (never > 1.05x)"
        else:
            c = "slow bleed"
        cat[c].append((x, h))
    print("\n2. TAXONOMY (actual trades)              design             | check              | median holders at entry")
    for c, z in sorted(cat.items(), key=lambda kv: sum(x["ret"] for x, _ in kv[1])):
        hh = [[x["ret"] for x, _ in z if half(x) == k] for k in (0, 1)]
        hs = [h for _, h in z if h is not None]
        print(f"   {c:36s} n {len(hh[0]):3d} SOL {sum(hh[0]) * SIZE:+.3f} | n {len(hh[1]):3d} SOL {sum(hh[1]) * SIZE:+.3f} | "
              f"{statistics.median(hs) if hs else '-'} (n {len(hs)})")
    hb = [(pre[-1][6], x["ret"]) for x, pre, _ in rows if pre and pre[-1][6] is not None]
    for lo, hi in ((0, 10), (10, 15), (15, 20), (20, 30), (30, 999)):
        z = [r for h, r in hb if lo <= h < hi]
        if z:
            print(f"   holders at entry {lo:3d}-{hi:<3d}: {stats(z)}")
    if "calib" in sys.argv:                            # calibration and taxonomy only (no fix results)
        return

    # 3. fix sweep vs replayed baseline
    def judge(new):
        """new: id(x) -> new return (0 for a skipped trade). Per half: totals and acceptance inputs."""
        out = []
        for k in (0, 1):
            z = [(new[id(x)], base[id(x)]) for x, _, _ in rows if half(x) == k]
            out.append(dict(new=sum(a for a, b in z), old=sum(b for a, b in z), dmean=statistics.mean(a - b for a, b in z),
                            win_new=sum(a for a, b in z if a > 0), win_old=sum(b for a, b in z if b > 0)))
        acc = (all(h["new"] >= h["old"] for h in out) and out[0]["dmean"] >= 0.02
               and all(h["win_new"] >= 0.75 * h["win_old"] for h in out))
        return out, acc

    def exits(over):
        P = dict(BASE, **over)
        return {id(x): sim(x, pre, post, P)[0] for x, pre, post in rows}

    def r1(H):
        return {id(x): 0.0 if pre and pre[-1][6] is not None and pre[-1][6] < H else base[id(x)] for x, pre, post in rows}

    def r4(H, over=None):
        P, out = dict(BASE, **(over or {})), {}
        for x, pre, post in rows:
            y = entry_at(x, pre, post, H)
            if y is None:
                out[id(x)] = 0.0
            elif y is x:
                out[id(x)] = sim(x, pre, post, P)[0]
            else:
                a, b = resplit(y, pre, post)
                out[id(x)] = sim(y, a, b, P)[0]
        return out

    pf = lambda x: pre_feats(x[0], x[1])                        # noqa: E731
    feats = {id(x): pre_feats(x, pre) for x, pre, _ in rows}
    s1 = lambda f: f["flow10"] < 0                              # noqa: E731
    s2 = lambda f: f["from_lo60"] >= 1.3 and f["flow10"] < 0     # noqa: E731
    fams = {
        "X1 profit lock": [("exit", dict(lock_arm=a, lock_give=b)) for a in (1.2, 1.3, 1.5) for b in (0.10, 0.15, 0.20)],
        "X2 hype fade": [("exit", dict(fade=f)) for f in (0.3, 0.5)],
        "X3 no high mode": [("exit", dict(high=False))],
        "X4 breakeven stop": [("exit", dict(be_arm=a)) for a in (1.2, 1.3)],
        "X5 half at 1.5x": [("exit", dict(half15=True))],
        "X6 early dead exit": [("exit", dict(early=s)) for s in (30, 60)],
        "X7 tighter M1 stop": [("exit", dict(m1_stop=s)) for s in (0.8, 0.85)],
        "S1 skip: selling into entry": [("skip", s1)],
        "S2 skip: bought the top": [("skip", s2)],
        "R1 holder floor (Rohit)": [("r1", H) for H in (15, 20)],
        "R2 activity fade (Rohit)": [("exit", dict(actfade=f)) for f in (0.3, 0.4)],
        "R3 holder decline (Rohit)": [("exit", dict(hdecl=d)) for d in (0.10, 0.20)],
        "R4 watchlist entry (Rohit)": [("r4", H) for H in (15, 20)],
    }

    def run(kind, arg):
        if kind == "exit":
            return exits(arg)
        if kind == "skip":
            return {id(x): 0.0 if arg(feats[id(x)]) else base[id(x)] for x, _, _ in rows}
        return r1(arg) if kind == "r1" else r4(arg)

    print("\n3. FIX SWEEP (vs replayed baseline; SOL at 0.1 SOL a trade; best variant chosen on design)")
    accepted = []
    for fam, variants in fams.items():
        res = [(k, a, judge(run(k, a))) for k, a in variants]
        best = max(res, key=lambda z: z[2][0][0]["new"] - z[2][0][0]["old"])
        for k, a, (h, acc) in res:
            lab = a if k != "skip" else "rule"
            print(f"   {fam:28s} {json.dumps(lab) if not callable(lab) else '':34s} design {(h[0]['new'] - h[0]['old']) * SIZE:+.3f} SOL "
                  f"({h[0]['dmean']:+5.1%}/trade) | check {(h[1]['new'] - h[1]['old']) * SIZE:+.3f} SOL ({h[1]['dmean']:+5.1%})"
                  f"{'  <- chosen' if (k, a) == best[:2] else ''}")
        acc = best[2][1]
        print(f"   => {fam}: {'ACCEPTED' if acc else 'rejected'}")
        if acc:
            accepted.append((fam, best[0], best[1]))

    # 4. final backtest: accepted entry rule (R4 over R1), accepted skips, accepted exits together
    over = {}
    for fam, k, a in accepted:
        if k == "exit":
            over.update(a)
    ent = next(((k, a) for fam, k, a in accepted if k == "r4"), None) or next(((k, a) for fam, k, a in accepted if k == "r1"), None)
    skips = [a for fam, k, a in accepted if k == "skip"]
    print("\n4. FINAL BACKTEST  new logic:", [fam + (f" {json.dumps(a)}" if k != "skip" else "") for fam, k, a in accepted] or "no fix accepted")
    new = r4(ent[1], over) if ent and ent[0] == "r4" else exits(over)
    if ent and ent[0] == "r1":
        new = {i: 0.0 if r1(ent[1])[i] == 0.0 and base[i] != 0.0 else new[i] for i in new}
    for i in new:
        if any(f(feats[i]) for f in skips):
            new[i] = 0.0
    taken = lambda x: not (new[id(x)] == 0.0)                   # noqa: E731
    for v in ("m1", "m2", "m3", "all"):
        for k, lab in ((0, "design"), (1, "check")):
            z = [x for x, _, _ in rows if half(x) == k and (v == "all" or x["v"] == v)]
            if not z:
                continue
            print(f"   {v:3s} {lab:6s} actual   {stats([x['ret'] for x in z])}")
            print(f"   {'':3s} {'':6s} baseline {stats([base[id(x)] for x in z])}")
            print(f"   {'':3s} {'':6s} NEW      {stats([new[id(x)] for x in z if taken(x)])}  "
                  f"(skipped {sum(not taken(x) for x in z)}; SOL incl. skips {sum(new[id(x)] for x in z) * SIZE:+.3f})")
    json.dump({"accepted": [(fam, k, a if k != "skip" else fam) for fam, k, a in accepted]},
              open(DATA_DIR / "m1" / "tf_accepted.json", "w"))


if __name__ == "__main__":
    main()
