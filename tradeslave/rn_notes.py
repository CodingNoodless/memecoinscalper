"""Vault notes for the RN1000 runner study: one note per curve runner (hand-written write-ups from data/runners/writeups.json
plus its facts), hub notes for every chart pattern / narrative category / attention source, and the study note.

    uv run python -X utf8 -m tradeslave.rn_notes
"""

from __future__ import annotations

import json
import re
import statistics
import time
from collections import defaultdict
from pathlib import Path

from .paths import DATA_DIR

V = Path(r"D:\vault\tradeslave")
RD = V / "Runners"
HD = RD / "Hubs"
STUDY = "2026-10-24 the last 1,000 runners (RN1000)"
DETECT = "2026-10-24 runner detection on 1,000 real runners (RD)"
OUT = DATA_DIR / "runners"


def k(x):
    return "?" if x is None else f"${x / 1e6:.2f}M" if x >= 1e6 else f"${x / 1e3:.0f}k"


def safe(s):
    return re.sub(r"\s+", " ", re.sub(r"[\\/:*?\"<>|#^\[\]]", "", str(s or "")))[:50].strip() or "unnamed"


def hub(title):
    """Hub note name, safe as a Windows file name and a wikilink."""
    t = title.replace(" / ", " & ").replace(">=", "at least").replace("<", "under").replace(">", "over")
    return re.sub(r'[\/:*?"<>|#^\[\]]', "", t).strip()


def attention_tags(f):
    L, tags = f.get("launch") or {}, []
    fol = f.get("x_followers")
    if f.get("x_info") and "could not be read" not in (f.get("x_info") or ""):
        tags.append("X post by a 10k+ account" if (fol or 0) >= 10_000 else "X post by a 1k-10k account" if (fol or 0) >= 1_000 else "X post by a small account")
    elif f.get("twitter"):
        tags.append("X link (unreadable)")
    if not (f.get("twitter") or f.get("website") or f.get("telegram")):
        tags.append("No socials at all")
    if f.get("website") and f.get("twitter") and f.get("telegram"):
        tags.append("Full brand rollout (X + site + TG)")
    if (L.get("snipe_share") or 0) >= 0.3:
        tags.append("Sniper-heavy launch (>= 30% of supply)")
    elif (L.get("snipers") or 0) <= 3 and (L.get("buyers_5m") or 0) >= 40:
        tags.append("Organic launch (few snipers, 40+ early buyers)")
    if (L.get("dev_buy") or 0) >= 5:
        tags.append("Big dev buy (>= 5 SOL)")
    if (L.get("early_callers") or 0) > 0:
        tags.append("Top caller in the early buyers")
    if L.get("tracked"):
        tags.append("Tracked wallet in the early buyers")
    C = f.get("chart") or {}
    if C.get("vol_48h") and f.get("ath") and C["vol_48h"] < 0.15 * f["ath"]:
        tags.append("Thin or inflated market (48 h volume under 15% of ATH)")   # a few wallets set the price; not real demand
    ch = f.get("curve_h")
    if ch is not None:
        tags.append("Fast curve (< 15 min)" if ch < 0.25 else "Long curve (> 6 h)" if ch > 6 else "Normal curve (15 min - 6 h)")
    return tags


def note_name(f):
    return f"Runner {safe(f.get('sym'))} - {safe(f.get('name'))} ({f['m'][:6]})"


