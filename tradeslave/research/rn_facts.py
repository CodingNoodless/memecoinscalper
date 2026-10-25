"""RN1000 fact sheets: everything known about each curve runner, for hand-written vault notes (no LLM).
Inputs (data/runners/): runners.json, details.json, cc/positive/<mint>.json (first 300 curve trades), candles/<mint>.json.
Output: data/runners/facts.json  (one dict per runner: launch, attention, chart, labels)

    uv run python -m tradeslave.research.rn_facts
"""

from __future__ import annotations

import json
import re
import statistics
import time

from ..paths import DATA_DIR

OUT = DATA_DIR / "runners"
CATS = [  # narrative category: first match on name + ticker + description + X post text
    ("AI / agent", r"\b(ai|agent|gpt|llm|bot|neural|grok|claude|openai|chatgpt|deepseek|gemini|robot|agi)\b"),
    ("Political", r"\b(trump|maga|biden|kamala|election|president|white house|congress|senate|politic|vance|musk doge)\b"),
    ("Celebrity / influencer", r"\b(elon|musk|mrbeast|kanye|ye\b|drake|taylor|swift|kardashian|streamer|kai cenat|speed|celebrity)\b"),
    ("Animal", r"\b(dog|doge|shib|cat|kitty|frog|pepe|monkey|ape|penguin|hamster|bird|fish|bear|bull|goat|horse|duck|wolf|fox|pig|cow|rat|capybara|otter|seal|panda|tiger|lion|dino)\b"),
    ("News / event", r"\b(news|breaking|today|announce|launch|event|war|hack|lawsuit|arrest|fed|rate|crash|ceo|acquir|listing)\b"),
    ("Charity / cause", r"\b(charity|donat|cause|save|fund|help|cancer|shelter|rescue|community[- ]owned)\b"),
    ("Crypto meta", r"\b(pump|sol|solana|bonk|memecoin|degen|ct\b|crypto|bitcoin|btc|eth|airdrop|launchpad|bags|trench)\b"),
    ("Culture / meme", r"\b(meme|lol|based|chad|sigma|wojak|npc|vibe|cope|gm\b|wagmi|ngmi|viral|tiktok|trend)\b"),
]
TRACK = {"4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9": "decu"}


def category(text):
    t = (text or "").lower()
    return next((c for c, rx in CATS if re.search(rx, t)), "Other / unclear")


def callers():
    for f in sorted((DATA_DIR / "callouts").glob("leaderboard_*.json"), reverse=True):
        try:
            return {w for u in json.loads(f.read_text(encoding="utf-8"))["callouts"] for w in u["wallets"] if not w.startswith("0x")}
        except (ValueError, KeyError):
            continue
    return set()


def chart(c, t_mig):
    """Post-migration shape from 5-minute USD candles (market cap = price x 1B supply)."""
    cs = [x for x in c if x[0] >= t_mig - 300]
    if len(cs) < 3:
        return dict(pattern="no candles", n=len(cs))
    mc = lambda x: x * 1e9  # noqa: E731
    o = mc(cs[0][1])
    hi = [mc(x[2]) for x in cs]
    k = max(range(len(cs)), key=lambda i: hi[i])
    peak, t_peak = hi[k], cs[k][0]
    last = mc(cs[-1][4])
    vol = sum(x[5] for x in cs)
    legs, run_hi, low_since = 0, o, o           # higher highs, each after a >= 40% pullback
    for x in cs:
        h, lo = mc(x[2]), mc(x[3])
        if h > run_hi * 1.3 and low_since <= run_hi * 0.6:
            legs += 1
        if h > run_hi:
            run_hi, low_since = h, h
        low_since = min(low_since, lo)
    dd_after = min((mc(x[3]) for x in cs[k:]), default=peak) / peak
    to_peak_h = (t_peak - t_mig) / 3600
    above = sum(1 for x in cs if mc(x[4]) >= 300_000) * 5
    if to_peak_h <= 0.5 and last <= 0.3 * peak:
        pat = "Instant send, then dump"
    elif to_peak_h <= 2 and last >= 0.5 * peak:
        pat = "Send and hold"
    elif legs >= 2:
        pat = "Staircase (multiple legs)"
    elif legs == 1:
        pat = "Second leg after a pullback"
    elif to_peak_h >= 12:
        pat = "Slow grind"
    elif last <= 0.2 * peak:
        pat = "Pump and fade"
    else:
        pat = "Single run, partial hold"
    return dict(pattern=pat, mig_mc=o, peak_mc=peak, to_peak_h=to_peak_h, last_mc=last, last_vs_peak=last / peak, legs=legs,
                dd_after=dd_after, vol_48h=vol, mins_above_300k=above, n=len(cs), hours=(cs[-1][0] - cs[0][0]) / 3600)


