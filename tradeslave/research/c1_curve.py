"""C1 (STRATEGY_TESTS_v3): logistic reach model at bonding-curve real SOL R = 40, sell at 75 / stop 30 (forward,
pre-registered; vault: "2026-10-21 curve reach model C1 (v3)").

    uv run python -m tradeslave.research.c1_curve record   # candle checks on every create, trades of R >= 40 tokens
    uv run python -m tradeslave.research.c1_curve score    # once, after the recording window + 80 min

Mints: every create from the PumpPortal feed (launch_watch writes data/c1/creates.jsonl). Each is checked on the
pump.fun candle API at fixed ages; candle highs give the max R reached (R = sqrt(price * V0 * T0) - V0, exact on the
curve). R >= 38 by candles -> every trade from creation to the first 40-candle + 80 min (swap-api, with priceSol =
post-trade spot), so the crossing, features, label and exits all come from the trades.
"""

from __future__ import annotations

import gzip
import json
import math
import statistics
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np
import requests

from ..paths import DATA_DIR
from .full_trades import Pace, ts as iso_ts
from .long_paths import fetch as candles
from .shock import K_POOL, fee as pool_fee

OUT = DATA_DIR / "c1"
CREATES = OUT / "creates.jsonl"
CHECKS = OUT / "checks.jsonl"
TRADES = OUT / "trades"
API = "https://swap-api.pump.fun/v2/coins/{}/trades"
V0, T0 = 30.0, 1_073_000_000.0                   # Pump Global, decoded from chain 2026-10-20 (graduation at R = 85.0)
R_IN, R_TP, R_STOP, R_SEL = 40.0, 75.0, 30.0, 38.0
CURVE_FEE = 0.0125                               # pump.fun/docs/fees: 0.95% protocol + 0.30% creator + 0% LP
AGES = (1200, 7200, 28800, 93600, 180000)        # 20 min, 2 h, 8 h, 26 h, 50 h
TAIL = 80 * 60
MAX_PAGES = 600
PDT = timezone(timedelta(hours=-7))
WINDOW = (datetime(2026, 10, 21, tzinfo=PDT).timestamp(), datetime(2026, 10, 28, tzinfo=PDT).timestamp())
EXTEND_TO = datetime(2026, 11, 4, tzinfo=PDT).timestamp()   # only if the test split has < 150 crossings (counts only)
USD, P_MIN, ENTRY_SLOTS, HOLD, LABEL_S = 20.0, 0.40, 4, 45 * 60, 3600
TX_SOL = 0.001
SOL_USDC = "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2"


def r_of(p):
    return math.sqrt(p * V0 * T0) - V0


def p_of(r):
    return (V0 + r) ** 2 / (V0 * T0)


# ---------------------------------------------------------------- recording

def fetch_trades(s, pace, mint, t0, end):
    """Every trade from creation to `end`, oldest first: [slot, idx, t, wallet, buy, sol, tokens, amm, priceSol]."""
    rows, cur, done = [], f"9999999999990000000000-{int(end * 1000)}", False
    for _ in range(MAX_PAGES):
        for k in range(8):
            pace.wait()
            try:
                r = s.get(API.format(mint), params={"limit": 100, "cursor": cur}, timeout=30)
            except requests.RequestException:
                time.sleep(3 * (k + 1))
                continue
            if r.status_code == 429:
                pace.bad()
                continue
            if r.status_code in (400, 404):
                return rows[::-1], True
            if r.ok:
                pace.good()
                break
            time.sleep(3 * (k + 1))
        else:
            return None, False
        d = r.json()
        for x in d["trades"]:
            sid = x["slotIndexId"]
            rows.append([int(sid[:12]), int(sid[12:]), iso_ts(x["timestamp"]), x["userAddress"], x["type"] == "buy",
                         float(x["amountSol"]), float(x["baseAmount"]), x["program"] != "pump", float(x["priceSol"])])
        cur = d["pagination"].get("nextCursor")
        if not d["pagination"].get("hasMore") or not cur or (rows and rows[-1][2] < t0 - 60):
            done = True
            break
    return rows[::-1], done


