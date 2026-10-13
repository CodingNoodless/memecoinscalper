# TradeSlaveBot paper trader

This project currently runs a paper-only trading loop. It does not load a Solana
wallet, sign transactions, or send swaps.

## Launch-dip scalper (new primary strategy)

`uv run python scalp.py` runs a separate PAPER engine built for many small,
fast trades on fresh pump.fun tokens: wait for the launch spike (snipers + dev),
buy when the price falls back to around its opening price and selling stalls,
sell the climb (default target +5%
NET of fees), and repeat on the same token while it keeps swinging. One asyncio
process watches up to `SCALP_MAX_WATCHED` tokens and holds up to
`SCALP_MAX_OPEN_POSITIONS` positions at once.

Data is free: PumpPortal's new-token websocket for discovery, and a Solana RPC
websocket (`accountSubscribe`) on each token's bonding curve, so every buy or
sell arrives as an exact curve state. Paper fills use pump.fun curve math with
the 1.25% fee on both sides, a network fee per transaction, ~0.8 s latency and
a 5% failed-transaction rate.

Every tick is recorded to `data/ticks/YYYY-MM-DD.jsonl.gz`. Replay it with
different settings:

```
uv run python -m tradeslave.scalper.backtest
uv run python -m tradeslave.scalper.backtest --grid take_profit_pct=0.03,0.05,0.08
```

All settings live in `tradeslave/scalper/config.py` and can be overridden in
`.env` as `SCALP_<NAME>` (e.g. `SCALP_TAKE_PROFIT_PCT=0.04`). The public Solana
RPC is the default; set `SCALP_RPC_WS` to another websocket endpoint if it
disconnects or rate-limits. Results: `scalp_trades.jsonl`, `scalp_wallet.json`,
`logs/scalper.log`.

`uv run python dashboard.py` now opens on the scalper monitor at
http://127.0.0.1:8765 (equity, win rate, per-trade results, open positions,
watchlist with rug-filter status, why it isn't buying, engine health). The
momentum bot's view moved to http://127.0.0.1:8765/momentum.

## Quick start

```
uv sync                 # install dependencies (add --group dev for tests)
uv run python main.py   # paper trader: scanner + trader threads
uv run python dashboard.py   # read-only dashboard at http://127.0.0.1:8765
uv run pytest           # unit tests (pure functions; no network, no real state)
```

Code lives in the `tradeslave/` package; `main.py` and `dashboard.py` are thin
entry points. Runtime state, logs and `.env` stay in the project root
(`tradeslave/paths.py`). Previous runs are kept under `archive/<date>/`.

`LLM_MODE` picks how one candidate is chosen from the screened top five:
`off` (default) takes the highest deterministic selection score with no model
latency; `shadow` trades the score pick and then logs whether Ollama would have
agreed (`llm_shadow_comparison` events); `on` restores the Ollama pick. Values
the code clamps (for example `MAX_HOLD_HOURS` above 2) and unknown `.env` keys
are logged as warnings at startup.

## Configuration

Copy `.env.example` to `.env` in the project directory and fill in local
credentials. The app loads that file automatically; variables already set in
the process environment take precedence. The scanner can run without
credentials when RugCheck's public endpoints are available. Paper entries
require `JUPITER_API_KEY`: the trader requests quote-only Jupiter V2
orders for a risk-sized USDC amount and never requests or executes a swap
transaction. If no quote is available, the candidate is skipped. Quotes are
adjusted by the configurable paper execution slippage and network-fee estimates.
RugCheck requests are serialized and paced by
`RUGCHECK_MIN_REQUEST_INTERVAL_SECONDS` (default 1.1 seconds), with bounded
retries and backoff for HTTP 429 responses and transient connection failures.

The exposed RugCheck API key that was previously in `config.py` has been
removed. Rotate it before setting a replacement in the environment.

## Candidate logic

