"""Per-token market state built from successive bonding-curve states.

Each curve change becomes a Tick. The change in virtual SOL reserves tells us
the direction and size of the trade(s) since the previous state: SOL in = buy
pressure, SOL out = sell pressure. Several trades landing in the same slot can
arrive as one change; the flow totals are still right.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .curve import TOKEN_UNIT, CurveState

TOTAL_SUPPLY_TOKENS = 1_000_000_000          # pump.fun tokens are minted with 1B supply
CURVE_START_VIRTUAL_TOKENS = 1_073_000_000   # virtual token reserves at curve creation


@dataclass(frozen=True)
class Tick:
    t: float              # seconds (epoch or replay clock)
    price: float          # SOL per token
    virtual_sol: float    # SOL
    flow_sol: float       # signed SOL into the curve since the previous tick
    virtual_tokens: float = 0.0  # whole tokens left in the virtual reserves
    tokens_sold: float = 0.0     # whole tokens sold into the curve by this update (0 for buys)


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
    # launch / safety facts
    dev_tokens: float = 0.0          # whole tokens the dev bought at creation
    mayhem: bool = False
    dev_sold: bool = False
    dev_sold_reason: str = ""
    floor_broken: bool = False
    zone_touch_t: float | None = None   # first time price entered the buy zone after the rise
    zone_low: float = 0.0               # lowest price since the zone touch
    last_tokens_sold: float = 0.0       # tokens sold into the curve by the latest update
    creator_launches: int = 1           # tokens this creator launched within serial_window_s
    creator_sell_times: list = field(default_factory=list)  # unconfirmed creator-wallet sells
    socials: dict | None = None         # parsed metadata; None until fetched
    twitter_reuse: int = 0              # earlier tokens that used the same Twitter link

    def update(self, t: float, state: CurveState) -> Tick | None:
        """Add a new curve state. Returns the tick, or None if nothing changed."""
        if self.state is not None and state.virtual_sol_reserves == self.state.virtual_sol_reserves \
                and state.virtual_token_reserves == self.state.virtual_token_reserves:
            return None
        flow = 0.0 if self.state is None else state.virtual_sol - self.state.virtual_sol
        tokens_sold = 0.0
        if self.state is not None and state.virtual_token_reserves > self.state.virtual_token_reserves:
            tokens_sold = (state.virtual_token_reserves - self.state.virtual_token_reserves) / TOKEN_UNIT
        self.state = state
        self.last_tokens_sold = tokens_sold
        tick = Tick(t=t, price=state.price_sol, virtual_sol=state.virtual_sol, flow_sol=flow,
                    virtual_tokens=state.virtual_token_reserves / TOKEN_UNIT, tokens_sold=tokens_sold)
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

    # --- launch analytics -----------------------------------------------------
    @property
    def dev_supply_pct(self) -> float:
        return self.dev_tokens / TOTAL_SUPPLY_TOKENS

    def early_supply_pct(self, window_s: float) -> float | None:
        """Share of the 1B supply bought from the curve within `window_s` of creation
        (dev + snipers + bundles). None until a curve update after the window arrives."""
        cutoff = self.created_at + window_s
        if not self.ticks or self.ticks[-1].t <= cutoff:
            return None
        at_cutoff = None
        for tick in self.ticks:
            if tick.t > cutoff:
                break
            at_cutoff = tick
        if at_cutoff is None:
            return None
        return max(0.0, (CURVE_START_VIRTUAL_TOKENS - at_cutoff.virtual_tokens) / TOTAL_SUPPLY_TOKENS)
