"""Write data/runners/runners.json from the curve runners the migration walk has confirmed so far (start < $10k, >= 60 s on
the curve, ATH >= $300k), so details / curve trades / candles can be collected while the walk continues. The walk overwrites
runners.json with the full list when it finishes; every collector resumes.

    uv run python -m tradeslave.research.rn_interim
"""

from __future__ import annotations

import json

from .rn_collect import ATH_MIN, CURVE_MIN_S, OUT, START_MAX


def main() -> None:
    rows = [json.loads(x) for x in (OUT / "migrations.jsonl").open()]
    R = {}
    for r in rows:
        if ((r.get("ath") or 0) >= ATH_MIN and r.get("created") and r["t"] - r["created"] >= CURVE_MIN_S
                and r.get("start_mc") is not None and r["start_mc"] < START_MAX):
            R[r["m"]] = r
    out = sorted(R.values(), key=lambda r: -r["t"])
    (OUT / "runners.json").write_text(json.dumps(out))
    print(f"interim runners.json: {len(out)} curve runners")


if __name__ == "__main__":
    main()
