import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import PROJECT_ROOT


STATE_DIR = PROJECT_ROOT
STATE_LOCK = threading.RLock()


def state_path(name: str) -> Path:
    return STATE_DIR / name


def load_json(name: str, default: Any) -> Any:
    path = state_path(name)
    with STATE_LOCK:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as source:
            return json.load(source)


def save_json(name: str, value: Any) -> None:
    """Atomically replace one state file so readers never see partial JSON."""
    path = state_path(name)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with STATE_LOCK:
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as destination:
                json.dump(value, destination, indent=2, allow_nan=False)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()


def append_jsonl(name: str, value: dict[str, Any]) -> None:
    """Append one durable JSON record for paper-trading observations."""
    path = state_path(name)
    with STATE_LOCK:
        with path.open("a", encoding="utf-8", newline="\n") as destination:
            destination.write(json.dumps(value, allow_nan=False) + "\n")
            destination.flush()
            os.fsync(destination.fileno())


def recent_token_attempts(cooldown_seconds: int) -> set[str]:
    """Return mints selected within the cooldown, pruning expired records."""
    now = time.time()
    with STATE_LOCK:
        try:
            records = load_json("token_attempts.json", {})
        except (OSError, json.JSONDecodeError):
            records = {}
        if not isinstance(records, dict):
            records = {}
        retained = {}
        recent = set()
        for address, record in records.items():
            if not isinstance(address, str) or not isinstance(record, dict):
                continue
            try:
                attempted_at = float(record.get("attempted_at"))
            except (TypeError, ValueError):
                continue
            if now - attempted_at < cooldown_seconds:
                retained[address] = record
                recent.add(address)
        # Seed cooldown from existing paper history so tokens selected before
        # this feature was added do not immediately reappear after restart.
        try:
            history = load_json("trade_history.json", [])
        except (OSError, json.JSONDecodeError):
            history = []
        if isinstance(history, list):
            for trade in history:
                if not isinstance(trade, dict):
                    continue
                address = trade.get("address")
                closed_at = trade.get("closed_at")
                if not isinstance(address, str) or not isinstance(closed_at, str):
                    continue
                try:
                    closed = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
                    if closed.tzinfo is None:
                        closed = closed.replace(tzinfo=timezone.utc)
                    attempted_at = closed.timestamp()
                except (TypeError, ValueError, OverflowError):
                    continue
                if now - attempted_at < cooldown_seconds:
                    retained[address] = {
                        "attempted_at": attempted_at,
                        "symbol": str(trade.get("symbol", "UNKNOWN"))[:40],
                        "reason": "recent_paper_trade_history",
                    }
                    recent.add(address)
        try:
            pending = load_json("pending_entry.json", None)
        except (OSError, json.JSONDecodeError):
            pending = None
        if isinstance(pending, dict):
            candidate = pending.get("candidate")
            address = candidate.get("tokenAddress") if isinstance(candidate, dict) else None
            watch_started_at = pending.get("watch_started_at")
            if isinstance(address, str) and isinstance(watch_started_at, str):
                try:
                    watched = datetime.fromisoformat(watch_started_at.replace("Z", "+00:00"))
                    if watched.tzinfo is None:
                        watched = watched.replace(tzinfo=timezone.utc)
                    attempted_at = watched.timestamp()
                except (TypeError, ValueError, OverflowError):
                    attempted_at = 0.0
                if now - attempted_at < cooldown_seconds:
                    retained[address] = {
                        "attempted_at": attempted_at,
                        "symbol": str(candidate.get("symbol", "UNKNOWN"))[:40],
                        "reason": "recent_pending_entry_watch",
                    }
                    recent.add(address)
        if retained != records:
            save_json("token_attempts.json", retained)
        return recent


def mark_token_attempt(address: str, symbol: str, reason: str = "selected") -> None:
    """Persist a selected mint so repeated scans do not keep recycling it."""
    if not address:
        return
    with STATE_LOCK:
        try:
            records = load_json("token_attempts.json", {})
        except (OSError, json.JSONDecodeError):
            records = {}
        if not isinstance(records, dict):
            records = {}
        records[address] = {
            "attempted_at": time.time(),
            "symbol": str(symbol or "UNKNOWN")[:40],
            "reason": str(reason)[:80],
        }
        save_json("token_attempts.json", records)


def save_wallet_and_clear_candidates(wallet: dict[str, Any]) -> None:
    """Commit a position transition and invalidate all outstanding signals."""
    with STATE_LOCK:
        save_json("paper_wallet.json", wallet)
        save_json("active_candidates.json", empty_candidate_snapshot())
        save_json("pending_entry.json", None)


def load_pending_entry() -> dict[str, Any] | None:
    value = load_json("pending_entry.json", None)
    return value if isinstance(value, dict) else None


def save_pending_entry(value: dict[str, Any] | None) -> None:
    save_json("pending_entry.json", value)


def empty_candidate_snapshot() -> dict[str, Any]:
    return {"schema_version": 2, "generated_at": None, "candidates": [], "llm_decisions": []}


def take_candidate_snapshot() -> Any:
    """Consume a candidate snapshot under the same lock used by the scanner."""
    path = state_path("active_candidates.json")
    with STATE_LOCK:
        if not path.exists():
            return empty_candidate_snapshot()
        try:
            with path.open("r", encoding="utf-8") as source:
                value = json.load(source)
        except (OSError, json.JSONDecodeError):
            save_json("active_candidates.json", empty_candidate_snapshot())
            return empty_candidate_snapshot()
        save_json("active_candidates.json", empty_candidate_snapshot())
        return value


def publish_candidate_snapshot_if_flat(snapshot: dict[str, Any]) -> bool:
    """Do not publish new signals while an entry watch or position is active."""
    with STATE_LOCK:
        pending_path = state_path("pending_entry.json")
        if pending_path.exists():
            try:
                with pending_path.open("r", encoding="utf-8") as source:
                    pending = json.load(source)
            except (OSError, json.JSONDecodeError):
                pending = None
            if isinstance(pending, dict):
                return False
        wallet_path = state_path("paper_wallet.json")
        if wallet_path.exists():
            with wallet_path.open("r", encoding="utf-8") as source:
                wallet = json.load(source)
            if not isinstance(wallet, dict) or wallet.get("active_trade"):
                return False
        save_json("active_candidates.json", snapshot)
        return True
