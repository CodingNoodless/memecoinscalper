"""Vault notes for the profitable-wallet study (WL): one note per wallet (strategy, stats, every feed trade with why it went
as it did), one note per entry / exit pattern linking trades with the same logic across wallets, and the study note.
Inputs: data/m1/wl_trades.json (research.wl_study), data/m1/wl/<wallet>.json (research.wl_fetch, 72 h own curve trades).

    uv run python -m tradeslave.wl_notes
"""

from __future__ import annotations

import json
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path

from .paths import DATA_DIR

V = Path(r"D:\vault\tradeslave")
WD = V / "Wallets"
PD = WD / "Patterns"
STUDY = "2026-10-24 profitable wallets study (WL)"
DROP = {"ARu4n5mFdZogZAravu7CcizaojWnS6oqka37gdLT5SZn"}   # its "profit" was tokens moved in from other wallets
ENTRY_DESC = {
    "launch burst": "Bought in the first 30 s of a coin, during a buying burst (net flow > 3 SOL in 30 s, buyer acceleration >= x2).",
    "early quiet": "Bought in the first 30 s, without a burst.",
    "dip buy": "Bought after 30 s while the price sat >= 25% below the coin's high so far, with buying coming back (net flow > 0).",
    "momentum breakout": "Bought after 30 s with the price >= 1.3x its 60 s low and buy/sell >= 1.3.",
    "late coin": "Bought a coin >= 5 min old, outside the patterns above.",
    "steady flow": "Bought after 30 s, none of the other conditions.",
}
EXIT_DESC = {
    "quick flip": "Sold within 15 s of the first buy.",
    "stop-out": "Sold at -15% or worse on the curve.",
    "sold near the top": "Sold within 10% of the peak since entry.",
    "sold when selling took over": "Sold > 10% off the peak, with >= 60% of the SOL in the previous 5 s being sells.",
    "sold on the fade": "Sold > 10% off the peak, before selling dominated.",
}


def pct(x):
    return f"{x:+.0%}" if abs(x) < 10 else f"{x:+.0f}x"


def pname(kind, p):
    return f"Pattern - {kind} - {p}"


def label(v):
    e = Counter(x["entry"] for x in v).most_common(1)[0][0]
    x_ = Counter(x["exit"] for x in v).most_common(1)[0][0]
    hold = statistics.median(x["hold"] for x in v)
    speed = "scalper" if hold < 20 else "fast trader" if hold < 60 else "swing trader"
    return f"{e} {speed}, {x_}"


def wname(w, v):
    return f"Wallet {w[:8]} - {label(v)}"


