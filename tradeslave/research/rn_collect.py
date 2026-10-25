"""RN1000: collect the latest 1,000 CURVE runners: pump.fun tokens that filled the bonding curve (>= 60 s from create to
migration; 95% of $300k coins in Oct 21-24 were instant graduations: created and migrated in the same minute, thin volume,
back to ~$2k), migrated, and reached an all-time-high market cap of $300k. Facts only:
no LLM is used anywhere here (the vault write-ups are written by hand from these facts).

Steps (each resumable; output in data/runners/):
  migrations  walk the migration authority's history back from now (Helius), batch-check all-time highs (pump.fun swap-api,
              100 per call) and stop at 1,000 runners -> migrations.jsonl, runners.json
  details     coin details (pump.fun coins-v2/mints, Rohit's login token), metadata JSON (description, socials), the linked
              X post (fxtwitter) -> details.json
  curve       the first 500 curve transactions per runner (Helius): early buyers, snipers, tracked wallets -> curve/<mint>.json
  candles     post-migration 5-minute candles for 48 h (GeckoTerminal) -> candles/<mint>.json

    uv run python -u -m tradeslave.research.rn_collect migrations|details|curve|candles
"""

from __future__ import annotations

import json
import sys
import time

import requests

from ..paths import DATA_DIR
from .bw_test import MIG_AUTH, gtfa, pool_and_mint
from .fl_test import helius_url

OUT = DATA_DIR / "runners"
N_RUNNERS, ATH_MIN, STEP, CURVE_MIN_S, START_MAX = 1000, 300_000, 2 * 3600, 60, 10_000   # Rohit: "starting market cap under 10k"
IPFS = ("https://ipfs.io/ipfs/", "https://gateway.pinata.cloud/ipfs/", "https://dweb.link/ipfs/", "https://cloudflare-ipfs.com/ipfs/")
UA = "tradeslave-research/0.1 (personal research; low rate)"


def _pump_headers():
    from dotenv import dotenv_values
    tok = (dotenv_values(".env").get("PUMP_JWT") or "").strip().strip('"').strip("'")
    return {"User-Agent": UA, "Accept": "application/json", **({"Authorization": f"Bearer {tok}"} if tok else {})}


def ath_batch(mints, H):
    out = {}
    for i in range(0, len(mints), 100):
        chunk = mints[i:i + 100]
        last = None
        for k in range(10):                      # patient: a silent gap here drops runners from the walk
            try:
                r = requests.post("https://swap-api.pump.fun/v1/coins/ath/batch", headers=H, json={"addresses": chunk}, timeout=30)
                last = r.status_code
                if r.status_code in (200, 201):
                    for m, v in r.json().items():
                        out[m] = (v or {}).get("athMarketCap")
                    break
            except (requests.RequestException, ValueError) as e:
                last = type(e).__name__
            time.sleep(min(60, 3 * 2 ** k))
        else:
            raise RuntimeError(f"ATH batch kept failing ({last})")
        time.sleep(0.4)
    return out


def created_times(mints, H):
    """mint -> creation time (s), from pump.fun coin details (50 per call)."""
    out = {}
    for i in range(0, len(mints), 50):
        chunk = mints[i:i + 50]
        for k in range(4):
            try:
                r = requests.post("https://frontend-api-v3.pump.fun/coins-v2/mints", headers=H, json={"mints": chunk}, timeout=30)
                if r.status_code in (200, 201):
                    for c in r.json():
                        if c.get("created_timestamp"):
                            out[c["mint"]] = c["created_timestamp"] / 1000
                    break
            except (requests.RequestException, ValueError):
                pass
            time.sleep(2 * (k + 1))
        time.sleep(0.4)
    return out


_sol: dict = {}


