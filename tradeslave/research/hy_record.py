"""HY: record hype signals (pump.fun livestreams, DexScreener boosts) and the price path of each hyped coin for 4 h.
Vault "2026-10-23 hype following - livestreams and paid boosts (HY)", pre-registered 23:55 PDT.

    uv run python -u -m tradeslave.research.hy_record record   # loop every 60 s
    uv run python -u -m tradeslave.research.hy_record score    # score the recorded paths
"""

from __future__ import annotations

import json
import statistics
import sys
import time

import requests

from ..paths import DATA_DIR

DIR = DATA_DIR / "hype"
LIVE = "https://frontend-api-v3.pump.fun/coins/currently-live"
BOOST = "https://api.dexscreener.com/token-boosts/latest/v1"
TOPB = "https://api.dexscreener.com/token-boosts/top/v1"
PRICE = "https://api.dexscreener.com/tokens/v1/solana/{}"
TRACK_S, COST = 4 * 3600, 0.025


def jget(s, url, **kw):
    try:
        r = s.get(url, timeout=30, **kw)
        return r.json() if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def prices(s, mints):
    out = {}
    for i in range(0, len(mints), 30):
        d = jget(s, PRICE.format(",".join(mints[i:i + 30])))
        for p in d or []:
            m = (p.get("baseToken") or {}).get("address")
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if m and p.get("priceNative") and (m not in out or liq > out[m][1]):
                out[m] = (float(p["priceNative"]), liq)
        time.sleep(0.3)
    return {m: v[0] for m, v in out.items()}


def record() -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    sig_p, path_p = DIR / "signals.jsonl", DIR / "paths.jsonl"
    seen = {(json.loads(x)["src"], json.loads(x)["m"]) for x in sig_p.open()} if sig_p.exists() else set()
    active = {}                                                    # (src, mint) -> signal time
    if sig_p.exists():
        for x in sig_p.open():
            j = json.loads(x)
            if time.time() - j["t"] < TRACK_S:
                active[(j["src"], j["m"])] = j["t"]
    s = requests.Session()
    s.headers["User-Agent"] = "tradeslave-research/0.1 (personal research; low rate)"
    while True:
        t0 = time.time()
        new = []
        live = jget(s, LIVE, params={"limit": 100, "offset": 0, "includeNsfw": "false"}) or []
        for c in live:
            if (c.get("num_participants") or 0) >= 20:
                new.append(("LIVE20", c["mint"], dict(viewers=c.get("num_participants"), mc=c.get("usd_market_cap"), complete=c.get("complete"))))
        for src, url in (("BOOST", BOOST), ("TOPBOOST", TOPB)):
            for b in jget(s, url) or []:
                if b.get("chainId") == "solana":
                    new.append((src, b["tokenAddress"], dict(amount=b.get("amount"), total=b.get("totalAmount"))))
        with sig_p.open("a") as fh:
            for src, m, det in new:
                if (src, m) in seen:
                    continue
                seen.add((src, m))
                active[(src, m)] = t0
                fh.write(json.dumps(dict(t=t0, src=src, m=m, **det)) + "\n")
        for k in [k for k, ts in active.items() if t0 - ts > TRACK_S]:
            active.pop(k)
        mints = sorted({m for _, m in active})
        px = prices(s, mints) if mints else {}
        with path_p.open("a") as fh:
            for m, p in px.items():
                fh.write(json.dumps(dict(t=t0, m=m, p=p)) + "\n")
        if int(t0) % 600 < 60:
            print(time.strftime("%H:%M"), f"signals {len(seen)}, tracking {len(mints)}, priced {len(px)}", flush=True)
        time.sleep(max(5.0, 60 - (time.time() - t0)))


def score() -> None:
    sig = [json.loads(x) for x in (DIR / "signals.jsonl").open()]
    path = {}
    for x in (DIR / "paths.jsonl").open():
        j = json.loads(x)
        path.setdefault(j["m"], []).append((j["t"], j["p"]))
    for v in path.values():
        v.sort()
    res = {}
    for s in sig:
        pts = [(t, p) for t, p in path.get(s["m"], []) if t > s["t"] + 30]       # entry: next poll after the signal
        if len(pts) < 2:
            continue
        te, pe = pts[0]
        if pts[-1][0] < te + 3600 and time.time() - te < TRACK_S:
            continue                                                             # not resolved yet
        out = {}
        for lab, h in (("hold15", 900), ("hold60", 3600), ("hold4h", TRACK_S)):
            q = [p for t, p in pts if t <= te + h]
            out[lab] = q[-1] / pe * (1 - COST) - 1
        main = None
        for t, p in pts:
            r = p / pe
            if r <= 0.75 or r >= 2.0 or t >= te + 3600:
                main = r * (1 - COST) - 1
                break
        out["MAIN"] = main if main is not None else pts[-1][1] / pe * (1 - COST) - 1
        res.setdefault(s["src"], []).append((time.strftime("%m-%d", time.localtime(te)), out))
    for src, v in res.items():
        print(f"{src}: n {len(v)}")
        for k in ("MAIN", "hold15", "hold60", "hold4h"):
            vals = [o[k] for _, o in v]
            days = sorted({d for d, _ in v})
            print(f"   {k:7s} mean {statistics.mean(vals):+6.1%} med {statistics.median(vals):+6.1%} win {sum(x > 0 for x in vals) / len(vals):4.0%} | "
                  + " ".join(f"{d}: {statistics.mean([o[k] for dd, o in v if dd == d]):+.1%}" for d in days))


if __name__ == "__main__":
    {"record": record, "score": score}[sys.argv[1] if len(sys.argv) > 1 else "score"]()
