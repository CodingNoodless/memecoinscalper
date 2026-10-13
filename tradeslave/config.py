import os
from pathlib import Path

from dotenv import load_dotenv

from .paths import PROJECT_ROOT


# Resolve relative to this file, not the IDE's working directory. Existing
# process environment values still take precedence over values in .env.
load_dotenv(dotenv_path=PROJECT_ROOT / ".env")


def _float_setting(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _int_setting(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


# Credentials are provided through the process environment. Never commit them.
RUGCHECK_API_KEY = os.getenv("RUGCHECK_API_KEY", "").strip()
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "").strip()

RUGCHECK_BASE_URL = "https://api.rugcheck.xyz/v1"
DEXSCREENER_BASE_URL = "https://api.dexscreener.com"
GECKOTERMINAL_BASE_URL = "https://api.geckoterminal.com/api/v2"
JUPITER_ORDER_URL = "https://api.jup.ag/swap/v2/order"
JUPITER_PRICE_URL = "https://api.jup.ag/price/v3"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:14b")
OLLAMA_TIMEOUT_SECONDS = _int_setting("OLLAMA_TIMEOUT_SECONDS", 180)

# Solana mainnet USDC mint. The paper simulator stores quoted token amounts in
# raw units and uses USDC quotes for both entry and exit.
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6

# Paper-trading configuration. These are simulation parameters, not a claim
# that the strategy is profitable or safe to deploy with a live wallet.
PAPER_STARTING_BALANCE = _float_setting("PAPER_STARTING_BALANCE", 100.0)
TRADE_SIZE_USD = _float_setting("TRADE_SIZE_USD", 50.0)
PAPER_EXECUTION_SLIPPAGE_BPS = _int_setting("PAPER_EXECUTION_SLIPPAGE_BPS", 50)
PAPER_NETWORK_FEE_USD = _float_setting("PAPER_NETWORK_FEE_USD", 0.02)
RISK_PER_TRADE_PCT = _float_setting("RISK_PER_TRADE_PCT", 0.01)
MAX_POSITION_PCT_OF_BALANCE = _float_setting("MAX_POSITION_PCT_OF_BALANCE", 0.05)
MAX_DAILY_LOSS_PCT = _float_setting("MAX_DAILY_LOSS_PCT", 0.03)

# Candidate gates. MAX_NEW_TOKEN_AGE_HOURS is retained as the env name for
# compatibility, but screening now applies it to the first tradeable pool.
# Tune only from time-ordered, out-of-sample paper results.
MAX_NEW_TOKEN_AGE_HOURS = _float_setting("MAX_NEW_TOKEN_AGE_HOURS", 2.0)
MIN_NEW_POOL_AGE_MINUTES = _float_setting("MIN_NEW_POOL_AGE_MINUTES", 15.0)
TOKEN_RETRY_COOLDOWN_SECONDS = _int_setting("TOKEN_RETRY_COOLDOWN_SECONDS", 21600)
MAX_CANDIDATE_AGE_SECONDS = _int_setting("MAX_CANDIDATE_AGE_SECONDS", 300)
ENTRY_WATCH_MAX_SECONDS = _int_setting("ENTRY_WATCH_MAX_SECONDS", 180)
ENTRY_WATCH_RECHECK_SECONDS = _int_setting("ENTRY_WATCH_RECHECK_SECONDS", 60)
ENTRY_WATCH_POLL_INTERVAL_SECONDS = _float_setting("ENTRY_WATCH_POLL_INTERVAL_SECONDS", 3.0)
ENTRY_PULLBACK_BAND_PCT = _float_setting("ENTRY_PULLBACK_BAND_PCT", 0.01)
ENTRY_RECLAIM_CONFIRMATION_PCT = _float_setting("ENTRY_RECLAIM_CONFIRMATION_PCT", 0.002)
MIN_LIQUIDITY_USD = _float_setting("MIN_LIQUIDITY_USD", 5_000.0)
MIN_TXNS_5M = _int_setting("MIN_TXNS_5M", 10)
MIN_SELECTION_SCORE = _float_setting("MIN_SELECTION_SCORE", 60.0)
MIN_ENTRY_PRICE_CHANGE_5M_PCT = _float_setting("MIN_ENTRY_PRICE_CHANGE_5M_PCT", -15.0)
MAX_ENTRY_PRICE_CHANGE_5M_PCT = _float_setting("MAX_ENTRY_PRICE_CHANGE_5M_PCT", 40.0)
MAX_RUGCHECK_SCORE = _int_setting("MAX_RUGCHECK_SCORE", 40)
MIN_LP_LOCKED_PCT = _float_setting("MIN_LP_LOCKED_PCT", 0.0)

STOP_LOSS_PCT = _float_setting("STOP_LOSS_PCT", -0.20)
TRAILING_STOP_ACTIVATION = _float_setting("TRAILING_STOP_ACTIVATION", 0.10)
TRAILING_STOP_PCT = _float_setting("TRAILING_STOP_PCT", 0.12)
TRAILING_STOP_FLOOR_PCT = _float_setting("TRAILING_STOP_FLOOR_PCT", 0.01)
MAX_HOLD_HOURS = min(_float_setting("MAX_HOLD_HOURS", 1.0), 2.0)

SCAN_INTERVAL_SECONDS = _int_setting("SCAN_INTERVAL_SECONDS", 15)
PAPER_POLL_INTERVAL_SECONDS = _int_setting("PAPER_POLL_INTERVAL_SECONDS", 5)
ACTIVE_TRADE_POLL_INTERVAL_SECONDS = _float_setting("ACTIVE_TRADE_POLL_INTERVAL_SECONDS", 1.25)
JUPITER_MIN_REQUEST_INTERVAL_SECONDS = _float_setting("JUPITER_MIN_REQUEST_INTERVAL_SECONDS", 1.05)
PAPER_OBSERVATION_INTERVAL_SECONDS = _int_setting("PAPER_OBSERVATION_INTERVAL_SECONDS", 15)
# A large immediate round-trip loss consumes too much of any plausible
# short-horizon edge. Cap stale local settings at the 3% paper-strategy budget.
MAX_ENTRY_ROUNDTRIP_LOSS_PCT = min(
    _float_setting("MAX_ENTRY_ROUNDTRIP_LOSS_PCT", 0.03), 0.03
)
QUOTE_SHOCK_CONFIRMATION_PCT = _float_setting("QUOTE_SHOCK_CONFIRMATION_PCT", 0.40)
QUOTE_CONFIRMATION_TOLERANCE_PCT = _float_setting("QUOTE_CONFIRMATION_TOLERANCE_PCT", 0.12)
MARKET_QUOTE_DIVERGENCE_ALERT_PCT = _float_setting("MARKET_QUOTE_DIVERGENCE_ALERT_PCT", 0.30)
CHART_MIN_CANDLES = _int_setting("CHART_MIN_CANDLES", 15)
CHART_MAX_STALENESS_SECONDS = _int_setting("CHART_MAX_STALENESS_SECONDS", 180)
CHART_MAX_ENTRY_3M_RETURN_PCT = _float_setting("CHART_MAX_ENTRY_3M_RETURN_PCT", 0.20)
CHART_MAX_ENTRY_CANDLE_RETURN_PCT = _float_setting("CHART_MAX_ENTRY_CANDLE_RETURN_PCT", 0.15)
CHART_MAX_ENTRY_UPPER_WICK_PCT = _float_setting("CHART_MAX_ENTRY_UPPER_WICK_PCT", 0.20)
CHART_MAX_EMA_EXTENSION_PCT = _float_setting("CHART_MAX_EMA_EXTENSION_PCT", 0.20)
CHART_REQUEST_MIN_INTERVAL_SECONDS = _float_setting("CHART_REQUEST_MIN_INTERVAL_SECONDS", 8.0)
QUOTE_CHART_FAST_EMA_PERIOD = _int_setting("QUOTE_CHART_FAST_EMA_PERIOD", 3)
QUOTE_CHART_SLOW_EMA_PERIOD = _int_setting("QUOTE_CHART_SLOW_EMA_PERIOD", 8)
QUOTE_CHART_BEARISH_CONFIRMATIONS = _int_setting("QUOTE_CHART_BEARISH_CONFIRMATIONS", 3)
HTTP_TIMEOUT_SECONDS = _int_setting("HTTP_TIMEOUT_SECONDS", 10)
RUGCHECK_HTTP_TIMEOUT_SECONDS = _int_setting("RUGCHECK_HTTP_TIMEOUT_SECONDS", 20)
RUGCHECK_MIN_REQUEST_INTERVAL_SECONDS = _float_setting(
    "RUGCHECK_MIN_REQUEST_INTERVAL_SECONDS", 1.1
)

if PAPER_STARTING_BALANCE <= 0:
    raise ValueError("PAPER_STARTING_BALANCE must be positive")
if TRADE_SIZE_USD <= 0:
    raise ValueError("TRADE_SIZE_USD must be positive")
if not 0 <= PAPER_EXECUTION_SLIPPAGE_BPS < 10_000:
    raise ValueError("PAPER_EXECUTION_SLIPPAGE_BPS must be in [0, 10000)")
if PAPER_NETWORK_FEE_USD < 0:
    raise ValueError("PAPER_NETWORK_FEE_USD cannot be negative")
if not 0 < RISK_PER_TRADE_PCT <= 0.05:
    raise ValueError("RISK_PER_TRADE_PCT must be in (0, 0.05]")
if not 0 < MAX_POSITION_PCT_OF_BALANCE <= 0.25:
    raise ValueError("MAX_POSITION_PCT_OF_BALANCE must be in (0, 0.25]")
if not 0 < MAX_DAILY_LOSS_PCT <= 0.10:
    raise ValueError("MAX_DAILY_LOSS_PCT must be in (0, 0.10]")
if MAX_NEW_TOKEN_AGE_HOURS <= 0 or MAX_CANDIDATE_AGE_SECONDS <= 0:
    raise ValueError("Token and candidate age limits must be positive")
if MIN_NEW_POOL_AGE_MINUTES < 0 or MIN_NEW_POOL_AGE_MINUTES >= MAX_NEW_TOKEN_AGE_HOURS * 60:
    raise ValueError("MIN_NEW_POOL_AGE_MINUTES must be nonnegative and below the maximum token age")
if TOKEN_RETRY_COOLDOWN_SECONDS < 0:
    raise ValueError("TOKEN_RETRY_COOLDOWN_SECONDS cannot be negative")
if ENTRY_WATCH_MAX_SECONDS <= 0 or ENTRY_WATCH_RECHECK_SECONDS <= 0:
    raise ValueError("Entry watch and recheck intervals must be positive")
if ENTRY_WATCH_RECHECK_SECONDS > ENTRY_WATCH_MAX_SECONDS:
    raise ValueError("Entry watch recheck interval cannot exceed the watch duration")
if ENTRY_WATCH_POLL_INTERVAL_SECONDS <= 0:
    raise ValueError("ENTRY_WATCH_POLL_INTERVAL_SECONDS must be positive")
if not 0 <= ENTRY_PULLBACK_BAND_PCT < 1:
    raise ValueError("ENTRY_PULLBACK_BAND_PCT must be in [0, 1)")
if not 0 <= ENTRY_RECLAIM_CONFIRMATION_PCT < 1:
    raise ValueError("ENTRY_RECLAIM_CONFIRMATION_PCT must be in [0, 1)")
if OLLAMA_TIMEOUT_SECONDS <= 0:
    raise ValueError("OLLAMA_TIMEOUT_SECONDS must be positive")
if MAX_HOLD_HOURS <= 0:
    raise ValueError("MAX_HOLD_HOURS must be positive")
if not 0 <= TRAILING_STOP_FLOOR_PCT < 0.10:
    raise ValueError("TRAILING_STOP_FLOOR_PCT must be in [0, 0.10)")
if RUGCHECK_MIN_REQUEST_INTERVAL_SECONDS < 0:
    raise ValueError("RUGCHECK_MIN_REQUEST_INTERVAL_SECONDS cannot be negative")
if RUGCHECK_HTTP_TIMEOUT_SECONDS <= 0:
    raise ValueError("RUGCHECK_HTTP_TIMEOUT_SECONDS must be positive")
if PAPER_OBSERVATION_INTERVAL_SECONDS <= 0:
    raise ValueError("PAPER_OBSERVATION_INTERVAL_SECONDS must be positive")
if ACTIVE_TRADE_POLL_INTERVAL_SECONDS <= 0:
    raise ValueError("ACTIVE_TRADE_POLL_INTERVAL_SECONDS must be positive")
if JUPITER_MIN_REQUEST_INTERVAL_SECONDS <= 0:
    raise ValueError("JUPITER_MIN_REQUEST_INTERVAL_SECONDS must be positive")
if not 0 <= MIN_LP_LOCKED_PCT <= 100:
    raise ValueError("MIN_LP_LOCKED_PCT must be in [0, 100]")
if MIN_ENTRY_PRICE_CHANGE_5M_PCT >= MAX_ENTRY_PRICE_CHANGE_5M_PCT:
    raise ValueError("MIN_ENTRY_PRICE_CHANGE_5M_PCT must be below MAX_ENTRY_PRICE_CHANGE_5M_PCT")
if not 0 <= MAX_ENTRY_ROUNDTRIP_LOSS_PCT < 1:
    raise ValueError("MAX_ENTRY_ROUNDTRIP_LOSS_PCT must be in [0, 1)")
if not 0 < QUOTE_SHOCK_CONFIRMATION_PCT < 1:
    raise ValueError("QUOTE_SHOCK_CONFIRMATION_PCT must be in (0, 1)")
if not 0 < QUOTE_CONFIRMATION_TOLERANCE_PCT < 1:
    raise ValueError("QUOTE_CONFIRMATION_TOLERANCE_PCT must be in (0, 1)")
if not 0 < MARKET_QUOTE_DIVERGENCE_ALERT_PCT < 2:
    raise ValueError("MARKET_QUOTE_DIVERGENCE_ALERT_PCT must be in (0, 2)")
if CHART_MIN_CANDLES < 13:
    raise ValueError("CHART_MIN_CANDLES must be at least 13 for the slow EMA")
if CHART_MAX_STALENESS_SECONDS <= 0 or CHART_REQUEST_MIN_INTERVAL_SECONDS < 0:
    raise ValueError("Chart staleness must be positive and chart request interval cannot be negative")
if not 0 < CHART_MAX_ENTRY_3M_RETURN_PCT < 2:
    raise ValueError("CHART_MAX_ENTRY_3M_RETURN_PCT must be in (0, 2)")
if not 0 < CHART_MAX_ENTRY_CANDLE_RETURN_PCT < 2:
    raise ValueError("CHART_MAX_ENTRY_CANDLE_RETURN_PCT must be in (0, 2)")
if not 0 < CHART_MAX_ENTRY_UPPER_WICK_PCT < 2:
    raise ValueError("CHART_MAX_ENTRY_UPPER_WICK_PCT must be in (0, 2)")
if not 0 < CHART_MAX_EMA_EXTENSION_PCT < 2:
    raise ValueError("CHART_MAX_EMA_EXTENSION_PCT must be in (0, 2)")
if QUOTE_CHART_FAST_EMA_PERIOD <= 0 or QUOTE_CHART_SLOW_EMA_PERIOD <= QUOTE_CHART_FAST_EMA_PERIOD:
    raise ValueError("Quote chart slow EMA period must exceed a positive fast EMA period")
if QUOTE_CHART_BEARISH_CONFIRMATIONS <= 0:
    raise ValueError("QUOTE_CHART_BEARISH_CONFIRMATIONS must be positive")