The scanner merges RugCheck's `stats/new_tokens` feed with GeckoTerminal's
Solana newest-pools feed. Its finite scan budget ranks pools that already pass
the inexpensive liquidity, activity, price, and momentum gates before age;
within that set it prefers fresher pools. This avoids repeatedly spending all
30 requests on the youngest low-liquidity pools. Brand-new pools often lack enough closed chart bars
for the entry rule, so the trending feed adds live pools that can be checked
against the same entry chart. GeckoTerminal's public data is cached for 60
seconds. The age limit applies to pool creation time for trending pools and
token creation time for RugCheck feed entries. It then checks authorities,
RugCheck's report summary, liquidity lock percentage, and minimum live-pool
liquidity/activity/momentum. GeckoTerminal pool metrics are used for new-pool
candidates; RugCheck feed candidates are matched to a Solana pool from DEX Screener.
Each scan logs eligible and rejection counts by discovery source so a quiet
scanner can be diagnosed without interpreting a zero-trade count alone.
It ranks up to five eligible tokens using `calculate_natural_selection_score`
and picks the top score (with `LLM_MODE=on`, the local model selects one instead). The score is an experimental ranking,
not a forecast of profit. In `on` mode, for each nonempty screened batch,
Ollama receives a schema-constrained address list; its selected address is
validated against that batch before the trader can use it. Transport errors,
malformed output, or an address outside the list fail closed and do not create
a trade. `OLLAMA_TIMEOUT_SECONDS` defaults to 180 seconds. Signals default to
expiring after `MAX_CANDIDATE_AGE_SECONDS=300`; longer windows can mean acting
on older market data, so tune this to your local model's response time.

The ranking gives more weight to liquidity and RugCheck risk than to transaction
counts, buy ratios, volume intensity, or short-window momentum. Those activity
signals can be wash-traded; this feed does not provide unique-maker concentration
data to detect that reliably.

Five-minute price changes below -15% or above +40% are rejected by default,
since the previous score rewarded extreme pumps and admitted sharp selloffs.
Before a paper position is opened, the trader requests a quote to sell the
estimated bought amount back to USDC and skips the entry if estimated
round-trip loss exceeds `MAX_ENTRY_ROUNDTRIP_LOSS_PCT` (3% by default and
hard-capped at 3% even when an older `.env` contains a larger value).

After a candidate is selected, the trader retrieves closed 1-minute
pool candles from GeckoTerminal and requires at least 15 fresh bars. The entry
chart gate requires EMA(5) above EMA(13), the last close above EMA(5), a rising
fast EMA, positive but bounded three-candle momentum, and no oversized last
candle, rejection wick, or excessive extension above EMA(13). Missing, stale, or insufficient
candle history fails closed and is logged. Once selected, a token gets an
exclusive bounded 180-second entry watch: discovery and Ollama scans pause,
Jupiter Price V3 is sampled every 3 seconds when it returns a trusted last-swap
price, with the selected DEX pool as fallback; the spot source and Jupiter
block ID are logged. Closed candles are refreshed every 60 seconds. Jupiter
may omit a price if the token has no recent qualifying swaps or its price fails
the API's reliability filters. A paper entry waits for a pullback within 1% of
EMA(5) and a sampled reclaim 0.2% above it, while the closed-bar trend,
momentum, wick, and extension checks remain in force. Those pullback/reclaim settings are
experimental entry timing heuristics, not evidence of profitability. GeckoTerminal's
public responses are cached for 60 seconds, so faster chart requests would
repeat old candle data. A process lock prevents multiple bot copies from
racing on the same paper wallet and signal files.

With an open paper position, discovery remains paused and the trader targets a
Jupiter sell-quote refresh every 1.25 seconds by default. Jupiter requests have a
1.05-second minimum spacing by default to stay within the Free plan's documented
1 request/second limit, including shock confirmations. Adjust the floor only
for the actual API plan and its current limit; DexScreener spot polling uses
its documented 300 requests/minute pair endpoint limit.

