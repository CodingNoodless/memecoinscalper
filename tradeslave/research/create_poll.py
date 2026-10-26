"""Complete create log from pump.fun's own listing (newest first), with X link, website and description.

The PumpPortal log (launch_watch -> data/c1/creates.jsonl) keeps only dev buys >= 1 SOL and drops on reconnects: it had 7 of
41 coins on the "Steal My Bitcoin" narrative (vault "2026-10-25 narrative leaders (NL)"). This polls /coins every 60 s and
pages back until it reaches the last create it has seen.

    uv run python -u -m tradeslave.research.create_poll    # -> data/narr_nl/creates_full.jsonl
"""

from __future__ import annotations

import json
import time

import requests

from ..paths import DATA_DIR
from .rn_collect import _pump_headers

OUT = DATA_DIR / "narr_nl" / "creates_full.jsonl"
KEEP = ("mint", "name", "symbol", "description", "twitter", "website", "telegram", "created_timestamp", "creator",
        "metadata_uri", "is_currently_live", "nsfw")
PAGES, EVERY = 30, 60


def _last():
    seen, last = set(), 0.0
    if OUT.exists():
        with OUT.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    x = json.loads(line)
                except ValueError:
                    continue
                seen.add(x["mint"])
                last = max(last, x["created_timestamp"] / 1000)
    return seen, last


def run() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    seen, last = _last()
    H = _pump_headers()
    print(f"create_poll: {len(seen)} creates logged, last {time.ctime(last) if last else 'none'}", flush=True)
    while True:
        new, oldest, pages = [], None, 0
        for k in range(PAGES):
            try:
                r = requests.get("https://frontend-api-v3.pump.fun/coins", headers=H, timeout=30,
                                 params={"offset": 50 * k, "limit": 50, "sort": "created_timestamp", "order": "DESC",
                                         "includeNsfw": "true"})
                js = r.json() if r.ok else None
            except (requests.RequestException, ValueError):
                js = None
            if not isinstance(js, list) or not js:
                break
            pages += 1
            for c in js:
                if c.get("mint") and c["mint"] not in seen:
                    seen.add(c["mint"])
                    new.append({x: c.get(x) for x in KEEP} | {"seen": time.time()})
            oldest = min(c.get("created_timestamp") or 0 for c in js) / 1000
            if last and oldest <= last:
                break
            time.sleep(0.5)
        if new:
            with OUT.open("a", encoding="utf-8") as fh:
                for x in sorted(new, key=lambda z: z["created_timestamp"] or 0):
                    fh.write(json.dumps(x) + "\n")
            last = max(last, max((x["created_timestamp"] or 0) for x in new) / 1000)
        gap = bool(last and oldest and oldest > last + 1 and pages == PAGES)
        print(f"{time.strftime('%H:%M:%S')} +{len(new)} creates ({pages} pages){' GAP: listing did not reach the last create' if gap else ''}",
              flush=True)
        time.sleep(EVERY)


if __name__ == "__main__":
    run()