def launch(rows, created):
    if not rows:
        return {}
    rows = sorted(rows, key=lambda r: (r[0] or 0, r[1]))
    t0, cr = rows[0][1], rows[0][2]
    first_sec = [r for r in rows if r[1] <= t0]
    dev_buy = sum(r[4] for r in rows if r[2] == cr and r[3] and r[1] <= t0 + 2)
    snipe = [r for r in rows if r[3] and r[2] != cr and r[1] <= t0 + 2]
    start = first_sec[-1][6] / first_sec[-1][7] if first_sec[-1][7] else None
    m5 = [r for r in rows if r[1] <= t0 + 300]
    span_s = rows[-1][1] - t0
    buyers5 = len({r[2] for r in m5 if r[3]})
    flow5 = sum(r[4] if r[3] else -r[4] for r in m5)
    cw = callers()
    early_callers = len({r[2] for r in rows if r[3] and r[2] in cw})
    tracked = sorted({TRACK[r[2]] for r in rows if r[3] and r[2] in TRACK})
    return dict(dev_buy=dev_buy, snipe_sol=sum(r[4] for r in snipe), snipers=len({r[2] for r in snipe}),
                snipe_share=sum(r[5] for r in snipe) / 1e9, start_px=start, buyers_5m=buyers5, flow_5m=flow5,
                trades_seen=len(rows), seen_span_min=span_s / 60, early_callers=early_callers, tracked=tracked,
                creator=cr)


def main() -> None:
    R = json.loads((OUT / "runners.json").read_text())
    D = json.loads((OUT / "details.json").read_text(encoding="utf-8")) if (OUT / "details.json").exists() else {}
    out = []
    for r in R:
        m = r["m"]
        d = D.get(m) or {}
        coin, meta, x = d.get("coin") or {}, d.get("meta") or {}, d.get("x") or {}
        cv = OUT / "cc" / "positive" / f"{m}.json"
        rows = json.loads(cv.read_text())["rows"] if cv.exists() else []
        cp = OUT / "candles" / f"{m}.json"
        cs = json.loads(cp.read_text())["c"] if cp.exists() else []
        desc = (meta.get("description") or coin.get("description") or "")[:600]
        text = " ".join(filter(None, [coin.get("name"), coin.get("symbol"), desc, x.get("info")]))
        f = dict(m=m, name=coin.get("name"), sym=coin.get("symbol"), desc=desc, created=r.get("created"), t_mig=r["t"],
                 curve_h=(r["t"] - r["created"]) / 3600 if r.get("created") else None, ath=r.get("ath"), start_mc=r.get("start_mc"),
                 ath_t=(coin.get("ath_market_cap_timestamp") or 0) / 1000 or None, twitter=meta.get("twitter"), website=meta.get("website"),
                 telegram=meta.get("telegram"), x_info=x.get("info"), category=category(text), charity=coin.get("is_charity"),
                 agent=bool(coin.get("tokenized_agent")), live_now=coin.get("is_currently_live"), mc_now=coin.get("market_cap_usd"),
                 launch=launch(rows, r.get("created")), chart=chart(cs, r["t"]))
        m_follow = re.search(r"([\d,]+) followers", x.get("info") or "")
        f["x_followers"] = int(m_follow.group(1).replace(",", "")) if m_follow else None
        h = re.search(r"Post by @([A-Za-z0-9_]+)", x.get("info") or "") or re.search(r"Profile @([A-Za-z0-9_]+)", x.get("info") or "")
        f["x_author"] = h.group(1) if h else None
        out.append(f)
    (OUT / "facts.json").write_text(json.dumps(out), encoding="utf-8")
    from collections import Counter
    print(f"{len(out)} fact sheets | patterns {Counter(f['chart'].get('pattern') for f in out).most_common()} | categories {Counter(f['category'] for f in out).most_common()}")
    print(f"with X post {sum(bool(f['x_info']) for f in out)}, candles {sum(f['chart'].get('n', 0) >= 3 for f in out)}, launch data {sum(bool(f['launch']) for f in out)}, "
          f"median curve time {statistics.median([f['curve_h'] for f in out if f['curve_h'] is not None]):.1f} h | built {time.strftime('%H:%M')}")


if __name__ == "__main__":
    main()
