"""The single cost model every backtest imports (vault "Data & Infra/Cost model.md"; every number is sourced there).

    from tradeslave.cost_model import COSTS, trade_return, entry_time, capacity

A backtest supplies the GROSS move it saw (exit spot / entry spot - 1, both read from data at decision time + latency,
never the signal bar's own close), the position size, and the pool's SOL-side reserve at entry and exit. This module
turns that into the net return per trade, as a fraction of the SOL put in:

  fee        1.25% of the SOL leg, each side (Rohit's locked worst case, 2026-10-16; never lowered by venue tiers)
  impact     constant-product price impact on the reserves at the time of each fill: buy s SOL into R -> avg price is
             (1 + s/R) x spot; sell v SOL-worth into R -> avg price is R/(R+v) x spot. Charged on BOTH sides, i.e. our
             own buy's impact is assumed gone by the exit (other traders sell into it), the conservative case.
  mev        sandwich / adverse-ordering cost per side (*assumption*, unmeasured)
  floor      impact + mev is never below the paper bots' 1% per side (M13/M13b paper unit), so no backtest gets better
             fills than the paper bots
  network    base + priority fee per transaction (0.0003 SOL locked minimum; raised if the measured fee is higher)
  failures   failed transactions still pay their fee; f/(1-f) extra transactions per leg on average
  stop gap   stop exits never fill at the stop: a stop-level exit is moved down by the measured M13b gap
  latency    entry_time() = decision time + ENTRY_LATENCY_S; the backtest must read the fill price there

Paper only: nothing here signs or sends anything.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Sequence

# --- measured inputs (see vault "Data & Infra/Cost model.md" for source and date of each) ---
# M13b paper stop exits, p_exit / stop - 1, n = 123 (data/m13b/trades.jsonl, 2026-10-29): *measured*
STOP_GAP_QUANTILES = ((0.05, -0.8506), (0.10, -0.6411), (0.25, -0.1544), (0.50, -0.0695), (0.75, -0.0212),
                      (0.90, -0.0063), (1.00, 0.0))
STOP_GAP_MEAN = -0.162
STOP_GAP_MEDIAN = -0.0695


@dataclass(frozen=True)
class Costs:
    fee_side: float = 0.0125            # locked (Rohit 2026-10-16)
    network_fee_sol: float = 0.0003     # per tx, base + priority; locked minimum
    mev_side: float = 0.005             # *assumption*: sandwich / ordering cost per side, unmeasured
    min_slip_side: float = 0.01         # paper bots' slippage per side (M13/M13b): floor on impact + mev
    fail_rate: float = 0.10             # failed-tx share per attempt (see Cost model.md for the measured basis)
    entry_latency_s: float = 1.5        # decision -> fill (feed p50 0.97 s + ~1 slot to land)
    stop_gap: float = STOP_GAP_MEAN     # applied to exits filled at a stop level


COSTS = Costs()


def entry_time(t_decision: float, costs: Costs = COSTS) -> float:
    """Earliest time a backtest may read its entry fill price."""
    return t_decision + costs.entry_latency_s


def buy_impact(size_sol: float, sol_reserve: float) -> float:
    """Average-price premium of buying size_sol into a constant-product pool with sol_reserve SOL."""
    if sol_reserve <= 0:
        raise ValueError("sol_reserve must be > 0")
    return size_sol / sol_reserve


def sell_impact(value_sol: float, sol_reserve: float) -> float:
    """Average-price discount of selling value_sol worth (at spot) into a pool with sol_reserve SOL."""
    if sol_reserve <= 0:
        raise ValueError("sol_reserve must be > 0")
    return value_sol / (sol_reserve + value_sol)


def sol_reserve_from_liquidity_usd(liquidity_usd: float, sol_usd: float) -> float:
    """DEX 'liquidity' figures count both sides; the SOL side is half."""
    return liquidity_usd / 2 / sol_usd


def trade_return(gross: float, size_sol: float, sol_reserve_in: float, sol_reserve_out: float | None = None, *,
                 stop_fill: bool = False, costs: Costs = COSTS) -> float:
    """Net return (fraction of size_sol) of one round trip.

    gross: exit spot / entry spot - 1, as the backtest observed it. stop_fill=True means the backtest exited at a stop
    LEVEL (not an observed trade price), so the measured stop gap is applied. sol_reserve_out defaults to the reserve
    implied by the price move on a constant-product pool (R_out = R_in * sqrt(1 + gross))."""
    if size_sol <= 0:
        raise ValueError("size_sol must be > 0")
    if sol_reserve_out is None:
        sol_reserve_out = sol_reserve_in * max(1 + gross, 1e-12) ** 0.5
    exit_rel = (1 + gross) * ((1 + costs.stop_gap) if stop_fill else 1.0)
    if exit_rel <= 0:
        return -1.0 - _tx_cost(size_sol, costs)
    slip_in = max(costs.min_slip_side, buy_impact(size_sol * (1 - costs.fee_side), sol_reserve_in) + costs.mev_side)
    tokens_value_at_entry_spot = size_sol * (1 - costs.fee_side) / (1 + slip_in)
    value_out_spot = tokens_value_at_entry_spot * exit_rel
    slip_out = max(costs.min_slip_side, sell_impact(value_out_spot, max(sol_reserve_out, 1e-12)) + costs.mev_side)
    sol_out = value_out_spot * (1 - slip_out) * (1 - costs.fee_side)
    return sol_out / size_sol - 1 - _tx_cost(size_sol, costs)


def _tx_cost(size_sol: float, costs: Costs) -> float:
    """Two legs, each needing 1/(1-f) attempts on average; every attempt pays the network fee."""
    return 2 * costs.network_fee_sol / (1 - costs.fail_rate) / size_sol


def round_trip_cost(size_sol: float, sol_reserve: float, costs: Costs = COSTS) -> float:
    """Net return of a trade with zero gross move: the hurdle a signal must clear."""
    return trade_return(0.0, size_sol, sol_reserve, sol_reserve, costs=costs)


def breakeven_gross(size_sol: float, sol_reserve: float, costs: Costs = COSTS) -> float:
    """Gross move needed for a net return of 0 (bisection)."""
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if trade_return(mid, size_sol, sol_reserve, costs=costs) < 0:
            lo = mid
        else:
            hi = mid
    return hi


def sample_stop_gap(u: float) -> float:
    """Empirical M13b stop gap at quantile u in [0, 1] (linear between the stored quantiles), for path simulations."""
    prev_q, prev_g = 0.0, STOP_GAP_QUANTILES[0][1]
    for q, g in STOP_GAP_QUANTILES:
        if u <= q:
            w = 0 if q == prev_q else (u - prev_q) / (q - prev_q)
            return prev_g + w * (g - prev_g)
        prev_q, prev_g = q, g
    return STOP_GAP_QUANTILES[-1][1]


def mean_net(gross: Sequence[float], size_sol: float, reserves_in: Sequence[float],
             reserves_out: Sequence[float] | None = None, stop_fills: Sequence[bool] | None = None,
             costs: Costs = COSTS) -> float:
    n = len(gross)
    rs_out = reserves_out if reserves_out is not None else [None] * n
    stops = stop_fills if stop_fills is not None else [False] * n
    return sum(trade_return(g, size_sol, ri, ro, stop_fill=s, costs=costs)
               for g, ri, ro, s in zip(gross, reserves_in, rs_out, stops)) / n


def capacity(gross: Sequence[float], reserves_in: Sequence[float], reserves_out: Sequence[float] | None = None,
             stop_fills: Sequence[bool] | None = None, costs: Costs = COSTS, max_size: float = 1000.0) -> float:
    """Position size (SOL) where mean net return after this model's slippage reaches 0.

    Returns 0.0 if the edge is negative at every size, max_size if still positive there. The per-tx network fee makes
    tiny sizes lose too, so the search starts at the best size on a log grid (0.01 SOL upward) and goes up from there."""
    f = lambda s: mean_net(gross, s, reserves_in, reserves_out, stop_fills, costs)  # noqa: E731
    grid = [0.01 * 1.25 ** i for i in range(80) if 0.01 * 1.25 ** i <= max_size]
    lo = max(grid, key=f)
    if f(lo) <= 0:
        return 0.0
    if f(max_size) > 0:
        return max_size
    hi = max_size
    for _ in range(60):
        mid = (lo * hi) ** 0.5
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
    return lo


def with_overrides(**kw) -> Costs:
    """Stress variants (e.g. fail_rate=0.3, entry_latency_s=7.2); never used to make a result pass."""
    return replace(COSTS, **kw)


def describe(costs: Costs = COSTS, sizes: Iterable[float] = (0.1, 0.5, 1.0), reserves: Iterable[float] = (30, 85)) -> str:
    rows = [f"fee {costs.fee_side:.2%}/side, mev {costs.mev_side:.2%}/side, slip floor {costs.min_slip_side:.2%}/side, "
            f"network {costs.network_fee_sol} SOL/tx, fail {costs.fail_rate:.0%}, latency {costs.entry_latency_s}s, "
            f"stop gap {costs.stop_gap:+.1%}"]
    for r in reserves:
        for s in sizes:
            rows.append(f"  size {s:>5} SOL, reserve {r:>4} SOL: round trip {round_trip_cost(s, r, costs):+.2%}, "
                        f"breakeven gross {breakeven_gross(s, r, costs):+.2%}")
    return "\n".join(rows)


if __name__ == "__main__":
    print(describe())
