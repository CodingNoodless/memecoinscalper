"""Strategy X: the retail rule book on BTC (moving averages, RSI, Bollinger, breakouts, MACD, time of day), alone
and combined, with a strict three-way time split.

    uv run python -m tradeslave.research.btc_rules <btc1h.npy>

Data: Binance BTCUSDT 1-hour closes 2018-01..2026-09 (data.binance.vision). Bars: 1h, 4h, 1d. Each rule gives a
position in {0, 1} (long only) or {-1, 0, 1} (long/short, as on a perp; funding ignored), acted on at the NEXT bar.
Cost per unit of position change: FEE (0.10%, a cheap exchange) and FEE_US (0.40%, a typical US retail taker).
Split: design 2018-2022, validation 2023-2024, test 2025-01..2026-09 (judged once).
Freeze: every rule net-positive with Sharpe > 0.3 in BOTH design and validation at FEE_US; the frozen
system = equal-weight average position of the top 5 of those by min(design, validation) Sharpe ("put strategies
together"). Pass: test net return > 0 and Sharpe > 0.5 at FEE_US, and above buy-and-hold's test return.
Pre-registered in the vault note "2026-10-19 BTC rule book (X)".
"""

from __future__ import annotations

import json
import sys

import numpy as np

from ..paths import DATA_DIR

FEE, FEE_US = 0.001, 0.004
DES_END, VAL_END = 1672531200, 1735689600          # 2023-01-01, 2025-01-01
FROZEN = DATA_DIR / "btc_rules_rule.json"


def sma(x, n):
    return np.convolve(x, np.ones(n) / n, "full")[: len(x)]


def ema(x, n):
    a, out, v = 2 / (n + 1), np.empty_like(x), x[0]
    for i, z in enumerate(x):
        v = a * z + (1 - a) * v
        out[i] = v
    return out


def rsi(c, n=14):
    d = np.diff(c, prepend=c[0])
    up, dn = ema(np.maximum(d, 0), 2 * n - 1), ema(np.maximum(-d, 0), 2 * n - 1)
    return 100 - 100 / (1 + up / np.maximum(dn, 1e-12))


def hold(enter, exit_):
    """Stateful position: 1 after an enter signal until an exit signal."""
    pos, p = np.zeros(len(enter)), 0.0
    for i in range(len(enter)):
        if p == 0 and enter[i]:
            p = 1.0
        elif p == 1 and exit_[i]:
            p = 0.0
        pos[i] = p
    return pos


