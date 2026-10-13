import json
import logging
import math
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from .config import (
    DEXSCREENER_BASE_URL,
    GECKOTERMINAL_BASE_URL,
    ENTRY_WATCH_MAX_SECONDS,
    ENTRY_WATCH_RECHECK_SECONDS,
    ENTRY_WATCH_POLL_INTERVAL_SECONDS,
    ENTRY_PULLBACK_BAND_PCT,
    ENTRY_RECLAIM_CONFIRMATION_PCT,
    ACTIVE_TRADE_POLL_INTERVAL_SECONDS,
    JUPITER_MIN_REQUEST_INTERVAL_SECONDS,
    CHART_MIN_CANDLES,
    CHART_MAX_STALENESS_SECONDS,
    CHART_MAX_ENTRY_3M_RETURN_PCT,
    CHART_MAX_ENTRY_CANDLE_RETURN_PCT,
    CHART_MAX_ENTRY_UPPER_WICK_PCT,
    CHART_MAX_EMA_EXTENSION_PCT,
    CHART_REQUEST_MIN_INTERVAL_SECONDS,
    HTTP_TIMEOUT_SECONDS,
    JUPITER_API_KEY,
    JUPITER_ORDER_URL,
    JUPITER_PRICE_URL,
    MAX_HOLD_HOURS,
    MAX_CANDIDATE_AGE_SECONDS,
    MIN_SELECTION_SCORE,
    MAX_ENTRY_ROUNDTRIP_LOSS_PCT,
    MARKET_QUOTE_DIVERGENCE_ALERT_PCT,
    PAPER_EXECUTION_SLIPPAGE_BPS,
    PAPER_NETWORK_FEE_USD,
    RISK_PER_TRADE_PCT,
    MAX_POSITION_PCT_OF_BALANCE,
    PAPER_OBSERVATION_INTERVAL_SECONDS,
    PAPER_POLL_INTERVAL_SECONDS,
    PAPER_STARTING_BALANCE,
    QUOTE_CONFIRMATION_TOLERANCE_PCT,
    QUOTE_CHART_FAST_EMA_PERIOD,
    QUOTE_CHART_SLOW_EMA_PERIOD,
    QUOTE_CHART_BEARISH_CONFIRMATIONS,
    QUOTE_SHOCK_CONFIRMATION_PCT,
    STOP_LOSS_PCT,
    TRADE_SIZE_USD,
    TRAILING_STOP_ACTIVATION,
    TRAILING_STOP_PCT,
    TRAILING_STOP_FLOOR_PCT,
    USDC_DECIMALS,
    USDC_MINT,
)
from .scanner import parse_timestamp
from .risk import daily_realized_risk
from .state import (
    STATE_LOCK,
    append_jsonl,
    load_json,
    load_pending_entry,
    mark_token_attempt,
    save_json,
    save_pending_entry,
    save_wallet_and_clear_candidates,
    take_candidate_snapshot,
)


logger = logging.getLogger(__name__)
SESSION = requests.Session()
_CHART_REQUEST_LOCK = threading.Lock()
_LAST_CHART_REQUEST_AT = 0.0
_CHART_CACHE: dict[str, tuple[float, list[list[Any]]]] = {}
_JUPITER_REQUEST_LOCK = threading.Lock()
_LAST_JUPITER_REQUEST_AT = 0.0
_warned_missing_jupiter_key = False
_warned_insufficient_paper_balance = False
_daily_loss_pause_logged_for: str | None = None


