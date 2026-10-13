import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import threading
import os

from config import (
    JUPITER_API_KEY,
    MAX_CANDIDATE_AGE_SECONDS,
    OLLAMA_MODEL,
    OLLAMA_TIMEOUT_SECONDS,
    RISK_PER_TRADE_PCT,
    MAX_POSITION_PCT_OF_BALANCE,
    MAX_DAILY_LOSS_PCT,
    MAX_ENTRY_ROUNDTRIP_LOSS_PCT,
    MAX_HOLD_HOURS,
    PAPER_OBSERVATION_INTERVAL_SECONDS,
    RUGCHECK_API_KEY,
)
from scanner import run_scanner_loop
from trader import run_trader_loop


_INSTANCE_LOCK_HANDLE = None


def _acquire_instance_lock(log_dir: Path):
    """Hold an OS lock so two bot copies cannot race on shared paper state."""
    lock_file = (log_dir.parent / ".tradeslavebot.lock").open("a+b")
    if lock_file.seek(0, os.SEEK_END) == 0:
        lock_file.write(b"0")
        lock_file.flush()
    lock_file.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        lock_file.close()
        return None
    return lock_file


def main() -> None:
    global _INSTANCE_LOCK_HANDLE
    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_format = "%(asctime)s %(levelname)s %(threadName)s %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                log_dir / "tradeslavebot.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            ),
        ],
    )
    logging.getLogger(__name__).info(
        "Starting paper trader (RugCheck key: %s, Jupiter key: %s, Ollama model: %s, Ollama timeout: %ss, signal max age: %ss, position stats interval: %ss)",
        "loaded" if RUGCHECK_API_KEY else "missing",
        "loaded" if JUPITER_API_KEY else "missing",
        OLLAMA_MODEL,
        OLLAMA_TIMEOUT_SECONDS,
        MAX_CANDIDATE_AGE_SECONDS,
        PAPER_OBSERVATION_INTERVAL_SECONDS,
    )
    logging.getLogger(__name__).info(
        "Paper risk controls (planned risk/trade: %.1f%%, max position: %.1f%% of cash, daily loss pause: %.1f%%, max entry round-trip cost: %.1f%%, max hold: %.1fh)",
        RISK_PER_TRADE_PCT * 100.0,
        MAX_POSITION_PCT_OF_BALANCE * 100.0,
        MAX_DAILY_LOSS_PCT * 100.0,
        MAX_ENTRY_ROUNDTRIP_LOSS_PCT * 100.0,
        MAX_HOLD_HOURS,
    )
    _INSTANCE_LOCK_HANDLE = _acquire_instance_lock(log_dir)
    if _INSTANCE_LOCK_HANDLE is None:
        logging.getLogger(__name__).error(
            "Another TradeSlaveBot instance already holds the process lock; this copy will exit"
        )
        return
    workers = [
        threading.Thread(name="scanner", target=run_scanner_loop, daemon=True),
        threading.Thread(name="paper-trader", target=run_trader_loop, daemon=True),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()


if __name__ == "__main__":
    main()
