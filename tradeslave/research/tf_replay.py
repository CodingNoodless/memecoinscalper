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
            lock_arm=None, lock_give=None, fade=None, be_arm=None, half15=False, early=None, m1_stop=None)
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
    return trades, ticks


def implied_v(b, sol, tok):
    """Virtual SOL reserve before a trade, from its own SOL and token amounts (constant product)."""
    c = K * sol / tok
    return (-sol + math.sqrt(sol * sol + 4 * c)) / 2 if b else (sol + math.sqrt(sol * sol + 4 * c)) / 2


def path(x, tr):
    """Spot price after each curve trade. Token changes are exact for every transaction (the SOL change is hidden
    for some routers), so the token reserve is walked by token deltas and its level is the median of the levels
    implied by the trades whose SOL is known. The entry boundary within the entry's second is the split whose
    price is closest to the bot's own p0; the path is then scaled to p0 (a small correction).
    Returns (pre, post): lists of [t, spot, is_buy, sol, wallet], before and after our entry."""
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
    for i, (s, t, w, b, sol, tok) in enumerate(tr):
        tk = max(1.0, t_start + cum[i] + (-tok if b else tok))
        v = K / tk
        rows.append([t, v / tk, b, sol if sol is not None else abs(v - prev_v), w])
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


def main() -> None:
    trades, ticks = load()
    rows = []
    for x in trades:
        tr = ticks.get(x["m"])
        if not tr:
            continue
        pre, post = path(x, tr)
        rows.append((x, pre, post))
    print(f"{len(rows)} of {len(trades)} trades have exact curve trades")
    half = lambda x: int(x["t"] >= SPLIT)

    # 1. calibration
    base = {id(x): sim(x, pre, post, dict(BASE)) for x, pre, post in rows}
    act = [x["ret"] for x, _, _ in rows]
    rep = [base[id(x)][0] for x, _, _ in rows]
    corr = statistics.correlation(act, rep) if len(rows) > 2 else 0
    print(f"\n1. CALIBRATION  actual mean {statistics.mean(act):+.1%}  replay mean {statistics.mean(rep):+.1%}  "
          f"corr {corr:.2f}  same exit {sum(base[id(x)][1] == x['exit'] for x, _, _ in rows) / len(rows):.0%}")
    for v in ("m1", "m2", "m3"):
        z = [(x["ret"], base[id(x)][0]) for x, _, _ in rows if x["v"] == v]
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
        cat[c].append(x)
    print("\n2. TAXONOMY (actual trades)              design                         | check")
    for c, z in sorted(cat.items(), key=lambda kv: sum(x["ret"] for x in kv[1])):
        h = [[x["ret"] for x in z if half(x) == k] for k in (0, 1)]
        print(f"   {c:36s} n {len(h[0]):3d} SOL {sum(h[0]) * SIZE:+.3f} | n {len(h[1]):3d} SOL {sum(h[1]) * SIZE:+.3f}"
              f"   bots {dict(sorted(defaultdict(int, {v: sum(x['v'] == v for x in z) for v in ('m1', 'm2', 'm3')}).items()))}")

    if "calib" in sys.argv:                            # calibration and taxonomy only (no fix results)
        return

    # 3. fix sweep vs replayed baseline
    fams = {
        "X1 profit lock": [dict(lock_arm=a, lock_give=b) for a in (1.2, 1.3, 1.5) for b in (0.10, 0.15, 0.20)],
        "X2 hype fade": [dict(fade=f) for f in (0.3, 0.5)],
        "X3 no high mode": [dict(high=False)],
        "X4 breakeven stop": [dict(be_arm=a) for a in (1.2, 1.3)],
        "X5 half at 1.5x": [dict(half15=True)],
        "X6 early dead exit": [dict(early=s) for s in (30, 60)],
        "X7 tighter M1 stop": [dict(m1_stop=s) for s in (0.8, 0.85)],
    }

    def score(over):
        P = dict(BASE, **over)
        res = {id(x): sim(x, pre, post, P) for x, pre, post in rows}
        out = []
        for k in (0, 1):
            z = [(res[id(x)][0], base[id(x)][0]) for x, _, _ in rows if half(x) == k]
            out.append(dict(n=len(z), new=sum(a for a, b in z), old=sum(b for a, b in z),
                            dmean=statistics.mean(a - b for a, b in z),
                            win_new=sum(a for a, b in z if a > 0), win_old=sum(b for a, b in z if b > 0)))
        return out, res

    accepted = {}
    print("\n3. FIX SWEEP (vs replayed baseline; SOL at 0.1 SOL a trade)")
    for fam, variants in fams.items():
        scored = [(o, score(o)) for o in variants]
        o, (h, _) = max(scored, key=lambda z: z[1][0][0]["new"] - z[1][0][0]["old"])
        acc = (all(hh["new"] >= hh["old"] for hh in h) and h[0]["dmean"] >= 0.02
               and all(hh["win_new"] >= 0.75 * hh["win_old"] for hh in h))
        for oo, (hh, _) in scored:
            print(f"   {fam:20s} {json.dumps(oo):42s} design {(hh[0]['new'] - hh[0]['old']) * SIZE:+.3f} SOL "
                  f"({hh[0]['dmean']:+.1%}/trade) | check {(hh[1]['new'] - hh[1]['old']) * SIZE:+.3f} SOL ({hh[1]['dmean']:+.1%})"
                  f"{'   <- chosen' if oo is o else ''}")
        print(f"   => {fam}: {'ACCEPTED' if acc else 'rejected'}")
        if acc:
            accepted.update(o)

    skips = {}
    for x, pre, post in rows:
        skips[id(x)] = pre_feats(x, pre)
    for name, f in (("S1 skip: net selling into entry", lambda s: s["flow10"] < 0),
                    ("S2 skip: bought the top", lambda s: s["from_lo60"] >= 1.3 and s["flow10"] < 0)):
        h = [[base[id(x)][0] for x, _, _ in rows if half(x) == k and f(skips[id(x)])] for k in (0, 1)]
        acc = all(sum(z) < 0 for z in h) and all(z for z in h)
        print(f"   {name:32s} skipped design n {len(h[0])} SOL {sum(h[0]) * SIZE:+.3f} | check n {len(h[1])} SOL "
              f"{sum(h[1]) * SIZE:+.3f}  => {'ACCEPTED' if acc else 'rejected'}")
        if acc:
            accepted.setdefault("_skip", []).append(name)

    # 4. final backtest
    print("\n4. FINAL BACKTEST: new logic =", json.dumps({k: v for k, v in accepted.items()}))
    skip_rules = accepted.pop("_skip", [])
    _, res = score(accepted)
    sk = {"S1 skip: net selling into entry": lambda s: s["flow10"] < 0,
          "S2 skip: bought the top": lambda s: s["from_lo60"] >= 1.3 and s["flow10"] < 0}
    for v in ("m1", "m2", "m3", "all"):
        for k, lab in ((0, "design"), (1, "check")):
            z = [(x, pre) for x, pre, _ in rows if half(x) == k and (v == "all" or x["v"] == v)]
            if not z:
                continue
            keep = [x for x, pre in z if not any(sk[s](skips[id(x)]) for s in skip_rules)]
            print(f"   {v:3s} {lab:6s} actual   {stats([x['ret'] for x, _ in z])}")
            print(f"   {'':3s} {'':6s} baseline {stats([base[id(x)][0] for x, _ in z])}")
            print(f"   {'':3s} {'':6s} NEW      {stats([res[id(x)][0] for x in keep])}")
    json.dump({"accepted": accepted, "skips": skip_rules}, open(DATA_DIR / "m1" / "tf_accepted.json", "w"))


if __name__ == "__main__":
    main()
