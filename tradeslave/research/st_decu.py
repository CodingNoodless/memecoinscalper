"""ST-decu: copy decu (4vw54Bm...) at our latency. His own 72 h trades (data/m1/st_decu_trades.json, from his signed txs);
our copy buys at his first buy + d and sells at his first sell + d, priced from the coin's pump.fun curve trade events.
Vault "2026-10-24 copying Twitch streamers (ST)", decu section, pre-registered 01:21 PDT.
PASS (copy +1 s): n >= 60 priced coins, mean > 0, and both time halves > 0.

    uv run python -u -m tradeslave.research.st_decu
"""

from __future__ import annotations

import json
import statistics

from ..paths import DATA_DIR
from .fl_test import helius_url
from .st_copy import FEE, TXR, price_at

DELAYS = (0, 1, 2, 5)


def main() -> None:
    url = helius_url()
    d = json.loads((DATA_DIR / "m1" / "st_decu_trades.json").read_text())
    per = []
    for m, ev in d.items():
        ev.sort()
        b = [e for e in ev if e[2] < 0 and e[1] > 0]
        s = [e for e in ev if e[2] > 0 and e[1] < 0]
        if b and s and m.endswith("pump"):
            per.append(dict(m=m, tb=b[0][0], ts=s[0][0], own=sum(e[2] for e in s) / -sum(e[2] for e in b) - 1))
    per.sort(key=lambda x: x["tb"])
    mid = statistics.median(x["tb"] for x in per)
    cache = {}
    for i, x in enumerate(per):
        for dd in DELAYS:
            pe, px = price_at(url, x["m"], x["tb"] + dd, cache), price_at(url, x["m"], x["ts"] + dd, cache)
            x[f"c{dd}"] = (px / pe * (1 - FEE) ** 2 - 1 - TXR) if pe and px else None
        if i % 25 == 0:
            print(f"{i}/{len(per)}", flush=True)
    (DATA_DIR / "m1" / "st_copy").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "m1" / "st_copy" / "decu.json").write_text(json.dumps(per))
    print(f"pump.fun coins with a buy and a sell: {len(per)}; his own per-coin ROI mean {statistics.mean(x['own'] for x in per):+.1%}")
    for dd in DELAYS:
        v = [x for x in per if x[f"c{dd}"] is not None]
        if not v:
            continue
        vals = [x[f"c{dd}"] for x in v]
        h = [[x[f"c{dd}"] for x in v if (x["tb"] < mid) == q] for q in (True, False)]
        print(f"copy +{dd} s: n {len(v)} mean {statistics.mean(vals):+.1%} med {statistics.median(vals):+.1%} win {sum(r > 0 for r in vals) / len(vals):.0%} "
              f"| halves {statistics.mean(h[0]):+.1%} / {statistics.mean(h[1]):+.1%} | his own on these {statistics.mean(x['own'] for x in v):+.1%}")
    v = [x for x in per if x["c1"] is not None]
    h = [[x["c1"] for x in v if (x["tb"] < mid) == q] for q in (True, False)]
    ok = len(v) >= 60 and statistics.mean(x["c1"] for x in v) > 0 and all(hh and statistics.mean(hh) > 0 for hh in h)
    print(f"ST-decu VERDICT: {'PASS' if ok else 'FAIL'}")


if __name__ == "__main__":
    main()
