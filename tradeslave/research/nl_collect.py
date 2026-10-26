"""NL (vault "2026-10-25 narrative leaders (NL)"): pump.fun details (X link, website, description) for every create the C1
recorder logged since Oct 20, so narrative clusters can be built from what a trader sees, not just the name.

    uv run python -u -m tradeslave.research.nl_collect details    # data/narr_nl/details.json (resumable)
    uv run python -u -m tradeslave.research.nl_collect attention  # last <= 200 trades before age 3 min, pre-screened coins
    uv run python -u -m tradeslave.research.nl_collect search     # amendment 1: complete clusters via pump.fun search
    uv run python -u -m tradeslave.research.nl_collect labels     # graduation + ATH for attention coins
"""

from __future__ import annotations

import json
import random
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

from ..paths import DATA_DIR
from .rb_collect import _page_back, _rows
from .rn_collect import _pump_headers, ath_batch

OUT = DATA_DIR / "narr_nl"
LO, HI = datetime(2026, 10, 22, 18).timestamp(), datetime(2026, 10, 25, 12).timestamp()
TAU, PRESCREEN_R, LOWER_N, SEED = 180, 20.0, 300, 25
DEADLINE = datetime(2026, 10, 26, 1, 30).timestamp()     # amendment 1: the sample is the files written by 01:30
KEEP = ("name", "symbol", "description", "twitter", "website", "telegram", "created_timestamp", "creator", "complete")


