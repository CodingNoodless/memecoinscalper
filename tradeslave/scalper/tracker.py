"""Per-token market state built from successive bonding-curve states.

Each curve change becomes a Tick. The change in virtual SOL reserves tells us
the direction and size of the trade(s) since the previous state: SOL in = buy
pressure, SOL out = sell pressure. Several trades landing in the same slot can
arrive as one change; the flow totals are still right.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .curve import CurveState


@dataclass(frozen=True)
class Tick:
    t: float              # seconds (epoch or replay clock)
    price: float          # SOL per token
    virtual_sol: float    # SOL
    flow_sol: float       # signed SOL into the curve since the previous tick


@dataclass
class TokenTracker:
    mint: str
    symbol: str
    created_at: float
    creator: str = ""
    curve_key: str = ""
    initial_buy_sol: float = 0.0
    ticks: deque = field(default_factory=lambda: deque(maxlen=4000))
    state: CurveState | None = None
    launch_price: float = 0.0
    peak_price: float = 0.0
    peak_t: float = 0.0
    last_update_t: float = 0.0
    # per-token trading memory (used by the strategy)
    entries: int = 0
    losses: int = 0
    last_exit_t: float = -1e18
    first_dip_done: bool = False

    def update(self, t: float, state: CurveState) -> Tick | None:
        """Add a new curve state. Returns the tick, or None if nothing changed."""
        if self.state is not None and state.virtual_sol_reserves == self.state.virtual_sol_reserves \
                and state.virtual_token_reserves == self.state.virtual_token_reserves:
            return None
        flow = 0.0 if self.state is None else state.virtual_sol - self.state.virtual_sol
        self.state = state
        tick = Tick(t=t, price=state.price_sol, virtual_sol=state.virtual_sol, flow_sol=flow)
        self.ticks.append(tick)
        if not self.launch_price:
            self.launch_price = tick.price
        if tick.price > self.peak_price:
            self.peak_price, self.peak_t = tick.price, t
        self.last_update_t = t
        return tick

    # --- queries ----------------------------------------------------------
    @property
    def price(self) -> float:
        return self.ticks[-1].price if self.ticks else 0.0

    def age(self, now: float) -> float:
        return now - self.created_at

    def window(self, now: float, seconds: float) -> list[Tick]:
        cutoff = now - seconds
        out = []
        for tick in reversed(self.ticks):
            if tick.t < cutoff:
                break
            out.append(tick)
        out.reverse()
        return out

    def flow(self, now: float, seconds: float) -> tuple[float, int]:
        """(net SOL bought, number of curve updates) over the window."""
        ticks = self.window(now, seconds)
        return sum(t.flow_sol for t in ticks), len(ticks)

    def low_since(self, since_t: float) -> float:
        lows = [t.price for t in self.ticks if t.t >= since_t]
        return min(lows) if lows else self.price

    def swing_high(self, now: float, seconds: float) -> tuple[float, float]:
        """(highest price, its time) over the window."""
        ticks = self.window(now, seconds)
        if not ticks:
            return self.price, now
        best = max(ticks, key=lambda t: t.price)
        return best.price, best.t

    def last_drop_pct(self) -> float:
        """Price drop of the most recent update (positive number = fell)."""
        if len(self.ticks) < 2 or self.ticks[-2].price <= 0:
            return 0.0
        return 1.0 - self.ticks[-1].price / self.ticks[-2].price
