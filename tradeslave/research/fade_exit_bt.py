"""FX fade-exit test (pre-registered 2026-10-22 14:15, vault note "2026-10-22 fade exits (FX)").

    uv run python -m tradeslave.research.fade_exit_bt

Same NC entries; exits replayed on every curve trade to entry + 10 min. A signal on a trade sells at the first trade
>= 1 s later less 1% extra slippage (stops: less 3%). Fees and impact 1.25% + 0.5% per side. Design = created before
2026-10-21 15:00 PDT, check = after. One look.
"""

from __future__ import annotations

import gzip
import json
import statistics

from ..paths import DATA_DIR
from .hold_trades import OUT as HOLD
from .nc_data import OUT as NC

FEE, SPLIT, HOLD_S = 0.0125 + 0.005, 1792623600, 600


def run(tr, te, e, rule, stop):
    """Net return. tr = [[t, price, is_buy, sol, wallet], ...] after entry, oldest first."""
    b = e * (1 + FEE)
    peak, armed, maxb = e, False, 0
    sig = None                                     # (time, slippage) of a pending sell
    for i, (t, p, buy, sol, w) in enumerate(tr):
        if sig and t >= sig[0] + 1:
            return p * (1 - sig[1]) * (1 - FEE) / b - 1
        if sig:
            continue
        if t - te > HOLD_S:
            return p * (1 - FEE) / b - 1
        peak = max(peak, p)
        x = p / e
        if x <= stop:
            sig = (t, 0.03)
            continue
        if rule == "F0" and x >= 2.0 or rule == "F1" and x >= 1.5:
            return min(p, e * (2.0 if rule == "F0" else 1.5)) * (1 - FEE) / b - 1   # resting take-profit
        if rule in ("F2", "F3", "F4", "F5") and peak >= 1.3 * e:
            armed = True
        if rule in ("F4", "F5"):
            w20 = {y[4] for y in tr[max(0, i - 400):i + 1] if y[2] and y[0] > t - 20}
            maxb = max(maxb, len(w20))
        if not armed:
            continue
        off = 0.15 if rule == "F2" else 0.25
        if p <= peak * (1 - off):
            sig = (t, 0.01)
        elif rule == "F4":
            fl = sum(y[3] if y[2] else -y[3] for y in tr[max(0, i - 400):i + 1] if y[0] > t - 15)
            if fl < 0:
                sig = (t, 0.01)
        elif rule == "F5" and maxb and len(w20) < 0.4 * maxb:
            sig = (t, 0.01)
    if not tr:
        return None
    return tr[-1][1] * (1 - (sig[1] if sig else 0)) * (1 - FEE) / b - 1


def main() -> None:
    nc = {r["m"]: r for r in json.loads(NC.read_text())}
    cells, n = {}, 0
    for p in HOLD.glob("*.json.gz"):
        d = json.load(gzip.open(p, "rt", encoding="utf-8"))
        r = nc.get(d["m"])
        if r is None:
            continue
        tr = [[x[2], x[5] / x[6], x[4], x[5], x[3]] for x in d["trades"] if not x[7] and x[6] > 0]
        after = [x for x in tr if x[0] > r["te"]]
        if not after:
            continue
        n += 1
        e = next(x[1] for x in tr if x[0] >= r["te"])
        pk = max(x[1] for x in after if x[0] <= r["te"] + HOLD_S) / e
        for rule in ("F0", "F1", "F2", "F3", "F4", "F5"):
            for stop in (0.7, 0.85):
                v = run(after, r["te"], e, rule, stop)
                if v is not None:
                    cells.setdefault((rule, stop), []).append((int(r["t0"] >= SPLIT), v, pk, d["m"]))
    print(n, "coins with hold trades")
    base = {(h, m): v for h, v, pk, m in cells[("F0", 0.7)]}
    for (rule, stop), z in sorted(cells.items()):
        out = []
        for h in (0, 1):
            v = [x[1] for x in z if x[0] == h]
            g = [x[1] - base[(h, x[3])] for x in z if x[0] == h and (h, x[3]) in base]
            mid = [x[1] for x in z if x[0] == h and 1.2 <= x[2] < 2.0]
            t = statistics.mean(v) / (statistics.stdev(v) / len(v) ** 0.5) if len(v) > 2 else 0
            out.append(f"n {len(v):3d} {statistics.mean(v):+6.1%} t {t:+5.2f} vsF0 {statistics.mean(g):+6.1%} "
                       f"1.2-2x coins {statistics.mean(mid) if mid else 0:+6.1%}")
        print(f"{rule} stop {stop}: design {out[0]} | check {out[1]}")


if __name__ == "__main__":
    main()