def main() -> None:
    X = [x for x in json.loads((DATA_DIR / "m1" / "wl_trades.json").read_text()) if x["w"] not in DROP]
    WD.mkdir(exist_ok=True)
    PD.mkdir(exist_ok=True)
    by = defaultdict(list)
    for x in X:
        by[x["w"]].append(x)
    names = {w: wname(w, v) for w, v in by.items()}
    for old in WD.glob("Wallet *.md"):
        old.unlink()
    for w, v in by.items():
        v.sort(key=lambda x: x["t"])
        full = DATA_DIR / "m1" / "wl" / f"{w}.json"
        h72 = ""
        if full.exists():
            j = json.loads(full.read_text())
            per = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
            for slot, ts, sig, m, b, sol, tok, vs, vt in j["trades"]:
                q = per[m]
                if b:
                    q[0] += sol
                    q[2] += tok
                else:
                    q[1] += sol
                    q[3] += tok
            rt = [q for q in per.values() if q[0] > 0 and 0.9 * q[2] <= q[3] <= 1.1 * q[2]]
            if rt:
                si, so = sum(q[0] for q in rt), sum(q[1] for q in rt)
                net = so * (1 - 0.0125) - si * (1 + 0.0125)
                h72 = (f"\n**Last 72 h on the bonding curve** (*measured*, Helius, all coins, not just the feed's): {len(j['trades'])} trades in "
                       f"{len(per)} coins; {len(rt)} clean round trips: spent {si:.1f} SOL, net after fees **{net:+.1f} SOL** "
                       f"({net / si:+.1%}), {sum(q[1] * (1 - 0.0125) > q[0] * (1 + 0.0125) for q in rt) / len(rt):.0%} won"
                       + (f" ({j['capped_windows']} history windows hit the page cap, so this undercounts)" if j["capped_windows"] else "") + ".\n")
        W_ = [x for x in v if x["own"] > 0]
        L_ = [x for x in v if x["own"] <= 0]
        ent = Counter(x["entry"] for x in v)
        ext = Counter(x["exit"] for x in v)
        med = lambda xs, k: statistics.median(x["f"][k] for x in xs) if xs else float("nan")  # noqa: E731
        md = [f"---\ntype: wallet\nwallet: {w}\nstudy: {STUDY}\ncreated: 2026-10-24\n---\n# {names[w]}\n",
              f"**Address:** `{w}` · Study: [[{STUDY}]]\n",
              f"**Strategy (from its trades):** {label(v)}. Median entry {statistics.median(x['f']['age_s'] for x in v):.0f} s after launch, "
              f"median market cap {statistics.median(x['f']['mcap_sol'] for x in v):.0f} SOL, median hold {statistics.median(x['hold'] for x in v):.0f} s, "
              f"median first buy {statistics.median(x['si'] / max(1, x['n_buys']) for x in v):.2f} SOL, {statistics.median(x['n_buys'] for x in v):.0f} buy(s) "
              f"and {statistics.median(x['n_sells'] for x in v):.0f} sell(s) per coin.",
              h72,
              f"**In our feed** (*measured*, {len(v)} clean round trips, {time.strftime('%b %d %H:%M', time.localtime(v[0]['t']))}–"
              f"{time.strftime('%b %d %H:%M', time.localtime(v[-1]['t']))} PT): mean **{statistics.mean(x['own'] for x in v):+.1%}** a trade after fees, "
              f"won {len(W_) / len(v):.0%}; runners (coin reached 2x within 30 min of its entry) {sum(x['runner'] for x in v) / len(v):.0%}.\n",
              f"**Copying it 1 s late** (*measured*): with its own exits {statistics.mean(x['copy_his_exit'] for x in v):+.1%}, "
              f"with TP 50% / stop 20% {statistics.mean(x['copy_tp50_sl20'] for x in v):+.1%}, holding 30 s {statistics.mean(x['copy_hold30'] for x in v):+.1%}.\n",
              "## Entry and exit patterns", "| Entry pattern | Trades | Its mean | Won | | Exit pattern | Trades | Its mean | Won |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        el, xl = ent.most_common(), ext.most_common()
        for i in range(max(len(el), len(xl))):
            a = el[i] if i < len(el) else None
            b = xl[i] if i < len(xl) else None
            fa = (f"[[{pname('entry', a[0])}\\|{a[0]}]] | {a[1]} | {statistics.mean(x['own'] for x in v if x['entry'] == a[0]):+.1%} | "
                  f"{sum(x['own'] > 0 for x in v if x['entry'] == a[0]) / a[1]:.0%}") if a else " | | |"
            fb = (f"[[{pname('exit', b[0])}\\|{b[0]}]] | {b[1]} | {statistics.mean(x['own'] for x in v if x['exit'] == b[0]):+.1%} | "
                  f"{sum(x['own'] > 0 for x in v if x['exit'] == b[0]) / b[1]:.0%}") if b else " | | |"
            md.append(f"| {fa} | | {fb} |")
        md += ["", "## Its winners vs its losers (medians at entry)", "| | Winners | Losers |", "| --- | --- | --- |"]
        for k, lab in (("age_s", "Coin age (s)"), ("mcap_sol", "Market cap (SOL)"), ("buyers_30", "Buyers in 30 s"), ("accel", "Buyer acceleration"),
                       ("flow_30", "Net flow 30 s (SOL)"), ("from_lo60", "Price vs 60 s low"), ("off_peak", "Price vs high so far")):
            md.append(f"| {lab} | {med(W_, k):.2f} | {med(L_, k):.2f} |")
        md.append(f"| Peak after entry | {statistics.median(x['peak'] for x in W_) if W_ else float('nan'):+.0%} | "
                  f"{statistics.median(x['peak'] for x in L_) if L_ else float('nan'):+.0%} |")
        md.append(f"| Hold (s) | {statistics.median(x['hold'] for x in W_) if W_ else float('nan'):.0f} | "
                  f"{statistics.median(x['hold'] for x in L_) if L_ else float('nan'):.0f} |")
        md += ["", f"## Every trade in the feed ({len(v)})",
               "Why = the coin state when it bought, what the price did, and how it sold (auto-written from the trade data).", "",
               "| Time (PT) | Coin | Entry | Exit | Result | Copy +1 s (TP50/SL20) | Why |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for x in v:
            md.append(f"| {time.strftime('%m-%d %H:%M:%S', time.localtime(x['t']))} | `{x['m'][:8]}` | {x['entry']} | {x['exit']} | "
                      f"**{pct(x['own'])}** | {pct(x['copy_tp50_sl20'])} | {'won' if x['own'] > 0 else 'lost'}: {x['why'].replace('|', '/')} |")
        (WD / f"{names[w]}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    for kind, desc in (("entry", ENTRY_DESC), ("exit", EXIT_DESC)):
        for p, d in desc.items():
            v = [x for x in X if x[kind] == p]
            if not v:
                continue
            md = [f"---\ntype: trade-pattern\nkind: {kind}\ncreated: 2026-10-24\n---\n# {pname(kind, p)}\n", f"**Rule:** {d}\n",
                  f"Study: [[{STUDY}]]\n",
                  f"**Trades with this logic:** {len(v)} from {len({x['w'] for x in v})} wallets. Their mean **{statistics.mean(x['own'] for x in v):+.1%}**, "
                  f"won {sum(x['own'] > 0 for x in v) / len(v):.0%}; runners {sum(x['runner'] for x in v) / len(v):.0%}. A 1 s copy: their exits "
                  f"{statistics.mean(x['copy_his_exit'] for x in v):+.1%}, TP50/SL20 {statistics.mean(x['copy_tp50_sl20'] for x in v):+.1%}, hold 30 s "
                  f"{statistics.mean(x['copy_hold30'] for x in v):+.1%} (*measured*).\n",
                  "## By wallet", "| Wallet | Trades | Its mean | Won |", "| --- | --- | --- | --- |"]
            for w, n in Counter(x["w"] for x in v).most_common():
                q = [x for x in v if x["w"] == w]
                md.append(f"| [[{names[w]}\\|{w[:8]}]] | {n} | {statistics.mean(x['own'] for x in q):+.1%} | {sum(x['own'] > 0 for x in q) / n:.0%} |")
            md += ["", "## The 15 best and 15 worst trades with this logic", "| Wallet | Time | Result | Why |", "| --- | --- | --- | --- |"]
            for x in sorted(v, key=lambda x: -x["own"])[:15] + sorted(v, key=lambda x: x["own"])[:15]:
                md.append(f"| [[{names[x['w']]}\\|{x['w'][:8]}]] | {time.strftime('%m-%d %H:%M', time.localtime(x['t']))} | {pct(x['own'])} | {x['why'].replace('|', '/')} |")
            (PD / f"{pname(kind, p)}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"{len(by)} wallet notes, {len(list(PD.glob('*.md')))} pattern notes")


if __name__ == "__main__":
    main()
