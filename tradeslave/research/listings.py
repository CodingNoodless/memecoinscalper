"""Strategy C2: buy a Solana memecoin on the DEX right after Binance / Upbit announce a listing (pre-registered;
vault: "2026-10-20 listing announcements (C2)").

    uv run python -m tradeslave.research.listings fetch      # announcements (Binance CMS + Upbit), last ~178 days
    uv run python -m tradeslave.research.listings map        # ticker -> Solana token + top pool (GeckoTerminal)
    uv run python -m tradeslave.research.listings candles    # 1-minute pool candles around each announcement
    uv run python -m tradeslave.research.listings design|test

Announcements: Binance "New Cryptocurrency Listing" (catalog 48: spot, Alpha, futures, HODLer airdrops...) and
Upbit trade notices (KRW/BTC/USDT market additions). Release time = the post's publish time. Prices: GeckoTerminal
1-minute OHLCV of the token's deepest Solana pool (free API, last 180 days only).
"""

from __future__ import annotations

import json
import math
import re
import statistics
import sys
import time
from datetime import datetime
from itertools import product

import requests

from ..paths import DATA_DIR

OUT = DATA_DIR / "listings"
ANN = OUT / "announcements.json"
MAP = OUT / "tokens.json"
CAND = OUT / "candles"
FROZEN = OUT / "rule.json"
H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
GT = "https://api.geckoterminal.com/api/v2"


def get(s, url, params=None, gap=2.5):
    for k in range(8):
        time.sleep(gap)
        try:
            r = s.get(url, params=params, headers=H, timeout=30)
        except requests.RequestException:
            continue
        if r.status_code == 429:
            time.sleep(60)
            continue
        if r.status_code == 404:
            return None
        if r.ok:
            try:
                return r.json()
            except ValueError:
                pass
    return None


def fetch():
    OUT.mkdir(parents=True, exist_ok=True)
    s, cut, out = requests.Session(), time.time() - 178 * 86400, []
    for p in range(1, 60):
        d = get(s, "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query",
                dict(type=1, catalogId=48, pageNo=p, pageSize=50), gap=1)
        arts = (d or {}).get("data", {}).get("catalogs", [{}])[0].get("articles") or []
        out += [dict(src="binance", t=a["releaseDate"] / 1000, title=a["title"]) for a in arts]
        if not arts or arts[-1]["releaseDate"] / 1000 < cut:
            break
    print(f"binance {len(out)}", flush=True)
    for p in range(1, 41):
        d = get(s, "https://api-manager.upbit.com/api/v1/announcements",
                dict(os="web", page=p, per_page=20, category="trade"), gap=8)
        n = (d or {}).get("data", {}).get("notices") or []
        out += [dict(src="upbit", t=datetime.fromisoformat(x["listed_at"]).timestamp(), title=x["title"]) for x in n]
        if not n or n[-1] and datetime.fromisoformat(n[-1]["listed_at"]).timestamp() < cut:
            break
    ANN.write_text(json.dumps([x for x in out if x["t"] >= cut], ensure_ascii=False), encoding="utf-8")
    print(f"total {len(out)}", flush=True)


SKIP = re.compile(r"bStock|Stock|TradFi|Collateral|Earn|\bMargin\b|Delivery|Quarterly|Pre-IPO|Dividend|Trading Pairs &|"
                  r"거래지원 종료|유의|경고|입출금|중단|변경|연장|해제|Will Add .* on", re.I)


def tickers(x):
    t = x["title"]
    if SKIP.search(t):
        return []
    if x["src"] == "upbit":
        if "신규 거래지원" in t or "디지털 자산 추가" in t:
            return [y for y in re.findall(r"\(([A-Z0-9]{2,12})\)", t) if y not in ("KRW", "BTC", "USDT")]
        return []
    out = []
    if re.search(r"Will List|Alpha|HODLer Airdrops|Will Launch", t):
        out += re.findall(r"\(([^)]{1,15})\)", t)
        out += re.findall(r"\b([A-Z0-9一-鿿]{2,15})USDT\b", t)
    return [o for o in dict.fromkeys(out) if not re.fullmatch(r"\d{4}-\d\d-\d\d", o) and o not in EQUITY]