def main() -> None:
    F = json.loads((OUT / "facts.json").read_text(encoding="utf-8"))
    W = json.loads((OUT / "writeups.json").read_text(encoding="utf-8")) if (OUT / "writeups.json").exists() else {}
    F = [f for f in F if f["m"] in W]
    RD.mkdir(parents=True, exist_ok=True)
    HD.mkdir(exist_ok=True)
    for old in RD.glob("Runner *.md"):
        old.unlink()
    hubs = defaultdict(list)                     # hub note name -> runners
    for f in F:
        w = W[f["m"]]
        L, C = f.get("launch") or {}, f.get("chart") or {}
        cat = w.get("category") or f["category"]
        reliable = (C.get("vol_48h") or 0) >= 50_000 and (C.get("n") or 0) >= 3
        pat = C.get("pattern") if reliable else "Chart data unreliable"
        tags = attention_tags(f)
        f.update(_cat=cat, _pat=pat, _tags=tags, _name=note_name(f))
        hubs[hub(f"Runner pattern - {pat}")].append(f)
        hubs[hub(f"Runner category - {cat}")].append(f)
        for t in tags:
            hubs[hub(f"Runner attention - {t}")].append(f)
        links = " · ".join([f"[[{hub('Runner pattern - ' + pat)}|{pat}]]", f"[[{hub('Runner category - ' + cat)}|{cat}]]"]
                           + [f"[[{hub('Runner attention - ' + t)}|{t}]]" for t in tags])
        md = [f"---\ntype: runner\nmint: {f['m']}\ncategory: {cat}\npattern: {pat}\nath_usd: {round(f.get('ath') or 0)}\n"
              f"created: {time.strftime('%Y-%m-%d %H:%M', time.localtime(f['created'])) if f.get('created') else ''}\n"
              f"migrated: {time.strftime('%Y-%m-%d %H:%M', time.localtime(f['t_mig']))}\n---\n",
              f"# {f.get('name')} (${f.get('sym')})\n",
              f"Study: [[{STUDY}]] · Detection test: [[{DETECT}]]\n",
              f"**Tags:** {links}\n",
              "| | |", "| --- | --- |",
              f"| Mint | `{f['m']}` |",
              f"| Created → migrated | {time.strftime('%b %d %H:%M', time.localtime(f['created'])) if f.get('created') else '?'} → "
              f"{time.strftime('%b %d %H:%M', time.localtime(f['t_mig']))} ({f['curve_h']:.1f} h on the curve) |",
              f"| Start market cap | {k(f.get('start_mc'))} |",
              f"| All-time high | {k(f.get('ath'))} |",
              f"| After migration | {k(C.get('mig_mc'))} → peak {k(C.get('peak_mc'))} after {C.get('to_peak_h', 0):.1f} h → {k(C.get('last_mc'))} at 48 h ({C.get('last_vs_peak', 0):.0%} of peak) |" if reliable else "| After migration | chart data unreliable |",
              f"| Time at or above $300k (5-min closes) | {C.get('mins_above_300k', 0)} min |" if reliable else "",
              f"| 48 h volume | {k(C.get('vol_48h'))} |" if reliable else "",
              f"| Launch | dev buy {L.get('dev_buy', 0):.2f} SOL · launch-second snipers {L.get('snipers')} ({L.get('snipe_share', 0):.0%} of supply) · "
              f"{L.get('buyers_5m')} buyers and {L.get('flow_5m', 0):+.1f} SOL net in the first 5 min |",
              f"| Socials | {'X' if f.get('twitter') else '-'} · {'site' if f.get('website') else '-'} · {'Telegram' if f.get('telegram') else '-'} |",
              "", "## Why it went viral", w.get("why", ""), "", "## What brought the attention", w.get("attention", ""), "",
              "## Chart", w.get("chart", ""), ""]
        if f.get("desc"):
            md += ["## Description (the coin's own)", "> " + f["desc"].replace("\n", " ")[:600], ""]
        if f.get("x_info") and "could not be read" not in f["x_info"]:
            md += ["## Linked X post (as read on collection)", "> " + f["x_info"].replace("\n", " ")[:700], ""]
        (RD / f"{f['_name']}.md").write_text("\n".join(x for x in md if x is not None) + "\n", encoding="utf-8")
    # hub notes: every runner sharing a pattern / category / attention source, with group stats
    for h, fs in hubs.items():
        def med(key, src="chart"):
            v = [((x.get(src) or {}) if src else x).get(key) for x in fs]
            v = [a for a in v if isinstance(a, (int, float))]
            return statistics.median(v) if v else None
        held = med("last_vs_peak")
        md = [f"---\ntype: runner-hub\n---\n# {h}\n", f"Study: [[{STUDY}]]\n",
              f"**{len(fs)} runners.** Medians: ATH {k(statistics.median([x.get('ath') or 0 for x in fs]))}, "
              f"time to peak after migration {med('to_peak_h') or 0:.1f} h, kept {held or 0:.0%} of peak at 48 h, "
              f"time on the curve {statistics.median([x['curve_h'] for x in fs if x.get('curve_h') is not None] or [0]):.1f} h.\n",
              "| Runner | ATH | Peak after | Kept at 48 h | Pattern | Category |", "| --- | --- | --- | --- | --- | --- |"]
        for x in sorted(fs, key=lambda x: -(x.get("ath") or 0)):
            C = x.get("chart") or {}
            md.append(f"| [[{x['_name']}\\|{safe(x.get('sym'))}]] | {k(x.get('ath'))} | {C.get('to_peak_h', 0):.1f} h | {C.get('last_vs_peak', 0):.0%} | {x['_pat']} | {x['_cat']} |")
        (HD / f"{h}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"{len(F)} runner notes, {len(hubs)} hub notes")


if __name__ == "__main__":
    main()