def record():
    TRADES.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    s.headers.update({"Origin": "https://pump.fun", "Accept": "application/json"})
    checked = defaultdict(set)                   # mint -> check indices done
    hot = {}                                     # mint -> (create row, first 40-candle time)
    if CHECKS.exists():
        for x in CHECKS.open(encoding="utf-8"):
            c = json.loads(x)
            checked[c["m"]].add(c["k"])
            if c.get("t40"):
                hot[c["m"]] = (c["c"], c["t40"])
    fetched = {p.name.split(".")[0] for p in TRADES.glob("*.json.gz")}
    lock = threading.Lock()
    pace = Pace()

    def check(c, k):
        cs = candles(s, c["m"], c["t"], "1m" if AGES[k] <= 7200 else "5m")
        if cs is None:
            return
        hi = max((x[2] for x in cs), default=0.0)
        t40 = next((x[0] for x in cs if x[2] >= p_of(R_SEL)), None)
        row = {"m": c["m"], "k": k, "t": time.time(), "maxR": round(r_of(hi), 2) if hi else None}
        if t40:
            row.update(t40=t40, c=c)
        with lock:
            checked[c["m"]].add(k)
            if t40 and c["m"] not in hot:
                hot[c["m"]] = (c, t40)
            with CHECKS.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")

    def trader():
        while True:
            now = time.time()
            with lock:
                due = [(m, c, t40) for m, (c, t40) in hot.items() if m not in fetched and now > t40 + TAIL + 120]
            if not due:
                time.sleep(30)
                continue
            for m, c, t40 in sorted(due, key=lambda x: x[2]):
                rows, complete = fetch_trades(s, pace, m, c["t"], t40 + TAIL)
                if rows is None:
                    continue
                with gzip.open(TRADES / f"{m}.json.gz", "wt", encoding="utf-8") as fh:
                    json.dump({"c": c, "t40": t40, "end": t40 + TAIL, "complete": complete, "trades": rows}, fh)
                fetched.add(m)
                print(time.strftime("%H:%M"), f"trades {m[:8]} {len(rows)}{'' if complete else ' TRUNCATED'}",
                      flush=True)

    threading.Thread(target=trader, daemon=True).start()
    pos, creates, last = 0, [], 0.0
    with ThreadPoolExecutor(4) as ex:
        while True:
            with CREATES.open("rb") as fh:                     # binary: byte offsets stay exact with CRLF
                fh.seek(pos)
                for line in fh:
                    if not line.endswith(b"\n"):
                        break
                    pos += len(line)
                    if line.strip():
                        c = json.loads(line)
                        if WINDOW[0] - 3600 <= c["t"] < EXTEND_TO:
                            creates.append(c)
            now = time.time()
            jobs = [(c, k) for c in creates for k, age in enumerate(AGES)
                    if k not in checked[c["m"]] and now - c["t"] >= age and c["m"] not in hot]
            list(ex.map(lambda a: check(*a), jobs))
            creates = [c for c in creates if now - c["t"] < AGES[-1] + 3600 and c["m"] not in hot]
            if now - last > 600:
                print(time.strftime("%H:%M"), f"creates tracked {len(creates)}, R>=40 tokens {len(hot)}, "
                      f"trade files {len(fetched)}, checks this pass {len(jobs)}", flush=True)
                last = now
            time.sleep(20)


# ---------------------------------------------------------------- scoring