EQUITY = {"AMD", "QCOM", "USAR", "SKHY", "POPMART", "UNITREE", "HK0625", "GPRO"}   # stock perps without "Stock"


def events():
    ev, last = [], {}
    for x in sorted(json.loads(ANN.read_text(encoding="utf-8")), key=lambda x: x["t"]):
        for tk in tickers(x):
            if x["t"] - last.get(tk, -1e18) > 3 * 86400:
                ev.append(dict(tk=tk, t=x["t"], src=x["src"], title=x["title"]))
            last[tk] = x["t"]
    return ev


def show():
    for e in events():
        print(e["src"], datetime.fromtimestamp(e["t"]).strftime("%m-%d %H:%M"), e["tk"], "|", e["title"][:90])


MEME = {"WIF", "SPX", "USELESS", "MARSCOIN", "牛来", "哈基米", "CASHCAT", "GIGADEV"}   # labelled from names, pre-prices


def mapping():
    s, out = requests.Session(), {}
    for tk in sorted({e["tk"] for e in events()}):
        d = get(s, f"{GT}/search/pools", dict(query=tk, include="base_token")) or {}
        sym = {x["id"]: (x["attributes"].get("symbol") or "") for x in d.get("included") or []}
        best = None
        for p in d.get("data") or []:
            a, base = p["attributes"], p["relationships"]["base_token"]["data"]["id"]
            liq = float(a.get("reserve_in_usd") or 0)
            if sym.get(base, "").upper() == tk.upper() and liq >= 50000 and (best is None or liq > best[0]):
                net = p["relationships"]["network"]["data"]["id"] if "network" in p["relationships"] else p["id"].split("_")[0]
                best = (liq, net, a["address"], a.get("pool_created_at"), a["name"])
        out[tk] = best
        print(tk, best, flush=True)
    MAP.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")


CG = "https://api.coingecko.com/api/v3"
NETS = {"solana": "solana", "ethereum": "eth", "binance-smart-chain": "bsc", "base": "base", "arbitrum-one": "arbitrum",
        "polygon-pos": "polygon_pos", "avalanche": "avax", "optimistic-ethereum": "optimism", "sui": "sui-network",
        "tron": "tron", "the-open-network": "ton", "aptos": "aptos", "megaeth": "megaeth", "hyperevm": "hyperevm"}


def mapping2():
    """Amendment 1: ticker -> CoinGecko coin (exact symbol, best market-cap rank) -> its contracts -> that token's
    deepest GeckoTerminal pool created before the first post. A ticker search alone picked fake-liquidity pools."""
    s, out = requests.Session(), {}
    first = {}
    for e in events():
        first.setdefault(e["tk"], e["t"])
    for tk, t0 in sorted(first.items()):
        d = get(s, f"{CG}/search", dict(query=tk), gap=7) or {}
        cs = [c for c in d.get("coins") or [] if (c.get("symbol") or "").upper() == tk.upper()]
        cs.sort(key=lambda c: c.get("market_cap_rank") or 10 ** 9)
        best = None
        if cs:
            info = get(s, f"{CG}/coins/{cs[0]['id']}", dict(localization="false", tickers="false", market_data="false",
                                                             community_data="false", developer_data="false"), gap=7) or {}
            for plat, addr in (info.get("platforms") or {}).items():
                net = NETS.get(plat)
                if not net or not addr:
                    continue
                pd = get(s, f"{GT}/networks/{net}/tokens/{addr}/pools") or {}
                for p in pd.get("data") or []:
                    a = p["attributes"]
                    made = datetime.fromisoformat(a["pool_created_at"].replace("Z", "+00:00")).timestamp() \
                        if a.get("pool_created_at") else 0
                    base = p["relationships"]["base_token"]["data"]["id"].split("_", 1)[1]
                    liq = float(a.get("reserve_in_usd") or 0)
                    if base.lower() == addr.lower() and made < t0 and liq >= 50000 and (best is None or liq > best[0]):
                        best = (liq, net, a["address"], a.get("pool_created_at"), a["name"], cs[0]["id"])
        out[tk] = best
        print(tk, best, flush=True)
    MAP.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")


