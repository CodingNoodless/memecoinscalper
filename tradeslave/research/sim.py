"""Event-level trade simulator: real curve fills, pump.fun fee both ways, network fee,
fill latency. Validated against live paper trades (reproduced 61/61 exactly)."""

from __future__ import annotations

import statistics

from ..scalper.curve import buy_tokens_out, sell_sol_out
from .dataset import L, Token

FEE_BPS = 125
NETWORK_FEE_SOL = 0.0003
SIZE_SOL = 0.05
LATENCY_S = 0.8


def simulate(tok: Token, t_decision: float, tp: float = 0.05, sl: float = 0.06, hold: float = 60.0,
             trail: tuple[float, float] | None = None, size: float = SIZE_SOL, latency: float = LATENCY_S,
             fee_bps: int = FEE_BPS, network_fee: float = NETWORK_FEE_SOL) -> float | None:
    """Net PnL fraction of buying at t_decision (+latency) and exiting per the policy."""
    i = tok.idx_at(t_decision + latency)
    if i < 0:
        return None
    tokens = buy_tokens_out(tok.state(i), int(size * L), fee_bps)
    if tokens <= 0:
        return None
    cost = size + network_fee
    t_fill = t_decision + latency
    peak, exit_t = -1.0, None
    j = i + 1
    while j < len(tok.t) and tok.t[j] <= t_fill + hold:
        pnl = (sell_sol_out(tok.state(j), tokens, fee_bps) / L - network_fee) / cost - 1
        peak = max(peak, pnl)
        if pnl >= tp or pnl <= -sl or (trail and peak >= trail[0] and peak - pnl >= trail[1]):
            exit_t = tok.t[j]
            break
        j += 1
    if exit_t is None:
        exit_t = t_fill + hold
    k = tok.idx_at(exit_t + latency)
    return (sell_sol_out(tok.state(k), tokens, fee_bps) / L - network_fee) / cost - 1


def summary(values) -> dict:
    v = [x for x in values if x is not None]
    if not v:
        return {"n": 0}
    return {"n": len(v), "win": round(sum(x > 0 for x in v) / len(v), 3), "mean": round(statistics.mean(v), 4),
            "median": round(statistics.median(v), 4), "total_sol": round(sum(v) * SIZE_SOL, 4)}
