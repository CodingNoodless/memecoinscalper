"""Pin every strategy setting the tests touch, so results never depend on the
local .env. load_dotenv() does not override variables already in the process
environment, so these must be set before tradeslave.config is imported."""

import os
import sys
from pathlib import Path

TEST_SETTINGS = {
    "LLM_MODE": "off",
    "PAPER_STARTING_BALANCE": "100",
    "TRADE_SIZE_USD": "50",
    "PAPER_EXECUTION_SLIPPAGE_BPS": "50",
    "RISK_PER_TRADE_PCT": "0.01",
    "MAX_POSITION_PCT_OF_BALANCE": "0.05",
    "MAX_DAILY_LOSS_PCT": "0.03",
    "MAX_NEW_TOKEN_AGE_HOURS": "2",
    "MIN_NEW_POOL_AGE_MINUTES": "15",
    "MIN_LIQUIDITY_USD": "5000",
    "MIN_TXNS_5M": "10",
    "MIN_SELECTION_SCORE": "60",
    "MIN_ENTRY_PRICE_CHANGE_5M_PCT": "-15",
    "MAX_ENTRY_PRICE_CHANGE_5M_PCT": "40",
    "MAX_RUGCHECK_SCORE": "40",
    "MIN_LP_LOCKED_PCT": "0",
    "STOP_LOSS_PCT": "-0.20",
    "MAX_HOLD_HOURS": "1",
    "ENTRY_PULLBACK_BAND_PCT": "0.01",
    "ENTRY_RECLAIM_CONFIRMATION_PCT": "0.002",
    "CHART_MAX_STALENESS_SECONDS": "180",
    "CHART_MAX_EMA_EXTENSION_PCT": "0.20",
    "QUOTE_CHART_FAST_EMA_PERIOD": "3",
    "QUOTE_CHART_SLOW_EMA_PERIOD": "8",
    "QUOTE_CHART_BEARISH_CONFIRMATIONS": "3",
}
os.environ.update(TEST_SETTINGS)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from tradeslave import state  # noqa: E402


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Redirect all state reads/writes to a temp folder (never the real paper account)."""
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    from tradeslave.scalper import engine
    monkeypatch.setattr(engine, "LOG_DIR", tmp_path / "logs")
    return tmp_path