def details() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / "details.json"
    D = json.loads(p.read_text()) if p.exists() else {}
    mints = []
    for line in (DATA_DIR / "c1" / "creates.jsonl").open():
        x = json.loads(line)
        if x.get("bc"):                                   # pump.fun curve creates only
            mints.append(x["m"])
    need = [m for m in dict.fromkeys(mints) if m not in D]
    print(f"details: {len(D)} cached, {len(need)} to fetch", flush=True)
    H = _pump_headers()
    for i in range(0, len(need), 50):
        chunk = need[i:i + 50]
        for k in range(8):
            try:
                r = requests.post("https://frontend-api-v3.pump.fun/coins-v2/mints", headers=H, json={"mints": chunk}, timeout=30)
                if r.status_code in (200, 201):
                    for c in r.json():
                        D[c["mint"]] = {x: c.get(x) for x in KEEP}
                    for m in chunk:
                        D.setdefault(m, None)
                    break
                if r.status_code == 429:
                    time.sleep(min(60, 5 * (k + 1)))
                    continue
            except (requests.RequestException, ValueError):
                pass
            time.sleep(min(60, 3 * 2 ** k))
        if (i // 50) % 100 == 0:
            p.write_text(json.dumps(D))
            print(f"details {i}/{len(need)} {time.strftime('%H:%M')}", flush=True)
        time.sleep(0.3)
    p.write_text(json.dumps(D))
    print(f"details done: {sum(v is not None for v in D.values())}/{len(D)} with records")


def universe():
    """Pre-screened coins (C1 max R >= 20) plus a seeded random 300 from each lower bucket, with bucket sizes."""
    mx = defaultdict(float)
    for line in (DATA_DIR / "c1" / "checks.jsonl").open():
        x = json.loads(line)
        mx[x["m"]] = max(mx[x["m"]], x.get("maxR") or 0)
    buckets = defaultdict(list)
    creates = {}
    for line in (DATA_DIR / "c1" / "creates.jsonl").open():
        x = json.loads(line)
        if not x.get("bc") or not LO <= x["t"] < HI or x["m"] not in mx:
            continue
        creates[x["m"]] = x
        r = mx[x["m"]]
        buckets["top" if r >= PRESCREEN_R else "<5" if r < 5 else "5-10" if r < 10 else "10-20"].append(x["m"])
    rng = random.Random(SEED)
    pick = {"top": sorted(buckets["top"])}
    for b in ("<5", "5-10", "10-20"):
        pick[b] = rng.sample(sorted(buckets[b]), min(LOWER_N, len(buckets[b])))
    return creates, {b: len(v) for b, v in buckets.items()}, pick


def attention() -> None:
    out = OUT / "trades"
    out.mkdir(parents=True, exist_ok=True)
    creates, sizes, pick = universe()
    up = OUT / "universe.json"
    if up.exists():                                       # frozen at the amendment-1 run: C1 checks keep moving bucket edges
        u = json.loads(up.read_text())
        sizes, pick = u["sizes"], u["pick"]
    else:
        up.write_text(json.dumps(dict(sizes=sizes, pick=pick)))
    todo = [(b, m) for b, ms in pick.items() for m in ms]
    random.Random(SEED + 1).shuffle(todo)                 # amendment 1: one seeded random order, so any prefix is uniform
    todo = [(b, m) for b, m in todo if not (out / f"{m}.json").exists()]
    print(f"attention: bucket sizes {sizes}; {len(todo)} coins to fetch", flush=True)
    H = _pump_headers()

    def job(bm):
        b, m = bm
        if time.time() >= DEADLINE:
            return "past deadline"
        c = creates[m]
        s = requests.Session()
        a, st = _page_back(s, H, m, c["t"] + TAU, 1)
        rows = _rows(a)
        hot = len(rows) >= 100 and min(r[0] for r in rows) > int(c["t"]) + 1
        if hot and st == "ok":
            b2, st = _page_back(s, H, m, min(r[0] for r in rows), 1)
            seen = {(r[0], r[1], r[3], r[4]) for r in rows}
            rows = [r for r in _rows(b2) if (r[0], r[1], r[3], r[4]) not in seen] + rows
        (out / f"{m}.json").write_text(json.dumps(dict(m=m, bucket=b, created=c["t"], uri=c.get("uri"), dev_sol=c.get("sol"),
                                                       mayhem=c.get("mh"), hot=hot, status=st, rows=rows)))
        return f"{b} {'hot' if hot else 'quiet'} {st}"

    n, got = 0, defaultdict(int)
    with ThreadPoolExecutor(2) as ex:
        for st in ex.map(lambda z: _safe(job, z), todo):
            n += 1
            got[st] += 1
            if n % 200 == 0:
                print(f"attention {n}/{len(todo)} {dict(got)} {time.strftime('%H:%M')}", flush=True)
    print(f"attention done {dict(got)}", flush=True)


def _safe(job, x):
    try:
        return job(x)
    except Exception as exc:  # noqa: BLE001
        return f"failed: {type(exc).__name__}"


SEARCH = "https://frontend-api-v3.pump.fun/coins/search-v2"


def search() -> None:
    """Amendment 1: complete each attention coin's narrative cluster with pump.fun search (name, ticker, <= 2 path keys;
    2 pages of 50 each). Loops until the attention fetch has stopped writing files."""
    from .nl_model import _alnum, keys
    D = json.loads((OUT / "details.json").read_text())
    p = OUT / "search.json"
    S = json.loads(p.read_text()) if p.exists() else {}
    H = _pump_headers()
    idle = 0
    while idle < 3:
        hot = []
        for q in sorted((OUT / "trades").glob("*.json")):
            if q.stem in S:
                continue
            x = json.loads(q.read_text())
            if x["hot"] and x["status"] == "ok":
                hot.append(x["m"])
        if not hot:
            idle += 1
            time.sleep(300)
            continue
        idle = 0
        for m in hot:
            d = D.get(m) or {}
            qs = [d.get("name") or "", d.get("symbol") or ""]
            qs += [k[3:] for k in sorted(keys(d.get("name"), d.get("symbol"), d)) if k.startswith("nm:") and k[3:] != _alnum(d.get("name"))
                   and not k[3:].isdigit()][:2]
            res = {}
            for qq in dict.fromkeys(q for q in qs if len(_alnum(q)) >= 3):
                for off in (0, 50):
                    js = None
                    for k in range(6):
                        try:
                            r = requests.get(SEARCH, headers=H, params={"searchTerm": qq, "limit": 50, "offset": off, "includeNsfw": "true"},
                                             timeout=30)
                            if r.ok:
                                js = r.json()
                                break
                            if r.status_code == 429:
                                time.sleep(5 * (k + 1))
                                continue
                        except (requests.RequestException, ValueError):
                            pass
                        time.sleep(2 * (k + 1))
                    js = js if isinstance(js, list) else (js or {}).get("coins") or []
                    for c in js:
                        res[c["mint"]] = dict(q=qq, name=c.get("name"), symbol=c.get("symbol"), t=(c.get("created_timestamp") or 0) / 1000,
                                              ath=c.get("ath_market_cap"), ath_t=(c.get("ath_market_cap_timestamp") or 0) / 1000,
                                              complete=c.get("complete"))
                    if len(js) < 50:
                        break
                    time.sleep(0.2)
            S[m] = dict(queries=qs, results=res)
            if len(S) % 100 == 0:
                p.write_text(json.dumps(S))
                print(f"search {len(S)} coins {time.strftime('%H:%M')}", flush=True)
        p.write_text(json.dumps(S))
    print(f"search done: {len(S)} coins", flush=True)


def labels() -> None:
    hot = [json.loads(p.read_text()) for p in sorted((OUT / "trades").glob("*.json"))]
    mints = [x["m"] for x in hot if x["hot"]]
    H = _pump_headers()
    D = {}
    for i in range(0, len(mints), 50):
        chunk = mints[i:i + 50]
        for k in range(8):
            try:
                r = requests.post("https://frontend-api-v3.pump.fun/coins-v2/mints", headers=H, json={"mints": chunk}, timeout=30)
                if r.status_code in (200, 201):
                    for c in r.json():
                        D[c["mint"]] = dict(complete=bool(c.get("complete")))
                    break
            except (requests.RequestException, ValueError):
                pass
            time.sleep(min(60, 3 * 2 ** k))
        time.sleep(0.3)
    ath = ath_batch(mints, H)
    for m in mints:
        D.setdefault(m, {})["ath"] = ath.get(m)
    (OUT / "labels.json").write_text(json.dumps(dict(fetched=time.time(), labels=D)))
    print(f"labels: {len(mints)} attention coins, {sum(1 for v in D.values() if v.get('complete'))} graduated, "
          f"{sum(1 for v in D.values() if (v.get('ath') or 0) >= 300_000)} with ATH >= $300k; fetched {time.strftime('%Y-%m-%d %H:%M')}")


if __name__ == "__main__":
    {"details": details, "attention": attention, "search": search, "labels": labels}[sys.argv[1] if len(sys.argv) > 1 else "details"]()
