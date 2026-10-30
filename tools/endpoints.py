"""RPC endpoint health, by label only (never keys or URLs with keys): writes lab/endpoints.json.

    uv run python tools/endpoints.py            # probe every configured endpoint once (1-2 cheap calls each)

Endpoints: the public RPC, every URL in HISTORY_RPC_URLS, RESEARCH_RPC_URL and any other *_RPC_URL* / HELIUS* setting
(read through tradeslave.config's .env loading; values are never printed). Status: working / exhausted (credit or
plan limit, e.g. Helius "max usage reached") / erroring. Exhausted keys are skipped until their reset date (monthly,
Rohit 2026-10-30). Each run prints how many endpoints are working per provider.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tradeslave import config  # noqa: E402,F401  (loads .env into the environment)
from tradeslave.research.chain import PUBLIC_RPC  # noqa: E402

OUT = ROOT / "lab" / "endpoints.json"


def provider(url: str) -> str:
    h = urlsplit(url).hostname or "?"
    for p in ("helius", "alchemy", "quiknode", "quicknode", "chainstack", "drpc", "ankr", "rpcfast", "shyft", "syndica"):
        if p in h:
            return "quicknode" if p == "quiknode" else p
    return "public" if url.startswith(PUBLIC_RPC) else h


def configured() -> list[str]:
    urls = [PUBLIC_RPC]
    for k, v in os.environ.items():
        if not v:
            continue
        ku = k.upper()
        if ku == "HISTORY_RPC_URLS":
            urls += [u.split("#")[0].strip() for u in v.split(",") if u.strip()]
        elif ("RPC" in ku and "URL" in ku) or ku.startswith("HELIUS"):
            if v.startswith("http"):
                urls.append(v.strip())
            elif ku.startswith("HELIUS") and len(v) >= 30 and "/" not in v:      # a bare Helius API key
                urls.append(f"https://mainnet.helius-rpc.com/?api-key={v.strip()}")
    out, seen = [], set()
    for u in urls:
        k = key_id(u)
        if k not in seen:
            seen.add(k)
            out.append(u)
    return out


def key_id(url: str) -> str:
    """Same host + same credential = one endpoint (hash only; the key itself is never stored or printed)."""
    import hashlib
    from urllib.parse import parse_qs
    sp = urlsplit(url)
    q = parse_qs(sp.query)
    cred = (q.get("api-key") or q.get("api_key") or [""])[0] or sp.path.rstrip("/").split("/")[-1]
    return hashlib.sha256(f"{sp.hostname}|{cred}".encode()).hexdigest()[:10]


def next_month_first(today: date) -> str:
    return date(today.year + (today.month == 12), today.month % 12 + 1, 1).isoformat()


def probe(url: str) -> dict:
    try:
        r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "getSlot"}, timeout=20)
        body = r.text[:300].lower()
        if r.status_code == 429 and ("max usage" in body or "credits" in body or "quota" in body):
            return {"status": "exhausted", "last_error": f"429 {body[:60]}"}
        if r.status_code != 200 or "error" in r.json():
            return {"status": "erroring", "last_error": f"{r.status_code} {body[:60]}"}
        return {"status": "working", "last_error": None}
    except Exception as exc:  # noqa: BLE001
        return {"status": "erroring", "last_error": type(exc).__name__}


def main() -> None:
    old = {e["label"]: e for e in json.loads(OUT.read_text())} if OUT.exists() else {}
    counts: dict[str, int] = {}
    rows = []
    for url in configured():
        p = provider(url)
        counts[p] = counts.get(p, 0) + 1
        label = f"{p}-{counts[p]}"
        prev = old.get(label, {})
        if prev.get("status") == "exhausted" and prev.get("reset_date", "9999") > date.today().isoformat():
            rows.append(prev)                      # skip until its reset date
            continue
        res = probe(url)
        row = {"provider": p, "label": label, "host": urlsplit(url).hostname, **res,
               "credits_used": prev.get("credits_used"), "checked": time.strftime("%Y-%m-%d %H:%M"),
               "reset_date": next_month_first(date.today()) if res["status"] == "exhausted" else None}
        rows.append(row)
    OUT.write_text(json.dumps(rows, indent=1))
    by: dict[str, dict] = {}
    for r in rows:
        by.setdefault(r["provider"], {"working": 0, "exhausted": 0, "erroring": 0})[r["status"]] += 1
    for r in rows:
        print(f"{r['label']:<14} {r['host']:<34} {r['status']:<10} {r['last_error'] or ''}"
              + (f" (reset {r['reset_date']})" if r.get("reset_date") else ""))
    print("working per provider: " + ", ".join(f"{p} {v['working']}/{sum(v.values())}" for p, v in by.items()))


if __name__ == "__main__":
    main()
