"""Token metadata: narrative and social links (Twitter/X, Telegram, website).

pump.fun tokens publish a metadata JSON at launch (PumpPortal's `uri`):
    {"name", "symbol", "description", "image", "twitter", "telegram",
     "website", "coin_community", ...}
Rohit's manual edge: tokens with a Twitter page / a real narrative did better.
The Twitter link comes in three kinds:
    tweet      x.com/<user>/status/<id>   - riding a narrative (a viral post)
    profile    x.com/<handle>             - the project has its own account
    community  x.com/i/communities/<id>
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import requests

# ipfs.io is often slow or rate-limited; pump.fun's own pinata gateway serves the same CIDs.
IPFS_GATEWAYS = ("https://pump.mypinata.cloud/ipfs/", "https://ipfs.io/ipfs/", "https://dweb.link/ipfs/")
_CID = re.compile(r"/ipfs/([A-Za-z0-9]+)")
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "tradeslavebot/scalper"


def candidate_urls(uri: str) -> list[str]:
    """The uri itself, plus other IPFS gateways for the same content."""
    if not isinstance(uri, str) or not uri.startswith(("http://", "https://")):
        return []
    match = _CID.search(uri)
    if not match:
        return [uri]
    cid = match.group(1)
    urls = [g + cid for g in IPFS_GATEWAYS]
    return urls if uri in urls else urls + [uri]


def fetch_metadata(uri: str, timeout_s: float = 4.0) -> dict | None:
    """Blocking fetch with gateway fallback. Returns the JSON object or None."""
    for url in candidate_urls(uri):
        try:
            response = SESSION.get(url, timeout=timeout_s)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, dict):
                    return data
        except (requests.RequestException, ValueError):
            continue
    return None


def _clean_url(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 300:
        return None
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value


def twitter_kind(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.").removeprefix("mobile.")
    if host not in {"x.com", "twitter.com"}:
        return "other"
    segments = [s for s in parts.path.split("/") if s]
    if not segments:
        return "other"
    if segments[0] == "i" and len(segments) > 1 and segments[1] == "communities":
        return "community"
    if len(segments) >= 3 and segments[1] == "status":
        return "tweet"
    if len(segments) == 1 and segments[0] not in {"home", "search", "explore", "i", "intent"}:
        return "profile"
    return "other"


def parse_socials(meta: dict | None) -> dict:
    """Normalise metadata into the features the strategy and reports use."""
    meta = meta if isinstance(meta, dict) else {}
    twitter = _clean_url(meta.get("twitter"))
    telegram = _clean_url(meta.get("telegram"))
    website = _clean_url(meta.get("website"))
    description = meta.get("description") if isinstance(meta.get("description"), str) else ""
    return {
        "fetched": bool(meta),
        "twitter": twitter,
        "twitter_kind": twitter_kind(twitter),
        "telegram": telegram,
        "website": website,
        "desc_len": len(description.strip()),
        "has_any": bool(twitter or telegram or website),
    }


def socials_ok(socials: dict | None, requirement: str) -> bool:
    """requirement: off | any | twitter | twitter_profile"""
    if requirement == "off":
        return True
    if not socials:
        return False
    if requirement == "any":
        return bool(socials.get("has_any"))
    if requirement == "twitter":
        return socials.get("twitter_kind") in {"tweet", "profile", "community"}
    if requirement == "twitter_profile":
        return socials.get("twitter_kind") == "profile"
    raise ValueError(f"unknown socials requirement {requirement!r}")
