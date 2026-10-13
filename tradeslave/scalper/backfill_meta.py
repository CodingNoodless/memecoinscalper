"""Backfill socials for tokens recorded before metadata capture existed.

    uv run python -m tradeslave.scalper.backfill_meta             # tokens that returned to opening
    uv run python -m tradeslave.scalper.backfill_meta --all       # every recorded token (slower)
    uv run python -m tradeslave.scalper.backfill_meta --rpc https://mainnet.helius-rpc.com/?api-key=KEY

For each token: find its first (creation) transaction via Solana RPC, read the
metadata link (name, symbol, uri) from the create event / instruction, fetch the
metadata JSON and save its socials to data/meta/backfill.jsonl. Resumable:
tokens already in the file are skipped. Free public RPC by default; it is
rate-limited, so requests are paced and retried.
"""

from __future__ import annotations

import argparse
import base64
import json
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from .config import load_config
from .recorder import TICK_DIR, read_ticks
from .report import BACKFILL, collect, load_backfill
from .socials import fetch_metadata, parse_socials

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
B58_INDEX = {c: i for i, c in enumerate(B58)}


def b58decode(text: str) -> bytes:
    number = 0
    for char in text:
        number = number * 58 + B58_INDEX[char]
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    pad = len(text) - len(text.lstrip("1"))
    return b"\x00" * pad + body


def _borsh_string(raw: bytes, offset: int) -> tuple[str, int]:
    (length,) = struct.unpack_from("<I", raw, offset)
    if length > 512:
        raise ValueError("string too long")
    start = offset + 4
    end = start + length
    if end > len(raw):
        raise ValueError("truncated")
    return raw[start:end].decode("utf-8"), end


def parse_name_symbol_uri(raw: bytes) -> tuple[str, str, str] | None:
    """pump.fun create instructions and CreateEvent both start (after an 8-byte
    discriminator, or 16 bytes for a self-CPI event) with three borsh strings:
    name, symbol, uri. Accept the first blob that parses sensibly."""
    for offset in (8, 16):
        try:
            name, pos = _borsh_string(raw, offset)
            symbol, pos = _borsh_string(raw, pos)
            uri, _ = _borsh_string(raw, pos)
        except (struct.error, ValueError, UnicodeDecodeError):
            continue
        if uri.startswith(("http://", "https://")) and 0 < len(symbol) <= 64 and len(name) <= 128:
            return name, symbol, uri
    return None


def find_uri_in_transaction(tx: dict) -> tuple[str, str, str] | None:
    meta = tx.get("meta") or {}
    blobs: list[bytes] = []
    for line in meta.get("logMessages") or []:
        if isinstance(line, str) and line.startswith("Program data: "):
            try:
                blobs.append(base64.b64decode(line.split(": ", 1)[1]))
            except ValueError:
                pass
    instructions = list(((tx.get("transaction") or {}).get("message") or {}).get("instructions") or [])
    for inner in meta.get("innerInstructions") or []:
        instructions.extend(inner.get("instructions") or [])
    for ix in instructions:
        data = ix.get("data")
        if isinstance(data, str) and data:
            try:
                blobs.append(b58decode(data))
            except KeyError:
                pass
    for blob in blobs:
        found = parse_name_symbol_uri(blob)
        if found:
            return found
    return None


class Rpc:
    def __init__(self, url: str, min_interval_s: float = 0.15):
        self.url = url
        self.min_interval_s = min_interval_s
        self.last = 0.0
        self.session = requests.Session()

    def call(self, method: str, params: list):
        for attempt in range(6):
            wait = self.min_interval_s - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            try:
                response = self.session.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                                             "params": params}, timeout=20)
            except requests.RequestException:
                time.sleep(1.5 * (attempt + 1))
                continue
            if response.status_code == 429:
                time.sleep(2.0 * (attempt + 1))
                continue
            body = response.json()
            if "error" in body:
                if body["error"].get("code") in (-32429, 429):
                    time.sleep(2.0 * (attempt + 1))
                    continue
                return None
            return body.get("result")
        return None

    def creation_signature(self, address: str, max_pages: int = 5) -> str | None:
        before, oldest = None, None
        for _ in range(max_pages):
            options = {"limit": 1000}
            if before:
                options["before"] = before
            page = self.call("getSignaturesForAddress", [address, options])
            if not page:
                break
            oldest = page[-1]["signature"]
            if len(page) < 1000:
                break
            before = oldest
        return oldest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true", help="every recorded token, not only zone touches")
    parser.add_argument("--rpc", default=None, help="Solana RPC HTTP URL (default: public mainnet)")
    parser.add_argument("--narrative", action="store_true",
                        help="also re-fetch tokens whose saved metadata lacks the name/description text")
    args = parser.parse_args()
    cfg = load_config()
    rpc_url = args.rpc or cfg.rpc_ws.replace("wss://", "https://").replace("ws://", "http://")
    paths = sorted(TICK_DIR.glob("*.jsonl.gz"))
    records = list(read_ticks(paths))
    symbols = {r["m"]: r.get("s", "?") for r in records if r.get("k") == "create"}
    known_uri = {r["m"]: r["uri"] for r in records if r.get("k") == "create" and r.get("uri")}
    recorded_meta = {r["m"] for r in records if r.get("k") == "meta" and (not args.narrative or "x_description" in r)}
    if args.all:
        mints = list(symbols)
    else:
        mints = [row["mint"] for row in collect(records, cfg, 0.09, 0.10, 60.0)]
    done = {m for m, s in load_backfill().items() if not args.narrative or "description" in s} | recorded_meta
    todo = [m for m in mints if m not in done]
    print(f"{len(mints)} tokens selected, {len(todo)} need socials (RPC: {rpc_url.split('?')[0]})")
    BACKFILL.parent.mkdir(parents=True, exist_ok=True)
    rpc = Rpc(rpc_url)
    found = []
    for i, mint in enumerate(todo, 1):
        if mint in known_uri:                       # newer recordings keep the uri: no RPC needed
            found.append((mint, (symbols.get(mint, ""), symbols.get(mint, ""), known_uri[mint])))
            continue
        signature = rpc.creation_signature(mint)
        tx = rpc.call("getTransaction", [signature, {"encoding": "json", "maxSupportedTransactionVersion": 0,
                                                      "commitment": "confirmed"}]) if signature else None
        parsed = find_uri_in_transaction(tx) if tx else None
        found.append((mint, parsed))
        if i % 10 == 0 or i == len(todo):
            print(f"  looked up {i}/{len(todo)} creation transactions ({sum(1 for _, p in found if p)} with a uri)")
    with_uri = [(m, p) for m, p in found if p]

    def fetch(item):
        mint, (name, symbol, uri) = item
        return mint, name, symbol, uri, parse_socials(fetch_metadata(uri, cfg.metadata_timeout_s))

    with ThreadPoolExecutor(max_workers=6) as pool, BACKFILL.open("a", encoding="utf-8") as out:
        for n, (mint, name, symbol, uri, socials) in enumerate(pool.map(fetch, with_uri), 1):
            out.write(json.dumps({"mint": mint, "symbol": symbol, "name": name, "uri": uri,
                                  "socials": socials, "backfilled_at": time.time()}) + "\n")
            if n % 10 == 0 or n == len(with_uri):
                print(f"  fetched metadata {n}/{len(with_uri)}")
    missing = len(todo) - len(with_uri)
    print(f"Done. Saved {len(with_uri)} to {BACKFILL}" + (f"; {missing} had no readable creation data" if missing else ""))
    print("Now run: uv run python -m tradeslave.scalper.report")


if __name__ == "__main__":
    main()
