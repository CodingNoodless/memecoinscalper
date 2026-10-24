"""DL: snapshot memecoin DLMM pools (Meteora public data API) every 30 min, and score LP returns between snapshots.
Vault "2026-10-23 memecoin DLMM liquidity provision (DL)", pre-registered 23:51 PDT.

    uv run python -u -m tradeslave.research.dl_record record    # loop: one snapshot every 30 min
    uv run python -u -m tradeslave.research.dl_record score     # LP return estimates over 6 h / 12 h windows
"""

from __future__ import annotations

import json
import math
import statistics
import sys
import time
from pathlib import Path

import requests

from ..paths import DATA_DIR

API = "https://dlmm.datapi.meteora.ag/pools"
DIR = DATA_DIR / "dlmm"
SOL = "So11111111111111111111111111111111111111112"
MAJORS = {"USDC", "USDT", "SOL", "WSOL", "JUP", "JTO", "BONK", "WIF", "JITOSOL", "MSOL", "BSOL", "PYTH", "RAY", "ORCA", "W", "TRUMP",
          "POPCAT", "PENGU", "FARTCOIN", "USDS", "PYUSD", "ETH", "WBTC", "CBBTC", "INF", "HNT", "RENDER"}
EVERY = 1800


def snapshot(s: requests.Session) -> int:
    pools = []
    for page in range(1, 6):
        try:
            r = s.get(API, params={"page": page, "page_size": 200, "sort_by": "volume_24h:desc"}, timeout=60)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        for p in r.json().get("data", []):
            x, y = p["token_x"], p["token_y"]
            if y.get("address") != SOL or (x.get("symbol") or "").upper() in MAJORS or p.get("is_blacklisted"):
                continue
            if (p.get("tvl") or 0) < 500:
                continue
            pools.append(dict(a=p["address"], name=p["name"], tvl=p["tvl"], price=p.get("current_price"), created=p.get("created_at"),
                              fees=p.get("fees"), pfees=p.get("protocol_fees"), vol=p.get("volume"), cfg=p.get("pool_config"),
                              mint=x.get("address")))
        time.sleep(1.0)
    DIR.mkdir(parents=True, exist_ok=True)
    t = int(time.time())
    (DIR / f"snap_{t}.json").write_text(json.dumps(dict(t=t, pools=pools)))
    return len(pools)


def record() -> None:
    s = requests.Session()
    s.headers["User-Agent"] = "tradeslave-research/0.1 (personal research; low rate)"
    while True:
        t0 = time.time()
        n = snapshot(s)
        print(time.strftime("%H:%M"), f"snapshot: {n} SOL-quoted non-major pools", flush=True)
        time.sleep(max(60.0, EVERY - (time.time() - t0)))


def score() -> None:
    snaps = sorted(DIR.glob("snap_*.json"))
    S = [json.loads(p.read_text()) for p in snaps]
    if len(S) < 2:
        print("need at least two snapshots")
        return
    for win, key in ((6 * 3600, "4h"), (12 * 3600, "12h")):
        rows = []
        for i, a in enumerate(S):
            later = [b for b in S[i + 1:] if abs(b["t"] - a["t"] - win) <= 1200]
            if not later:
                continue
            b = later[0]
            bmap = {p["a"]: p for p in b["pools"]}
            for p in a["pools"]:
                f24 = ((p["fees"] or {}).get("24h") or 0) - ((p["pfees"] or {}).get("24h") or 0)
                age_h = (a["t"] * 1000 - (p["created"] or a["t"] * 1000)) / 3.6e6
                if p["tvl"] < 5000 or age_h < 6 or f24 / p["tvl"] < 0.05 or ((p["vol"] or {}).get("24h") or 0) < 2 * p["tvl"]:
                    continue
                q = bmap.get(p["a"])
                if q is None or (q["tvl"] or 0) < 500 or not p["price"] or not q["price"]:
                    r, fees = 0.05, 0.0
                else:
                    r = q["price"] / p["price"]
                    hours = win / 3600
                    fw = ((q["fees"] or {}).get("12h" if hours >= 12 else "4h") or 0) - ((q["pfees"] or {}).get("12h" if hours >= 12 else "4h") or 0)
                    fees = fw * (hours / (12 if hours >= 12 else 4)) / p["tvl"]
                opt = fees + math.sqrt(r) - 1
                pes = fees + (r - 1 if r < 1 else 0.0)
                rows.append(dict(t=a["t"], fees=fees, r=r, opt=opt, pes=pes))
        if not rows:
            print(f"{win // 3600} h: no complete windows yet")
            continue
        print(f"{win // 3600} h windows: n {len(rows)} | fees mean {statistics.mean(x['fees'] for x in rows):+.1%} | price ratio median "
              f"{statistics.median(x['r'] for x in rows):.2f} | LP optimistic mean {statistics.mean(x['opt'] for x in rows):+.1%} med "
              f"{statistics.median(x['opt'] for x in rows):+.1%} | pessimistic mean {statistics.mean(x['pes'] for x in rows):+.1%} med "
              f"{statistics.median(x['pes'] for x in rows):+.1%}")


if __name__ == "__main__":
    {"record": record, "score": score}[sys.argv[1] if len(sys.argv) > 1 else "score"]()