def _record_pipeline_event(event: str, **fields: Any) -> None:
    try:
        append_jsonl(
            "pipeline_events.jsonl",
            {"at": utc_timestamp(), "component": "paper_trader", "event": event, **fields},
        )
    except Exception:
        logger.exception("Could not persist pipeline event %s", event)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _ema(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    value = sum(values[:period]) / period
    alpha = 2.0 / (period + 1.0)
    for point in values[period:]:
        value = alpha * point + (1.0 - alpha) * value
    return value


def get_entry_chart_analysis(pool_address: str) -> dict[str, Any]:
    """Fetch closed 1m pool candles and apply a conservative trend/extension gate."""
    global _LAST_CHART_REQUEST_AT
    if not pool_address:
        raise ValueError("candidate has no pool address for chart lookup")

    cached = _CHART_CACHE.get(pool_address)
    if cached and time.monotonic() - cached[0] < 60.0:
        rows = cached[1]
    else:
        with _CHART_REQUEST_LOCK:
            cached = _CHART_CACHE.get(pool_address)
            if cached and time.monotonic() - cached[0] < 60.0:
                rows = cached[1]
            else:
                wait_for_slot = CHART_REQUEST_MIN_INTERVAL_SECONDS - (
                    time.monotonic() - _LAST_CHART_REQUEST_AT
                )
                if wait_for_slot > 0:
                    time.sleep(wait_for_slot)
                response = SESSION.get(
                    f"{GECKOTERMINAL_BASE_URL}/networks/solana/pools/{pool_address}/ohlcv/minute",
                    params={"aggregate": "1", "limit": "30", "currency": "usd", "token": "base"},
                    headers={"Accept": "application/json;version=20230203"},
                    timeout=HTTP_TIMEOUT_SECONDS,
                )
                _LAST_CHART_REQUEST_AT = time.monotonic()
                response.raise_for_status()
                body = response.json()
                data = body.get("data") if isinstance(body, dict) else None
                attributes = data.get("attributes") if isinstance(data, dict) else None
                rows = attributes.get("ohlcv_list") if isinstance(attributes, dict) else None
                if not isinstance(rows, list):
                    raise ValueError("GeckoTerminal returned no OHLCV candle list")
                _CHART_CACHE[pool_address] = (time.monotonic(), rows)

    now_epoch = int(time.time())
    current_minute_start = now_epoch - (now_epoch % 60)
    candles: list[dict[str, float]] = []
    seen_timestamps: set[int] = set()
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            timestamp = int(row[0])
        except (TypeError, ValueError):
            continue
        if timestamp >= current_minute_start or timestamp in seen_timestamps:
            continue  # Drop the still-forming candle; use closed bars only.
        values = [_finite_number(value) for value in row[1:6]]
        if any(value is None for value in values):
            continue
        open_price, high, low, close, volume = values
        if min(open_price, high, low, close) <= 0 or volume < 0:
            continue
        seen_timestamps.add(timestamp)
        candles.append({
            "timestamp": float(timestamp), "open": open_price, "high": high,
            "low": low, "close": close, "volume": volume,
        })
    candles.sort(key=lambda candle: candle["timestamp"])
    candles = candles[-30:]
    if not candles:
        raise ValueError("no valid closed 1-minute candles")

    closes = [candle["close"] for candle in candles]
    latest = candles[-1]
    latest_closed_at = latest["timestamp"] + 60.0
    staleness_seconds = max(0.0, now_epoch - latest_closed_at)
    if staleness_seconds > CHART_MAX_STALENESS_SECONDS:
        return {
            "eligible": False, "rejection_reason": "chart_candles_stale",
            "candle_count": len(candles), "latest_candle_at": int(latest["timestamp"]),
            "staleness_seconds": round(staleness_seconds, 1),
        }
    if len(candles) < CHART_MIN_CANDLES:
        return {
            "eligible": False, "rejection_reason": "insufficient_closed_chart_history",
            "candle_count": len(candles), "latest_candle_at": int(latest["timestamp"]),
            "staleness_seconds": round(staleness_seconds, 1),
        }

    fast_ema = _ema(closes, 5)
    slow_ema = _ema(closes, 13)
    prior_fast_ema = _ema(closes[:-3], 5)
    three_candle_return = closes[-1] / closes[-4] - 1.0
    last_candle_return = latest["close"] / latest["open"] - 1.0
    upper_wick_pct = max(0.0, latest["high"] - max(latest["open"], latest["close"])) / latest["close"]
    ema_extension = latest["close"] / slow_ema - 1.0 if slow_ema else None

    reasons = []
    if fast_ema is None or slow_ema is None or prior_fast_ema is None:
        reasons.append("chart_ema_unavailable")
    else:
        if fast_ema <= slow_ema:
            reasons.append("chart_bearish_ema_alignment")
        if latest["close"] <= fast_ema:
            reasons.append("chart_close_below_fast_ema")
        if fast_ema <= prior_fast_ema:
            reasons.append("chart_fast_ema_not_rising")
        if ema_extension is not None and ema_extension > CHART_MAX_EMA_EXTENSION_PCT:
            reasons.append("chart_price_overextended_from_slow_ema")
    if three_candle_return <= 0:
        reasons.append("chart_no_positive_3m_momentum")
    if three_candle_return > CHART_MAX_ENTRY_3M_RETURN_PCT:
        reasons.append("chart_3m_move_overextended")
    if last_candle_return > CHART_MAX_ENTRY_CANDLE_RETURN_PCT:
        reasons.append("chart_latest_candle_spike")
    if upper_wick_pct > CHART_MAX_ENTRY_UPPER_WICK_PCT:
        reasons.append("chart_latest_candle_upper_wick_rejection")

    return {
        "eligible": not reasons,
        "rejection_reason": reasons[0] if reasons else None,
        "rejection_reasons": reasons,
        "candle_count": len(candles),
        "latest_candle_at": int(latest["timestamp"]),
        "staleness_seconds": round(staleness_seconds, 1),
        "last_close_usd": latest["close"],
        "fast_ema_5": fast_ema,
        "slow_ema_13": slow_ema,
        "prior_fast_ema_5": prior_fast_ema,
        "three_candle_return_pct": three_candle_return * 100.0,
        "last_candle_return_pct": last_candle_return * 100.0,
        "last_upper_wick_pct": upper_wick_pct * 100.0,
        "slow_ema_extension_pct": ema_extension * 100.0 if ema_extension is not None else None,
        "recent_volume_usd_3m": sum(candle["volume"] for candle in candles[-3:]),
    }


def _update_quote_chart(trade: dict[str, Any], net_exit_usd: float, observed_at: str) -> dict[str, Any]:
    """Track executable sell-value samples and detect a confirmed short-term rollover."""
    history = trade.get("quote_chart_history")
    if not isinstance(history, list):
        history = []
    history.append({"at": observed_at, "net_exit_usd": net_exit_usd})
    history = history[-24:]
    trade["quote_chart_history"] = history
    values = [
        value for item in history
        if isinstance(item, dict)
        and (value := _finite_number(item.get("net_exit_usd"))) is not None
        and value > 0
    ]

    fast = _ema(values, QUOTE_CHART_FAST_EMA_PERIOD)
    slow = _ema(values, QUOTE_CHART_SLOW_EMA_PERIOD)
    previous_fast = _ema(values[:-1], QUOTE_CHART_FAST_EMA_PERIOD) if len(values) > 1 else None
    previous_value = values[-2] if len(values) > 1 else None
    bearish = (
        fast is not None and slow is not None and previous_fast is not None
        and previous_value is not None
        and fast < slow and fast < previous_fast and net_exit_usd < previous_value
    )
    count = int(trade.get("quote_chart_bearish_count", 0)) + 1 if bearish else 0
    trade["quote_chart_bearish_count"] = count
    return {
        "sample_count": len(values), "fast_ema_net_exit_usd": fast,
        "slow_ema_net_exit_usd": slow, "bearish_confirmations": count,
        "bearish_exit": count >= QUOTE_CHART_BEARISH_CONFIRMATIONS,
    }


def get_swap_quote(input_mint: str, output_mint: str, input_amount_raw: int) -> dict[str, Any] | None:
    """Fetch a quote-only Jupiter V2 order. No wallet or transaction is used."""
    global _LAST_JUPITER_REQUEST_AT
    if not JUPITER_API_KEY or input_amount_raw <= 0:
        return None
    response = _jupiter_get(
        JUPITER_ORDER_URL,
        params={
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(input_amount_raw),
        },
    )
    response.raise_for_status()
    quote = response.json()
    if not isinstance(quote, dict):
        return None
    try:
        out_amount = int(quote.get("outAmount", "0"))
    except (TypeError, ValueError):
        return None
    if out_amount <= 0:
        return None
    return {
        "out_amount_raw": out_amount,
        "fee_bps": _finite_number(quote.get("feeBps")),
        "router": quote.get("router"),
        "quoted_at": utc_timestamp(),
    }


def _jupiter_get(url: str, params: dict[str, str]) -> requests.Response:
    global _LAST_JUPITER_REQUEST_AT
    with _JUPITER_REQUEST_LOCK:
        wait_for_slot = JUPITER_MIN_REQUEST_INTERVAL_SECONDS - (
            time.monotonic() - _LAST_JUPITER_REQUEST_AT
        )
        if wait_for_slot > 0:
            time.sleep(wait_for_slot)
        _LAST_JUPITER_REQUEST_AT = time.monotonic()
        return SESSION.get(
            url,
            params=params,
            headers={"x-api-key": JUPITER_API_KEY, "Accept": "application/json"},
            timeout=HTTP_TIMEOUT_SECONDS,
        )


def _get_jupiter_token_price(address: str) -> dict[str, Any]:
    observed_at = utc_timestamp()
    response = _jupiter_get(JUPITER_PRICE_URL, {"ids": address})
    response.raise_for_status()
    body = response.json()
    item = body.get(address) if isinstance(body, dict) else None
    price = _finite_number(item.get("usdPrice")) if isinstance(item, dict) else None
    if price is None or price <= 0:
        return {
            "market_price_source": "Jupiter Price V3",
            "market_price_observed_at": observed_at,
            "market_price_error": "No trusted Jupiter price returned for token",
        }
    return {
        "market_price_source": "Jupiter Price V3",
        "market_price_observed_at": observed_at,
        "market_price_usd": price,
        "market_price_block_id": item.get("blockId"),
        "market_price_token_decimals": item.get("decimals"),
    }


def _get_market_price(address: str, preferred_pair_address: str | None = None) -> dict[str, Any]:
    """Get display-only USD spot stats from the most liquid DexScreener pool."""
    observed_at = utc_timestamp()
    try:
        response = SESSION.get(
            f"{DEXSCREENER_BASE_URL}/token-pairs/v1/solana/{address}",
            headers={"Accept": "application/json"},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        if response.status_code == 404:
            pair = None
        else:
            response.raise_for_status()
            pairs = response.json()
            if not isinstance(pairs, list):
                raise ValueError("DexScreener returned a non-list pool response")
            eligible = []
            for item in pairs:
                if not isinstance(item, dict) or item.get("chainId") != "solana":
                    continue
                base_token = item.get("baseToken")
                liquidity = _finite_number((item.get("liquidity") or {}).get("usd"))
                if (
                    isinstance(base_token, dict)
                    and base_token.get("address") == address
                    and liquidity is not None
                    and liquidity > 0
                ):
                    eligible.append((liquidity, item))
            if preferred_pair_address:
                pair = next(
                    (item for _, item in eligible if item.get("pairAddress") == preferred_pair_address),
                    None,
                )
            else:
                pair = max(eligible, key=lambda row: row[0])[1] if eligible else None
    except Exception as exc:
        return {
            "market_price_source": "DexScreener",
            "market_price_observed_at": observed_at,
            "market_price_error": f"{type(exc).__name__}: {exc}",
        }
    if pair is None:
        return {
            "market_price_source": "DexScreener",
            "market_price_observed_at": observed_at,
            "market_price_error": "No eligible Solana pool returned",
        }
    price = _finite_number(pair.get("priceUsd"))
    result = {
        "market_price_source": "DexScreener",
        "market_price_observed_at": observed_at,
        "market_price_usd": price,
        "market_price_pair_address": str(pair.get("pairAddress")) if pair.get("pairAddress") is not None else None,
        "market_price_dex_id": str(pair.get("dexId")) if pair.get("dexId") is not None else None,
        "market_price_liquidity_usd": _finite_number((pair.get("liquidity") or {}).get("usd")),
    }
    if price is None or price <= 0:
        result["market_price_error"] = "Pool did not return a positive USD price"
    return result


def _get_entry_watch_price(address: str, pair_address: str | None) -> dict[str, Any]:
    """Prefer Jupiter's latest trusted swap price; fall back to the same DEX pool."""
    try:
        price = _get_jupiter_token_price(address)
    except requests.RequestException as exc:
        price = {
            "market_price_source": "Jupiter Price V3",
            "market_price_observed_at": utc_timestamp(),
            "market_price_error": f"{type(exc).__name__}: {exc}",
        }
    if price.get("market_price_usd") is not None:
        return price

    fallback = _get_market_price(address, pair_address)
    fallback["market_price_fallback_reason"] = price.get("market_price_error")
    if fallback.get("market_price_usd") is not None:
        fallback["market_price_source"] = "DexScreener pool (Jupiter Price V3 unavailable)"
    else:
        fallback["market_price_error"] = (
            f"Jupiter: {price.get('market_price_error')}; "
            f"DEX Screener: {fallback.get('market_price_error')}"
        )
    return fallback


def _empty_wallet() -> dict[str, Any]:
    return {"schema_version": 2, "balance": PAPER_STARTING_BALANCE, "active_trade": None}


def _load_wallet() -> dict[str, Any]:
    wallet = load_json("paper_wallet.json", None)
    if wallet is None:
        from .state import state_path

        if state_path("paper_wallet.json").exists():
            raise ValueError("paper_wallet.json contains null; refusing to reset paper account")
        wallet = _empty_wallet()
        save_json("paper_wallet.json", wallet)
    if not isinstance(wallet, dict):
        raise ValueError("paper_wallet.json must contain an object")
    balance = _finite_number(wallet.get("balance"))
    if balance is None or balance < 0 or "active_trade" not in wallet:
        raise ValueError("paper_wallet.json is malformed; refusing to reset paper account")
    wallet["balance"] = balance
    account_changed = False
    if not isinstance(wallet.get("account_id"), str) or not wallet.get("account_id"):
        # Existing paper history can span manual resets. Give the current cash
        # balance a baseline so new performance and daily-risk stats do not
        # silently combine separate paper-account runs.
        wallet["account_id"] = uuid.uuid4().hex[:12]
        wallet["account_started_at"] = utc_timestamp()
        wallet["starting_balance"] = balance
        account_changed = True
    if isinstance(wallet.get("active_trade"), dict) and not wallet["active_trade"].get("account_id"):
        wallet["active_trade"]["account_id"] = wallet["account_id"]
        account_changed = True
    if account_changed:
        save_json("paper_wallet.json", wallet)
        _record_pipeline_event(
            "paper_account_baseline",
            account_id=wallet["account_id"],
            starting_balance_usd=wallet.get("starting_balance", balance),
            reason="legacy_wallet_migrated",
        )
    active = wallet.get("active_trade")
    if isinstance(active, dict) and active.get("trade_id"):
        history = load_json("trade_history.json", [])
        recorded_close = next(
            (
                row for row in history
                if isinstance(row, dict) and row.get("trade_id") == active.get("trade_id")
            ),
            None,
        ) if isinstance(history, list) else None
        if recorded_close is not None:
            recorded_balance = _finite_number(recorded_close.get("wallet_balance"))
            if recorded_balance is not None:
                wallet["balance"] = recorded_balance
            wallet["active_trade"] = None
            save_wallet_and_clear_candidates(wallet)
            logger.warning(
                "Recovered paper close for trade=%s from durable history; wallet cash reconciled to $%.2f",
                active.get("trade_id"), wallet["balance"],
            )
            _record_pipeline_event(
                "paper_close_reconciled", account_id=wallet.get("account_id"),
                trade_id=active.get("trade_id"), wallet_balance=wallet["balance"],
            )
    return wallet


def _sized_trade_amount(balance: float) -> float:
    """Size paper entries to a small loss budget and an exposure ceiling."""
    stop_distance = abs(STOP_LOSS_PCT)
    if stop_distance <= 0:
        return 0.0
    risk_budget = balance * RISK_PER_TRADE_PCT
    risk_sized_amount = risk_budget / stop_distance
    exposure_cap = balance * MAX_POSITION_PCT_OF_BALANCE
    return round(min(TRADE_SIZE_USD, risk_sized_amount, exposure_cap), 2)


def _candidate_is_fresh(snapshot: dict[str, Any], candidate: dict[str, Any], now: datetime) -> bool:
    generated_at = parse_timestamp(snapshot.get("generated_at"))
    observed_at = parse_timestamp(candidate.get("observed_at"))
    if generated_at is None or observed_at is None:
        return False
    generated_age = (now - generated_at).total_seconds()
    observed_age = (now - observed_at).total_seconds()
    return (
        -5 <= generated_age <= MAX_CANDIDATE_AGE_SECONDS
        and -5 <= observed_age <= MAX_CANDIDATE_AGE_SECONDS
    )


def _choose_candidate(
    snapshot: Any,
) -> tuple[tuple[dict[str, Any], dict[str, Any]] | None, str, dict[str, Any]]:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 2:
        return None, "invalid_snapshot", {}
    decisions = snapshot.get("llm_decisions")
    candidates = snapshot.get("candidates")
    if not isinstance(decisions, list) or not isinstance(candidates, list):
        return None, "missing_candidate_or_decision_list", {"scan_id": snapshot.get("scan_id")}
    if not candidates:
        return None, "no_candidates", {"scan_id": snapshot.get("scan_id")}
    if not decisions:
        return None, "no_valid_ollama_decision", {"scan_id": snapshot.get("scan_id"), "candidate_count": len(candidates)}

    candidate_by_address = {
        candidate.get("tokenAddress"): candidate
        for candidate in candidates
        if isinstance(candidate, dict) and isinstance(candidate.get("tokenAddress"), str)
    }
    valid_buys = []
    now = datetime.now(timezone.utc)
    rejected: Counter[str] = Counter()
    signal_age_seconds = None
    for decision in decisions:
        if not isinstance(decision, dict):
            rejected["malformed_decision"] += 1
            continue
        if decision.get("action") != "BUY":
            rejected["not_buy"] += 1
            continue
        address = decision.get("address")
        candidate = candidate_by_address.get(address)
        if candidate is None:
            rejected["address_not_in_candidates"] += 1
            continue
        observed_at = parse_timestamp(candidate.get("observed_at"))
        signal_age_seconds = round((now - observed_at).total_seconds(), 2) if observed_at else None
        if not _candidate_is_fresh(snapshot, candidate, now):
            rejected["stale_or_missing_timestamp"] += 1
            continue
        score = _finite_number(candidate.get("selection_score"))
        confidence = decision.get("confidence")
        if score is None or score < MIN_SELECTION_SCORE:
            rejected["selection_score_below_minimum"] += 1
            continue
        if isinstance(confidence, bool) or not isinstance(confidence, int) or not 0 <= confidence <= 100:
            rejected["invalid_confidence"] += 1
            continue
        # Confidence is retained for diagnostics, not treated as a calibrated
        # probability. Ollama nominates one address; deterministic gates still
        # control whether that signal is actionable.
        valid_buys.append((score, candidate, decision))
    if not valid_buys:
        return None, "no_usable_buy_decision", {
            "scan_id": snapshot.get("scan_id"),
            "candidate_count": len(candidates),
            "decision_count": len(decisions),
            "rejected_decisions": dict(rejected),
            "signal_age_seconds": signal_age_seconds,
        }
    _, candidate, decision = max(valid_buys, key=lambda row: row[0])
    return (candidate, decision), "selected", {
        "scan_id": snapshot.get("scan_id"),
        "candidate_count": len(candidates),
        "decision_count": len(decisions),
        "signal_age_seconds": signal_age_seconds,
        "selection_score": candidate.get("selection_score"),
        "symbol": candidate.get("symbol"),
        "address": candidate.get("tokenAddress"),
        "confidence": decision.get("confidence"),
    }


def _paper_haircut(amount_usd: float) -> float:
    return amount_usd * (1.0 - PAPER_EXECUTION_SLIPPAGE_BPS / 10_000.0)


def _entry_pullback_reclaim_status(
    pending: dict[str, Any],
    chart_analysis: dict[str, Any],
    current_price: float | None,
) -> tuple[bool, str]:
    """Require a fresh spot-price pullback and reclaim near the closed-bar EMA."""
    if current_price is None or current_price <= 0:
        return False, "live_market_price_unavailable"

    fast_ema = _finite_number(chart_analysis.get("fast_ema_5"))
    slow_ema = _finite_number(chart_analysis.get("slow_ema_13"))
    if fast_ema is None or slow_ema is None or fast_ema <= slow_ema:
        return False, "closed_chart_trend_not_bullish"
    latest_candle_at = _finite_number(chart_analysis.get("latest_candle_at"))
    if latest_candle_at is None:
        return False, "closed_chart_timestamp_unavailable"
    chart_staleness_seconds = max(0.0, time.time() - (latest_candle_at + 60.0))
    if chart_staleness_seconds > CHART_MAX_STALENESS_SECONDS:
        return False, "closed_chart_stale"

    rejection_reasons = chart_analysis.get("rejection_reasons")
    if not isinstance(rejection_reasons, list):
        rejection_reasons = [chart_analysis.get("rejection_reason")] if chart_analysis.get("rejection_reason") else []
    # A live spot price can replace the delayed candle-close-above-EMA check.
    # Structural trend, momentum, wick, and extension gates still apply.
    blocking_reasons = [
        reason for reason in rejection_reasons
        if reason and reason != "chart_close_below_fast_ema"
    ]
    if blocking_reasons:
        return False, "closed_chart_gate:" + ",".join(str(reason) for reason in blocking_reasons)

    live_extension = current_price / slow_ema - 1.0
    if current_price <= slow_ema:
        return False, "live_price_below_slow_ema"
    if live_extension > CHART_MAX_EMA_EXTENSION_PCT:
        return False, "live_price_overextended_from_slow_ema"

    if current_price <= fast_ema * (1.0 + ENTRY_PULLBACK_BAND_PCT):
        pending["pullback_seen"] = True
    samples = pending.get("price_samples")
    previous_price = None
    if isinstance(samples, list) and len(samples) >= 2 and isinstance(samples[-2], dict):
        previous_price = _finite_number(samples[-2].get("price_usd"))

    if not pending.get("pullback_seen"):
        return False, "waiting_for_pullback_near_fast_ema"
    prior_near_ema = (
        previous_price is not None
        and previous_price <= fast_ema * (1.0 + ENTRY_PULLBACK_BAND_PCT)
    )
    reclaimed = current_price >= fast_ema * (1.0 + ENTRY_RECLAIM_CONFIRMATION_PCT)
    if not (prior_near_ema and reclaimed):
        return False, "waiting_for_fast_ema_reclaim"
    return True, "fast_ema_pullback_reclaimed"


def _record_trade_exit(wallet: dict[str, Any], trade: dict[str, Any], quote: dict[str, Any], reason: str) -> None:
    # The trade ID is the idempotency key. Commit history before wallet state:
    # if the process exits between the two atomic file writes, the next loop
    # reconciles cash from the recorded close without paying the proceeds twice.
    with STATE_LOCK:
        trade_id = trade.get("trade_id")
        history = load_json("trade_history.json", [])
        if not isinstance(history, list):
            history = []
        existing = next(
            (row for row in history if isinstance(row, dict) and trade_id and row.get("trade_id") == trade_id),
            None,
        )
        if existing is not None:
            recorded_balance = _finite_number(existing.get("wallet_balance"))
            if recorded_balance is not None:
                wallet["balance"] = recorded_balance
            wallet["active_trade"] = None
            save_wallet_and_clear_candidates(wallet)
            logger.warning("Reconciled already-closed paper trade=%s without duplicating proceeds", trade_id)
            return

        gross_output_usd = quote["out_amount_raw"] / (10**USDC_DECIMALS)
        estimated_output_usd = _paper_haircut(gross_output_usd)
        net_proceeds_usd = max(0.0, estimated_output_usd - PAPER_NETWORK_FEE_USD)
        entry_cost_usd = float(trade["invested_usd"]) + float(trade["entry_network_fee_usd"])
        profit_usd = net_proceeds_usd - entry_cost_usd
        opened_at = parse_timestamp(trade.get("opened_at"))
        hold_duration_seconds = (
            max(0.0, (datetime.now(timezone.utc) - opened_at).total_seconds())
            if opened_at is not None else None
        )

        ending_balance = float(wallet["balance"]) + net_proceeds_usd
        exit_row = {
            "closed_at": utc_timestamp(),
            "opened_at": trade.get("opened_at"),
            "hold_duration_seconds": hold_duration_seconds,
            "symbol": trade["symbol"],
            "address": trade["address"],
            "trade_id": trade_id,
            "account_id": trade.get("account_id"),
            "discovery_source": trade.get("discovery_source"),
            "scan_id": trade.get("scan_id"),
            "selection_score": trade.get("selection_score"),
            "llm_confidence": trade.get("llm_confidence"),
            "entry_roundtrip_quote_usd": trade.get("entry_roundtrip_quote_usd"),
            "entry_roundtrip_loss_pct": trade.get("entry_roundtrip_loss_pct"),
            "entry_chart_analysis": trade.get("entry_chart_analysis"),
            "entry_quote_out_raw": trade["token_amount_raw"],
            "entry_market_price_usd": trade.get("entry_market_price_usd"),
            "last_market_price_usd": trade.get("last_market_price_usd"),
            "last_market_price_observed_at": trade.get("last_market_price_observed_at"),
            "exit_quote_out_usdc_raw": quote["out_amount_raw"],
            "invested_usd": trade["invested_usd"],
            "entry_network_fee_usd": trade["entry_network_fee_usd"],
            "exit_network_fee_usd": PAPER_NETWORK_FEE_USD,
            "estimated_execution_slippage_bps_each_side": PAPER_EXECUTION_SLIPPAGE_BPS,
            "gross_exit_quote_usd": gross_output_usd,
            "net_proceeds_usd": net_proceeds_usd,
            "profit_usd": profit_usd,
            "profit_pct_on_total_cost": profit_usd / entry_cost_usd if entry_cost_usd else None,
            "exit_reason": reason,
            "wallet_balance": ending_balance,
            "exit_quote_router": quote.get("router"),
            "exit_quote_chart_sample_count": len(trade.get("quote_chart_history", [])),
            "exit_quote_chart": trade.get("last_quote_chart"),
        }
        history.append(exit_row)
        save_json("trade_history.json", history)

        wallet["balance"] = ending_balance
        wallet["active_trade"] = None
        save_wallet_and_clear_candidates(wallet)
        _record_pipeline_event(
            "paper_exit",
            trade_id=trade_id, account_id=trade.get("account_id"), scan_id=trade.get("scan_id"),
            symbol=trade.get("symbol"), address=trade.get("address"),
            reason=reason, estimated_pnl_usd=profit_usd,
            estimated_pnl_pct=profit_usd / entry_cost_usd if entry_cost_usd else None,
            exit_quote_usd=gross_output_usd, wallet_balance=wallet["balance"],
            exit_quote_router=quote.get("router"),
            entry_market_price_usd=trade.get("entry_market_price_usd"),
            entry_roundtrip_quote_usd=trade.get("entry_roundtrip_quote_usd"),
            entry_roundtrip_loss_pct=trade.get("entry_roundtrip_loss_pct"),
            entry_chart_analysis=trade.get("entry_chart_analysis"),
            exit_quote_chart_sample_count=len(trade.get("quote_chart_history", [])),
            exit_quote_chart=trade.get("last_quote_chart"),
            hold_duration_seconds=hold_duration_seconds,
            last_market_price_usd=trade.get("last_market_price_usd"),
            last_market_price_observed_at=trade.get("last_market_price_observed_at"),
        )
        logger.info(
            "Paper exit trade=%s %s (%s): %s, held_seconds=%s, market_price=%s, exit_quote=$%.4f, PnL $%.2f, balance $%.2f",
            trade_id, trade["symbol"], trade["address"], reason,
            round(hold_duration_seconds, 1) if hold_duration_seconds is not None else "unknown",
            f"${trade['last_market_price_usd']:.12g}" if trade.get("last_market_price_usd") is not None else "unavailable",
            gross_output_usd, profit_usd, wallet["balance"],
        )


def _manage_active_trade(wallet: dict[str, Any]) -> None:
    trade = wallet.get("active_trade")
    if not isinstance(trade, dict):
        raise ValueError("active_trade must be an object or null")
    if not all(key in trade for key in ("address", "token_amount_raw", "invested_usd", "entry_network_fee_usd")):
        logger.error("Active trade has legacy or incomplete fields; keeping it open for manual review")
        return

    try:
        quote = get_swap_quote(trade["address"], USDC_MINT, int(trade["token_amount_raw"]))
    except requests.RequestException as exc:
        last_logged = parse_timestamp(trade.get("last_quote_error_logged_at"))
        now = datetime.now(timezone.utc)
        if last_logged is None or (now - last_logged).total_seconds() >= PAPER_OBSERVATION_INTERVAL_SECONDS:
            logger.warning(
                "Exit quote request failed trade=%s scan=%s symbol=%s: %s",
                trade.get("trade_id"), trade.get("scan_id"), trade.get("symbol", "UNKNOWN"), exc,
            )
            _record_pipeline_event(
                "exit_quote_error", trade_id=trade.get("trade_id"), scan_id=trade.get("scan_id"),
                symbol=trade.get("symbol"), address=trade.get("address"),
                error_type=type(exc).__name__, error=str(exc),
            )
            trade["last_quote_error_logged_at"] = utc_timestamp()
            wallet["active_trade"] = trade
            save_json("paper_wallet.json", wallet)
        return
    if quote is None:
        last_logged = parse_timestamp(trade.get("last_quote_error_logged_at"))
        now = datetime.now(timezone.utc)
        if last_logged is None or (now - last_logged).total_seconds() >= PAPER_OBSERVATION_INTERVAL_SECONDS:
            logger.warning("No executable exit quote trade=%s for %s; keeping position open", trade.get("trade_id"), trade.get("symbol", "UNKNOWN"))
            _record_pipeline_event(
                "exit_quote_unavailable", trade_id=trade.get("trade_id"), scan_id=trade.get("scan_id"),
                symbol=trade.get("symbol"), address=trade.get("address"),
            )
            trade["last_quote_error_logged_at"] = utc_timestamp()
            wallet["active_trade"] = trade
            save_json("paper_wallet.json", wallet)
        return
    trade.pop("last_quote_error_logged_at", None)

    gross_output_usd = quote["out_amount_raw"] / (10**USDC_DECIMALS)
    estimated_output_usd = _paper_haircut(gross_output_usd)
    net_proceeds_usd = max(0.0, estimated_output_usd - PAPER_NETWORK_FEE_USD)
    total_cost = float(trade["invested_usd"]) + float(trade["entry_network_fee_usd"])
    pnl_pct = (net_proceeds_usd - total_cost) / total_cost if total_cost else -1.0

    previous_estimate = _finite_number(trade.get("last_estimated_exit_usd"))
    if previous_estimate is not None and previous_estimate > 0:
        quote_drop_pct = (previous_estimate - net_proceeds_usd) / previous_estimate
        if quote_drop_pct >= QUOTE_SHOCK_CONFIRMATION_PCT:
            # Quote-only paper execution can otherwise book a single anomalous
            # aggregator response as a catastrophic fill. Confirm sudden gaps
            # with a second quote request; real execution would still
            # remain subject to on-chain price movement and landing risk.
            last_shock_at = parse_timestamp(trade.get("last_quote_shock_at"))
            if (
                last_shock_at is not None
                and (datetime.now(timezone.utc) - last_shock_at).total_seconds()
                < PAPER_OBSERVATION_INTERVAL_SECONDS
            ):
                # Avoid repeatedly doubling quote traffic while the provider is
                # returning unstable prices; recheck on the next observation window.
                wallet["active_trade"] = trade
                save_json("paper_wallet.json", wallet)
                return

            try:
                confirmation_quote = get_swap_quote(
                    trade["address"], USDC_MINT, int(trade["token_amount_raw"])
                )
            except requests.RequestException as exc:
                confirmation_quote = None
                confirmation_error = f"{type(exc).__name__}: {exc}"
            else:
                confirmation_error = None

            if confirmation_quote is None:
                logger.warning(
                    "Large exit quote shock trade=%s %s: %.1f%% drop; confirmation unavailable, keeping paper position open",
                    trade.get("trade_id"), trade.get("symbol"), quote_drop_pct * 100.0,
                )
                _record_pipeline_event(
                    "exit_quote_shock_unconfirmed", trade_id=trade.get("trade_id"),
                    scan_id=trade.get("scan_id"), symbol=trade.get("symbol"),
                    previous_net_estimate_usd=previous_estimate,
                    first_net_estimate_usd=net_proceeds_usd,
                    quote_drop_pct=quote_drop_pct, confirmation_error=confirmation_error,
                )
                trade["last_quote_shock_at"] = utc_timestamp()
                wallet["active_trade"] = trade
                save_json("paper_wallet.json", wallet)
                return

            confirmation_gross_usd = confirmation_quote["out_amount_raw"] / (10**USDC_DECIMALS)
            confirmation_net_usd = max(0.0, _paper_haircut(confirmation_gross_usd) - PAPER_NETWORK_FEE_USD)
            quote_disagreement_pct = abs(confirmation_net_usd - net_proceeds_usd) / max(
                confirmation_net_usd, net_proceeds_usd, 1e-9
            )
            _record_pipeline_event(
                "exit_quote_shock_confirmation", trade_id=trade.get("trade_id"),
                scan_id=trade.get("scan_id"), symbol=trade.get("symbol"),
                previous_net_estimate_usd=previous_estimate,
                first_net_estimate_usd=net_proceeds_usd,
                confirmation_net_estimate_usd=confirmation_net_usd,
                quote_drop_pct=quote_drop_pct,
                quote_disagreement_pct=quote_disagreement_pct,
                confirmed=quote_disagreement_pct <= QUOTE_CONFIRMATION_TOLERANCE_PCT,
            )
            if quote_disagreement_pct > QUOTE_CONFIRMATION_TOLERANCE_PCT:
                logger.warning(
                    "Large exit quote shock trade=%s %s: first $%.4f vs confirmation $%.4f (%.1f%% disagreement); retaining position and retrying next poll",
                    trade.get("trade_id"), trade.get("symbol"), net_proceeds_usd,
                    confirmation_net_usd, quote_disagreement_pct * 100.0,
                )
                trade["last_quote_shock_at"] = utc_timestamp()
                wallet["active_trade"] = trade
                save_json("paper_wallet.json", wallet)
                return
            trade.pop("last_quote_shock_at", None)
            # Use the conservative of two closely agreeing quotes for PnL.
            if confirmation_net_usd < net_proceeds_usd:
                quote = confirmation_quote
                gross_output_usd = confirmation_gross_usd
                net_proceeds_usd = confirmation_net_usd
                pnl_pct = (net_proceeds_usd - total_cost) / total_cost if total_cost else -1.0
        else:
            trade.pop("last_quote_shock_at", None)
    else:
        trade.pop("last_quote_shock_at", None)

    peak_exit_value = _finite_number(trade.get("peak_estimated_exit_usd")) or 0.0
    if net_proceeds_usd > peak_exit_value:
        trade["peak_estimated_exit_usd"] = net_proceeds_usd
        peak_exit_value = net_proceeds_usd
    quote_chart = _update_quote_chart(trade, net_proceeds_usd, quote["quoted_at"])
    trade["last_quote_chart"] = quote_chart
    trade["last_quote_at"] = quote["quoted_at"]
    trade["last_estimated_exit_usd"] = net_proceeds_usd
    trade["last_estimated_pnl_pct"] = pnl_pct

    last_observation = parse_timestamp(trade.get("last_observation_logged_at"))
    observation_time = datetime.now(timezone.utc)
    if (
        last_observation is None
        or (observation_time - last_observation).total_seconds() >= PAPER_OBSERVATION_INTERVAL_SECONDS
    ):
        try:
            market_stats = _get_market_price(trade["address"])
            if market_stats.get("market_price_usd") is not None:
                trade["last_market_price_usd"] = market_stats["market_price_usd"]
                trade["last_market_price_observed_at"] = market_stats["market_price_observed_at"]
            entry_market_price = _finite_number(trade.get("entry_market_price_usd"))
            current_market_price = _finite_number(market_stats.get("market_price_usd"))
            market_return_pct = (
                current_market_price / entry_market_price - 1.0
                if entry_market_price and current_market_price is not None
                else None
            )
            pnl_return_divergence_pct_points = (
                (market_return_pct - pnl_pct) * 100.0 if market_return_pct is not None else None
            )
            if (
                pnl_return_divergence_pct_points is not None
                and abs(pnl_return_divergence_pct_points) >= MARKET_QUOTE_DIVERGENCE_ALERT_PCT * 100.0
            ):
                logger.warning(
                    "Displayed market and executable quote diverge trade=%s %s: spot_return=%+.1f%% quote_pnl=%+.1f%% divergence=%+.1fpp; exit decisions use the sell quote",
                    trade.get("trade_id"), trade.get("symbol"), market_return_pct * 100.0,
                    pnl_pct * 100.0, pnl_return_divergence_pct_points,
                )
            append_jsonl(
                "paper_observations.jsonl",
                {
                    "observed_at": utc_timestamp(),
                    "symbol": trade.get("symbol"),
                    "address": trade.get("address"),
                    "status": "open",
                    "gross_exit_quote_usd": gross_output_usd,
                    "estimated_net_proceeds_usd": net_proceeds_usd,
                    "estimated_pnl_usd": net_proceeds_usd - total_cost,
                    "estimated_pnl_pct": pnl_pct,
                    "peak_estimated_exit_usd": peak_exit_value,
                    "trailing_active": bool(trade.get("trailing_active")),
                    "quote_router": quote.get("router"),
                    "trade_id": trade.get("trade_id"),
                    "account_id": trade.get("account_id"),
                    "scan_id": trade.get("scan_id"),
                    "stop_loss_pct": STOP_LOSS_PCT,
                    "trailing_activation_pct": TRAILING_STOP_ACTIVATION,
                    "trailing_stop_pct": TRAILING_STOP_PCT,
                    "max_hold_hours": MAX_HOLD_HOURS,
                    "market_return_pct": market_return_pct,
                    "quote_vs_market_divergence_pct_points": pnl_return_divergence_pct_points,
                    "quote_chart": quote_chart,
                    **market_stats,
                },
            )
            trade["last_observation_logged_at"] = utc_timestamp()
            market_price_display = (
                f"${market_stats['market_price_usd']:.12g}"
                if market_stats.get("market_price_usd") is not None
                else f"unavailable ({market_stats.get('market_price_error', 'unknown error')})"
            )
            logger.info(
                "Position watch trade=%s scan=%s %s (%s): market_price=%s %s exit_quote=$%.4f net_estimate=$%.4f pnl=$%.4f (%+.2f%%) peak=$%.4f trailing=%s quote_ema=%s/%s bearish=%d/%d",
                trade.get("trade_id"), trade.get("scan_id"), trade.get("symbol"), trade.get("address"),
                market_price_display, market_stats.get("market_price_source", ""),
                gross_output_usd, net_proceeds_usd, net_proceeds_usd - total_cost,
                pnl_pct * 100.0, peak_exit_value, bool(trade.get("trailing_active")),
                f"{quote_chart['fast_ema_net_exit_usd']:.4f}" if quote_chart["fast_ema_net_exit_usd"] is not None else "n/a",
                f"{quote_chart['slow_ema_net_exit_usd']:.4f}" if quote_chart["slow_ema_net_exit_usd"] is not None else "n/a",
                quote_chart["bearish_confirmations"], QUOTE_CHART_BEARISH_CONFIRMATIONS,
            )
            _record_pipeline_event(
                "position_observation", trade_id=trade.get("trade_id"), account_id=trade.get("account_id"), scan_id=trade.get("scan_id"),
                symbol=trade.get("symbol"), address=trade.get("address"),
                gross_exit_quote_usd=gross_output_usd, estimated_net_proceeds_usd=net_proceeds_usd,
                estimated_pnl_usd=net_proceeds_usd - total_cost, estimated_pnl_pct=pnl_pct,
                peak_estimated_exit_usd=peak_exit_value, trailing_active=bool(trade.get("trailing_active")),
                stop_loss_pct=STOP_LOSS_PCT, trailing_activation_pct=TRAILING_STOP_ACTIVATION,
                trailing_stop_pct=TRAILING_STOP_PCT, max_hold_hours=MAX_HOLD_HOURS,
                market_return_pct=market_return_pct,
                quote_vs_market_divergence_pct_points=pnl_return_divergence_pct_points,
                quote_chart=quote_chart,
                quote_router=quote.get("router"),
                **market_stats,
            )
        except (OSError, ValueError, TypeError):
            logger.exception("Could not persist paper PnL observation")

    if pnl_pct >= TRAILING_STOP_ACTIVATION:
        trade["trailing_active"] = True

    exit_reason = None
    if pnl_pct <= STOP_LOSS_PCT:
        exit_reason = f"Net stop loss reached ({pnl_pct:.1%})"
    elif trade.get("trailing_active") and peak_exit_value > 0:
        drawdown_from_peak = (peak_exit_value - net_proceeds_usd) / peak_exit_value
        trailing_level_usd = max(
            peak_exit_value * (1.0 - TRAILING_STOP_PCT),
            total_cost * (1.0 + TRAILING_STOP_FLOOR_PCT),
        )
        if net_proceeds_usd <= trailing_level_usd:
            exit_reason = (
                f"Net trailing stop reached ({drawdown_from_peak:.1%} from peak; "
                f"floor ${total_cost * (1.0 + TRAILING_STOP_FLOOR_PCT):.4f})"
            )
    if exit_reason is None and quote_chart["bearish_exit"]:
        exit_reason = (
            "Executable-quote trend rollover: EMA"
            f"{QUOTE_CHART_FAST_EMA_PERIOD} below EMA{QUOTE_CHART_SLOW_EMA_PERIOD} for "
            f"{quote_chart['bearish_confirmations']} falling quotes"
        )
    opened_at = parse_timestamp(trade.get("opened_at"))
    if exit_reason is None and opened_at is not None:
        holding_hours = (datetime.now(timezone.utc) - opened_at).total_seconds() / 3600.0
        if holding_hours >= MAX_HOLD_HOURS:
            exit_reason = f"Maximum holding time reached ({holding_hours:.1f}h)"

    if exit_reason:
        _record_trade_exit(wallet, trade, quote, exit_reason)
    else:
        wallet["active_trade"] = trade
        save_json("paper_wallet.json", wallet)


def _try_open_trade(wallet: dict[str, Any]) -> None:
    global _warned_missing_jupiter_key, _daily_loss_pause_logged_for
    if not JUPITER_API_KEY:
        if not _warned_missing_jupiter_key:
            logger.warning("Paper entries are disabled until JUPITER_API_KEY is configured")
            _warned_missing_jupiter_key = True
        return

    day, daily_pnl, daily_loss_limit, day_start_balance = daily_realized_risk(wallet)
    if daily_pnl <= -daily_loss_limit:
        if load_pending_entry() is not None:
            save_pending_entry(None)
        if _daily_loss_pause_logged_for != day:
            logger.warning(
                "Paper entries paused for UTC day %s: realized PnL $%.2f reached the daily loss limit of $%.2f (opening balance $%.2f)",
                day, daily_pnl, daily_loss_limit, day_start_balance,
            )
            _record_pipeline_event(
                "daily_loss_limit_reached", account_id=wallet.get("account_id"),
                day_utc=day, realized_pnl_usd=daily_pnl,
                loss_limit_usd=daily_loss_limit, opening_balance_usd=day_start_balance,
            )
            _daily_loss_pause_logged_for = day
        return

    trade_size_usd = _sized_trade_amount(float(wallet.get("balance", 0.0)))
    required_cash = trade_size_usd + PAPER_NETWORK_FEE_USD
    if trade_size_usd <= 0 or wallet["balance"] < required_cash:
        _record_pipeline_event(
            "entry_rejected_insufficient_balance",
            account_id=wallet.get("account_id"), balance=wallet["balance"],
            required_cash=required_cash, sized_trade_usd=trade_size_usd,
        )
        return

    pending = load_pending_entry()
    now = datetime.now(timezone.utc)
    if pending and (
        not isinstance(pending.get("candidate"), dict)
        or not isinstance(pending.get("decision"), dict)
    ):
        logger.warning("Discarding malformed pending entry watch")
        save_pending_entry(None)
        pending = None

    # A pending watch is exclusive: do not consume newer scans or replace the
    # token while actively timing its entry.
    if pending is None:
        snapshot = take_candidate_snapshot()
        selected, status, details = _choose_candidate(snapshot)
        if selected is None:
            if details.get("scan_id") or details.get("candidate_count"):
                logger.info(
                    "Trader consumed scan=%s candidates=%s decisions=%s status=%s details=%s",
                    details.get("scan_id"), details.get("candidate_count", 0),
                    details.get("decision_count", 0), status, json.dumps(details, separators=(",", ":")),
                )
                _record_pipeline_event("trader_signal_rejected", status=status, **details)
            return
        candidate, decision = selected
        scan_id = snapshot.get("scan_id")
        pending = {
            "watch_started_at": utc_timestamp(),
            "expires_at": (now + timedelta(seconds=ENTRY_WATCH_MAX_SECONDS))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "last_chart_check_at": None,
            "chart_analysis": None,
            "last_market_price_usd": None,
            "price_samples": [],
            "pullback_seen": False,
            "scan_id": scan_id,
            "candidate": candidate,
            "decision": decision,
        }
        save_pending_entry(pending)
        mark_token_attempt(
            candidate.get("tokenAddress", ""),
            candidate.get("symbol", "UNKNOWN"),
            "ollama_selected_entry_watch",
        )
        _record_pipeline_event(
            "entry_watch_started", scan_id=scan_id, symbol=candidate.get("symbol"),
            address=candidate.get("tokenAddress"), expires_at=pending["expires_at"],
            spot_poll_interval_seconds=ENTRY_WATCH_POLL_INTERVAL_SECONDS,
            chart_refresh_interval_seconds=ENTRY_WATCH_RECHECK_SECONDS,
        )
        logger.info(
            "Trader accepted Ollama signal scan=%s %s (%s) score=%.2f confidence=%d age_seconds=%s reason=%s; starting exclusive %.1fs spot watch",
            scan_id, candidate.get("symbol"), candidate.get("tokenAddress"),
            candidate.get("selection_score", 0), decision["confidence"],
            details.get("signal_age_seconds"), decision.get("reasoning", ""),
            ENTRY_WATCH_POLL_INTERVAL_SECONDS,
        )
        logger.info(
            "Trader consumed scan=%s candidates=%s decisions=%s status=%s details=%s",
            details.get("scan_id"), details.get("candidate_count", 0),
            details.get("decision_count", 0), status, json.dumps(details, separators=(",", ":")),
        )
        _record_pipeline_event("trader_signal_consumed", status=status, **details)

    pending_expiry = parse_timestamp(pending.get("expires_at")) if pending else None
    if pending and (pending_expiry is None or pending_expiry <= now):
        logger.info(
            "Entry watch expired scan=%s symbol=%s after %ss without a confirmed chart setup",
            pending.get("scan_id"),
            (pending.get("candidate") or {}).get("symbol"),
            ENTRY_WATCH_MAX_SECONDS,
        )
        _record_pipeline_event(
            "entry_watch_expired", scan_id=pending.get("scan_id"),
            symbol=(pending.get("candidate") or {}).get("symbol"),
            address=(pending.get("candidate") or {}).get("tokenAddress"),
            watch_started_at=pending.get("watch_started_at"),
            expires_at=pending.get("expires_at"),
        )
        save_pending_entry(None)
        pending = None

    if pending is None:
        return

    candidate = pending.get("candidate")
    decision = pending.get("decision")
    scan_id = pending.get("scan_id")
    if not isinstance(candidate, dict) or not isinstance(decision, dict):
        logger.warning("Discarding malformed pending entry watch scan=%s", scan_id)
        save_pending_entry(None)
        return

    watch_age = max(
        0.0,
        (now - (parse_timestamp(pending.get("watch_started_at")) or now)).total_seconds(),
    )

    pair_address = (candidate.get("pair_data") or {}).get("pairAddress")
    last_chart_check = parse_timestamp(pending.get("last_chart_check_at"))
    should_refresh_chart = (
        not isinstance(pending.get("chart_analysis"), dict)
        or last_chart_check is None
        or (now - last_chart_check).total_seconds() >= ENTRY_WATCH_RECHECK_SECONDS
    )
    if should_refresh_chart:
        pending["last_chart_check_at"] = utc_timestamp()
        try:
            chart_analysis = get_entry_chart_analysis(str(pair_address or ""))
        except (requests.RequestException, ValueError, TypeError) as exc:
            logger.warning(
                "Entry watch chart refresh failed scan=%s symbol=%s; retrying within %.0fs: %s",
                scan_id, candidate["symbol"], max(0.0, ENTRY_WATCH_MAX_SECONDS - watch_age), exc,
            )
            _record_pipeline_event(
                "entry_watch_chart_error", scan_id=scan_id, symbol=candidate["symbol"],
                address=candidate["tokenAddress"], pair_address=pair_address,
                error_type=type(exc).__name__, error=str(exc),
                watch_age_seconds=round(watch_age, 1), expires_at=pending.get("expires_at"),
            )
            chart_analysis = pending.get("chart_analysis")
        else:
            pending["chart_analysis"] = chart_analysis
            _record_pipeline_event(
                "entry_chart_analysis", scan_id=scan_id, symbol=candidate["symbol"],
                address=candidate["tokenAddress"], pair_address=pair_address,
                analysis=chart_analysis,
            )
    else:
        chart_analysis = pending.get("chart_analysis")

    if not isinstance(chart_analysis, dict):
        save_pending_entry(pending)
        return

    entry_market_stats = _get_entry_watch_price(
        candidate["tokenAddress"], str(pair_address) if pair_address else None,
    )
    current_price = _finite_number(entry_market_stats.get("market_price_usd"))
    sample = {
        "at": entry_market_stats.get("market_price_observed_at", utc_timestamp()),
        "price_usd": current_price,
    }
    samples = pending.get("price_samples")
    if not isinstance(samples, list):
        samples = []
    if current_price is not None:
        samples.append(sample)
    pending["price_samples"] = samples[-120:]
    if current_price is not None:
        pending["last_market_price_usd"] = current_price
        pending["last_market_price_observed_at"] = sample["at"]

    entry_ready, entry_wait_reason = _entry_pullback_reclaim_status(
        pending, chart_analysis, current_price,
    )
    fast_ema = _finite_number(chart_analysis.get("fast_ema_5"))
    slow_ema = _finite_number(chart_analysis.get("slow_ema_13"))
    latest_candle_at = _finite_number(chart_analysis.get("latest_candle_at"))
    chart_staleness_seconds = (
        max(0.0, time.time() - (latest_candle_at + 60.0))
        if latest_candle_at is not None else None
    )
    _record_pipeline_event(
        "entry_watch_price", scan_id=scan_id, symbol=candidate["symbol"],
        address=candidate["tokenAddress"], pair_address=pair_address,
        market_price_usd=current_price,
        market_price_source=entry_market_stats.get("market_price_source"),
        market_price_block_id=entry_market_stats.get("market_price_block_id"),
        market_price_observed_at=sample["at"],
        market_price_error=entry_market_stats.get("market_price_error"),
        fast_ema_5=fast_ema, slow_ema_13=slow_ema,
        pullback_seen=bool(pending.get("pullback_seen")),
        entry_ready=entry_ready, wait_reason=entry_wait_reason,
        watch_age_seconds=round(watch_age, 1),
        chart_staleness_seconds=round(chart_staleness_seconds, 1) if chart_staleness_seconds is not None else None,
    )
    now_text = utc_timestamp()
    last_heartbeat = parse_timestamp(pending.get("last_heartbeat_at"))
    if last_heartbeat is None or (now - last_heartbeat).total_seconds() >= 15:
        pending["last_heartbeat_at"] = now_text
        logger.info(
            "Entry watch scan=%s %s price=%s source=%s block=%s EMA5=%s EMA13=%s pullback_seen=%s ready=%s reason=%s age=%.0fs expires_in=%.0fs",
            scan_id, candidate["symbol"],
            f"${current_price:.12g}" if current_price is not None else "unavailable",
            entry_market_stats.get("market_price_source", "unknown"),
            entry_market_stats.get("market_price_block_id", "n/a"),
            f"${fast_ema:.12g}" if fast_ema is not None else "n/a",
            f"${slow_ema:.12g}" if slow_ema is not None else "n/a",
            bool(pending.get("pullback_seen")), entry_ready, entry_wait_reason,
            watch_age, max(0.0, ENTRY_WATCH_MAX_SECONDS - watch_age),
        )
    save_pending_entry(pending)
    if not entry_ready:
        return

    logger.info(
        "Entry pullback/reclaim confirmed scan=%s %s price=$%.12g EMA5=$%.12g EMA13=$%.12g candles=%s 3m=%s extension=%s",
        scan_id, candidate["symbol"], current_price, fast_ema, slow_ema,
        chart_analysis.get("candle_count"), chart_analysis.get("three_candle_return_pct"),
        chart_analysis.get("slow_ema_extension_pct"),
    )

    amount_raw = int(round(trade_size_usd * (10**USDC_DECIMALS)))
    logger.info("Requesting paper entry quote scan=%s symbol=%s address=%s amount_usd=%.2f risk_budget=%.2f", scan_id, candidate["symbol"], candidate["tokenAddress"], trade_size_usd, float(wallet["balance"]) * RISK_PER_TRADE_PCT)
    _record_pipeline_event(
        "entry_quote_requested", scan_id=scan_id, symbol=candidate["symbol"],
        address=candidate["tokenAddress"], input_mint=USDC_MINT,
        input_amount_raw=amount_raw, amount_usd=trade_size_usd,
        account_id=wallet.get("account_id"),
    )
    try:
        quote = get_swap_quote(USDC_MINT, candidate["tokenAddress"], amount_raw)
    except requests.RequestException as exc:
        _record_pipeline_event(
            "entry_quote_error", scan_id=scan_id, symbol=candidate["symbol"],
            address=candidate["tokenAddress"], error_type=type(exc).__name__, error=str(exc),
        )
        logger.warning("Entry quote request failed scan=%s symbol=%s address=%s: %s", scan_id, candidate["symbol"], candidate["tokenAddress"], exc)
        return
    if quote is None:
        logger.info("No Jupiter entry route scan=%s for %s (%s); paper entry skipped", scan_id, candidate["symbol"], candidate["tokenAddress"])
        _record_pipeline_event("entry_quote_unavailable", scan_id=scan_id, symbol=candidate["symbol"], address=candidate["tokenAddress"])
        return

    entry_market_stats = _get_market_price(candidate["tokenAddress"])
    if entry_market_stats.get("market_price_error"):
        logger.info(
            "Entry spot price unavailable scan=%s symbol=%s: %s",
            scan_id, candidate["symbol"], entry_market_stats["market_price_error"],
        )

    token_amount_raw = quote["out_amount_raw"]
    estimated_tokens = int(token_amount_raw * (1.0 - PAPER_EXECUTION_SLIPPAGE_BPS / 10_000.0))
    if estimated_tokens <= 0:
        _record_pipeline_event("entry_rejected_zero_estimated_tokens", scan_id=scan_id, symbol=candidate["symbol"], address=candidate["tokenAddress"], quoted_token_amount_raw=token_amount_raw)
        return

    # Confirm that the token amount just quoted for purchase has a viable sell
    # route at approximately the intended trade size before simulating entry.
    try:
        roundtrip_quote = get_swap_quote(candidate["tokenAddress"], USDC_MINT, estimated_tokens)
    except requests.RequestException as exc:
        _record_pipeline_event(
            "entry_roundtrip_quote_error", scan_id=scan_id,
            symbol=candidate["symbol"], address=candidate["tokenAddress"],
            error_type=type(exc).__name__, error=str(exc),
        )
        logger.warning("Entry skipped scan=%s symbol=%s because sell preflight failed: %s", scan_id, candidate["symbol"], exc)
        return
    if roundtrip_quote is None:
        _record_pipeline_event(
            "entry_roundtrip_quote_unavailable", scan_id=scan_id,
            symbol=candidate["symbol"], address=candidate["tokenAddress"],
        )
        logger.info("Entry skipped scan=%s symbol=%s because no immediate sell route is available", scan_id, candidate["symbol"])
        return
    roundtrip_gross_usd = roundtrip_quote["out_amount_raw"] / (10**USDC_DECIMALS)
    roundtrip_net_usd = max(0.0, _paper_haircut(roundtrip_gross_usd) - PAPER_NETWORK_FEE_USD)
    roundtrip_cost_usd = trade_size_usd + PAPER_NETWORK_FEE_USD
    roundtrip_loss_pct = max(0.0, (roundtrip_cost_usd - roundtrip_net_usd) / roundtrip_cost_usd)
    _record_pipeline_event(
        "entry_roundtrip_preflight", scan_id=scan_id, symbol=candidate["symbol"],
        address=candidate["tokenAddress"], buy_router=quote.get("router"),
        sell_router=roundtrip_quote.get("router"), sell_quote_usd=roundtrip_gross_usd,
        estimated_net_proceeds_usd=roundtrip_net_usd,
        estimated_roundtrip_loss_pct=roundtrip_loss_pct,
        max_allowed_loss_pct=MAX_ENTRY_ROUNDTRIP_LOSS_PCT,
    )
    if roundtrip_loss_pct > MAX_ENTRY_ROUNDTRIP_LOSS_PCT:
        logger.info(
            "Entry skipped scan=%s symbol=%s: quoted round-trip loss %.1f%% exceeds %.1f%% limit",
            scan_id, candidate["symbol"], roundtrip_loss_pct * 100.0,
            MAX_ENTRY_ROUNDTRIP_LOSS_PCT * 100.0,
        )
        _record_pipeline_event(
            "entry_rejected_roundtrip_loss", scan_id=scan_id,
            symbol=candidate["symbol"], address=candidate["tokenAddress"],
            estimated_roundtrip_loss_pct=roundtrip_loss_pct,
            max_allowed_loss_pct=MAX_ENTRY_ROUNDTRIP_LOSS_PCT,
        )
        return

    trade = {
        "trade_id": uuid.uuid4().hex[:12],
        "scan_id": scan_id,
        "account_id": wallet.get("account_id"),
        "opened_at": utc_timestamp(),
        "symbol": candidate["symbol"],
        "address": candidate["tokenAddress"],
        "discovery_source": candidate.get("discovery_source"),
        "token_amount_raw": estimated_tokens,
        "invested_usd": trade_size_usd,
        "entry_network_fee_usd": PAPER_NETWORK_FEE_USD,
        "selection_score": candidate["selection_score"],
        "llm_confidence": decision["confidence"],
        "llm_reasoning": decision.get("reasoning", ""),
        "entry_quote_out_raw": token_amount_raw,
        "entry_quote_fee_bps": quote.get("fee_bps"),
        "entry_quote_router": quote.get("router"),
        "entry_quote_at": quote["quoted_at"],
        "entry_roundtrip_quote_usd": roundtrip_gross_usd,
        "entry_roundtrip_loss_pct": roundtrip_loss_pct,
        "entry_chart_analysis": chart_analysis,
        "entry_market_price_usd": entry_market_stats.get("market_price_usd"),
        "last_market_price_usd": entry_market_stats.get("market_price_usd"),
        "last_market_price_observed_at": entry_market_stats.get("market_price_observed_at"),
        "estimated_execution_slippage_bps": PAPER_EXECUTION_SLIPPAGE_BPS,
        "peak_estimated_exit_usd": trade_size_usd,
        "trailing_active": False,
        "quote_chart_history": [{
            "at": roundtrip_quote["quoted_at"],
            "net_exit_usd": roundtrip_net_usd,
        }],
        "quote_chart_bearish_count": 0,
    }
    wallet["balance"] -= required_cash
    wallet["active_trade"] = trade
    save_wallet_and_clear_candidates(wallet)
    try:
        append_jsonl(
            "paper_observations.jsonl",
            {
                "observed_at": trade["opened_at"],
                "symbol": trade["symbol"],
                "address": trade["address"],
                "status": "entry",
                "trade_id": trade["trade_id"],
                "account_id": trade.get("account_id"),
                "scan_id": trade.get("scan_id"),
                "invested_usd": trade_size_usd,
                "selection_score": candidate["selection_score"],
                "llm_confidence": decision["confidence"],
                "entry_quote_router": quote.get("router"),
                "entry_chart_analysis": chart_analysis,
                **entry_market_stats,
            },
        )
    except OSError:
        logger.exception("Could not persist paper entry observation")
    logger.info(
        "Paper entry trade=%s scan=%s %s (%s), score %.2f confidence=%d invested=$%.2f roundtrip_loss=%.1f%% market_price=%s quote_tokens_raw=%d router=%s balance $%.2f",
        trade["trade_id"], trade.get("scan_id"),
        trade["symbol"], trade["address"], trade["selection_score"],
        trade["llm_confidence"],
        trade_size_usd,
        trade["entry_roundtrip_loss_pct"] * 100.0,
        f"${trade['entry_market_price_usd']:.12g}" if trade.get("entry_market_price_usd") is not None else "unavailable",
        trade["entry_quote_out_raw"], trade["entry_quote_router"], wallet["balance"],
    )
    _record_pipeline_event(
        "paper_entry", trade_id=trade["trade_id"], account_id=trade.get("account_id"), scan_id=scan_id,
        discovery_source=trade.get("discovery_source"),
        symbol=trade["symbol"], address=trade["address"],
        selection_score=trade["selection_score"], llm_confidence=trade["llm_confidence"],
        llm_reasoning=trade["llm_reasoning"], invested_usd=trade["invested_usd"],
        risk_per_trade_pct=RISK_PER_TRADE_PCT,
        max_position_pct_of_balance=MAX_POSITION_PCT_OF_BALANCE,
        quoted_token_amount_raw=trade["entry_quote_out_raw"], estimated_token_amount_raw=trade["token_amount_raw"],
        quote_router=trade.get("entry_quote_router"), quote_fee_bps=trade.get("entry_quote_fee_bps"),
        entry_market_price_usd=trade.get("entry_market_price_usd"),
        market_price_source=entry_market_stats.get("market_price_source"),
        market_price_observed_at=entry_market_stats.get("market_price_observed_at"),
        market_price_error=entry_market_stats.get("market_price_error"),
        entry_chart_analysis=chart_analysis,
        wallet_balance=wallet["balance"],
    )


def run_trader_loop() -> None:
    global _warned_insufficient_paper_balance
    logger.info(
        "Paper trader started; idle=%ss, entry-watch spot=%ss, active-position quote=%ss (Jupiter request floor=%ss); entry charts require %d closed 1m candles; executable quote rollover uses EMA(%d)/EMA(%d) with %d confirmations",
        PAPER_POLL_INTERVAL_SECONDS, ENTRY_WATCH_POLL_INTERVAL_SECONDS,
        ACTIVE_TRADE_POLL_INTERVAL_SECONDS, JUPITER_MIN_REQUEST_INTERVAL_SECONDS,
        CHART_MIN_CANDLES,
        QUOTE_CHART_FAST_EMA_PERIOD, QUOTE_CHART_SLOW_EMA_PERIOD,
        QUOTE_CHART_BEARISH_CONFIRMATIONS,
    )
    while True:
        loop_started = time.monotonic()
        next_poll_interval = PAPER_POLL_INTERVAL_SECONDS
        try:
            wallet = _load_wallet()
            if wallet.get("active_trade") is None:
                sized_amount = _sized_trade_amount(wallet["balance"])
                required_cash = sized_amount + PAPER_NETWORK_FEE_USD
                if wallet["balance"] < required_cash:
                    if not _warned_insufficient_paper_balance:
                        logger.warning(
                            "Paper entries are paused: balance $%.2f is below risk-sized trade requirement $%.2f (position $%.2f + fee $%.2f).",
                            wallet["balance"], required_cash, sized_amount, PAPER_NETWORK_FEE_USD,
                        )
                        _warned_insufficient_paper_balance = True
                else:
                    _warned_insufficient_paper_balance = False
                _try_open_trade(wallet)
                if wallet.get("active_trade") is not None:
                    next_poll_interval = ACTIVE_TRADE_POLL_INTERVAL_SECONDS
                elif load_pending_entry() is not None:
                    next_poll_interval = ENTRY_WATCH_POLL_INTERVAL_SECONDS
            else:
                _manage_active_trade(wallet)
                next_poll_interval = ACTIVE_TRADE_POLL_INTERVAL_SECONDS
        except requests.RequestException as exc:
            logger.warning("Paper quote request failed; retaining wallet state: %s", exc)
        except (json.JSONDecodeError, OSError, ValueError, TypeError, KeyError) as exc:
            logger.exception("Paper trader state error; no trade action taken: %s", exc)
        except Exception:
            logger.exception("Unexpected paper trader failure; no trade action taken")
        elapsed = time.monotonic() - loop_started
        time.sleep(max(0.0, next_poll_interval - elapsed))