def event(d):
    """Crossing, features, label and the trade paths for one token, or None if the curve never crossed 40."""
    c, tr = d["c"], d["trades"]
    curve = [x for x in tr if not x[7]]
    k = next((i for i, x in enumerate(curve) if r_of(x[8]) >= R_IN), None)
    if k is None or r_of(curve[k][8]) >= 84.5:      # never crossed, or the crossing tx completed the curve: untradeable
        return None
    x = curve[k]
    pre = curve[:k + 1]
    hold = defaultdict(float)
    for y in pre:
        hold[y[3]] += y[6] if y[4] else -y[6]
    pos = sorted((v for v in hold.values() if v > 0), reverse=True)
    circ = sum(pos) or 1.0
    feats = [math.log1p(max(0.0, x[2] - c["t"])), math.log1p(len({y[3] for y in pre if y[4]})),
             sum(not y[4] for y in pre) / len(pre), pos[0] / circ if pos else 0.0, sum(pos[:10]) / circ,
             100 * max(0.0, hold.get(c.get("dev"), 0.0)) / 1e9, float(float(c.get("sol") or 0) > 0),
             math.log1p(sum(y[5] for y in pre if y[4] and x[2] - 60 < y[2] <= x[2]))]
    amm = [y for y in tr if y[7]]
    mig = amm[0][2] if amm else None
    reach = any(r_of(y[8]) >= R_TP for y in curve[k + 1:] if y[2] <= x[2] + LABEL_S) or (
        mig is not None and mig <= x[2] + LABEL_S) or (
        len(curve) > k + 1 and r_of(curve[-1][8]) >= R_TP and curve[-1][2] <= x[2] + LABEL_S)
    return dict(m=c["m"], t0=c["t"], tx=x[2], f=feats, y=int(reach), k=k, curve=curve, amm=amm, mig=mig,
                complete=d["complete"], mh=bool(c.get("mh")))


def sell_curve(q, p_obs):
    """Our q tokens sold into the observed curve state with our buy still in it (own-impact)."""
    v = math.sqrt(p_obs * V0 * T0)
    t = V0 * T0 / v
    return (V0 * T0 / max(t - q, 1.0) - v) * (1 - CURVE_FEE)


def trade(e, sol_usd, slots=ENTRY_SLOTS, tx=TX_SOL):
    cv = e["curve"]
    k = e["k"]
    j = max(i for i in range(k, len(cv)) if cv[i][0] <= cv[k][0] + slots)
    if r_of(cv[j][8]) >= 84.5:
        return None                                              # curve completed before we could buy
    x = USD / sol_usd
    v = math.sqrt(cv[j][8] * V0 * T0)
    q = V0 * T0 / v - V0 * T0 / (v + x * (1 - CURVE_FEE))
    te = cv[j][2]
    out, why = None, "END"
    for i in range(j + 1, len(cv)):
        y = cv[i]
        if y[2] > te + HOLD:
            last = cv[i - 1]
            out, why = sell_curve(q, last[8]), "TIME"
            break
        r = r_of(y[8])
        if r >= R_TP:
            if r >= 84.5:                                        # this tx completed the curve: PumpSwap exit
                break
            out, why = sell_curve(q, y[8]), "TP"
            break
        if r <= R_STOP:
            nxt = cv[i + 1] if i + 1 < len(cv) else y
            out, why = sell_curve(q, nxt[8]), "STOP"
            break
    if out is None and e["mig"] is not None and e["mig"] <= te + HOLD:
        bar = math.floor(e["mig"] / 60) * 60 + 120
        y = next((a for a in e["amm"] if a[2] >= bar), e["amm"][-1])
        p = y[8]
        res = math.sqrt(K_POOL * p)
        gross = q * p
        out, why = gross * (1 - pool_fee(p * 1e9)) * res / (res + gross), "TP_SWAP"
    if out is None:                                              # data ended (time exit at the last state)
        out, why = sell_curve(q, cv[-1][8]), "TIME" if cv[-1][2] >= te + HOLD - 60 else "END"
    return (out - 2 * tx) / x - 1, why


def stats(v):
    if len(v) < 3:
        return dict(n=len(v)), f"n {len(v)}"
    m, sd = statistics.mean(v), statistics.stdev(v)
    w, ls = [a for a in v if a > 0], [a for a in v if a <= 0]
    st = dict(n=len(v), mean=m, median=statistics.median(v), win=len(w) / len(v),
              pf=sum(w) / -sum(ls) if ls and sum(ls) else float("inf"), t=m / sd * math.sqrt(len(v)) if sd else 0,
              ex2=statistics.mean(sorted(v)[:-2]), best=max(v), worst=min(v))
    return st, (f"n {st['n']:4d} mean {m:+.2%} median {st['median']:+.2%} win {st['win']:.0%} PF {st['pf']:.2f} "
                f"t {st['t']:+.2f} ex-top2 {st['ex2']:+.2%} best {st['best']:+.0%} worst {st['worst']:+.0%}")


