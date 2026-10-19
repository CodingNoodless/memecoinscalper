"""Polymarket resolved markets + hourly price history, for strategy V (prediction-market favourites).

    uv run python -m tradeslave.research.poly_data markets   # closed binary markets, volume >= MIN_VOL
    uv run python -m tradeslave.research.poly_data history   # Yes-token price, last WINDOW_D days before close
    ... markets all / history all                            # unbiased universe: volume >= $1k, last 4 days

Lifetime volume is a FUTURE quantity: upsets attract volume, so a high MIN_VOL keeps winning longshots more
often than losing ones (measured: Yes at 5-35c "wins" 4 points too often in >= $50k markets). The "all"
universe uses a $1k floor that nearly every market clears whatever its outcome (files *_all.jsonl).

Gamma API (public): market list (question, endDate = scheduled end known in advance, closedTime, outcome
prices at resolution, fee type, tick size). CLOB prices-history (public, <= ~10 days per request): hourly
Yes price over [max(start, endDate - WINDOW_D), closedTime]. Markets that closed before endDate - WINDOW_D
are skipped (a "within WINDOW_D of the scheduled end" rule never saw them open).
Output: data/poly/markets.jsonl, data/poly/hist.jsonl ({"id", "h": [[t, p], ...]}).
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

from ..paths import DATA_DIR

OUT = DATA_DIR / "poly"
GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB = "https://clob.polymarket.com/prices-history"
MIN_VOL = 50_000
END_MIN = "2025-10-01"
WINDOW_D = 14
CHUNK = 9 * 86400


def ts(s: str | None) -> float | None:
    if not s:
        return None
    s = re.sub(r"\.\d+", "", s.replace(" ", "T").replace("Z", "+00:00"))
    if s.endswith("+00"):
        s += ":00"
    if len(s) == 10:
        s += "T00:00:00+00:00"
    return datetime.fromisoformat(s).timestamp()


ALL = "all" in sys.argv[2:]
if ALL:
    MIN_VOL, WINDOW_D = 1_000, 4
SUF = "_all" if ALL else ""


def markets() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    n, off, cur = 0, 0, None
    with (OUT / f"markets{SUF}.jsonl").open("w", encoding="utf-8") as f:
        while True:
            q = dict(closed="true", limit=100, end_date_min=END_MIN, volume_num_min=MIN_VOL)
            if cur:
                q["after_cursor"] = cur
            for k in range(5):
                try:
                    r = s.get(GAMMA + "/keyset", params=q, timeout=30)
                    if r.ok:
                        break
                except requests.RequestException:
                    pass
                time.sleep(3 * (k + 1))
            d = r.json()
            page, cur = d.get("markets") or [], d.get("next_cursor")
            if not page:
                break
            for m in page:
                if json.loads(m.get("outcomes") or "[]") != ["Yes", "No"] or m.get("umaResolutionStatus") != "resolved":
                    continue
                ev = (m.get("events") or [{}])[0]
                f.write(json.dumps({
                    "id": m["id"], "q": m["question"], "event": ev.get("slug"), "start": ts(m.get("startDate")),
                    "end": ts(m.get("endDate")), "closed": ts(m.get("closedTime")),
                    "res": [float(x) for x in json.loads(m["outcomePrices"])], "vol": m.get("volumeNum"),
                    "fee": m.get("feeType"), "fees": m.get("feesEnabled"), "tick": m.get("orderPriceMinTickSize"),
                    "negRisk": m.get("negRisk"), "yes": json.loads(m["clobTokenIds"])[0]}) + "\n")
                n += 1
            off += len(page)
            if not cur:
                break
            if off % 2000 == 0:
                print(f"  {off} listed, {n} kept", flush=True)
    print(f"{n} resolved binary markets", flush=True)


def history() -> None:
    ms = [json.loads(line) for line in (OUT / f"markets{SUF}.jsonl").open(encoding="utf-8")]
    path = OUT / f"hist{SUF}.jsonl"
    have = {json.loads(line)["id"] for line in path.open(encoding="utf-8")} if path.exists() else set()
    todo = [m for m in ms if m["id"] not in have and m["end"] and m["closed"]
            and m["closed"] >= m["end"] - WINDOW_D * 86400
            and not (ALL and ((m["start"] or 0) > m["end"] - 2 * 86400 or int(m["id"]) % 2))]   # "all": listed by
    # end - 2 d (V2 decides at end - 24/72 h) and a 1-in-2 sample by market id (unbiased; halves fetch time)   # V2 decides at end - 24/72 h: must be listed by then
    print(f"{len(todo)} markets to fetch ({len(ms)} listed)", flush=True)
    lock = threading.Lock()
    s = requests.Session()
    done = [0]

    def one(m):
        a = max(m["start"] or 0, m["end"] - WINDOW_D * 86400)
        b = m["closed"] + 3600
        h = []
        while a < b:
            e = min(a + CHUNK, b)
            for k in range(6):
                try:
                    r = s.get(CLOB, params=dict(market=m["yes"], startTs=int(a), endTs=int(e), fidelity=60), timeout=30)
                    if r.status_code == 429:
                        time.sleep(10)
                        continue
                    if r.ok:
                        h += [[x["t"], x["p"]] for x in r.json().get("history", [])]
                    break
                except requests.RequestException:
                    time.sleep(3)
            a = e
        with lock, path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"id": m["id"], "h": h}) + "\n")
            done[0] += 1
            if done[0] % 1000 == 0:
                print(f"  {done[0]}/{len(todo)}", flush=True)

    with ThreadPoolExecutor(16 if ALL else 8) as ex:
        list(ex.map(one, todo))


def main() -> None:
    {"markets": markets, "history": history}[sys.argv[1] if len(sys.argv) > 1 else "markets"]()


if __name__ == "__main__":
    main()
