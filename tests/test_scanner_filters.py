from datetime import datetime, timedelta, timezone

import pytest

from tradeslave.scanner import (
    _pair_filter_rejection_reason,
    _token_age_hours,
    calculate_natural_selection_score,
    parse_timestamp,
    rugcheck_filter_rejection_reason,
    rugcheck_summary_rejection_reason,
    select_by_score,
    token_extension_rejection_reason,
)

TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


def pair(liquidity=20_000, buys=30, sells=20, price="0.001", m5=5.0, h1=10.0, volume=2_000):
    return {
        "liquidity": {"usd": liquidity},
        "txns": {"m5": {"buys": buys, "sells": sells}},
        "priceUsd": price,
        "priceChange": {"m5": m5, "h1": h1},
        "volume": {"m5": volume},
    }


def report(score=10, lp=80, risks=None):
    return {"score_normalised": score, "lpLockedPct": lp, "risks": risks if risks is not None else []}


# --- timestamps -----------------------------------------------------------

def test_parse_timestamp_handles_nanoseconds_and_z():
    parsed = parse_timestamp("2026-10-12T17:57:31.123456789Z")
    assert parsed == datetime(2026, 10, 12, 17, 57, 31, 123456, tzinfo=timezone.utc)


def test_parse_timestamp_rejects_garbage():
    assert parse_timestamp("not a date") is None
    assert parse_timestamp("") is None
    assert parse_timestamp(None) is None


def test_naive_timestamp_is_treated_as_utc():
    assert parse_timestamp("2026-10-12T00:00:00").tzinfo == timezone.utc


# --- pool age -------------------------------------------------------------

def test_age_prefers_pair_creation_time():
    now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    created = now - timedelta(minutes=90)
    token = {"pair_data": {"pairCreatedAt": created.timestamp() * 1000}, "createdAt": "2020-01-01T00:00:00Z"}
    assert _token_age_hours(token, now) == pytest.approx(1.5)


def test_age_falls_back_to_pool_created_at():
    now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    token = {"poolCreatedAt": "2026-10-12T11:30:00Z"}
    assert _token_age_hours(token, now) == pytest.approx(0.5)


def test_future_creation_time_is_invalid():
    now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    assert _token_age_hours({"poolCreatedAt": "2026-10-12T13:00:00Z"}, now) is None


# --- cheap market gates ---------------------------------------------------

def test_healthy_pair_passes():
    assert _pair_filter_rejection_reason(pair()) is None


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"liquidity": None}, "liquidity_missing"),
        ({"liquidity": 4_999}, "liquidity_below_minimum"),
        ({"buys": 5, "sells": 4}, "five_minute_transaction_count_below_minimum"),
        ({"price": "0"}, "price_missing_or_nonpositive"),
        ({"m5": -15.1}, "five_minute_selloff_too_sharp"),
        ({"m5": 40.1}, "five_minute_pump_overextended"),
    ],
)
def test_pair_gates(overrides, reason):
    assert _pair_filter_rejection_reason(pair(**overrides)) == reason


def test_pair_gate_bounds_are_inclusive():
    assert _pair_filter_rejection_reason(pair(liquidity=5_000, buys=5, sells=5, m5=-15.0)) is None
    assert _pair_filter_rejection_reason(pair(m5=40.0)) is None


# --- RugCheck ---------------------------------------------------------------

def test_clean_report_passes():
    assert rugcheck_summary_rejection_reason(report()) is None


@pytest.mark.parametrize(
    "data, reason",
    [
        ({"risks": []}, "rugcheck_score_missing"),
        (report(score=41), "rugcheck_score_over_limit"),
        ({"score_normalised": 5}, "rugcheck_risks_missing_or_invalid"),
        (report(risks=[{"level": "Danger"}]), "rugcheck_danger_or_critical_risk"),
        (report(risks=[{"level": "critical"}]), "rugcheck_danger_or_critical_risk"),
        ({"score_normalised": 5, "risks": []}, "lp_lock_percentage_missing"),
        ("not a dict", "invalid_rugcheck_data"),
    ],
)
def test_rugcheck_summary_gates(data, reason):
    assert rugcheck_summary_rejection_reason(data) == reason


def test_warning_level_risk_is_allowed():
    assert rugcheck_summary_rejection_reason(report(risks=[{"level": "warn"}])) is None


def test_active_authorities_are_rejected():
    base = report()
    assert rugcheck_filter_rejection_reason({"mintAuthority": "X", "freezeAuthority": None}, base) == "mint_authority_active"
    assert rugcheck_filter_rejection_reason({"mintAuthority": None, "freezeAuthority": "Y"}, base) == "freeze_authority_active"
    assert rugcheck_filter_rejection_reason({"mintAuthority": None, "freezeAuthority": None}, base) is None


def test_missing_authority_state_fails_closed():
    assert rugcheck_filter_rejection_reason({}, report()) == "authority_state_missing"


@pytest.mark.parametrize(
    "extensions, reason",
    [
        ({"permanentDelegate": "abc"}, "blocked_token_extension:permanentdelegate"),
        ({"transfer_hook": {"programId": "x"}}, "blocked_token_extension:transferhook"),
        ({"TransferFeeConfig": True}, "blocked_token_extension:transferfeeconfig"),
        ({"pausableConfig": "enabled"}, "blocked_token_extension:pausable"),
        ({"nonTransferable": True}, "blocked_token_extension:nontransferable"),
        ({"permanentDelegate": None, "transferHook": "false"}, None),
        ({"metadataPointer": "x"}, None),
    ],
)
def test_token_extensions(extensions, reason):
    assert token_extension_rejection_reason({}, {"token_extensions": extensions}) == reason


def test_token_2022_without_extension_data_fails_closed():
    assert token_extension_rejection_reason({"program": TOKEN_2022}, {}) == "token_2022_extension_data_missing"


# --- score ---------------------------------------------------------------

def test_score_is_bounded():
    for args in [(pair(), report(), 0.25), (pair(liquidity=10**9, buys=10**6), report(score=0), 0.25),
                 (pair(liquidity=1), report(score=100), 2.0)]:
        assert 0.0 <= calculate_natural_selection_score(*args) <= 100.0


def test_score_missing_fields_is_zero():
    assert calculate_natural_selection_score({}, report()) == 0.0


def test_more_liquidity_scores_higher():
    low = calculate_natural_selection_score(pair(liquidity=6_000), report(), 1.0)
    high = calculate_natural_selection_score(pair(liquidity=40_000), report(), 1.0)
    assert high > low


def test_riskier_rugcheck_scores_lower():
    safe = calculate_natural_selection_score(pair(), report(score=1), 1.0)
    risky = calculate_natural_selection_score(pair(), report(score=40), 1.0)
    assert safe > risky


def test_fresher_pool_scores_higher():
    fresh = calculate_natural_selection_score(pair(), report(), 0.3)
    old = calculate_natural_selection_score(pair(), report(), 1.9)
    assert fresh > old


# --- deterministic selector -------------------------------------------------

def test_select_by_score_picks_highest_and_matches_decision_shape():
    tokens = [
        {"symbol": "A", "tokenAddress": "a", "selection_score": 71.6},
        {"symbol": "B", "tokenAddress": "b", "selection_score": 80.2},
    ]
    [decision] = select_by_score(tokens)
    assert decision["address"] == "b"
    assert decision["action"] == "BUY"
    assert decision["confidence"] == 80
    assert decision["selector"] == "score"


def test_select_by_score_empty():
    assert select_by_score([]) == []