When the exit quote falls by at least `QUOTE_SHOCK_CONFIRMATION_PCT` (40% by
default) from the prior estimate, the trader requests a second quote and only
acts on the shock if both estimates agree within
`QUOTE_CONFIRMATION_TOLERANCE_PCT` (12% by default). A mismatched or missing
confirmation leaves the paper position open for the next poll and records the
event. Position observations compare DEX Screener's price return with the
Jupiter sell-quote PnL and alert when the gap exceeds
`MARKET_QUOTE_DIVERGENCE_ALERT_PCT` (30 percentage points by default). This
surfaces quote/market divergence; the DEX price remains display-only.
Exit defaults use a 20% net stop, with a 10% trailing-stop activation and 12%
pullback from peak. Once the trailing stop activates, its calculated stop level
cannot fall below entry cost plus the configured 1% profit floor; a market gap
can still cause the quote to fill below that level. Maximum holding time is one
hour by default and hard-capped at two hours. These controls reduce time spent waiting
for a large recovery, but cannot cap losses across a gap or a token that becomes
unsellable; the next observed quote may already be far below the threshold.
For faster rollover detection, the trader keeps up to 24 recent executable
Jupiter sell-value samples and exits after three consecutive falling samples
with EMA(3) below EMA(8). This quote-derived chart avoids treating an indicative
spot chart as an executable exit price.

Position size is the minimum of the configured trade-size cap, the amount
consistent with a 1% planned account risk at the stop, and 5% of available
paper cash. Realized losses reaching 3% of that day's opening cash pause both
discovery and new entries until the next UTC day. This is a paper-account risk
brake, not protection against a gap, a stale quote, or an unsellable token.
Close records use trade IDs to avoid double-counting after an interrupted
close; the dashboard ignores legacy close history from before the current
paper-account baseline so manually reset balances do not produce misleading
returns.

These thresholds are experimental heuristics. They have not
been calibrated against time-ordered historical or forward returns and should
not be interpreted as probabilities or as evidence of profitability. No
strategy can promise consistent daily profit; the goal of these changes is to
reduce avoidable costs, loss concentration, and repeated bad scans while the
paper strategy gathers cleaner evidence.
For paper sampling, the default RugCheck ceiling is 40 (the upper end of its
documented warning band); danger/critical reports and enabled risky Token-2022
extensions still fail. The separate LP-lock floor defaults to zero because
RugCheck already incorporates LP-lock risk into its score. Review this setting
before any future live deployment.

## Paper state

`paper_wallet.json`, `trade_history.json`, and `active_candidates.json` are
runtime state and are ignored by Git. State writes use atomic replacement. The
candidate snapshot has a schema version and expiry; the trader clears it when
consumed or when a position changes. Existing legacy active positions without
raw token amounts are preserved and left open for review rather than silently
reset.

Runtime logs are written to `logs/tradeslavebot.log` with size-based rotation.
While a paper position is open, estimated PnL snapshots are appended to
`paper_observations.jsonl` every `PAPER_OBSERVATION_INTERVAL_SECONDS` seconds.
Those active-position snapshots also include a display-only USD spot price from
the most liquid eligible Solana pool returned by DEX Screener, at entry and at
each observation interval (15 seconds by default). The stop and PnL calculations continue to use the
Jupiter sell quote; DEX Screener's `priceUsd` is an indicative market statistic,
not an executable fill price.
Entry-watch spot samples and readiness reasons, scanner candidates, Ollama
requests/results/errors, trader signal validation, Jupiter entry quote
outcomes, entries, periodic position checks, and exits are also recorded in
`pipeline_events.jsonl`. These records include public token addresses and model
reasoning, but never API keys.

## Local dashboard

Run `python dashboard.py` in a second terminal, then open
`http://127.0.0.1:8765` in a browser. It is a read-only view of local paper state
and refreshes every five seconds. It displays cash, estimated liquidation
equity, realized and open PnL, trade outcomes, the active position, and a cash
and equity history chart. Indicative DEX spot price is labeled separately from
the Jupiter quote used for paper PnL. The dashboard binds only to localhost,
does not expose credentials, and does not place or change trades. Stop it with
Ctrl+C.

The displayed return compares current paper equity with tracked capital. It
estimates deposits or withdrawals between closed trades from the recorded paper
balances, so resets are reflected when the trade history is intact. If runtime
history is deleted or edited independently, the return and cumulative PnL may
not reconcile; reset the runtime state together when beginning a fresh
measurement period.
