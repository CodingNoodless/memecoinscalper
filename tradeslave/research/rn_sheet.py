"""Print compact RN1000 fact sheets for a slice of runners, so the write-ups can be written by hand from them.

    uv run python -X utf8 -m tradeslave.research.rn_sheet <start> <count>     (skips runners that already have a write-up)
"""

from __future__ import annotations

import json
import sys
import time

from ..paths import DATA_DIR

OUT = DATA_DIR / "runners"


def k(x):
    return "?" if x is None else f"${x / 1e6:.2f}M" if x >= 1e6 else f"${x / 1e3:.0f}k"


def main() -> None:
    F = json.loads((OUT / "facts.json").read_text(encoding="utf-8"))
    W = json.loads((OUT / "writeups.json").read_text(encoding="utf-8")) if (OUT / "writeups.json").exists() else {}
    a, n = int(sys.argv[1]), int(sys.argv[2])
    todo = [f for f in F if f["m"] not in W][a:a + n]
    for f in todo:
        L, C = f.get("launch") or {}, f.get("chart") or {}
        print(f"### {f['m']} | {f.get('name')} (${f.get('sym')}) | cat {f['category']} | created {time.strftime('%m-%d %H:%M', time.localtime(f['created'])) if f.get('created') else '?'}")
        print(f"desc: {(f.get('desc') or '').replace(chr(10), ' ')[:300]}")
        if f.get("x_info"):
            print(f"X: {f['x_info'].replace(chr(10), ' ')[:420]}")
        print(f"links: tw={bool(f.get('twitter'))} web={bool(f.get('website'))} tg={bool(f.get('telegram'))} charity={f.get('charity')} agent={f.get('agent')}")
        print(f"launch: start {k(f.get('start_mc'))}, dev buy {L.get('dev_buy', 0):.2f} SOL, launch-2s snipers {L.get('snipers')} ({L.get('snipe_share', 0):.0%} of supply), "
              f"buyers in 5 min {L.get('buyers_5m')}, net flow 5 min {L.get('flow_5m', 0):+.1f} SOL, early top callers {L.get('early_callers')}, tracked {L.get('tracked')}")
        print(f"curve {f['curve_h']:.2f} h -> migrated | ATH {k(f.get('ath'))} | chart: {C.get('pattern')}; mig {k(C.get('mig_mc'))}, peak {k(C.get('peak_mc'))} "
              f"after {C.get('to_peak_h', 0):.1f} h, legs {C.get('legs')}, 48 h end {k(C.get('last_mc'))} ({C.get('last_vs_peak', 0):.0%} of peak), "
              f"{C.get('mins_above_300k')} min >= $300k, vol {k(C.get('vol_48h'))} | now {k(f.get('mc_now'))}")
        print()


if __name__ == "__main__":
    main()
