"""Knowledge map for the Obsidian vault (D:/vault/tradeslave).

    uv run python -m tradeslave.vault_map

Writes `Map/` (one note per topic plus the master map), one note per known issue under `Codebase/Issues/`, and a
"Connections" block (between <!-- map:start --> and <!-- map:end -->) in every experiment, strategy, session,
project, codebase, review, research-theme and loose note. Re-running replaces the blocks, so it is safe to repeat.
Topic assignment is keyword-based plus the curated lineage below; review the map, don't treat it as ground truth.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from pathlib import Path

VAULT = Path(r"D:\vault\tradeslave")
START, END = "<!-- map:start -->", "<!-- map:end -->"
SKIP_DIRS = {".obsidian", "Templates", "Sources", "Map", ".trash", "Trades"}
SRC_DIR = VAULT / "Research" / "Runner research" / "Sources"
# trade data-point notes (Trades/Data points) -> words that place them in a topic
DP_TOPIC = {"Data - Narrative score": "narrative", "Data - X author followers": "KOL narrative",
            "Data - Holders at entry": "holders entry", "Data - Holder trend before entry": "holders entry",
            "Data - Run-up before entry": "entry dip", "Data - Market cap at entry": "entry bonding curve",
            "Data - Hour of entry": "regime hour of day", "Data - First buyers out before entry": "sniper holders",
            "Data - Create-slot buyers": "sniper bundle", "Data - Biggest seller while held": "dev creator exits",
            "Data - Sell vs buy flow while held": "volume net flow exits", "Data - First 10 s after entry": "entry exits",
            "Data - M5 model probability": "runner logistic", "Data - Exit reason": "exits stop"}

# topic: (description, keyword regex, research themes it maps to)
TOPICS = {
    "Entries": ("Where and when to buy: band touches, confirmation, pullbacks, dips, entry timing.",
                r"\bentr(y|ies)\b|pullback|\bdip\b|band touch|reclaim|limit buy|top.?blast", ["Entries"]),
    "Exits": ("Stops, take-profits, trailing, holder exits, stall/fade exits, partial sells.",
              r"\bexits?\b|stop.?loss|\bstops?\b|take.?profit|\bTP\b|trail|HDECL|stall|fade|half at", ["Exits and profit-taking"]),
    "Narrative and LLM scoring": ("Narrative quality, the LLM scorer (Gemini/qwen), X-post links, copy coins.",
                                  r"narrative|\bLLM\b|gemini|qwen|ollama|\bscore[sd]?\b|x post|tweet|meta\b",
                                  ["Narrative and meta", "Social virality"]),
    "Bots, snipers and bundles": ("Snipers, bundles, wash/volume bots, overhang, fake volume.",
                                  r"sniper|bundl|wash|volume bot|overhang|looper|bot.?wave|slot.?0|same.?slot",
                                  ["Bots, snipers and bundles"]),
    "Holders": ("Holder counts and growth, top-10/top-1 concentration, fresh wallets.",
                r"holders?\b|top.?10|top.?1\b|concentration|fresh wallet|new wallets?", ["Holder growth and distribution"]),
    "Dev and creator": ("Dev buys and sells, creator dumps, serial deployers, launching our own coins.",
                        r"\bdev\b|creator|deployer|dev.?sell|dev.?buy|dev_hold", ["Dev and creator behaviour"]),
    "Smart money and copy trading": ("Skilled/insider wallets, KOLs, copy trading, imitation.",
                                     r"smart.?(money|wallet)|copy.?trad|\bKOL|skilled trader|imitat|wallet signal|wallet feed",
                                     ["Smart money and KOL wallets"]),
    "Volume and flow": ("Buy/sell SOL flow, volume, buy pressure, big buys.",
                        r"\bvolume\b|net (SOL )?flow|buy pressure|buy SOL|buy10|buy30|big.?share|inflow", ["Volume and buy pressure"]),
    "Regime and timing": ("Hour of day, evening vs overnight, market heat, SOL price, mood.",
                          r"regime|overnight|evening|hour of day|time.of.day|market heat|market mood|17:00", ["Timing and market regime"]),
    "Graduation and migration": ("Bonding-curve progress, graduation, PumpSwap/Raydium, post-graduation trading.",
                                 r"graduat|migrat|pumpswap|raydium|bonding.?curve|post.?grad", ["Market cap stages and graduation"]),
    "Runner prediction": ("Telling runners apart early: RN features, the M5 logistic model, AUCs, labels.",
                          r"runner|\bAUC\b|logistic|separat|label", ["Early momentum and liquidity velocity", "Base rates and expectations"]),
    "Paper bots M1-M5": ("The live paper bots M1-M5 and their rule versions.",
                         r"\bM[1-5]\b|m[1-5]_bot|m[1-5]-v\d|paper bot", []),
    "Data, feed and RPC": ("Live feed, recorders, datasets, Helius/RPC providers, outages.",
                           r"\bRPC\b|helius|websocket|live feed|recorder|dataset|\bticks?\b|feed gap|quicknode|alchemy|shyft",
                           ["Tools and data sources"]),
    "Method and overfitting": ("Train/held-out splits, pre-registration, overfitting, leaks, forward tests.",
                               r"overfit|held.?out|out.of.sample|pre.?regist|in.?sample|validation|leak|forward test|holdout", []),
    "Execution and costs": ("Fees, slippage, own price impact, latency, fill realism.",
                            r"\bfees?\b|slippage|price impact|latency|\bfills?\b|priority fee|jito", []),
    "Creator launches (J)": ("Launching our own coins: J1/J2 dev-buy-and-sell.",
                             r"\bJ[12]\b|creator launch|launcher|creator economics", []),
    "Autopilot and ML": ("Self-learning autopilot, learned exits, neural/imitation models, shadow lanes.",
                         r"autopilot|neural|learned exit|champion|shadow lane|imitation|\bN5\b", []),
    "Other markets": ("Non-pump.fun work: perps, Kalshi, weather, prediction markets, listings, gas, BTC.",
                      r"kalshi|\bperps?\b|weather|polymarket|prediction.?market|listing|gas price|\bBTC\b|liquidation|maker reward",
                      []),
}

# curated lineage (both directions are added)
LINEAGE = [
    ("2026-10-21 M1 automated manual process", "2026-10-21 M2 rug-filtered M1"),
    ("2026-10-21 M2 rug-filtered M1", "2026-10-21 M3 post-mortem fixes"),
    ("2026-10-21 M3 post-mortem fixes", "2026-10-22 trade forensics and fix sweep (TF)"),
    ("2026-10-22 trade forensics and fix sweep (TF)", "2026-10-22 trade patterns (TF2)"),
    ("2026-10-22 trade patterns (TF2)", "2026-10-22 Rohit M4 notes (TF3)"),
    ("2026-10-22 Rohit M4 notes (TF3)", "2026-10-22 M4 variants backtest (MV)"),
    ("2026-10-22 M4 variants backtest (MV)", "2026-10-22 what separates runners (RN)"),
    ("2026-10-22 evening missed runners (MR)", "2026-10-22 what separates runners (RN)"),
    ("2026-10-22 what separates runners (RN)", "2026-10-22 M5 runner bot (M5)"),
    ("2026-10-22 M5 runner bot (M5)", "2026-10-23 sniper overhang test (OH)"),
    ("2026-10-22 M5 runner bot (M5)", "2026-10-23 buy quality and big-holder dumps (BQ)"),
    ("2026-10-22 M5 runner bot (M5)", "2026-10-23 creator dump veto (CV)"),
    ("2026-10-22 M5 runner bot (M5)", "2026-10-23 M5 entry redesign (ER)"),
    ("2026-10-23 sniper overhang test (OH)", "Runner research hub"),
    ("2026-10-23 M5 entry redesign (ER)", "Runner research hub"),
    ("2026-10-23 M5 entry redesign (ER)", "2026-10-23 M5 profit plan"),
    ("2026-10-23 morning brief", "2026-10-23 M5 profit plan"),
    ("2026-10-22 evening report", "2026-10-23 morning brief"),
    ("notes on m5 trades", "2026-10-22 M5 runner bot (M5)"),
    ("notes on m5 trades", "2026-10-23 M5 entry redesign (ER)"),
    ("notes on m1m2m3 trades", "2026-10-21 M1 automated manual process"),
    ("notes on m1m2m3 trades", "2026-10-21 M2 rug-filtered M1"),
    ("notes on m1m2m3 trades", "2026-10-21 M3 post-mortem fixes"),
    ("Notes on M4 trades", "2026-10-22 Rohit M4 notes (TF3)"),
    ("Notes on M4 trades", "2026-10-22 M4 variants backtest (MV)"),
    ("2026-10-17 creator economics", "J - Creator launches"),
    ("2026-10-18 perp shock reversal (R)", "R - Perp shock reversal"),
    ("2026-10-17 Kalshi 15-minute fair value", "M - Kalshi crypto fair value"),
    ("2026-10-18 imitation policy (N5)", "N - Imitate skilled traders"),
    ("2026-10-18 skilled traders and dip-runner", "N - Imitate skilled traders"),
    ("2026-10-17 graduated runners on candles", "H - Graduated runner trading"),
    ("2026-10-17 memecoin majors systematic", "I - Memecoin majors systematic"),
    ("2026-10-15 autopilot v3 first cycles", "F - Autopilot (self-learning)"),
    ("2026-10-16 first full day of v3", "F - Autopilot (self-learning)"),
    ("2026-10-17 forward-test autopsy and v5 fixes", "F - Autopilot (self-learning)"),
    ("2026-10-13 overnight run research", "E - Launch-dip scalper"),
    ("2026-10-12 entry watch 360s", "A - Post-launch momentum"),
    ("2026-10-15 wallet signal, launch sniping, market heat", "G - Selective launch sniping"),
    ("2026-10-14 money flows, copy trading, graduations", "B - Smart-wallet copy"),
    ("2026-10-21 post-graduation dip spec (PGD0)", "C - Migration trades"),
    ("2026-10-22 M5 runner bot (M5)", "2026-10-23 M6 bad-buy filters (M6)"),
    ("The path to profitability", "The path to profitability - evidence"),
    ("The path to profitability - evidence", "2026-10-23 M6 bad-buy filters (M6)"),
    ("The path to profitability", "Rug avoidance research (R4-R5)"),
    ("Rug avoidance research (R4-R5)", "2026-10-23 coin age gate (AG)"),
    ("Rug avoidance research (R4-R5)", "2026-10-23 funding links (FL)"),
    ("Rug avoidance research (R4-R5)", "2026-10-23 BOOST window (BW)"),
    ("2026-10-23 M6 bad-buy filters (M6)", "2026-10-23 coin age gate (AG)"),
    ("2026-10-23 sniper overhang test (OH)", "2026-10-23 M6 bad-buy filters (M6)"),
    ("2026-10-23 creator dump veto (CV)", "2026-10-23 funding links (FL)"),
    ("2026-10-21 post-graduation dip spec (PGD0)", "2026-10-23 BOOST window (BW)"),
    ("C - Migration trades", "2026-10-23 BOOST window (BW)"),
    ("Rug avoidance research (R4-R5)", "Runner research hub"),
    ("2026-10-23 coordinated-exit and serial-funder tests (CX)", "2026-10-23 exit anatomy - max loss and max profit (XA)"),
    ("2026-10-23 exit anatomy - max loss and max profit (XA)", "Max profit and max 25% loss - what the vault says"),
    ("The path to profitability", "Max profit and max 25% loss - what the vault says"),
    ("Max profit and max 25% loss - what the vault says", "M6 proposals (m6-v3)"),
    ("2026-10-22 fade exits (FX)", "2026-10-23 exit anatomy - max loss and max profit (XA)"),
    ("Rug avoidance research (R4-R5)", "Research synthesis R7 - avoiding the deep losses"),
    ("Research synthesis R7 - avoiding the deep losses", "Research R7 - source digest (S468-S789)"),
    ("Research synthesis R7 - avoiding the deep losses", "2026-10-23 coordinated-exit and serial-funder tests (CX)"),
    ("2026-10-23 funding links (FL)", "2026-10-23 coordinated-exit and serial-funder tests (CX)"),
    ("2026-10-23 coordinated-exit and serial-funder tests (CX)", "M6 proposals (m6-v3)"),
    ("Research synthesis R7 - avoiding the deep losses", "M6 proposals (m6-v3)"),
    ("2026-10-23 M6 bad-buy filters (M6)", "M6 proposals (m6-v3)"),
    ("Research synthesis R7 - avoiding the deep losses", "Runner research hub"),
    ("Brainstorm(running on 0 sleep)", "F - Autopilot (self-learning)"),
    ("Brainstorm(running on 0 sleep)", "2026-10-18 learned exits (U)"),
    ("Brainstorm(running on 0 sleep)", "2026-10-22 M5 runner bot (M5)"),
    ("Brainstorm(running on 0 sleep)", "2026-10-15 autopilot v3 first cycles"),
    ("trading bot", "Home"),
    ("trading bot", "Path to 1k (2026-10-19)"),
    ("trading bot", "Profit plan v2"),
    ("trading bot", "Idea bank 2026-10-19"),
    ("trading bot", "2026-10-21 M1 automated manual process"),
    ("Idea bank 2026-10-19", "Path to 1k (2026-10-19)"),
    ("notes on m5 trades", "The path to profitability"),
]


def body(text):
    return re.sub(re.escape(START) + r".*?" + re.escape(END), "", text, flags=re.S)


def links(text):
    return {l.split("|")[0].split("#")[0].strip() for l in re.findall(r"\[\[([^\]]+)\]\]", text)}


def issue_notes():
    """Split Codebase/Known issues.md into one note per issue; returns {issue note name: text}."""
    src = (VAULT / "Codebase" / "Known issues.md").read_text(encoding="utf-8")
    out = {}
    for m in re.finditer(r"^\| (\d+) \| (.+?) \| (.+?) \| (.+?) \|$", src, re.M):
        n, issue, status, phase = m.groups()
        out[f"Issue {int(n):02d}"] = (issue, f"- **Status:** {status}\n- **Phase:** {phase}\n\n{issue}")
    for m in re.finditer(r"^## #(\d+) (.+?)\n(.*?)(?=^## |\Z)", src, re.M | re.S):
        n, title, text = m.groups()
        out[f"Issue {int(n):02d}"] = (title.strip(), text.strip())
    d = VAULT / "Codebase" / "Issues"
    d.mkdir(exist_ok=True)
    notes = {}
    for name, (title, text) in sorted(out.items()):
        clean = re.sub(r"[\\/:*?\"<>|#^\[\]`]", "", title).replace("**", "")[:70].strip()
        full = f"{name} - {clean}"
        notes[full] = f"---\ntags: [issue]\n---\n# {name}: {title}\n\nFrom [[Known issues]].\n\n{text}\n"
        (d / f"{full}.md").write_text(notes[full] + f"\n{START}\n{END}\n", encoding="utf-8")
    return notes


def verdicts():
    """Experiment note -> its row's verdict (last column of Experiments.md)."""
    out = {}
    for line in (VAULT / "Experiments" / "Experiments.md").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\|.*?\[\[([^\]|]+)(?:\|[^\]]*)?\]\].*\|\s*([^|]+?)\s*\|\s*$", line)
        if m:
            out.setdefault(m.group(1).strip(), re.sub(r"\[\[([^\]|]+)(\|[^\]]*)?\]\]", r"\1", m.group(2))[:160])
    return out


