"""Solana RPC access for research backfills (Helius or any JSON-RPC endpoint)."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import requests

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
TX_OPTS = {"encoding": "json", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}
_VERSION_HINT = re.compile(r'"?maxSupportedTransactionVersion"?\s*:\s*(\d+)')


def _wanted_version(error: dict | None) -> int | None:
    """The transaction version an RPC error asks for ('... not supported ... maxSupportedTransactionVersion: 1')."""
    match = _VERSION_HINT.search(str((error or {}).get("message", "")))
    return int(match.group(1)) if match else None


def env_rpc() -> str | None:
    url = os.environ.get("RESEARCH_RPC_URL")
    if url:
        return url
    env = Path(".env")
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("RESEARCH_RPC_URL="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def host_of(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0].split("?", 1)[0]


class Rpc:
    def __init__(self, url: str, rps: float):
        self.url, self.min_gap, self.last = url, 1.0 / max(rps, 0.1), 0.0
        self.session = requests.Session()
        self.calls = 0
        self.credits = 0          # Helius credit estimate: 1 per standard call, 10 per 100 txs for gTFA
        self.errors: dict[str, int] = {}
        self.gtfa: bool | None = None if "helius" in url else False   # Helius-only method, probed on first use

    def request(self, method: str, params: list):
        """(result, error) with pacing and retries on rate limits / transient failures."""
        error = None
        for attempt in range(8):
            wait = self.min_gap - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            self.calls += 1
            try:
                response = self.session.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                                             "params": params}, timeout=60)
            except requests.RequestException as exc:
                error = {"code": "network", "message": type(exc).__name__}
                time.sleep(1.5 * (attempt + 1))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                error = {"code": response.status_code, "message": "http"}
                time.sleep(2.0 * (attempt + 1))
                continue
            try:
                body = response.json()
            except ValueError:
                error = {"code": "json", "message": "bad body"}
                time.sleep(1.0)
                continue
            if "error" in body:
                error = body["error"]
                if error.get("code") in (-32429, 429, -32005, -32007, -32014, -32016):
                    time.sleep(2.0 * (attempt + 1))
                    continue
                break
            result = body.get("result")
            if method == "getTransactionsForAddress":
                n = len(result.get("data", [])) if isinstance(result, dict) else len(result or [])
                self.credits += max(10, 10 * -(-n // 100))
            else:
                self.credits += 1
            return result, None
        key = f"{method}:{(error or {}).get('code')}"
        self.errors[key] = self.errors.get(key, 0) + 1
        return None, error

    def call(self, method: str, params: list):
        return self.request(method, params)[0]


def _gtfa(rpc: Rpc, address: str, t_from: float, t_to: float, max_pages: int):
    txs, token = [], None
    for _ in range(max_pages):
        options = {"transactionDetails": "full", "sortOrder": "asc", "limit": 1000,
                   "encoding": "json", "maxSupportedTransactionVersion": TX_OPTS["maxSupportedTransactionVersion"],
                   "filters": {"blockTime": {"gte": int(t_from), "lte": int(t_to) + 1}, "status": "succeeded"}}
        if token:
            options["paginationToken"] = token
        result, error = rpc.request("getTransactionsForAddress", [address, options])
        wanted = _wanted_version(error)
        if wanted is not None and wanted > TX_OPTS["maxSupportedTransactionVersion"]:
            TX_OPTS["maxSupportedTransactionVersion"] = wanted     # the chain moved on: accept newer versions
            options["maxSupportedTransactionVersion"] = wanted
            result, error = rpc.request("getTransactionsForAddress", [address, options])
        if error is not None:
            return None, error
        data = result.get("data", []) if isinstance(result, dict) else (result or [])
        txs.extend(data)
        token = result.get("paginationToken") if isinstance(result, dict) else None
        if not token:
            return txs, None
    return None, {"code": "too_many", "message": "page cap"}


def _sigs_then_txs(rpc: Rpc, address: str, t_from: float, t_to: float, max_pages: int, stats: dict):
    found, before = [], None
    for _ in range(max_pages):
        options = {"limit": 1000}
        if before:
            options["before"] = before
        page = rpc.call("getSignaturesForAddress", [address, options])
        if page is None:
            stats["why"] = "rpc_error"
            return None
        for item in page:
            bt = item.get("blockTime") or 0
            if item.get("err") is None and t_from <= bt <= t_to + 1:
                found.append(item["signature"])
        if not page or len(page) < 1000 or (page[-1].get("blockTime") or 0) < t_from:
            break
        before = page[-1]["signature"]
    else:
        stats["why"] = "too_many"
        return None
    stats["sigs"] = len(found)
    txs = []
    for sig in reversed(found):                       # oldest first
        tx, error = rpc.request("getTransaction", [sig, TX_OPTS])
        wanted = _wanted_version(error)
        if wanted is not None and wanted > TX_OPTS["maxSupportedTransactionVersion"]:
            TX_OPTS["maxSupportedTransactionVersion"] = wanted
            tx, error = rpc.request("getTransaction", [sig, TX_OPTS])
        if tx is None:
            stats["tx_missing"] = stats.get("tx_missing", 0) + 1
            continue
        tx.setdefault("transaction", {}).setdefault("signatures", [sig])
        txs.append(tx)
    return txs


def address_transactions(rpc: Rpc, address: str, t_from: float, t_to: float, max_pages: int = 20):
    """Successful transactions touching `address` in [t_from, t_to], in chain order, plus fetch stats.

    Uses Helius getTransactionsForAddress (one call per 1000 transactions, 10
    credits per 100) when the endpoint supports it, else signatures + one
    getTransaction per signature."""
    stats: dict = {}
    if rpc.gtfa is not False:
        txs, error = _gtfa(rpc, address, t_from, t_to, max_pages)
        if error is None:
            rpc.gtfa = True
            stats["method"] = "gtfa"
            stats["sigs"] = len(txs)
            return txs, stats
        if rpc.gtfa is None and error.get("code") not in ("too_many", "network") and _wanted_version(error) is None:
            rpc.gtfa = False                          # not available on this plan/endpoint: fall back for good
            print(f"  getTransactionsForAddress unavailable ({error.get('message', error)}); using getTransaction per signature")
        elif error.get("code") == "too_many":
            stats["method"] = "gtfa"
            return None, stats
    stats["method"] = "sigs"
    return _sigs_then_txs(rpc, address, t_from, t_to, max_pages, stats), stats


def signature_of(tx: dict) -> str:
    sigs = (tx.get("transaction") or {}).get("signatures") or []
    return sigs[0] if sigs else ""