def score():
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from .listings import GT, get
    end = WINDOW[1]
    ev, trunc = [], 0
    for p in TRADES.glob("*.json.gz"):
        d = json.load(gzip.open(p, "rt", encoding="utf-8"))
        if not WINDOW[0] <= d["c"]["t"] < end:
            continue
        e = event(d)
        if e:
            ev.append(e)
            trunc += not e["complete"]
    ev.sort(key=lambda e: e["t0"])
    cut = int(len(ev) * 0.6)
    tr, te = ev[:cut], ev[cut:]
    print(f"crossings of R=40: {len(ev)} (truncated histories {trunc}, mayhem {sum(e['mh'] for e in ev)}); "
          f"train {len(tr)} test {len(te)}")
    s = requests.Session()
    d = get(s, f"{GT}/networks/solana/pools/{SOL_USDC}/ohlcv/day", dict(aggregate=1, limit=60, currency="usd"))
    sol = {int(r[0]) // 86400: r[4] for r in d["data"]["attributes"]["ohlcv_list"]}
    solp = lambda t: sol.get(int(t // 86400)) or statistics.mean(sol.values())      # noqa: E731
    model = make_pipeline(StandardScaler(), LogisticRegression())
    model.fit(np.array([e["f"] for e in tr]), np.array([e["y"] for e in tr]))
    p = model.predict_proba(np.array([e["f"] for e in te]))[:, 1]
    print(f"base rate P(reach 75 | crossed 40): train {statistics.mean(e['y'] for e in tr):.1%}, "
          f"test {statistics.mean(e['y'] for e in te):.1%}")
    print("calibration (test, predicted-probability deciles): bin | n | mean p | actual")
    order = np.argsort(p)
    for b in np.array_split(order, 10):
        if len(b):
            print(f"   {p[b].min():.2f}-{p[b].max():.2f} | {len(b):4d} | {p[b].mean():.2f} | "
                  f"{np.mean([te[i]['y'] for i in b]):.2f}")
    res = {}
    for name, sel in (("C1", [i for i in range(len(te)) if p[i] >= P_MIN]), ("C1-0", list(range(len(te))))):
        for label, kw in (("main", {}), ("tx 0.0002", dict(tx=0.0002)), ("1 slot", dict(slots=1)),
                          ("10 slots", dict(slots=10))):
            rows = [(te[i], trade(te[i], solp(te[i]["tx"]), **kw)) for i in sel]
            rows = [(e, r) for e, r in rows if r]
            st, txt = stats([r[0] for _, r in rows])
            print(f"{name:5s} {label:9s} {txt} | exits {dict(Counter(r[1] for _, r in rows))}")
            res[(name, label)] = (st, rows)
    rows = res[("C1", "main")][1]
    if rows:
        x = USD / statistics.mean(sol.values())
        print(f"fees as % of position (C1 main): curve 2 x {CURVE_FEE:.2%} + tx {2 * TX_SOL / x:.2%} of "
              f"{x:.3f} SOL")
        gaps = sorted(r[0] for _, r in rows if r[1] == "STOP")
        if gaps:
            print(f"stop fills (formula {(60 / 70) ** 2 - 1:+.1%} before costs): n {len(gaps)} median "
                  f"{statistics.median(gaps):+.1%} p10 {gaps[len(gaps) // 10]:+.1%} worst {gaps[0]:+.1%}")
    st, st0 = res[("C1", "main")][0], res[("C1-0", "main")][0]
    checks = dict(n=st.get("n", 0) >= 60, mean=st.get("mean", -1) >= 0.05, pf=st.get("pf", 0) >= 1.3,
                  ex2=st.get("ex2", -1) > 0, t=st.get("t", 0) >= 3,
                  vs_c10=st.get("mean", -1) - st0.get("mean", 0) >= 0.10)
    print("\nPASS checks (C1 test split):", checks, "->", "PASS" if all(checks.values()) else "FAIL")


def main() -> None:
    {"record": record, "score": score}[sys.argv[1]]()


if __name__ == "__main__":
    main()
