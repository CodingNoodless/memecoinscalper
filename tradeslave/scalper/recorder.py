"""Append-only tick recorder: every token creation and curve change, gzip JSONL,
one file per UTC day under data/ticks/. Replayed by the backtester."""

from __future__ import annotations

import gzip
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from ..paths import DATA_DIR

TICK_DIR = DATA_DIR / "ticks"


class TickRecorder:
    def __init__(self, directory: Path = TICK_DIR, flush_every_s: float = 5.0):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._day = ""
        self._file = None
        self._last_flush = 0.0
        self.flush_every_s = flush_every_s
        self.records = 0

    def _handle(self, t: float):
        day = datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat()
        if day != self._day:
            self.close()
            self._day = day
            self._file = gzip.open(self.directory / f"{day}.jsonl.gz", "at", encoding="utf-8")
        return self._file

    def write(self, record: dict) -> None:
        handle = self._handle(record["t"])
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        self.records += 1
        now = time.monotonic()
        if now - self._last_flush >= self.flush_every_s:
            handle.flush()
            self._last_flush = now

    def create(self, t, mint, symbol, state, creator="", curve_key="", initial_buy_sol=0.0,
               dev_tokens=0.0, mayhem=False) -> None:
        self.write({"k": "create", "t": round(t, 3), "m": mint, "s": symbol, "cr": creator, "bc": curve_key,
                    "ib": initial_buy_sol, "dt": dev_tokens, "mh": int(bool(mayhem)),
                    "vt": state.virtual_token_reserves if state else None,
                    "vs": state.virtual_sol_reserves if state else None})

    def curve(self, t, mint, state, slot=None) -> None:
        self.write({"k": "curve", "t": round(t, 3), "m": mint, "vt": state.virtual_token_reserves,
                    "vs": state.virtual_sol_reserves, "rt": state.real_token_reserves,
                    "rs": state.real_sol_reserves, "c": int(state.complete), "sl": slot})

    def dev_sell(self, t, mint, how) -> None:
        self.write({"k": "devsell", "t": round(t, 3), "m": mint, "how": how})

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


def read_ticks(paths: list[Path]):
    """Yield recorded dicts from one or more .jsonl.gz files, oldest first per file."""
    for path in sorted(paths):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue  # a torn last line after a crash