def sol_usd(t):
    """SOL/USD for the hour containing t (Coinbase public candles, cached on disk)."""
    p = OUT / "sol_usd.json"
    if not _sol and p.exists():
        _sol.update({int(k): v for k, v in json.loads(p.read_text()).items()})
    h = int(t // 3600 * 3600)
    if h not in _sol:
        start = h - 200 * 3600
        try:
            r = requests.get("https://api.exchange.coinbase.com/products/SOL-USD/candles", timeout=20, headers={"User-Agent": UA},
                             params={"granularity": 3600, "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
                                     "end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(h + 3600))})
            for c in r.json() if r.ok else []:
                _sol[int(c[0])] = float(c[4])                  # [time, low, high, open, close, volume]
            p.write_text(json.dumps(_sol))
        except (requests.RequestException, ValueError):
            pass
    return _sol.get(h) or _sol.get(min(_sol, key=lambda k: abs(k - h))) if _sol else None


def start_mc(url, mint, created):
    """Market cap (USD) right after the coin's first trades on the curve (the create + dev buy)."""
    from ..scalper.trade_events import parse_trade_events
    txs, _ = gtfa(url, mint, created - 5, created + 300, "full", 20, 1)
    ev = sorted((e for tx in txs for e in parse_trade_events((tx.get("meta") or {}).get("logMessages") or []) if e["mint"] == mint and e["vt"]),
                key=lambda e: e["ts"])
    if not ev:
        return None
    first = [e for e in ev if e["ts"] <= ev[0]["ts"]]          # every trade in the launch second (dev buy, bundles)
    e = first[-1]
    usd = sol_usd(e["ts"])
    return e["vs"] / e["vt"] * 1e-3 * 1e9 * usd if usd else None


def fetch_meta(s, uri):
    """Metadata JSON, trying other public IPFS gateways when the first one fails."""
    urls = [uri]
    for g in IPFS:
        if uri.startswith(g):
            cid = uri[len(g):]
            urls = [g2 + cid for g2 in IPFS]
            break
    for u in urls:
        try:
            r = s.get(u, timeout=10)
            if r.ok:
                return r.json()
        except (requests.RequestException, ValueError):
            continue
    return {}


def migrations() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    url, H = helius_url(), _pump_headers()
    mp = OUT / "migrations.jsonl"
    rows = [json.loads(x) for x in mp.open()] if mp.exists() else []
    seen = {r["m"] for r in rows}
    end = min((r["t"] for r in rows if r.get("t")), default=time.time())
    miss = [r["m"] for r in rows if r.get("ath") is None]
    if miss:                                     # backfill rows saved while the ATH endpoint was failing
        ath = ath_batch(miss, H)
        for r in rows:
            if r.get("ath") is None and ath.get(r["m"]) is not None:
                r["ath"] = ath[r["m"]]
        print(f"backfilled ATH for {sum(1 for m in miss if ath.get(m) is not None)} of {len(miss)} rows", flush=True)
    created = {r["m"]: r.get("created") for r in rows if r.get("created")}
    need = [r["m"] for r in rows if (r.get("ath") or 0) >= ATH_MIN and r["m"] not in created]
    created.update(created_times(need, H))
    smc = {r["m"]: r["start_mc"] for r in rows if r.get("start_mc") is not None}

    def is_runner(r):
        c = created.get(r["m"])
        if (r.get("ath") or 0) < ATH_MIN or c is None or r["t"] - c < CURVE_MIN_S:
            return False
        if r["m"] not in smc:
            smc[r["m"]] = start_mc(url, r["m"], c)
        return smc[r["m"]] is not None and smc[r["m"]] < START_MAX
    runners = [r for r in rows if is_runner(r)]
    if miss:
        for r in rows:
            if r["m"] in created:
                r["created"] = created[r["m"]]
            if r["m"] in smc:
                r["start_mc"] = smc[r["m"]]
        tmp = mp.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
        tmp.replace(mp)
    print(f"resuming: {len(rows)} migrations, {len(runners)} curve runners, going back from {time.strftime('%m-%d %H:%M', time.localtime(end))}", flush=True)
    while len(runners) < N_RUNNERS:
        a = end - STEP
        txs, err = gtfa(url, MIG_AUTH, a, end, max_pages=30)
        batch = []
        for tx in txs:
            pool, m = pool_and_mint(tx)
            if m and m not in seen:
                seen.add(m)
                batch.append(dict(m=m, pool=pool, t=tx.get("blockTime"), sig=(tx.get("transaction") or {}).get("signatures", [None])[0]))
        ath = ath_batch([b["m"] for b in batch], H)
        cands = [b["m"] for b in batch if (ath.get(b["m"]) or 0) >= ATH_MIN]
        created.update(created_times(cands, H))
        n_new = 0
        with mp.open("a") as fh:
            for b in batch:
                b["ath"] = ath.get(b["m"])
                if b["m"] in created:
                    b["created"] = created[b["m"]]
                ok = is_runner(b)
                if b["m"] in smc:
                    b["start_mc"] = smc[b["m"]]
                fh.write(json.dumps(b) + "\n")
                if ok:
                    runners.append(b)
                    n_new += 1
        print(f"{time.strftime('%m-%d %H:%M', time.localtime(a))}: {len(batch)} migrations ({'capped' if err else 'complete'}), "
              f"{len(cands)} >= $300k, {n_new} curve runners | so far {len(runners)}", flush=True)
        end = a
    runners.sort(key=lambda r: -r["t"])
    (OUT / "runners.json").write_text(json.dumps(runners[:N_RUNNERS]))
    print(f"done: {len(runners[:N_RUNNERS])} runners, oldest migration {time.strftime('%Y-%m-%d %H:%M', time.localtime(runners[:N_RUNNERS][-1]['t']))}")


def details() -> None:
    from .m1_bot import SOC, tweet_info
    H = _pump_headers()
    R = json.loads((OUT / "runners.json").read_text())
    dp = OUT / "details.json"
    D = json.loads(dp.read_text(encoding="utf-8")) if dp.exists() else {}
    todo = [r["m"] for r in R if r["m"] not in D]
    for i in range(0, len(todo), 50):
        chunk = todo[i:i + 50]
        try:
            r = requests.post("https://frontend-api-v3.pump.fun/coins-v2/mints", headers=H, json={"mints": chunk}, timeout=30)
            for c in r.json() if r.status_code in (200, 201) else []:
                D[c["mint"]] = dict(coin={k: c.get(k) for k in ("name", "symbol", "description", "created_timestamp", "creator", "ath_market_cap",
                                                                    "ath_market_cap_timestamp", "metadata_uri", "image_uri", "is_charity", "tokenized_agent",
                                                                    "is_currently_live", "market_cap_usd", "volume_1h_usd", "program", "mayhem")})
        except (requests.RequestException, ValueError) as exc:
            print("details batch failed", type(exc).__name__, flush=True)
        time.sleep(0.6)
    s = requests.Session()
    s.headers["User-Agent"] = UA
    for n, (m, d) in enumerate(D.items()):
        if "meta" in d and any((d.get("meta") or {}).values()):
            continue
        uri = (d.get("coin") or {}).get("metadata_uri")
        meta = fetch_meta(s, uri) if uri else {}
        d["meta"] = {k: meta.get(k) for k in ("description", "twitter", "website", "telegram", "createdOn", "showName")}
        link = str(meta.get("twitter") or "")
        if "x.com" in link or "twitter.com" in link:
            info, _keys = tweet_info(SOC, link)
            d["x"] = dict(link=link, info=info)
            time.sleep(0.4)
        if n % 50 == 0:
            dp.write_text(json.dumps(D), encoding="utf-8")
            print(f"details {n}/{len(D)}", flush=True)
    dp.write_text(json.dumps(D), encoding="utf-8")
    print(f"details done: {len(D)} coins, {sum('x' in d for d in D.values())} with an X post")


def curve() -> None:
    from ..scalper.trade_events import parse_trade_events
    url = helius_url()
    R = json.loads((OUT / "runners.json").read_text())
    D = json.loads((OUT / "details.json").read_text(encoding="utf-8")) if (OUT / "details.json").exists() else {}
    cd = OUT / "curve"
    cd.mkdir(exist_ok=True)
    for n, r in enumerate(R):
        p = cd / f"{r['m']}.json"
        if p.exists():
            continue
        created = ((D.get(r["m"]) or {}).get("coin") or {}).get("created_timestamp")
        a = (created / 1000 - 5) if created else r["t"] - 7 * 86400
        txs, err = gtfa(url, r["m"], a, r["t"] + 60, max_pages=5)
        rows = []
        for tx in txs:
            for e in parse_trade_events((tx.get("meta") or {}).get("logMessages") or []):
                if e["mint"] == r["m"]:
                    rows.append([tx.get("slot"), e["ts"], e["user"], e["buy"], e["sol"] / 1e9, e["tok"] / 1e6, e["vs"] / 1e9, e["vt"] / 1e6])
        p.write_text(json.dumps(dict(m=r["m"], created=created, t_mig=r["t"], capped=bool(err), rows=rows)))
        if n % 50 == 0:
            print(f"curve {n}/{len(R)}: {len(rows)} trades", flush=True)
    print("curve done")


def candles() -> None:
    from .candles import Gecko
    R = json.loads((OUT / "runners.json").read_text())
    cd = OUT / "candles"
    cd.mkdir(exist_ok=True)
    g = Gecko()
    for n, r in enumerate(R):
        p = cd / f"{r['m']}.json"
        if p.exists():
            continue
        before = r["t"] + 48 * 3600 if r["t"] + 48 * 3600 < time.time() - 60 else None
        params = {"aggregate": 5, "limit": 1000, "currency": "usd", "token": r["m"]}
        if before:
            params["before_timestamp"] = int(before)
        js = g.get(f"/pools/{r['pool']}/ohlcv/minute", params) if r.get("pool") else None
        lst = (((js or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        rows = sorted([[float(x) for x in c] for c in lst if c[0] >= r["t"] - 300])
        p.write_text(json.dumps(dict(m=r["m"], pool=r.get("pool"), t_mig=r["t"], c=rows)))
        if n % 25 == 0:
            print(f"candles {n}/{len(R)}: {len(rows)} candles, 429s {g.rate_limited}", flush=True)
    print("candles done")


if __name__ == "__main__":
    {"migrations": migrations, "details": details, "curve": curve, "candles": candles}[sys.argv[1]]()
