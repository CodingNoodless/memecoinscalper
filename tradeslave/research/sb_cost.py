"""SB stage 2: what fast-dumping devs cost our paper bots. For every closed M5-M9/M10 trade: the creator's launch buy, whether
the creator sold before / after our entry, and the coin's age at entry; mean return by bucket. Read-only (feed DB + create log).

    uv run python -X utf8 -m tradeslave.research.sb_cost
"""

from __future__ import annotations

import glob
import json
import sqlite3
import statistics
from collections import defaultdict

from ..assist import _creates
from ..paths import DATA_DIR
from .live_feed import DB

BOTS = ("m5", "m6", "m7", "m8", "m9", "m10")


def main() -> None:
    _, dev_of = _creates()
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = []
    for b in BOTS:
        for f in glob.glob(str(DATA_DIR / b / "trades.jsonl")):
            for line in open(f, encoding="utf-8", errors="ignore"):
                r = json.loads(line)
                if r.get("ret") is None:
                    continue
                m, cr = r["m"], dev_of.get(r["m"])
                tr = db.execute("SELECT t, w, b, sol FROM tr WHERE m=? ORDER BY slot, seq", (m,)).fetchall()
                if not tr or not cr:
                    continue
                t0 = tr[0][0]
                dev_buy = sum(s for t, w, bb, s in tr if w == cr and bb and t - t0 <= 2)
                sold_before = any(w == cr and not bb and t <= r["t"] for t, w, bb, s in tr)
                sold_after60 = any(w == cr and not bb and r["t"] < t <= r["t"] + 60 for t, w, bb, s in tr)
                rows.append(dict(bot=b, m=m, ret=r["ret"], age=r["t"] - t0, dev_buy=dev_buy, sold_before=sold_before, sold_after=sold_after60))
    print(f"{len(rows)} closed trades with a known creator and feed history ({', '.join(BOTS)})")

    def show(name, xs):
        if xs:
            v = [x["ret"] for x in xs]
            print(f"  {name:52s} {len(v):4d} trades  mean {statistics.mean(v):+6.1%}  <= -25%: {sum(a <= -0.25 for a in v) / len(v):4.0%}")

    print("by creator's launch buy:")
    for lo, hi in ((0, 1), (1, 4), (4, 8), (8, 1e9)):
        show(f"dev buy {lo}-{hi if hi < 1e9 else '+'} SOL", [x for x in rows if lo <= x["dev_buy"] < hi])
    print("dev buy >= 4 SOL, by what the dev did:")
    big = [x for x in rows if x["dev_buy"] >= 4]
    show("dev had already sold when we entered", [x for x in big if x["sold_before"]])
    show("dev still holding, sold within 60 s after our entry", [x for x in big if not x["sold_before"] and x["sold_after"]])
    show("dev still holding, didn't sell within 60 s", [x for x in big if not x["sold_before"] and not x["sold_after"]])
    print("dev buy >= 4 SOL and still holding, by coin age at entry:")
    hold = [x for x in big if not x["sold_before"]]
    for lo, hi in ((0, 15), (15, 60), (60, 1e9)):
        show(f"age {lo}-{hi if hi < 1e9 else '+'} s", [x for x in hold if lo <= x["age"] < hi])
    per = defaultdict(list)
    for x in rows:
        per[x["bot"]].append(x)
    print("cost per bot of entries where the dev (>= 4 SOL buy) was still holding and sold within 60 s:")
    for b, xs in per.items():
        hit = [x for x in xs if x["dev_buy"] >= 4 and not x["sold_before"] and x["sold_after"]]
        print(f"  {b}: {len(hit)} of {len(xs)} trades, sum of returns {sum(x['ret'] for x in hit):+.2f} (whole book {sum(x['ret'] for x in xs):+.2f})")


if __name__ == "__main__":
    main()