def rules(t, c):
    out = {}
    for f, s in ((5, 20), (10, 50), (20, 100), (50, 200), (10, 30), (20, 50)):
        up = sma(c, f) > sma(c, s)
        out[f"sma{f}/{s}"] = (up.astype(float), np.where(up, 1.0, -1.0))
        up = ema(c, f) > ema(c, s)
        out[f"ema{f}/{s}"] = (up.astype(float), np.where(up, 1.0, -1.0))
    r = rsi(c)
    for lo, hi in ((30, 50), (25, 55), (35, 65)):
        lg = hold(r < lo, r > hi)
        sh = hold(r > 100 - lo, r < 100 - hi)
        out[f"rsi{lo}/{hi}"] = (lg, lg - sh)
    m, sd = sma(c, 20), np.sqrt(np.maximum(sma(c * c, 20) - sma(c, 20) ** 2, 0))
    for k in (2.0, 2.5):
        lg = hold(c < m - k * sd, c > m)
        sh = hold(c > m + k * sd, c < m)
        out[f"boll{k}"] = (lg, lg - sh)
    for n in (20, 55, 100):
        hi = np.r_[np.full(n, np.inf), np.array([c[i - n:i].max() for i in range(n, len(c))])]
        lo = np.r_[np.full(n, -np.inf), np.array([c[i - n // 2:i].min() for i in range(n, len(c))])]
        lg = hold(c > hi, c < lo)
        lo2 = np.r_[np.full(n, -np.inf), np.array([c[i - n:i].min() for i in range(n, len(c))])]
        hi2 = np.r_[np.full(n, np.inf), np.array([c[i - n // 2:i].max() for i in range(n, len(c))])]
        sh = hold(c < lo2, c > hi2)
        out[f"donchian{n}"] = (lg, lg - sh)
    macd = ema(c, 12) - ema(c, 26)
    up = macd > ema(macd, 9)
    out["macd"] = (up.astype(float), np.where(up, 1.0, -1.0))
    return out


def evaluate(t, c, pos, fee, lo, hi, bars_per_year):
    pos = np.r_[0.0, pos[:-1]]                      # act on the next bar
    r = np.r_[0.0, c[1:] / c[:-1] - 1]
    net = pos * r - np.abs(np.diff(np.r_[0.0, pos])) * fee
    sel = (t >= lo) & (t < hi)
    x = net[sel]
    tot = float(np.prod(1 + x) - 1)
    sh = float(x.mean() / x.std() * np.sqrt(bars_per_year)) if x.std() > 0 else 0.0
    trades = float(np.abs(np.diff(pos[sel])).sum() / 2)
    return tot, sh, trades


def main() -> None:
    a = np.load(sys.argv[1])
    t1, c1 = a[:, 0], a[:, 1]
    periods = {"design": (0, DES_END), "val": (DES_END, VAL_END), "test": (VAL_END, 1e12)}
    rows = []
    for step, lab in ((1, "1h"), (4, "4h"), (24, "1d")):
        t, c = t1[step - 1::step], c1[step - 1::step]
        bpy = 365 * 24 / step
        for name, (lg, ls) in rules(t, c).items():
            for kind, pos in (("long", lg), ("l/s", ls)):
                res = {p: evaluate(t, c, pos, FEE_US, lo, hi, bpy) for p, (lo, hi) in periods.items()}
                rows.append(dict(bar=lab, rule=name, kind=kind, step=step, **{p: v for p, v in res.items()}))
    bh = {p: float(c1[np.searchsorted(t1, min(hi, t1[-1]), "right") - 1] / c1[np.searchsorted(t1, lo)] - 1)
          for p, (lo, hi) in periods.items()}
    print(f"buy & hold: design {bh['design']:+.0%}, validation {bh['val']:+.0%}, test {bh['test']:+.0%}")
    print(f"{len(rows)} rule variants at {FEE_US:.2%}/side. Positive in design: "
          f"{sum(r['design'][0] > 0 for r in rows)}, in validation: {sum(r['val'][0] > 0 for r in rows)}, both: "
          f"{sum(r['design'][0] > 0 and r['val'][0] > 0 for r in rows)}")
    ok = [r for r in rows if r["design"][0] > 0 and r["val"][0] > 0 and r["design"][1] > 0.3 and r["val"][1] > 0.3]
    ok.sort(key=lambda r: -min(r["design"][1], r["val"][1]))
    print("passing design + validation (top 12):")
    for r in ok[:12]:
        print(f"  {r['bar']} {r['rule']:12s} {r['kind']:5s} design {r['design'][0]:+6.0%} (Sh {r['design'][1]:.2f}, "
              f"{r['design'][2]:.0f} trades)  val {r['val'][0]:+5.0%} (Sh {r['val'][1]:.2f})")
    if len(sys.argv) > 2 and sys.argv[2] == "test":
        top = json.loads(FROZEN.read_text())["members"]
        pick = [r for r in rows if [r["bar"], r["rule"], r["kind"]] in top]
        t, c = t1, c1
        pos = np.zeros(len(t1))
        for r in pick:
            st = r["step"]
            tt, cc = t1[st - 1::st], c1[st - 1::st]
            lg, ls = rules(tt, cc)[r["rule"]]
            p = lg if r["kind"] == "long" else ls
            pos += np.r_[np.zeros(st - 1), np.repeat(p, st)][: len(t1)]  # known at the bar's last hour, held after
        pos /= len(pick)
        for fee in (FEE, FEE_US):
            for p_ in ("design", "val", "test"):
                lo, hi = periods[p_]
                tot, sh, n = evaluate(t, c, pos, fee, lo, hi, 365 * 24)
                print(f"COMBINED fee {fee:.2%} {p_:6s}: {tot:+.1%} Sharpe {sh:.2f} (turnover {n:.0f})")
        for r in pick:
            print(f"  member {r['bar']} {r['rule']} {r['kind']}: test {r['test'][0]:+.1%} Sh {r['test'][1]:.2f}")
    elif ok:
        FROZEN.write_text(json.dumps({"members": [[r["bar"], r["rule"], r["kind"]] for r in ok[:5]]}))
        print("frozen top 5:", [[r["bar"], r["rule"], r["kind"]] for r in ok[:5]])
    else:
        print("nothing passes design + validation")


if __name__ == "__main__":
    main()
