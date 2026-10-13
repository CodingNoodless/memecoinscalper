"""Pump.fun bonding-curve state, account decoding and fill math.

The curve prices trades on VIRTUAL reserves as a constant product:
    k = virtual_sol * virtual_tokens
Buying adds SOL and removes tokens; selling does the reverse. The protocol fee
(fee_bps, about 1.25% per side) is charged on the SOL leg.
"""

from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass

LAMPORTS_PER_SOL = 1_000_000_000
TOKEN_DECIMALS = 6
TOKEN_UNIT = 10 ** TOKEN_DECIMALS

# Anchor account discriminator: first 8 bytes of sha256("account:<Name>").
BONDING_CURVE_DISCRIMINATOR = hashlib.sha256(b"account:BondingCurve").digest()[:8]
_LAYOUT = struct.Struct("<5Q?")  # 5 x u64 reserves/supply, then `complete` bool


@dataclass(frozen=True)
class CurveState:
    virtual_token_reserves: int  # raw token units (6 decimals)
    virtual_sol_reserves: int    # lamports
    real_token_reserves: int = 0
    real_sol_reserves: int = 0
    token_total_supply: int = 0
    complete: bool = False       # True once the curve graduated to an AMM pool

    @property
    def price_sol(self) -> float:
        """Spot price in SOL per whole token."""
        if self.virtual_token_reserves <= 0:
            return 0.0
        return (self.virtual_sol_reserves / LAMPORTS_PER_SOL) / (self.virtual_token_reserves / TOKEN_UNIT)

    @property
    def virtual_sol(self) -> float:
        return self.virtual_sol_reserves / LAMPORTS_PER_SOL


# Returned when a curve finished: pump.fun zeroes the reserves after migration.
GRADUATED = CurveState(0, 0, complete=True)


def decode_bonding_curve(data_b64: str) -> CurveState | None:
    """Decode a BondingCurve account (base64). Returns None if it isn't one."""
    try:
        raw = base64.b64decode(data_b64)
    except (ValueError, TypeError):
        return None
    if len(raw) < 8 + _LAYOUT.size or raw[:8] != BONDING_CURVE_DISCRIMINATOR:
        return None
    vt, vs, rt, rs, supply, complete = _LAYOUT.unpack_from(raw, 8)
    if vt <= 0 or vs <= 0:
        return GRADUATED if complete else None
    return CurveState(vt, vs, rt, rs, supply, bool(complete))


def encode_bonding_curve(state: CurveState) -> str:
    """Inverse of decode (used by tests and synthetic replays)."""
    body = _LAYOUT.pack(
        state.virtual_token_reserves, state.virtual_sol_reserves, state.real_token_reserves,
        state.real_sol_reserves, state.token_total_supply, state.complete,
    )
    return base64.b64encode(BONDING_CURVE_DISCRIMINATOR + body + bytes(32)).decode()


def state_from_pumpportal(message: dict) -> CurveState | None:
    """PumpPortal reports vSolInBondingCurve in SOL and vTokensInBondingCurve in whole tokens."""
    try:
        v_sol = float(message["vSolInBondingCurve"])
        v_tokens = float(message["vTokensInBondingCurve"])
    except (KeyError, TypeError, ValueError):
        return None
    if v_sol <= 0 or v_tokens <= 0:
        return None
    return CurveState(
        virtual_token_reserves=int(round(v_tokens * TOKEN_UNIT)),
        virtual_sol_reserves=int(round(v_sol * LAMPORTS_PER_SOL)),
    )


def buy_tokens_out(state: CurveState, sol_in_lamports: int, fee_bps: int) -> int:
    """Tokens received for spending sol_in (fee included in sol_in)."""
    if sol_in_lamports <= 0:
        return 0
    net = sol_in_lamports * 10_000 // (10_000 + fee_bps)
    k = state.virtual_sol_reserves * state.virtual_token_reserves
    new_tokens = -(-k // (state.virtual_sol_reserves + net))  # ceil division
    return max(0, state.virtual_token_reserves - new_tokens)


def sell_sol_out(state: CurveState, tokens_in: int, fee_bps: int) -> int:
    """Lamports received for selling tokens_in, after the fee."""
    if tokens_in <= 0:
        return 0
    k = state.virtual_sol_reserves * state.virtual_token_reserves
    new_sol = -(-k // (state.virtual_token_reserves + tokens_in))
    gross = max(0, state.virtual_sol_reserves - new_sol)
    return gross * (10_000 - fee_bps) // 10_000
