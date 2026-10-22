"""SN score-snipe test (pre-registered 2026-10-22 08:35, vault note "2026-10-22 score-snipe (SN)").

    uv run python -m tradeslave.research.snipe_bt

Buy a score >= 7 coin d seconds after the bot scored it (d = 1, 3), at the last trade before then; sell at 30/60/120 s
after create, or TP 2x / stop 0.7 / 30 min on 1-min candles. Costs: 1.25% fee + 0.5% impact per side, +2% land
slippage on the buy. Design = created before 2026-10-21 15:00 PDT, check = after. One look.
"""

from __future__ import annotations

import gzip
import json
import math
import statistics

from ..paths import DATA_DIR
from .early_trades import OUT
from .entry_bt import CACHE, universe

FEE, LAND, SPLIT = 0.0125 + 0.005, 0.02, 1792623600


def score_age():
    age = {}
    for v in ("m1", "m2", "m3"):
        for line in (DATA_DIR / v / "cands.jsonl").open(encoding="utf-8"):
            c = json.loads(line)
            if c.get("score") is not None and "age" in c:
                age[c["m"]] = min(age.get(c["m"], 9e9), c["age"])
    return age


def px(tr, t):
    """SOL per token of the last curve trade at or before t (first trade if none yet)."""
    last = tr[0]
    for x in tr:
        if x[2] > t:
            break
        last = x
    return last[5] / last[6] if last[6] else None


def main() -> None:
    age, cells = score_age(), {}
    for x in universe():
        p = OUT / f"{x['m']}.json.gz"
        if not p.exists():
            continue
        d = json.load(gzip.open(p, "rt", encoding="utf-8"))
        tr = [y for y in d["trades"] if not y[7] and y[6] > 0]
        if not tr:
            continue
        t0 = tr[0][2]                                           # create = the dev's first trade
        cp = CACHE / f"{x['m']}.json"
        c = json.loads(cp.read_text()) if cp.exists() else []
        for dl in (1, 3):
            te = t0 + age.get(x["m"], 3) + dl
            e = px(tr, te)
            if not e:
                continue
            b = e * (1 + FEE + LAND)
            for hs in (30, 60, 120):
                if te < t0 + hs:
                    cells.setdefault((dl, f"sell {hs}s"), {0: [], 1: []})[int(t0 >= SPLIT)].append(
                        px(tr, t0 + hs) * (1 - FEE) / b - 1)
            if c:                                               # candles are SOL per UI token; match units
                k = 10 ** round(math.log10(c[0][4] / (tr[0][5] / tr[0][6])))
                ee, r = e * k, None
                for y in c:
                    if y[0] < te:                                   # first full minute after entry
                        continue
                    if y[0] - te > 1800:
                        r = y[1] * (1 - FEE) / (ee * (1 + FEE + LAND)) - 1
                        break
                    if y[3] <= ee * 0.7:
                        r = min(ee * 0.7, y[1]) * 0.97 * (1 - FEE) / (ee * (1 + FEE + LAND)) - 1
                        break
                    if y[2] >= ee * 2:
                        r = 2 * (1 - FEE) / (1 + FEE + LAND) - 1
                        break
                if r is None and c[-1][0] > te:
                    r = c[-1][4] * (1 - FEE) / (ee * (1 + FEE + LAND)) - 1
                if r is not None:
                    cells.setdefault((dl, "tp2 stop.7 30m"), {0: [], 1: []})[int(t0 >= SPLIT)].append(r)
    for (dl, ex), h in sorted(cells.items()):
        a, b = h[0], h[1]
        tb = statistics.mean(b) / (statistics.stdev(b) / len(b) ** 0.5) if len(b) > 2 else 0
        print(f"d {dl}s {ex:16s} design n {len(a):4d} {statistics.mean(a) if a else 0:+7.1%} | "
              f"check n {len(b):4d} {statistics.mean(b) if b else 0:+7.1%} t {tb:+.2f}")


if __name__ == "__main__":
    main()
