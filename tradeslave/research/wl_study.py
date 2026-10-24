"""WL study: every feed round trip of the studied wallets (wl_fetch.WALLETS + decu), with the coin state at entry, the price path
after it, the context of the exit, the outcome after fees, and what a 1 s copy would have made. Each trade gets an entry
pattern and an exit pattern (fixed rules below) so trades with the same logic can be linked.
Writes data/m1/wl_trades.json.

    uv run python -u -m tradeslave.research.wl_study
"""

from __future__ import annotations

import bisect
import json
import sqlite3
import statistics

from ..paths import DATA_DIR
from . import br_test as B
from .dc_style import exit_ret
from .hs_test import coins
from .wl_fetch import WALLETS

DECU = "4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9"
FEE = 0.0125
OUT = DATA_DIR / "m1" / "wl_trades.json"


def entry_pattern(f):
    if f["age_s"] <= 30 and f["flow_30"] > 3 and f["accel"] >= 2:
        return "launch burst"
    if f["age_s"] <= 30:
        return "early quiet"
    if f["off_peak"] <= -0.25 and f["flow_30"] > 0:
        return "dip buy"
    if f["from_lo60"] >= 1.3 and f["buy_ratio_60"] >= 1.3:
        return "momentum breakout"
    if f["age_s"] >= 300:
        return "late coin"
    return "steady flow"


def exit_pattern(x):
    if x["hold"] <= 15:
        return "quick flip"
    if x["ret_curve"] <= -0.15:
        return "stop-out"
    if x["off_peak_sell"] >= -0.10:
        return "sold near the top"
    if x["sell_share_5s"] >= 0.6:
        return "sold when selling took over"
    return "sold on the fade"


def reason(x):
    f = x["f"]
    s = (f"bought a {x['entry']} at {f['age_s']:.0f} s (mcap {f['mcap_sol']:.0f} SOL, {f['buyers_30']} buyers in 30 s, "
         f"net flow {f['flow_30']:+.1f} SOL, accel x{f['accel']:.1f}); ")
    s += f"price then peaked {x['peak']:+.0%} after {x['t_peak']:.0f} s"
    s += f" (drew down {x['dd']:+.0%} first); " if x["dd"] <= -0.1 else "; "
    s += f"sold after {x['hold']:.0f} s at {x['ret_curve']:+.0%} on the curve ({x['exit']}"
    s += f", {x['off_peak_sell']:+.0%} off the peak)" if x["peak"] > 0.05 else ")"
    if x["creator_sold"]:
        s += "; the creator had sold during the hold"
    return s


def main() -> None:
    db = sqlite3.connect(DATA_DIR / "feed" / "trades.sqlite")
    t_end = db.execute("SELECT max(t) FROM tr").fetchone()[0]
    want = set(WALLETS) | {DECU}
    allc = [(m, r) for m, r in coins() if r[0][0] + 600 <= t_end]
    out = []
    for m, rows in allc:
        ws = {r[4] for r in rows if r[2]} & want
        if not ws:
            continue
        ts = [r[0] for r in rows]
        cr = rows[0][4]
        for w in ws:
            idx = [i for i, r in enumerate(rows) if r[4] == w]
            buys = [i for i in idx if rows[i][2]]
            kb = buys[0]
            sells = [i for i in idx if not rows[i][2] and i > kb]
            if kb < 3 or not sells:
                continue
            si = sum(rows[i][3] for i in buys)
            so = sum(rows[i][3] for i in sells)
            ti = sum(rows[i][5] for i in buys)
            to = sum(rows[i][5] for i in sells)
            if not 0.9 * ti <= to <= 1.1 * ti:
                continue
            ks = sells[0]
            f = B.state(rows, kb - 1)
            f["age_s"] = rows[kb][0] - rows[0][0]
            f["mcap_sol"] = rows[kb - 1][1] * 1e9
            pk_before = max(r[1] for r in rows[:kb])
            f["off_peak"] = rows[kb - 1][1] / pk_before - 1
            pe = rows[kb][1]
            seg = rows[kb:ks + 1]
            pk = max(r[1] for r in seg)
            lo_before_peak = min(r[1] for r in seg[:next(i for i, r in enumerate(seg) if r[1] == pk) + 1])
            w5 = [r for r in rows[:ks] if r[0] > rows[ks][0] - 5]
            x = dict(w=w, m=m, t=rows[kb][0], f=f, si=si, so=so, own=so * (1 - FEE) / (si * (1 + FEE)) - 1,
                     hold=rows[ks][0] - rows[kb][0], ret_curve=rows[ks - 1][1] / pe - 1, peak=pk / pe - 1,
                     t_peak=next(r[0] for r in seg if r[1] == pk) - rows[kb][0], dd=lo_before_peak / pe - 1,
                     off_peak_sell=rows[ks - 1][1] / pk - 1,
                     sell_share_5s=sum(r[3] for r in w5 if not r[2]) / max(1e-9, sum(r[3] for r in w5)),
                     creator_sold=any(r[4] == cr and not r[2] for r in seg), n_buys=len(buys), n_sells=len(sells),
                     max_after_30m=max((r[1] for r in rows[kb:] if r[0] <= rows[kb][0] + 1800), default=pe) / pe - 1)
            ke = max(0, bisect.bisect_right(ts, rows[kb][0] + 1) - 1)
            x["copy_his_exit"] = exit_ret(rows, ts, ke, rows[ks][0], None)
            x["copy_tp50_sl20"] = exit_ret(rows, ts, ke, rows[ks][0], ("tpsl", 0.5, 0.2))
            x["copy_hold30"] = exit_ret(rows, ts, ke, rows[ks][0], ("hold", 30))
            x["entry"] = entry_pattern(f)
            x["exit"] = exit_pattern(x)
            x["runner"] = x["max_after_30m"] >= 1.0
            x["why"] = reason(x)
            out.append(x)
    OUT.write_text(json.dumps(out))
    print(f"{len(out)} round trips from {len({x['w'] for x in out})} wallets")
    for w in sorted({x["w"] for x in out}, key=lambda w: -sum(1 for x in out if x["w"] == w)):
        v = [x for x in out if x["w"] == w]
        print(f"  {w[:8]}… n {len(v):4d} own {statistics.mean(x['own'] for x in v):+6.1%} win {sum(x['own'] > 0 for x in v) / len(v):4.0%} "
              f"copy+1s own-exit {statistics.mean(x['copy_his_exit'] for x in v):+6.1%} TP50/SL20 {statistics.mean(x['copy_tp50_sl20'] for x in v):+6.1%}")


if __name__ == "__main__":
    main()