def candles():
    CAND.mkdir(parents=True, exist_ok=True)
    s, mp = requests.Session(), json.loads(MAP.read_text(encoding="utf-8"))
    for e in events():
        m = mp.get(e["tk"])
        f = CAND / f"{e['tk']}_{int(e['t'])}.json"
        if not m or f.exists() or time.time() - e["t"] < 26 * 3600:
            continue
        rows = {}
        for end in (e["t"] + 25 * 3600, e["t"] + 25 * 3600 - 1000 * 60):
            d = get(s, f"{GT}/networks/{m[1]}/pools/{m[2]}/ohlcv/minute",
                    dict(aggregate=1, before_timestamp=int(end), limit=1000, currency="usd", token="base")) or {}
            for r in d.get("data", {}).get("attributes", {}).get("ohlcv_list") or []:
                rows[int(r[0])] = r[:6]
        f.write_text(json.dumps(dict(e, pool=m, c=sorted(rows.values())), ensure_ascii=False), encoding="utf-8")
        print(e["tk"], len(rows), flush=True)


# ---------------------------------------------------------------- evaluation

LAG, COST, COST2 = 60, 0.01, 0.02
HOLDS = (15, 60, 240, 1440)


def outcome(d, hold, cost=COST):
    c, t = d["c"], d["t"]
    if not any(t - 3600 <= x[0] < t for x in c):                     # no trading in the hour before: dead pool
        return None
    k = next((i for i, x in enumerate(c) if x[0] >= t + LAG), None)
    if k is None or c[k][0] > t + LAG + 600:
        return None
    e, end = c[k][1], c[k][0] + hold * 60
    xs = [x for x in c[k:] if x[0] <= end]
    x = xs[-1][4]
    return (x / e) * (1 - cost) / (1 + cost) - 1


def stats(v):
    if len(v) < 3:
        return f"n {len(v)} " + " ".join(f"{r:+.1%}" for r in v)
    m, s = statistics.mean(v), statistics.stdev(v)
    return f"n {len(v):3d} mean {m:+.2%} t {m / s * math.sqrt(len(v)):+.2f} median {statistics.median(v):+.2%}"


def judge():
    ds = sorted((json.loads(f.read_text(encoding="utf-8")) for f in CAND.glob("*.json")), key=lambda d: d["t"])
    rows = [(d, outcome(d, 60)) for d in ds]
    rows = [(d, r) for d, r in rows if r is not None]
    print(f"events with candles {len(ds)}, tradeable {len(rows)}")
    v = [r for _, r in rows]
    h = len(v) // 2
    print(f"MAIN 60 min, 1%/side: {stats(v)}\n   halves {stats(v[:h])} / {stats(v[h:])}")
    print(f"   2%/side: {stats([outcome(d, 60, COST2) for d, _ in rows])}")
    for hold in HOLDS:
        vv = [(d, outcome(d, hold)) for d, _ in rows]
        print(f"  hold {hold:4d}: all {stats([r for _, r in vv])} | binance {stats([r for d, r in vv if d['src'] == 'binance'])}"
              f" | upbit {stats([r for d, r in vv if d['src'] == 'upbit'])} | meme {stats([r for d, r in vv if d['tk'] in MEME])}")
    for d, r in rows:
        pre = [x for x in d["c"] if d["t"] - 3600 <= x[0] < d["t"]]
        jump = d["c"][next(i for i, x in enumerate(d["c"]) if x[0] >= d["t"] + LAG)][1] / pre[-1][4] - 1
        print(f"    {d['src']:7s} {d['tk']:10s} {d['pool'][1]:8s} move before entry {jump:+7.1%}  60 min {r:+7.1%}")


def main() -> None:
    {"fetch": fetch, "events": show, "map": mapping2, "candles": candles, "judge": judge}[sys.argv[1]]()


if __name__ == "__main__":
    main()
