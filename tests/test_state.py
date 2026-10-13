import time

from tradeslave import state


def test_save_and_load_roundtrip_leaves_no_temp_files(state_dir):
    state.save_json("x.json", {"a": 1})
    assert state.load_json("x.json", None) == {"a": 1}
    assert [p.name for p in state_dir.iterdir()] == ["x.json"]


def test_load_missing_returns_default(state_dir):
    assert state.load_json("missing.json", {"d": 1}) == {"d": 1}


def test_append_jsonl(state_dir):
    state.append_jsonl("log.jsonl", {"n": 1})
    state.append_jsonl("log.jsonl", {"n": 2})
    assert (state_dir / "log.jsonl").read_text().splitlines() == ['{"n": 1}', '{"n": 2}']


def test_snapshot_not_published_while_position_open(state_dir):
    state.save_json("paper_wallet.json", {"balance": 100, "active_trade": {"trade_id": "t"}})
    assert state.publish_candidate_snapshot_if_flat({"schema_version": 2, "candidates": [1]}) is False


def test_snapshot_not_published_during_entry_watch(state_dir):
    state.save_json("paper_wallet.json", {"balance": 100, "active_trade": None})
    state.save_pending_entry({"candidate": {"tokenAddress": "a"}})
    assert state.publish_candidate_snapshot_if_flat({"schema_version": 2, "candidates": [1]}) is False


def test_snapshot_published_when_flat_then_consumed_once(state_dir):
    state.save_json("paper_wallet.json", {"balance": 100, "active_trade": None})
    snap = {"schema_version": 2, "candidates": [1], "llm_decisions": []}
    assert state.publish_candidate_snapshot_if_flat(snap) is True
    assert state.take_candidate_snapshot() == snap
    assert state.take_candidate_snapshot()["candidates"] == []


def test_token_cooldown_marks_and_expires(state_dir):
    state.mark_token_attempt("mintA", "AAA")
    assert "mintA" in state.recent_token_attempts(3600)
    records = state.load_json("token_attempts.json", {})
    records["mintA"]["attempted_at"] = time.time() - 7200
    state.save_json("token_attempts.json", records)
    assert "mintA" not in state.recent_token_attempts(3600)
    assert state.load_json("token_attempts.json", {}) == {}


def test_cooldown_seeded_from_recent_trade_history(state_dir):
    from datetime import datetime, timezone
    closed = datetime.now(timezone.utc).isoformat()
    state.save_json("trade_history.json", [{"address": "mintB", "closed_at": closed, "symbol": "B"}])
    assert "mintB" in state.recent_token_attempts(3600)