def main() -> None:
    issues = issue_notes()
    notes = {}
    for p in VAULT.rglob("*.md"):
        if SKIP_DIRS & set(p.relative_to(VAULT).parts[:-1]) and "Issues" not in p.parts:
            continue
        if p.stem == "Experiments" or p.parent.name == "Templates":
            continue
        notes[p.stem] = p
    themes = {p.stem for p in (VAULT / "Research" / "Runner research" / "Themes").glob("*.md")}
    kind = lambda n: ("issue" if notes[n].parent.name == "Issues" else "theme" if n in themes else          # noqa: E731
                      notes[n].parent.name if notes[n].parent != VAULT else "note")
    text = {n: body(p.read_text(encoding="utf-8", errors="ignore")) for n, p in notes.items()}
    tops = {}
    for n, t in text.items():
        words = max(200, len(t.split()))
        hits = {k: len(re.findall(rx, t, re.I)) + 5 * len(re.findall(rx, n, re.I)) for k, (_, rx, _) in TOPICS.items()}
        need = 3 if len(t.split()) >= 300 else 1                 # short notes (Rohit's ideas) still get topics
        dens = {k: v / words * 1000 for k, v in hits.items() if v >= need}
        tops[n] = [k for k, _ in sorted(dens.items(), key=lambda x: -x[1])[:5]]
        if n in themes:
            tops[n] = sorted({k for k, v in TOPICS.items() if n in v[2]} | set(tops[n][:2]))
    freq = Counter(k for v in tops.values() for k in v)
    edges = defaultdict(set)
    for a, b in LINEAGE:
        if a in notes and b in notes:
            edges[a].add(b)
            edges[b].add(a)
    for n, t in text.items():
        for l in links(t):
            if l in notes and l != n:
                edges[n].add(l)
                edges[l].add(n)
    for n in notes:                                             # issue notes link to the notes that mention them
        if kind(n) == "issue":
            num = int(n.split()[1])
            for m_, t in text.items():
                if kind(m_) != "issue" and re.search(rf"(?<![\d.])#{num}\b", t) and "Known issues" not in m_:
                    edges[n].add(m_)
                    edges[m_].add(n)

    def related(n, k=8):
        sc = {}
        for m_ in notes:
            if m_ == n or kind(m_) == "theme" and kind(n) == "theme":
                continue
            shared = set(tops[n]) & set(tops[m_])
            s = sum(math.log(len(notes) / freq[x]) for x in shared) + (4 if m_ in edges[n] else 0)
            if len(shared) >= 2 or m_ in edges[n]:
                sc[m_] = s
        return [m_ for m_, _ in sorted(sc.items(), key=lambda x: -x[1])[:k]]

    # research sources (Research/Runner research/Sources): topics from their text and their theme links
    srcs = {p.stem: p for p in SRC_DIR.glob("S*.md")}
    stext = {s: body(p.read_text(encoding="utf-8", errors="ignore")) for s, p in srcs.items()}
    stops = {}
    for s, t in stext.items():
        words = max(80, len(t.split()))
        dens = {k: (len(re.findall(rx, t, re.I)) + 3 * len(re.findall(rx, s, re.I))) / words * 1000
                for k, (_, rx, _) in TOPICS.items()}
        by_theme = {k for k, v in TOPICS.items() if any(th in links(t) for th in v[2])}
        stops[s] = sorted(by_theme | {k for k, d in sorted(dens.items(), key=lambda x: -x[1])[:3] if d > 0})
    sfreq = Counter(k for v in list(tops.values()) + list(stops.values()) for k in v)
    total = len(notes) + len(srcs)
    idf = lambda ks: sum(math.log(total / sfreq[k]) for k in ks)                                         # noqa: E731
    dpts = {p.stem: p for p in (VAULT / "Trades" / "Data points").glob("Data - *.md")}
    dtops = {d: [k for k, (_, rx, _) in TOPICS.items() if re.search(rx, d + " " + DP_TOPIC.get(d, ""), re.I)]
             for d in dpts}

    def best(n_tops, pool, k, need=1):
        sc = {m_: idf(set(n_tops) & set(ts)) for m_, ts in pool.items()}
        return [m_ for m_, v in sorted(sc.items(), key=lambda x: (-x[1], x[0]))
                if v > 0 and len(set(n_tops) & set(pool[m_])) >= need][:k]

    note_src = {n: best(tops[n], stops, 8, need=2 if len(tops[n]) > 2 else 1) for n in notes
                if kind(n) in ("Experiments", "Strategies", "issue", "Sessions", "note", "Project", "Reviews",
                               "Runner research", "Data & Infra", "Codebase")}

    # trades (Trades/<bot>/*.md, written by trade_notes) -> the notes they link, so every note can link back
    trades = {p.stem: p for p in (VAULT / "Trades").glob("M*/*.md")}
    tcat, tret, cited = {}, {}, defaultdict(list)
    for tn, p in trades.items():
        tt = p.read_text(encoding="utf-8", errors="ignore")
        m_ = re.search(r"^category: (\w+)", tt, re.M)
        r_ = re.search(r"^ret: (-?[\d.]+)", tt, re.M)
        tcat[tn], tret[tn] = (m_.group(1) if m_ else "?"), (float(r_.group(1)) if r_ else 0.0)
        for lk in links(tt):
            cited[lk].append(tn)
    CAT_NOTE = {"win_big": "Trades - Win, 2x or more", "win": "Trades - Win", "gave_back": "Trades - Gave back a gain",
                "rug_gap": "Trades - Rug gap", "shaken_out": "Trades - Shaken out", "cut_winner": "Trades - Cut a winner",
                "instant_dump": "Trades - Instant dump", "slow_bleed": "Trades - Slow bleed"}
    TOPIC_DP = defaultdict(list)                                  # topic -> data-point notes
    for d, ts in dtops.items():
        for k in ts:
            TOPIC_DP[k].append(d)

    def trade_line(n, k=6):
        """'Trades: N cite this (category counts) · examples' for a note that trades link to, else None."""
        ts = cited.get(n, [])
        if not ts:
            return None
        cats = Counter(tcat[t] for t in ts)
        ex = sorted(ts, key=lambda t: tret[t])
        pick = list(dict.fromkeys(ex[:k // 2] + ex[::-1][:k - k // 2]))   # worst and best examples
        return (f"- **Trades ({len(ts)} link here):** "
                + ", ".join(f"[[{CAT_NOTE.get(c, c)}|{c.replace('_', ' ')}]] {v}" for c, v in cats.most_common())
                + " · e.g. " + ", ".join(f"[[{t}]]" for t in pick))

    vd = verdicts()
    md = VAULT / "Map"
    md.mkdir(exist_ok=True)
    for old in md.glob("*.md"):
        old.unlink()
    by_topic = defaultdict(list)
    for n, ts in tops.items():
        for k in ts:
            by_topic[k].append(n)
    order = ["Experiments", "Strategies", "issue", "Sessions", "Project", "Codebase", "Reviews", "Data & Infra", "note",
             "theme", "Runner research"]
    label = {"issue": "Known issues", "note": "Rohit's notes and loose notes", "theme": "Research themes (sources inside)",
             "Runner research": "Research hub"}
    for k, (desc, _, th) in TOPICS.items():
        lines = [f"---\ntags: [map]\n---\n# {k}\n\n{desc}\n\nPart of [[Memecoin bot knowledge map]]. "
                 f"Research themes: {', '.join(f'[[{t}]]' for t in th) or 'none'}.\n"]
        co = Counter(x for n in by_topic[k] for x in tops[n] if x != k)
        lines.append("**Overlaps most with:** " + ", ".join(f"[[Map - {x}]] ({c})" for x, c in co.most_common(5)) + "\n")
        groups = defaultdict(list)
        for n in by_topic[k]:
            groups[kind(n)].append(n)
        for g in sorted(groups, key=lambda g: order.index(g) if g in order else 99):
            lines.append(f"## {label.get(g, g)}")
            for n in sorted(groups[g]):
                lines.append(f"- [[{n}]]" + (f": {vd[n]}" if n in vd else ""))
            lines.append("")
        if TOPIC_DP.get(k):
            lines.append("## Trade data points")
            lines += [f"- [[{d}]]" for d in sorted(TOPIC_DP[k])]
            lines.append("- All trades: [[Trades index]]")
            lines.append("")
        ss = sorted(s for s, ts in stops.items() if k in ts)
        if ss:
            lines.append(f"## Research sources ({len(ss)})")
            lines += [f"- [[{s}|{s[:4]}]] {s[5:]}" for s in ss]
            lines.append("")
        (md / f"Map - {k}.md").write_text("\n".join(lines), encoding="utf-8")
    master = ["---\ntags: [map, moc]\n---\n# Memecoin bot knowledge map\n",
              "One map of everything: each topic note lists the experiments (with verdicts), strategies, known issues, "
              "sessions, research themes and Rohit's notes that touch it. Every note also carries a **Connections** "
              "block with its topics and its closest related notes. Rebuilt by `uv run python -m tradeslave.vault_map`.\n",
              "Start points: [[Home]] · [[Experiments]] · [[Known issues]] · [[Runner research hub]] · [[Roadmap]]"
              " · [[Trades index]] (every M1–M5 trade, why it won or lost)\n",
              "## Topics", ""]
    for k, (desc, _, _) in TOPICS.items():
        ex = sum(1 for n in by_topic[k] if kind(n) == "Experiments")
        master.append(f"- **[[Map - {k}]]**: {desc} ({len(by_topic[k])} notes, {ex} experiments)")
    master += ["", "## Experiment lineage (how one result led to the next)", ""]
    master += [f"- [[{a}]] → [[{b}]]" for a, b in LINEAGE if a in notes and b in notes]
    master += ["", "## Known issues by topic", ""]
    master += [f"- [[{n}]]: " + ", ".join(f"[[Map - {x}]]" for x in tops[n]) for n in sorted(notes) if kind(n) == "issue"]
    master += ["", "## Every experiment by topic", "", "| Experiment | Topics | Verdict |", "| --- | --- | --- |"]
    for n in sorted((n for n in notes if kind(n) == "Experiments")):
        master.append(f"| [[{n}]] | {', '.join(f'[[Map - {x}]]' for x in tops[n])} | {vd.get(n, '').replace('|', '/')} |")
    (md / "Memecoin bot knowledge map.md").write_text("\n".join(master) + "\n", encoding="utf-8")

    for n, p in notes.items():
        raw = p.read_text(encoding="utf-8", errors="ignore")
        blk = [START, "## Connections (auto map)",
               "- **Topics:** " + (", ".join(f"[[Map - {k}]]" for k in tops[n]) or "none") + " · [[Memecoin bot knowledge map]]"]
        rel = related(n)
        for g in ("Experiments", "Strategies", "issue", "theme"):
            xs = [m_ for m_ in rel if kind(m_) == g]
            if xs:
                blk.append(f"- **{label.get(g, g)}:** " + ", ".join(f"[[{m_}]]" for m_ in xs))
        rest = [m_ for m_ in rel if kind(m_) not in ("Experiments", "Strategies", "issue", "theme")]
        if rest:
            blk.append("- **Also related:** " + ", ".join(f"[[{m_}]]" for m_ in rest))
        if kind(n) == "theme":
            ex = [m_ for m_ in by_topic.get(next(iter(k for k, v in TOPICS.items() if n in v[2]), ""), [])
                  if kind(m_) == "Experiments"]
            if ex:
                blk.append("- **Our experiments on this theme:** " + ", ".join(f"[[{m_}]]" for m_ in sorted(ex)))
        if note_src.get(n):
            blk.append("- **Research sources:** " + ", ".join(f"[[{s}|{s[:4]}]]" for s in note_src[n]))
        dp = sorted({d for k in tops[n] for d in TOPIC_DP.get(k, [])})[:5]
        if dp and kind(n) != "theme":
            blk.append("- **Trade data points:** " + ", ".join(f"[[{d}]]" for d in dp) + " · [[Trades index]]")
        tl = trade_line(n)
        if tl:
            blk.append(tl)
        blk.append(END)
        block = "\n".join(blk)
        if START in raw:
            raw = re.sub(re.escape(START) + r".*?" + re.escape(END), lambda _: block, raw, flags=re.S)
        else:
            raw = raw.rstrip("\n") + "\n\n" + block + "\n"
        p.write_text(raw, encoding="utf-8")

    back = defaultdict(list)                                    # source -> notes that list it
    for n, ss in note_src.items():
        for s in ss:
            back[s].append(n)
    for s, p in srcs.items():
        ex = best(stops[s], {n: tops[n] for n in notes if kind(n) == "Experiments"}, 6)
        ex = sorted(set(ex) | {n for n in back[s] if kind(n) == "Experiments"})
        iss = sorted(set(best(stops[s], {n: tops[n] for n in notes if kind(n) == "issue"}, 3))
                     | {n for n in back[s] if kind(n) == "issue"})
        other = sorted({n for n in back[s] if kind(n) not in ("Experiments", "issue")})
        dp = best(stops[s], dtops, 4)
        sib = best(stops[s], {x: ts for x, ts in stops.items() if x != s}, 6, need=2 if len(stops[s]) > 1 else 1)
        blk = [START, "## Connections (auto map)",
               "- **Topics:** " + (", ".join(f"[[Map - {k}]]" for k in stops[s]) or "none")
               + " · [[Memecoin bot knowledge map]] · [[Runner research hub]]"]
        if ex:
            blk.append("- **Experiments:** " + ", ".join(f"[[{n}]]" for n in ex))
        if iss:
            blk.append("- **Known issues:** " + ", ".join(f"[[{n}]]" for n in iss))
        if dp:
            blk.append("- **Trade data points:** " + ", ".join(f"[[{d}]]" for d in dp) + " · [[Trades index]]")
        if other:
            blk.append("- **Notes that cite this topic:** " + ", ".join(f"[[{n}]]" for n in other))
        if sib:
            blk.append("- **Related sources:** " + ", ".join(f"[[{x}|{x[:4]}]]" for x in sib))
        tl = trade_line(s)
        if tl:
            blk.append(tl.replace("link here", "cite this source"))
        blk.append(END)
        block = "\n".join(blk)
        raw = p.read_text(encoding="utf-8", errors="ignore")
        if START in raw:
            raw = re.sub(re.escape(START) + r".*?" + re.escape(END), lambda _: block, raw, flags=re.S)
        else:
            raw = raw.rstrip("\n") + "\n\n" + block + "\n"
        p.write_text(raw, encoding="utf-8")
    print(f"{len(notes)} notes, {len(srcs)} sources, {len(issues)} issue notes, {len(TOPICS)} topics; topic sizes:",
          {k: len(v) for k, v in by_topic.items()})


if __name__ == "__main__":
    main()
