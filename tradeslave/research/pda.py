"""Solana program-derived addresses in pure Python (no solana libraries needed).

Used to find a graduated token's canonical PumpSwap pool without an API call:
    pool authority = PDA(["pool-authority", mint], pump program)
    pool           = PDA(["pool", u16 index 0, pool authority, mint, WSOL], PumpSwap program)
"""

from __future__ import annotations

import hashlib

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL = "So11111111111111111111111111111111111111112"

# ed25519 curve constants (to test that a PDA is OFF the curve)
_P = 2 ** 255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + raw


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


def _on_curve(b: bytes) -> bool:
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    y2 = y * y % _P
    u, v = (y2 - 1) % _P, (_D * y2 + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    if x2 == 0:
        return True
    return pow(x2, (_P - 1) // 2, _P) == 1          # x^2 must be a square mod p


def find_pda(seeds: list[bytes], program: str) -> str:
    prog = b58decode(program)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + prog + b"ProgramDerivedAddress").digest()
        if not _on_curve(h):
            return b58encode(h)
    raise ValueError("no PDA")


def pumpswap_pool_address(mint: str) -> str:
    authority = find_pda([b"pool-authority", b58decode(mint)], PUMP_PROGRAM)
    return find_pda([b"pool", (0).to_bytes(2, "little"), b58decode(authority), b58decode(mint), b58decode(WSOL)],
                    PUMP_AMM_PROGRAM)
