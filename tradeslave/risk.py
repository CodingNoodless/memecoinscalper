"""Account-scoped paper risk controls shared by scanner and trader."""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any

from .config import MAX_DAILY_LOSS_PCT
from .state import load_json


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def daily_realized_risk(wallet: dict[str, Any]) -> tuple[str, float, float, float]:
    """Return UTC day, account-scoped realized PnL, loss cap, and day start.

    Legacy closes without an account ID are excluded after wallet migration;
    duplicate close rows are counted once by trade ID.
    """
    day = datetime.now(timezone.utc).date().isoformat()
    account_id = wallet.get("account_id")
    history = load_json("trade_history.json", [])
    if not isinstance(history, list):
        history = []
    seen_trade_ids: set[str] = set()
    daily_pnl = 0.0
    for row in history:
        if not isinstance(row, dict) or row.get("account_id") != account_id:
            continue
        closed_at = row.get("closed_at")
        if not isinstance(closed_at, str):
            continue
        try:
            parsed = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed.astimezone(timezone.utc).date().isoformat() != day:
            continue
        trade_id = row.get("trade_id")
        if isinstance(trade_id, str) and trade_id:
            if trade_id in seen_trade_ids:
                continue
            seen_trade_ids.add(trade_id)
        profit = _number(row.get("profit_usd"))
        if profit is not None:
            daily_pnl += profit

    balance = _number(wallet.get("balance")) or 0.0
    day_start_balance = max(0.01, balance - daily_pnl)
    loss_limit = day_start_balance * MAX_DAILY_LOSS_PCT
    return day, daily_pnl, loss_limit, day_start_balance
