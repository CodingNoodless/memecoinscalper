"""pump.fun TradeEvent from program logs ("Program data: <base64>").

Layout (Anchor event): 8-byte discriminator sha256("event:TradeEvent")[:8], then
mint (32), sol_amount u64, token_amount u64, is_buy u8, user (32), timestamp i64,
virtual_sol_reserves u64, virtual_token_reserves u64, ... (later fields unused).
Verified 2026-10-15 against 400 transactions with exact curve deltas: token
amount, side and SOL amount (= change in the curve's lamports, fees excluded)
matched 400/400. `user` is the real trader even when someone else pays the fee.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import struct

TRADE_EVENT = hashlib.sha256(b"event:TradeEvent").digest()[:8]
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def parse_trade_events(logs: list[str]) -> list[dict]:
    out = []
    for line in logs or []:
        if not isinstance(line, str) or not line.startswith("Program data: "):
            continue
        try:
            raw = base64.b64decode(line[14:])
        except (binascii.Error, ValueError):
            continue
        if raw[:8] != TRADE_EVENT or len(raw) < 8 + 32 + 16 + 1 + 32 + 8 + 16:
            continue
        o = 8
        mint = b58encode(raw[o:o + 32]); o += 32
        sol, tok = struct.unpack_from("<QQ", raw, o); o += 16
        is_buy = bool(raw[o]); o += 1
        user = b58encode(raw[o:o + 32]); o += 32
        ts, vs, vt = struct.unpack_from("<qQQ", raw, o)
        out.append({"mint": mint, "sol": sol, "tok": tok, "buy": is_buy, "user": user, "ts": ts, "vs": vs, "vt": vt})
    return out


def encode_trade_event(mint: str, sol: int, tok: int, buy: bool, user: str, ts: int, vs: int, vt: int) -> str:
    """Inverse of parse_trade_events for tests (pubkeys as base58)."""
    def b58decode(text):
        n = 0
        for ch in text:
            n = n * 58 + _B58.index(ch)
        raw = n.to_bytes(32, "big")
        return raw
    body = (TRADE_EVENT + b58decode(mint) + struct.pack("<QQ", sol, tok) + bytes([int(buy)]) + b58decode(user)
            + struct.pack("<qQQ", ts, vs, vt) + b"\0" * 64)
    return "Program data: " + base64.b64encode(body).decode()
