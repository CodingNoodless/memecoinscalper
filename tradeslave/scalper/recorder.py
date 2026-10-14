"""Append-only tick recorder: every token creation and curve change, gzip JSONL,
one file per UTC day under data/ticks/. Replayed by the backtester."""

from __future__ import annotations

import gzip
import json
import time
import zlib
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
               dev_tokens=0.0, mayhem=False, uri="", name="") -> None:
        self.write({"k": "create", "t": round(t, 3), "m": mint, "s": symbol, "cr": creator, "bc": curve_key,
                    "ib": initial_buy_sol, "dt": dev_tokens, "mh": int(bool(mayhem)), "uri": uri, "nm": name,
                    "vt": state.virtual_token_reserves if state else None,
                    "vs": state.virtual_sol_reserves if state else None})

    def curve(self, t, mint, state, slot=None) -> None:
        self.write({"k": "curve", "t": round(t, 3), "m": mint, "vt": state.virtual_token_reserves,
                    "vs": state.virtual_sol_reserves, "rt": state.real_token_reserves,
                    "rs": state.real_sol_reserves, "c": int(state.complete), "sl": slot})

    def metadata(self, t, mint, socials: dict) -> None:
        self.write({"k": "meta", "t": round(t, 3), "m": mint, **{f"x_{k}": v for k, v in socials.items()}})

    def trade(self, t, mint, wallet, is_buy, sol, tokens, vs, vt, signature) -> None:
        """One trade with the trader's wallet (PumpPortal token-trade stream)."""
        self.write({"k": "trade", "t": round(t, 3), "m": mint, "w": wallet, "b": int(bool(is_buy)), "sol": sol,
                    "tok": tokens, "vs": vs, "vt": vt, "sig": signature})

    def migration(self, t, mint, fields: dict) -> None:
        self.write({"k": "migr", "t": round(t, 3), "m": mint, **{f"x_{k}": v for k, v in fields.items()}})

    def creator_sell(self, t, mint) -> None:
        """The creator wallet sold something (not necessarily this token)."""
        self.write({"k": "csell", "t": round(t, 3), "m": mint})

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


GZIP_HEADER = b"\x1f\x8b\x08"


def _members(data: bytes):
    """Decompressed bytes of each gzip member in `data`.

    The recorder appends a new gzip member every time the scalper starts. If a
    run is killed mid-write, its member is cut off and the next run's member
    follows the broken bytes. A plain gzip reader stops at the break and loses
    everything after it, so here a damaged member gives up what it decoded and
    reading resumes at the next gzip header."""
    pos = 0
    while True:
        pos = data.find(GZIP_HEADER, pos)
        if pos < 0:
            return
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out, cursor, step, broken = [], pos, 1 << 18, False
        while cursor < len(data) and not decoder.eof:
            piece = data[cursor:cursor + step]
            try:
                out.append(decoder.decompress(piece))
            except zlib.error:
                broken = True
                out = [_salvage(data[pos:cursor + len(piece)])]
                break
            cursor += len(piece)
        yield b"".join(out)
        if decoder.eof and not broken:
            pos = cursor - len(decoder.unused_data)
        else:
            pos += len(GZIP_HEADER)


def _salvage(member: bytes, step: int = 1024) -> bytes:
    """What a damaged gzip member decodes to before the damage."""
    decoder, out = zlib.decompressobj(16 + zlib.MAX_WBITS), []
    for i in range(0, len(member), step):
        try:
            out.append(decoder.decompress(member[i:i + step]))
        except zlib.error:
            break
    return b"".join(out)


def read_ticks(paths: list[Path]):
    """Yield recorded dicts from one or more .jsonl.gz files, oldest first per file.

    Tolerates a file that is still being written (no gzip end marker yet) and a
    file with a damaged stretch from a crashed run: complete lines before and
    after the damage are returned; a torn line is skipped."""
    for path in sorted(paths):
        for chunk in _members(Path(path).read_bytes()):
            for line in chunk.split(b"\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
