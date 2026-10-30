"""Round-2 batch-1 train-only killtests on existing data (no gates, no holdout; vault Strategies/Hypotheses/*).

    uv run python -m tradeslave.research.batch1_killtests pls1     # short new memecoin perps after listing
    uv run python -m tradeslave.research.batch1_killtests pmn1     # sell memecoin moonshot contracts (Polymarket)

Each prints the full pre-registered grid (no selection), n per cell, and the kill verdict from the hypothesis note.
"""

from __future__ import annotations

import json
import re
import statistics
import sys

from ..paths import DATA_DIR

TAKER = 0.0005        # Binance futures VIP0 taker per side (sourced); funding not in the data -> 0 (assumption)


def pls1() -> None:
    """Skeptic fixes: listings = series starting after 2023-01-02 (the dataset's start is not a listing), no BTC/ETH/SOL,
    funding from the data (`f`: [t, rate] per 8 h; a short receives +rate), a BTC-hedged column, results by month."""
    import bisect
    import time as _t
    load = lambda s: json.loads((DATA_DIR / "perps" / f"{s}.json").read_text())  # noqa: E731
    btc = load("BTC")["k"]
    bt = [x[0] for x in btc]
    rows = []
    for f in sorted((DATA_DIR / "perps").glob("*.json")):
        d = json.loads(f.read_text())
        if d["sym"] in ("BTC", "ETH", "SOL") or not d.get("k") or d["k"][0][0] <= 1672617600:
            continue
        rows.append(d)
    print(f"real listings (first candle after 2023-01-02, no BTC/ETH/SOL): {len(rows)}: "
          + ", ".join(f"{d['sym']} {_t.strftime('%Y-%m', _t.gmtime(d['k'][0][0]))}" for d in rows))
    for D in (1, 3, 7):
        for K in (7, 30):
            res = []
            for d in rows:
                k = d["k"]
                i0, i1 = 24 * D, 24 * (D + K)
                if i1 >= len(k):
                    continue
                t_in, t_out = k[i0][0], k[i1][0]
                p_in, p_out = k[i0][1], k[i1][1]
                fund = sum(r for t, r in d.get("f") or [] if t_in <= t < t_out)
                short = (p_in - p_out) / p_in + fund - 2 * TAKER
                j0, j1 = bisect.bisect_left(bt, t_in), bisect.bisect_left(bt, t_out)
                hedge = (btc[min(j1, len(btc) - 1)][1] / btc[min(j0, len(btc) - 1)][1] - 1) - 2 * TAKER
                res.append((short, short + hedge, _t.strftime("%Y-%m", _t.gmtime(t_in))))
            if res:
                sh = [x[0] for x in res]
                hd = [x[1] for x in res]
                by = {}
                for x in res:
                    by.setdefault(x[2][:7], []).append(x[0])
                print(f"D {D} K {K:>2}: n {len(res)}, short mean {statistics.mean(sh):+.1%} median {statistics.median(sh):+.1%} "
                      f"win {sum(r > 0 for r in sh) / len(sh):.0%} | BTC-hedged mean {statistics.mean(hd):+.1%} median "
                      f"{statistics.median(hd):+.1%} | months with mean > 0: {sum(statistics.mean(v) > 0 for v in by.values())}/{len(by)}")
    print("kill rule (prereg): mean < +10% or median < 0 (hedged column is the cleaner read); n ~31 can never pass 300")


MEME = re.compile(r"\b(bonk|wif|dogwifhat|pepe|pengu|fartcoin|trump coin|\$trump|doge|dogecoin|shib|shiba|pump\.fun|\$pump|"
                  r"popcat|mog|brett|floki|memecoin)\b", re.I)
TARGET = re.compile(r"(market ?cap|\$\s?\d|reach|above|hit|price)", re.I)


def pmn1() -> None:
    markets = {}
    with (DATA_DIR / "poly" / "markets_all.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            m = json.loads(line)
            q = m.get("q") or ""
            if MEME.search(q) and TARGET.search(q) and m.get("res") and len(m["res"]) == 2 and m.get("end"):
                markets[m["id"]] = m
    hist = {}
    with (DATA_DIR / "poly" / "hist_all.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            h = json.loads(line)
            if h["id"] in markets:
                hist[h["id"]] = h["h"]
    print(f"memecoin target markets with a resolution: {len(markets)}; with price history: {len(hist)}")
    for lo, hi in ((0.03, 0.10), (0.10, 0.20)):
        for days in (7, 30):
            rets = []
            for mid, h in hist.items():
                m = markets[mid]
                t_end = m.get("closed") or m["end"]
                yes_paid = float(m["res"][0])                  # res = [yes, no] payout (checked below)
                pt = next(((t, p) for t, p in h if t <= t_end - days * 86400 and lo <= p <= hi), None)
                if pt is None:
                    continue
                cost_no = 1 - pt[1] + 0.01                     # buy 'no' at 1 - yes + 1c slippage (assumption)
                rets.append(((1 - yes_paid) - cost_no) / cost_no)
            if rets:
                print(f"yes {lo:.2f}-{hi:.2f}, >= {days:>2} d before end: n {len(rets)}, mean {statistics.mean(rets):+.2%},"
                      f" median {statistics.median(rets):+.2%}, 'yes' won {sum(r < 0 for r in rets)}")
            else:
                print(f"yes {lo:.2f}-{hi:.2f}, >= {days} d: n 0")
    # sanity: res[0] is the yes payout if final yes prices sit near res[0]
    agree = [abs(h[-1][1] - float(markets[i]["res"][0])) < 0.2 for i, h in hist.items() if h]
    print(f"res[0] = yes payout check: last yes price within 0.2 of res[0] in {sum(agree)}/{len(agree)} markets")
    print("kill rule: mean < +10% per contract or fewer than 50 qualifying markets")


if __name__ == "__main__":
    {"pls1": pls1, "pmn1": pmn1}[sys.argv[1]]()
