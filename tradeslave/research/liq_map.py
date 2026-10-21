"""Strategy Y19: Hyperliquid's public liquidation map (forward paper test; pre-registered; vault:
"2026-10-20 liquidation map (Y19)").

    uv run python -m tradeslave.research.liq_map scan     # every 30 min: top accounts' liquidation prices -> buckets
    uv run python -m tradeslave.research.liq_map score    # H1 cluster bids / H2 magnet vs controls, HL 1-min candles

Each scan sums, per coin, the notional that would be force-closed in 0.5%-wide buckets of distance from the mid
(longs below, shorts above, -30%..+30%), over the leaderboard's top N accounts by account value.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from ..paths import DATA_DIR

OUT = DATA_DIR / "liq_map"
SNAP = OUT / "snap.jsonl"
INFO = "https://api.hyperliquid.xyz/info"
BOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
TOP, TRADERS, EVERY, RPS = 1500, 3000, 1800, 7.0
WS = "wss://api.hyperliquid.xyz/ws"
STEP, SPAN = 0.005, 0.30
MEME = {"kPEPE", "kBONK", "kSHIB", "kFLOKI", "DOGE", "WIF", "POPCAT", "FARTCOIN", "MOODENG", "PNUT", "GOAT", "TRUMP",
        "PENGU", "MEW", "BOME", "PUMP", "BRETT", "SPX", "MOG"}
COST, THRU = 0.003, 0.002


class Limiter:
    def __init__(self, rps):
        self.gap, self.next, self.lock = 1 / rps, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            now = time.time()
            t = max(now, self.next)
            self.next = t + self.gap
        time.sleep(max(0.0, t - now))


def post(s, lim, body):
    for k in range(5):
        lim.wait()
        try:
            r = s.post(INFO, json=body, timeout=20)
            if r.status_code == 429:
                time.sleep(10)
                continue
            if r.ok:
                return r.json()
        except (requests.RequestException, ValueError):
            time.sleep(2)
    return None


def accounts(s):
    rows = s.get(BOARD, timeout=120).json()["leaderboardRows"]
    rows.sort(key=lambda x: -float(x.get("accountValue") or 0))
    return [x["ethAddress"] for x in rows[:TOP]]


def scan_once(s, lim, users):
    meta, ctx = post(s, lim, {"type": "metaAndAssetCtxs"})
    mids = {u["name"]: float(c["midPx"] or c["markPx"]) for u, c in zip(meta["universe"], ctx) if c.get("markPx")}
    vol = {u["name"]: float(c.get("dayNtlVlm") or 0) for u, c in zip(meta["universe"], ctx)}
    nb = int(round(SPAN / STEP))
    book = {}                                    # coin -> {"L": [..nb], "S": [..nb]}
    lock = threading.Lock()

    def one(u):
        st = post(s, lim, {"type": "clearinghouseState", "user": u})
        for p in (st or {}).get("assetPositions") or []:
            p = p["position"]
            c, lp, szi = p["coin"], p.get("liquidationPx"), float(p["szi"])
            if not lp or c not in mids or szi == 0:
                continue
            d = float(lp) / mids[c] - 1
            side = "L" if szi > 0 else "S"
            if (side == "L" and not -SPAN < d < 0) or (side == "S" and not 0 < d < SPAN):
                continue
            k = min(int(abs(d) / STEP), nb - 1)
            with lock:
                b = book.setdefault(c, {"L": [0.0] * nb, "S": [0.0] * nb})
                b[side][k] += abs(float(p["positionValue"]))

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(one, users))
    return dict(t=time.time(), mids={c: mids[c] for c in book}, vol={c: vol[c] for c in book},
                book={c: {k: [round(x) for x in v] for k, v in b.items()} for c, b in book.items()}, n=len(users))


SEEN: dict = {}                                  # memecoin trader address -> last time seen trading


def collect(coins):
    """Amendment 1: the top accounts by value hardly hold memecoins, so also scan addresses seen trading them
    (websocket trades carry both users)."""
    from websockets.sync.client import connect
    while True:
        try:
            with connect(WS, max_size=None) as ws:
                for c in coins:
                    ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": c}}))
                for msg in ws:
                    d = json.loads(msg)
                    if d.get("channel") == "trades":
                        for x in d["data"]:
                            for u in x.get("users") or []:
                                SEEN[u] = time.time()
        except Exception as exc:  # noqa: BLE001 - reconnect
            print(time.strftime("%H:%M"), "trade feed:", exc, flush=True)
            time.sleep(5)


def scan():
    OUT.mkdir(parents=True, exist_ok=True)
    s, lim = requests.Session(), Limiter(RPS)
    meta = post(s, lim, {"type": "meta"})
    threading.Thread(target=collect, args=([u["name"] for u in meta["universe"] if u["name"] in MEME],),
                     daemon=True).start()
    top, got = [], 0.0
    while True:
        t0 = time.time()
        try:
            if time.time() - got > 6 * 3600 or not top:
                top, got = accounts(s), time.time()
            recent = sorted(SEEN, key=SEEN.get, reverse=True)[:TRADERS]
            users = list(dict.fromkeys(top + recent))
            snap = scan_once(s, lim, users)
            with SNAP.open("a", encoding="utf-8") as f:
                f.write(json.dumps(snap) + "\n")
            big = sorted(((sum(b["L"]) + sum(b["S"]), c) for c, b in snap["book"].items()), reverse=True)[:5]
            print(time.strftime("%H:%M"), f"scan {time.time() - t0:.0f}s users {len(users)} (traders {len(recent)}) coins {len(snap['book'])}",
                  " ".join(f"{c} ${v / 1e6:.1f}M" for v, c in big), flush=True)
        except Exception as exc:  # noqa: BLE001 - keep scanning
            print(time.strftime("%H:%M"), "scan failed:", exc, flush=True)
        time.sleep(max(60.0, EVERY - (time.time() - t0)))


# ---------------------------------------------------------------- scoring

def candles(s, coin, lo, hi, cache={}):
    if coin not in cache:
        rows, start = [], int(lo * 1000)
        while start < hi * 1000:
            d = s.post(INFO, json={"type": "candleSnapshot", "req": {"coin": coin, "interval": "1m", "startTime": start,
                                                                       "endTime": int(hi * 1000)}}, timeout=30).json()
            if not d:
                break
            rows += [(x["t"] / 1000, float(x["o"]), float(x["h"]), float(x["l"]), float(x["c"])) for x in d]
            start = d[-1]["t"] + 60000
            time.sleep(0.3)
        cache[coin] = sorted(dict((r[0], r) for r in rows).values())
    return cache[coin]


def bid_trade(c, t, bid, tp_mult=1.03, hold=86400, valid=1800):
    k = next((i for i, r in enumerate(c) if t <= r[0] < t + valid and r[3] <= bid * (1 - THRU)), None)
    if k is None:
        return None
    tf, tp = c[k][0], bid * tp_mult
    if c[k][4] >= tp * (1 + THRU):
        x = tp
    else:
        x = None
        for r in c[k + 1:]:
            if r[0] > tf + hold:
                break
            if r[2] >= tp * (1 + THRU):
                x = tp
                break
            last = r[4]
        if x is None:
            if c[-1][0] < tf + hold:
                return "open"
            x = last
    return tf, (x / bid) * (1 - COST) / (1 + COST) - 1


def buy_trade(c, t, tp, hold=4 * 3600):
    k = next((i for i, r in enumerate(c) if r[0] >= t + 60), None)
    if k is None or c[-1][0] < c[k][0] + hold:
        return "open"
    e, x = c[k][4], None
    for r in c[k + 1:]:
        if r[0] > c[k][0] + hold:
            break
        if r[2] >= tp * (1 + THRU):
            x = tp
            break
        last = r[4]
    x = last if x is None else x
    return c[k][0], (x / e) * (1 - COST) / (1 + COST) - 1


def stats(v):
    if len(v) < 3:
        return f"n {len(v)}"
    m, sd = statistics.mean(v), statistics.stdev(v)
    return f"n {len(v):4d} mean {m:+.2%} t {m / sd * math.sqrt(len(v)) if sd else 0:+.2f} win {sum(x > 0 for x in v) / len(v):.0%}"


def score():
    snaps = [json.loads(x) for x in SNAP.open(encoding="utf-8")]
    s = requests.Session()
    lo, hi = snaps[0]["t"] - 60, time.time()
    res = {k: [] for k in ("H1", "H1c", "H2", "H2c")}
    last1, last2 = {}, {}
    for sn in snaps:
        for coin, b in sn["book"].items():
            v, mid = sn["vol"].get(coin, 0), sn["mids"][coin]
            if v <= 0:
                continue
            grp = "meme" if coin in MEME else "other"
            L = [(b["L"][k], k) for k in range(int(0.03 / STEP), int(0.15 / STEP))]
            nL, kL = max(L)
            S = [(b["S"][k], k) for k in range(int(0.01 / STEP), int(0.05 / STEP))]
            nS, kS = max(S)
            c = None
            # H1: bid at the cluster's lower edge x 0.99; control: same distance at no-cluster scans
            dist = (kL + 1) * STEP
            hit1 = nL >= 0.01 * v
            if hit1 or sn["t"] - last1.get(coin, -1e18) >= 86400:
                c = c or candles(s, coin, lo, hi)
                r = bid_trade(c, sn["t"], mid * (1 - dist) * 0.99)
                if isinstance(r, tuple):
                    key = "H1" if hit1 else "H1c"
                    if not hit1 or r[0] - last1.get(coin, -1e18) >= 86400:
                        res[key].append((grp, r[1]))
                        if hit1:
                            last1[coin] = r[0]
            # H2: buy now, exit at the short cluster's lower edge; control: same exit distance, no cluster
            hit2 = nS >= 0.01 * v
            if sn["t"] - last2.get(coin, -1e18) >= 4 * 3600:
                c = c or candles(s, coin, lo, hi)
                r = buy_trade(c, sn["t"], mid * (1 + kS * STEP))
                if isinstance(r, tuple):
                    res["H2" if hit2 else "H2c"].append((grp, r[1]))
                    last2[coin] = sn["t"]
    print(f"scans {len(snaps)} from {time.strftime('%m-%d %H:%M', time.localtime(snaps[0]['t']))}")
    for k, xs in res.items():
        print(f"{k:4s} meme {stats([r for g, r in xs if g == 'meme'])} | all {stats([r for _, r in xs])}")


def main() -> None:
    {"scan": scan, "score": score}[sys.argv[1]]()


if __name__ == "__main__":
    main()
