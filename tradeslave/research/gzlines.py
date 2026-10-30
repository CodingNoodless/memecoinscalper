"""Read JSON lines from gzip files that may hold several members or a member cut by a crash (kill mid-write).

Each member is decompressed incrementally; when one breaks, its lines up to the break are kept and the reader moves to
the next member header. Partial lines at a break are dropped."""

from __future__ import annotations

import json
import zlib

HEADER = b"\x1f\x8b\x08"
CHUNK = 1 << 16


def _member(raw: bytes, i: int) -> tuple[bytes, int | None]:
    """(decompressed bytes, end offset of the member or None if it broke / was cut)."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out, pos = [], i
    while pos < len(raw):
        try:
            out.append(d.decompress(raw[pos:pos + CHUNK]))
        except zlib.error:
            return b"".join(out), None
        pos += CHUNK
        if d.eof:
            return b"".join(out), len(raw) - len(d.unused_data) if pos >= len(raw) else pos - len(d.unused_data)
    return b"".join(out), None


def lines(path) -> list[str]:
    raw = open(path, "rb").read()
    out, i = [], raw.find(HEADER)
    while 0 <= i < len(raw):
        text, end = _member(raw, i)
        parts = text.decode("utf-8", "ignore").split("\n")
        out.extend(p for p in parts[:-1] if p.strip())
        if end is not None and parts[-1].strip():
            out.append(parts[-1])
        i = end if end is not None and raw[end:end + 3] == HEADER else raw.find(HEADER, i + 3 if end is None else end)
    return out


def records(path):
    for line in lines(path):
        try:
            yield json.loads(line)
        except ValueError:
            continue
