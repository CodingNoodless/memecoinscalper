"""One vault note per closed M1-M5 paper trade, with why it won or lost and what could have gone better.

    uv run python -m tradeslave.trade_notes

Writes D:/vault/tradeslave/Trades/<bot>/<note>.md, a per-bot index and Trades/Trades index.md, then links them into
the knowledge map. Everything in a note is computed from our own records: the bot's trade record, the coin's curve
trades (Helius ticks in data/m1/ticks for set-A coins, the live feed for coins since 2026-10-22 15:19) and the
narrative scorer's output. The explanations are rule-generated from those numbers, not hand-written; a trade whose
curve trades we don't have says so and is explained from its record only.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from .paths import DATA_DIR
from .research.live_feed import DB as FEED_DB
from .research.tf_replay import path

VAULT = Path(r"D:\vault\tradeslave")
OUT = VAULT / "Trades"
START, END = "<!-- map:start -->", "<!-- map:end -->"
SIZE, COST = 0.1, 0.03                       # 0.1 SOL paper size; ~2.5% curve fees + tx fees per round trip
BOTS = {
    "m1": ("M1", ["2026-10-21 M1 automated manual process"], "notes on m1m2m3 trades"),
    "m2": ("M2", ["2026-10-21 M2 rug-filtered M1", "2026-10-21 M1 automated manual process"], "notes on m1m2m3 trades"),
    "m3": ("M3", ["2026-10-21 M3 post-mortem fixes", "2026-10-21 M1 automated manual process"], "notes on m1m2m3 trades"),
    "m4": ("M4", ["2026-10-22 M4 variants backtest (MV)", "2026-10-22 Rohit M4 notes (TF3)"], "Notes on M4 trades"),
    "m5": ("M5", ["2026-10-22 M5 runner bot (M5)", "2026-10-23 M5 entry redesign (ER)"], "notes on m5 trades"),
    "m6": ("M6", ["2026-10-23 M6 bad-buy filters (M6)", "2026-10-22 M5 runner bot (M5)"], "The path to profitability"),
    "m7": ("M7", ["2026-10-23 M7 vs M8 forward test (m6-v3 entries, two exit sets)", "M6 proposals (m6-v3)"],
           "Max profit and max 25% loss - what the vault says"),
    "m9": ("M9", ["2026-10-23 pattern search for M9 (PX)", "2026-10-23 M9 design test - M1 + M5 entries with loss controls (M9)", "2026-10-21 M1 automated manual process",
                  "2026-10-22 M5 runner bot (M5)"], "Max profit and max 25% loss - what the vault says"),
    "m12": ("M12", ["2026-10-24 M12 - M8 with hype context (M12)", "2026-10-23 M7 vs M8 forward test (m6-v3 entries, two exit sets)"], "Max profit and max 25% loss - what the vault says"),
    "m12r": ("M12r", ["2026-10-24 M12 - M8 with hype context (M12)"], "Max profit and max 25% loss - what the vault says"),
    "m13": ("M13", ["2026-10-25 M13 - runner model at migration (M13)", "2026-10-24 runner detection on 1,000 real runners (RD)",
                    "2026-10-24 the last 1,000 runners (RN1000)"], "Max profit and max 25% loss - what the vault says"),
    "m13b": ("M13b", ["2026-10-25 M13 - runner model at migration (M13)"], "Max profit and max 25% loss - what the vault says"),
    "m11": ("M11", ["2026-10-24 M11 - leader wave rider (M11)", "2026-10-24 learning decu's style (DC)"], "Max profit and max 25% loss - what the vault says"),
    "m10": ("M10", ["2026-10-23 M10 flagship model - backtest (M10)"], "Max profit and max 25% loss - what the vault says"),
    "m9e": ("M9e", ["2026-10-23 pattern search for M9 (PX)"], "Max profit and max 25% loss - what the vault says"),
    "m8": ("M8", ["2026-10-23 M7 vs M8 forward test (m6-v3 entries, two exit sets)", "M6 proposals (m6-v3)"],
           "Max profit and max 25% loss - what the vault says"),
}
# Rohit's idea / brainstorm notes and research write-ups each outcome category speaks to
IDEAS = {"rug_gap": ["The path to profitability", "The path to profitability - evidence", "Rug avoidance research (R4-R5)"],
         "instant_dump": ["The path to profitability", "Rug avoidance research (R4-R5)", "Brainstorm(running on 0 sleep)"],
         "slow_bleed": ["Brainstorm(running on 0 sleep)", "trading bot"],
         "gave_back": ["trading bot", "Idea bank 2026-10-19"], "shaken_out": ["Idea bank 2026-10-19"],
         "cut_winner": ["Idea bank 2026-10-19"], "win": ["trading bot", "Path to 1k (2026-10-19)"],
         "win_big": ["Path to 1k (2026-10-19)", "Brainstorm(running on 0 sleep)"]}
CATS = {   # category: (title, what it means, experiments, research themes, issue keywords)
    "win_big": ("Win, 2x or more", "The coin ran and the bot sold into it.",
                ["2026-10-22 what separates runners (RN)"], ["Exits and profit-taking"], []),
    "win": ("Win", "Closed above entry after costs.", ["2026-10-22 trade patterns (TF2)"], ["Exits and profit-taking"], []),
    "gave_back": ("Gave back a gain", "Was up 30% or more at some point, closed at a loss.",
                  ["2026-10-22 fade exits (FX)", "2026-10-22 trade forensics and fix sweep (TF)"],
                  ["Exits and profit-taking"], []),
    "rug_gap": ("Rug gap", "The stop filled far below its level: one or a few sells took the price through it.",
                ["2026-10-23 creator dump veto (CV)", "2026-10-23 buy quality and big-holder dumps (BQ)",
                 "2026-10-23 M6 bad-buy filters (M6)", "2026-10-23 funding links (FL)", "2026-10-23 coin age gate (AG)",
                 "2026-10-23 coordinated-exit and serial-funder tests (CX)",
                 "2026-10-23 exit anatomy - max loss and max profit (XA)"],
                ["Bots, snipers and bundles", "Dev and creator behaviour"], ["Issue 01", "Issue 14", "Issue 36"]),
    "shaken_out": ("Shaken out", "Stopped out, then the coin went to 2x of our entry within 15 min.",
                   ["2026-10-22 trade forensics and fix sweep (TF)"], ["Exits and profit-taking"], []),
    "cut_winner": ("Cut a winner", "A non-stop exit sold, then the coin went to 2x of our entry within 15 min.",
                   ["2026-10-22 trade forensics and fix sweep (TF)", "2026-10-22 M4 variants backtest (MV)"],
                   ["Exits and profit-taking"], []),
    "instant_dump": ("Instant dump", "Never got 5% above entry: we bought the top of the move.",
                     ["2026-10-23 sniper overhang test (OH)", "2026-10-23 M5 entry redesign (ER)",
                      "2026-10-23 M6 bad-buy filters (M6)", "2026-10-23 coin age gate (AG)",
                      "2026-10-23 funding links (FL)"],
                     ["Entries", "Bots, snipers and bundles"], ["Issue 36"]),
    "slow_bleed": ("Slow bleed", "Rose a little, never 30%, drifted down to an exit.",
                   ["2026-10-22 trade patterns (TF2)", "2026-10-22 fade exits (FX)"],
                   ["Volume and buy pressure", "Holder growth and distribution"], []),
}


def ticks_rows(x):
    p = DATA_DIR / "m1" / "ticks" / f"{x['m']}.json.gz"
    if not p.exists():
        return None
    with gzip.open(p, "rt", encoding="utf-8") as fh:
        tr = sorted(json.load(fh)["trades"], key=lambda r: (r[0], r[1]))
    pre, post = path(x, tr)
    return (pre, post) if post else None


def feed_rows(db, x):
    rows = db.execute("SELECT t, w, b, sol, tok, vs, vt FROM tr WHERE m=? ORDER BY slot, seq", (x["m"],)).fetchall()
    if not rows:
        return None
    bal, h, out = {}, 0, []
    for t, w, b, sol, tok, vs, vt in rows:
        b0 = bal.get(w, 0.0)
        b1 = b0 + (tok if b else -tok)
        bal[w] = b1
        h += (b0 <= 1 < b1) - (b1 <= 1 < b0)
        if vt:
            out.append([t, vs / vt, bool(b), sol, w, tok, h])
    pre = [r for r in out if r[0] <= x["t"]]
    post = [r for r in out if r[0] > x["t"]]
    return (pre, post) if post and pre else None


def analyse(x, pp, src=None):
    """Numbers for one trade. pp = (pre, post) curve rows or None."""
    p0, t0, te = x["p0"], x["t"], x.get("t_exit", x["t"])
    a = dict(ret=x["ret"], peak=x["peak"] / p0, fill=x["p_exit"] / p0, held=te - t0, exit=x.get("exit"),
             score=x.get("score"), mc=x.get("mc"), stop=x.get("stop", 0.7), data=None)
    if not pp:
        return a
    pre, post = pp
    a["data"] = src
    win15 = [r for r in post if r[0] <= t0 + 900]
    if win15:
        hi = max(win15, key=lambda r: r[1])
        lo = min(win15, key=lambda r: r[1])
        a.update(hi15=hi[1] / p0, t_hi15=max(0.0, hi[0] - t0), lo15=lo[1] / p0, t_lo15=max(0.0, lo[0] - t0),
                 end15=win15[-1][1] / p0)
    a["at10"] = next((r[1] for r in reversed(post) if r[0] <= t0 + 10), p0) / p0
    a["at60"] = next((r[1] for r in reversed(post) if r[0] <= t0 + 60), p0) / p0
    after = [r for r in post if te < r[0] <= te + 900]
    a["after_hi"] = max((r[1] for r in after), default=x["p_exit"]) / p0
    hold = [r for r in post if r[0] <= te + 3]
    sells = [r for r in hold if not r[2] and r[3]]
    creator = pre[0][4] if pre else None
    early = {r[4] for r in pre if r[2] and r[0] <= pre[0][0] + 5} if pre else set()
    if sells:
        big = max(sells, key=lambda r: r[3])
        who = ("the creator (first buyer on the curve)" if big[4] == creator else
               "an early wallet (bought in the first 5 s)" if big[4] in early else "another wallet")
        a.update(big_sol=big[3], big_t=big[0] - t0, big_who=who, big_p=big[1] / p0)
    a["creator_sold"] = sum(r[3] or 0 for r in hold if not r[2] and r[4] == creator)
    a["early_sold"] = sum(r[3] or 0 for r in hold if not r[2] and r[4] in early and r[4] != creator)
    a["sell_sol"] = sum(r[3] or 0 for r in hold if not r[2])
    a["buy_sol"] = sum(r[3] or 0 for r in hold if r[2])
    if pre and pre[-1][6] is not None:
        a["h_entry"] = pre[-1][6]
        a["h_exit"] = (hold[-1][6] if hold else pre[-1][6])
        a["h_max_pre"] = max(r[6] for r in pre if r[6] is not None)
    rec = [r for r in pre if r[0] > t0 - 600]
    if rec:
        a["pre_lo"] = min(r[1] for r in rec) / p0
    first5 = []
    for r in pre:
        if r[2] and r[4] not in first5 and r[4] != creator:
            first5.append(r[4])
        if len(first5) == 5:
            break
    if first5:
        bal = defaultdict(float)
        for r in pre:
            bal[r[4]] += r[5] if r[2] else -r[5]
        a["top5_out"] = sum(1 for w in first5 if bal[w] <= 1)
    return a


def category(a):
    if a["ret"] > 0:
        return "win_big" if a["ret"] >= 0.9 else "win"
    if a["peak"] >= 1.3:
        return "gave_back"
    if a["exit"] == "STOP" and a["fill"] < a["stop"] - 0.10:
        return "rug_gap"
    if a.get("after_hi", 0) >= 2:
        return "shaken_out" if a["exit"] == "STOP" else "cut_winner"
    return "instant_dump" if a["peak"] < 1.05 else "slow_bleed"


def why(a, cat):
    s = []
    held = f"{a['held'] / 60:.1f} min" if a["held"] >= 90 else f"{a['held']:.0f} s"
    s.append(f"Held {held}; peak {a['peak']:.2f}× entry; sold by **{a['exit']}** at {a['fill']:.2f}× "
             f"→ **{a['ret']:+.1%}** ({a['ret'] * SIZE:+.4f} SOL at 0.1 SOL).")
    if a["data"] is None:
        s.append("No curve trades are on file for this coin (it predates the live feed and has no Helius ticks), so "
                 "the explanation uses the bot's record only.")
    if cat in ("win", "win_big"):
        s.append(f"It won because the price rose to {a['peak']:.2f}× while we held and the exit sold at "
                 f"{a['fill']:.2f}×.")
        if a.get("after_hi") and a["after_hi"] > a["fill"] * 1.3:
            s.append(f"The coin kept going after we sold (high {a['after_hi']:.2f}× of entry within 15 min of the "
                     f"exit), so a looser exit or a partial sell would have caught more.")
    elif cat == "gave_back":
        s.append(f"It lost because the gain wasn't locked: it was up to {a['peak']:.2f}× and the exit only fired at "
                 f"{a['fill']:.2f}×.")
    elif cat == "rug_gap":
        s.append(f"It lost because the price jumped through the {a['stop']:.2f}× stop and the next poll filled at "
                 f"{a['fill']:.2f}×; a polling stop can't cap a one-step dump (Issue 01, Issue 36).")
    elif cat in ("shaken_out", "cut_winner"):
        s.append(f"It lost on the exit, not the pick: after we sold the coin went to {a['after_hi']:.2f}× of our "
                 f"entry within 15 min.")
    elif cat == "instant_dump":
        s.append("It lost because we bought the top: the price never got 5% above our entry.")
    else:
        s.append(f"It lost slowly: the best it did was {a['peak']:.2f}×, then demand faded until the "
                 f"{a['exit']} exit.")
    if a.get("big_sol"):
        s.append(f"The largest sell while we held was {a['big_sol']:.2f} SOL by {a['big_who']} at +{a['big_t']:.0f} s "
                 f"(price after it {a['big_p']:.2f}× entry).")
    if a.get("sell_sol") is not None and a.get("buy_sol") is not None and a["data"]:
        s.append(f"While we held: {a['buy_sol']:.2f} SOL bought vs {a['sell_sol']:.2f} SOL sold"
                 + (f"; the creator sold {a['creator_sold']:.2f} SOL" if a.get("creator_sold") else "")
                 + (f"; first-5-second wallets sold {a['early_sold']:.2f} SOL" if a.get("early_sold") else "") + ".")
    if a.get("h_entry") is not None:
        s.append(f"Holders: {a['h_entry']} at entry (peak before entry {a['h_max_pre']}), {a['h_exit']} at the exit.")
    if a.get("hi15") is not None:
        s.append(f"First 15 min after entry: high {a['hi15']:.2f}× at +{a['t_hi15']:.0f} s, low {a['lo15']:.2f}× at "
                 f"+{a['t_lo15']:.0f} s, {a['end15']:.2f}× at +15 min; {a['at10']:.2f}× after 10 s, "
                 f"{a['at60']:.2f}× after 60 s.")
    return s


def better(a, cat, bot):
    """What could have gone better, each tied to a tested experiment so the claim is honest about the evidence."""
    s, links = [], set()
    sc = a.get("score")
    if sc is not None and sc <= 3 and cat not in ("win", "win_big"):
        s.append(f"Skip low narrative scores: this coin scored {sc}. A gate at score ≥ 4 removed about 3 points of "
                 f"average loss in [[2026-10-23 M5 entry redesign (ER)]] (not enough on its own); Rohit flagged "
                 f"the same pattern in [[notes on m5 trades]].")
        links |= {"Map - Narrative and LLM scoring"}
    if a.get("h_entry") is not None and a["h_entry"] < 20 and cat not in ("win", "win_big"):
        s.append(f"Few holders at entry ({a['h_entry']}). Rohit's observation in [[notes on m1m2m3 trades]] is that "
                 f"trades on coins under about 15–20 holders stall and stop; M4/M5 added holder rules for this.")
        links |= {"Map - Holders"}
    if a.get("h_max_pre") and a.get("h_entry") is not None and a["h_entry"] < 0.8 * a["h_max_pre"] \
            and cat not in ("win", "win_big"):
        s.append(f"The coin was already losing holders when we bought ({a['h_max_pre']} → {a['h_entry']}). Rohit's "
                 f"[[Notes on M4 trades]] call this buying a dying coin.")
    if a.get("top5_out") is not None and a["top5_out"] >= 3 and cat not in ("win", "win_big"):
        s.append(f"{a['top5_out']} of the first 5 buyers had already sold out before our entry, the 'top buyers "
                 f"leaving' sign from [[Notes on M4 trades]].")
    if a.get("pre_lo") is not None and a["pre_lo"] <= 0.6 and cat not in ("win", "win_big"):
        s.append(f"We bought after a big run-up (the 10-min low before entry was {a['pre_lo']:.2f}× our price). "
                 f"Waiting for a dip was tested and did not help on average ([[2026-10-23 M5 entry redesign (ER)]]).")
        links |= {"Map - Entries"}
    if cat == "rug_gap" or (cat == "instant_dump" and a.get("big_sol", 0) >= 1):
        if a.get("big_who", "").startswith("the creator"):
            s.append("The creator dumped into us. A creator-holding veto was tested and failed on average "
                     "([[2026-10-23 creator dump veto (CV)]]); the creator was the top seller in 9 of 24 deep "
                     "losses ([[2026-10-23 buy quality and big-holder dumps (BQ)]]).")
            links |= {"Map - Dev and creator"}
        elif a.get("big_who", "").startswith("an early"):
            s.append("An early (sniper/bundle) wallet dumped into us. An overhang filter was tested and the sign "
                     "was reversed ([[2026-10-23 sniper overhang test (OH)]]).")
            links |= {"Map - Bots, snipers and bundles"}
        s.append("After entry nothing helps: a faster stop gains about 0–0.05× (Issue 36). Only entry selection "
                 "can avoid these.")
    if cat == "gave_back":
        s.append(f"Lock part of the gain: a trailing exit at 0.8× of the peak would have sold near "
                 f"{0.8 * a['peak']:.2f}× instead of {a['fill']:.2f}×. Fade and lock exits were tested in "
                 f"[[2026-10-22 fade exits (FX)]]; partial take-profits lowered the mean on M5 entries (ER B1/B2).")
        links |= {"Map - Exits"}
    if cat in ("shaken_out", "cut_winner"):
        s.append(f"The exit was too tight for this coin: it later reached {a['after_hi']:.2f}× of entry. Wider "
                 f"stops and slower holder exits were tested in [[2026-10-22 M4 variants backtest (MV)]] and "
                 f"[[2026-10-22 trade forensics and fix sweep (TF)]]; they cost more on the coins that kept falling.")
        links |= {"Map - Exits"}
    if cat == "slow_bleed":
        s.append("Demand faded with no single dump. A stall/volume-decay exit (sell when activity drops after a "
                 "small gain) is the research's fix; the stall exit was neutral on M5 entries (ER B3).")
        links |= {"Map - Volume and flow"}
    if cat in ("win", "win_big") and a.get("after_hi") and a["after_hi"] > a["fill"] * 1.3:
        s.append("Selling half and letting the rest run would have done better on this one; on M5 entries overall "
                 "it lowered the mean because it cut the TPs (ER B1/B2).")
    hr = dt.datetime.fromtimestamp(a["t"]).hour
    if hr in (0, 1) and bot == "M5" and cat not in ("win", "win_big"):
        s.append("Taken at 00:00–02:00, when M5's overnight trades averaged about −35% (n 12); see "
                 "[[Map - Regime and timing]].")
    if not s:
        s.append("Nothing specific stands out beyond the bot's general lack of edge at this entry point "
                 f"(see the {bot} experiment notes).")
    return s, links


def name_of(bot, x):
    t = dt.datetime.fromtimestamp(x["t"])
    nm = re.sub(r"[\\/:*?\"<>|#^\[\]`$%{}]", "", x.get("name") or x["m"][:6]).strip()[:40] or x["m"][:6]
    return f"{bot.upper()} {t:%m-%d %H%M} {nm} {x['m'][:4]}"


def rohit_lines():
    out = {}
    for f in ("notes on m1m2m3 trades", "Notes on M4 trades", "notes on m5 trades"):
        p = VAULT / f"{f}.md"
        if p.exists():
            out[f] = re.sub(re.escape(START) + r".*?" + re.escape(END), "", p.read_text(encoding="utf-8"), flags=re.S)
    return out


LEDGER = DATA_DIR / "research" / "ledger.jsonl"
CRED = {"high": 3.0, "medium-high": 2.5, "medium": 2.0, "low-medium": 1.5, "medium-low": 1.5, "low": 1.0}


def b_score(v):
    return None if v is None else "0–2" if v <= 2 else "3–4" if v <= 4 else "5–6" if v <= 6 else "7+"


def b_foll(v):
    return None if v is None else "<100" if v < 100 else "100–1k" if v < 1000 else "1k–10k" if v < 10000 else "10k+"


def b_hold(v):
    return None if v is None else "<15" if v < 15 else "15–29" if v < 30 else "30–59" if v < 60 else "60+"


def b_mc(v):
    return None if not v else "<$5k" if v < 5000 else "$5–8k" if v < 8000 else "$8–12k" if v < 12000 else "≥$12k"


def b_hour(t):
    h = dt.datetime.fromtimestamp(t).hour
    return "17–24 (Rohit's hours)" if h >= 17 else "00–06" if h < 6 else "06–12" if h < 12 else "12–17"


def _holder_trend(a, r):
    if not a.get("h_max_pre"):
        return None, None
    return (f"{a['h_entry']}/{a['h_max_pre']}",
            "falling (<80% of its peak)" if a["h_entry"] < 0.8 * a["h_max_pre"] else "at or near its peak")


def _runup(a, r):
    if a.get("pre_lo") is None:
        return None, None
    v = a["pre_lo"]
    return f"{v:.2f}×", ("big run-up (low ≤ 0.6×)" if v <= 0.6 else "moderate (0.6–0.85×)" if v <= 0.85 else
                         "flat (> 0.85×)")


def _top5(a, r):
    v = a.get("top5_out")
    return (None, None) if v is None else (v, "0–1" if v <= 1 else "2" if v == 2 else "3+")


def _slot(a, r):
    v = r.get("create_slot_buyers")
    return (None, None) if v is None else (v, "0" if v == 0 else "1–3" if v <= 3 else "4+")


def _seller(a, r):
    if not a.get("data"):
        return None, None
    if not a.get("big_sol"):
        return None, "no sells"
    w = a["big_who"]
    return f"{a['big_sol']:.2f} SOL", ("creator" if w.startswith("the c") else "early wallet" if w.startswith("an e")
                                       else "other wallet")


def _flow(a, r):
    if not a.get("data") or a.get("buy_sol") is None:
        return None, None
    s_, b_ = a["sell_sol"], a["buy_sol"]
    return f"{s_:.1f}/{b_:.1f} SOL", ("sells > 2× buys" if s_ > 2 * b_ else "sells 1–2× buys" if s_ > b_ else
                                      "buys ≥ sells")


def _first10(a, r):
    if not a.get("data") or a.get("at10") is None:
        return None, None
    v = a["at10"]
    return f"{v:.2f}×", "down > 10%" if v < 0.9 else "flat (0.90–1.05×)" if v <= 1.05 else "up > 5%"


def _prob(a, r):
    v = r.get("p")
    return (None, None) if v is None else (v, "<0.4" if v < 0.4 else "0.4–0.5" if v < 0.5 else "≥0.5")


# data point: (description, value(a, decision record) -> (value, range), research keyword regex, experiments)
DIMS = {
    "Narrative score": ("The LLM scorer's 0–10 narrative score for the coin's X link.",
                        lambda a, r: (a.get("score"), b_score(a.get("score"))),
                        r"narrative|catalyst|\bmeta\b|story|meme quality",
                        ["2026-10-22 narrative coins and blind labels (NC, LB)", "2026-10-22 score-snipe (SN)",
                         "2026-10-23 M5 entry redesign (ER)"]),
    "X author followers": ("Followers of the account behind the coin's X post (from the scorer).",
                           lambda a, r: (r.get("followers"), b_foll(r.get("followers"))),
                           r"follower|influencer|\bKOL|tweet", ["2026-10-22 narrative coins and blind labels (NC, LB)"]),
    "Holders at entry": ("Wallets holding more than 1 token when we bought.",
                         lambda a, r: (a.get("h_entry", r.get("holders")), b_hold(a.get("h_entry", r.get("holders")))),
                         r"holders?\b", ["2026-10-22 what separates runners (RN)", "2026-10-22 Rohit M4 notes (TF3)",
                                          "2026-10-23 M6 bad-buy filters (M6)"]),
    "Holder trend before entry": ("Holders at entry vs the most the coin had before entry.", _holder_trend,
                                  r"dying|holders.{0,30}(drop|fall|leav|sell)|exit liquidity",
                                  ["2026-10-22 Rohit M4 notes (TF3)", "2026-10-22 M4 variants backtest (MV)"]),
    "Run-up before entry": ("Lowest price in the 10 min before entry, as a fraction of our entry price.", _runup,
                            r"\bdip|pullback|top.?blast|chas|fomo|vertical|exit liquidity",
                            ["2026-10-23 M5 entry redesign (ER)", "2026-10-21 entry timing and evening review",
                             "2026-10-23 coin age gate (AG)"]),
    "Market cap at entry": ("Market cap in USD when we bought.", lambda a, r: (a.get("mc"), b_mc(a.get("mc"))),
                            r"market cap|\bmc\b|migration", ["2026-10-22 trade patterns (TF2)"]),
    "Hour of entry": ("Pacific time of the buy.",
                      lambda a, r: (dt.datetime.fromtimestamp(a["t"]).strftime("%H:%M"), b_hour(a["t"])),
                      r"hour|time of day|\bEU\b|\bUS\b|overnight",
                      ["2026-10-21 entry timing and evening review", "2026-10-22 evening missed runners (MR)"]),
    "First buyers out before entry": ("How many of the first 5 buyers (creator excluded) had sold out before our "
                                      "entry.", _top5, r"early buyers|first buyers|sniper|insider",
                                      ["2026-10-23 sniper overhang test (OH)", "2026-10-22 Rohit M4 notes (TF3)",
                                       "2026-10-23 M6 bad-buy filters (M6)", "2026-10-23 funding links (FL)"]),
    "Create-slot buyers": ("Wallets that bought in the coin's create slot (bundle sign; M1–M4 checks).", _slot,
                           r"bundl|same block|create.?slot|jito",
                           ["2026-10-21 M2 rug-filtered M1", "2026-10-23 sniper overhang test (OH)",
                            "2026-10-23 funding links (FL)"]),
    "Biggest seller while held": ("Who made the largest single sell while we held.", _seller,
                                  r"dev.?sell|creator|dump|bundl|rug",
                                  ["2026-10-23 creator dump veto (CV)", "2026-10-23 buy quality and big-holder dumps (BQ)",
                                   "2026-10-23 funding links (FL)"]),
    "Sell vs buy flow while held": ("SOL sold vs SOL bought by everyone else while we held.", _flow,
                                    r"sell pressure|buy.?sell|net flow|volume",
                                    ["2026-10-23 buy quality and big-holder dumps (BQ)", "2026-10-22 fade exits (FX)"]),
    "First 10 s after entry": ("Price 10 s after entry vs entry.", _first10, r"momentum|first minute|spike|instant",
                               ["2026-10-22 what separates runners (RN)",
                                "2026-10-22 trade forensics and fix sweep (TF)"]),
    "M5 model probability": ("M5's runner-model probability at the buy (threshold = train p90).", _prob,
                             r"runner|predict|graduat", ["2026-10-22 M5 runner bot (M5)"]),
    "Exit reason": ("The exit rule that closed the trade.", lambda a, r: (a.get("exit"), a.get("exit")),
                    r"take.?profit|stop.?loss|sell half|trail|\bexit",
                    ["2026-10-22 fade exits (FX)", "2026-10-22 M4 variants backtest (MV)"]),
}
CAT_RX = {"win_big": r"take.?profit|sell half|scale out|runner", "win": r"take.?profit|scale out|sell half",
          "gave_back": r"round.?trip|take.?profit|trail|sell half|lock", "rug_gap": r"rug|dump|dev.?sell|bundl|nuke",
          "shaken_out": r"stop.?loss|shaken|wick|swing low", "cut_winner": r"too early|let.{0,10}run|moon bag|hold",
          "instant_dump": r"top.?blast|exit liquidity|chas|fomo|\bdip|pullback",
          "slow_bleed": r"volume.{0,20}(die|drop|fall|slow)|dying|stall|flatline"}


def sources_for(rx, ledger, names, k=6):
    sc = []
    for r in ledger:
        txt = " ".join(r.get("claims", []) + r.get("tests", []) + [r["title"]])
        h = len(re.findall(rx, txt, re.I))
        if h:
            sc.append((h * CRED.get(r.get("cred", "low"), 1.0), r["id"]))
    return [names[i] for _, i in sorted(sc, reverse=True) if i in names][:k]


def records(bot):
    """Per mint: scorer records and decision records (passed checks / M5 buy), in file (time) order."""
    sc, dec = defaultdict(list), defaultdict(list)
    p = DATA_DIR / bot / "cands.jsonl"
    if not p.exists():
        return sc, dec
    for line in p.open(encoding="utf-8"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        st = r.get("stage")
        if st is None and "score" in r:
            sc[r["m"]].append(r)
        elif (st == "checks" and r.get("ok")) or (st == "m5" and r.get("buy")):
            dec[r["m"]].append(r)
    return sc, dec


def last_before(rs, t, slack=5.0):
    c = [r for r in rs if float(r["t"]) <= t + slack]
    return c[-1] if c else (rs[0] if rs else None)


def dp_name(k):
    return f"Data - {k}"


def head(x, a, cat, label, t):
    title, meaning = CATS[cat][0], CATS[cat][1]
    return [f"---\ntype: trade\nbot: {label}\nrules: {x.get('rules')}\ncategory: {cat}\nret: {x['ret']:.4f}\n"
            f"date: {t:%Y-%m-%d}\nmint: {x['m']}\n---",
            f"# {label} · {x.get('name')} · {x['ret']:+.1%}\n",
            f"- **Bot:** [[Trades - {label}|{label}]] ({x.get('rules')}) · entered {t:%Y-%m-%d %H:%M:%S} PT · "
            f"mc ${x.get('mc') or 0:,.0f} · narrative score {x.get('score')}",
            f"- **Outcome:** [[Trades - {title}|{title}]]: {meaning}",
            f"- **Coin:** `{x['m']}` · [pump.fun](https://pump.fun/coin/{x['m']})"
            + (f" · [X post](https://x.com/i/status/{x['nkey']})" if x.get("nkey") else ""),
            "- **Data used:** " + {"ticks": "Helius curve ticks", "feed": "live-feed curve trades",
                                   None: "bot record only"}[a["data"]] + "\n"]


def main() -> None:
    db = sqlite3.connect(f"file:{FEED_DB}?mode=ro", uri=True)
    notes_r = rohit_lines()
    ledger = [json.loads(l) for l in LEDGER.open(encoding="utf-8")] if LEDGER.exists() else []
    src_dir = VAULT / "Research" / "Runner research" / "Sources"
    snames = {int(p.stem[1:4]): p.stem for p in src_dir.glob("S*.md") if p.stem[1:4].isdigit()}
    issue_files = sorted(p.stem for p in (VAULT / "Codebase" / "Issues").glob("*.md"))
    all_tr = []
    for bot in BOTS:
        for line in (DATA_DIR / bot / "trades.jsonl").open(encoding="utf-8"):
            if line.strip():
                x = json.loads(line)
                if "ret" in x and x.get("p0"):
                    all_tr.append((bot, x))
    by_mint = defaultdict(list)
    for bot, x in all_tr:
        by_mint[x["m"]].append(name_of(bot, x))
    recs = {bot: records(bot) for bot in BOTS}
    items = []
    for bot, x in all_tr:
        pp, src = ticks_rows(x), "ticks"
        if not pp:
            pp, src = feed_rows(db, x), "feed"
        a = analyse(x, pp, src)
        a["t"] = x["t"]
        sc_r = last_before(recs[bot][0].get(x["m"], []), x["t"]) or {}
        de_r = last_before(recs[bot][1].get(x["m"], []), x["t"]) or {}
        merged = dict(de_r, followers=sc_r.get("followers"))
        dims = {}
        for k, d in DIMS.items():
            try:
                v, b = d[1](a, merged)
            except (KeyError, TypeError, ZeroDivisionError):
                v, b = None, None
            if b is not None:
                dims[k] = (v, b)
        items.append(dict(bot=bot, x=x, a=a, cat=category(a), sc=sc_r, de=de_r, dims=dims, nm=name_of(bot, x)))
    stats = defaultdict(lambda: defaultdict(list))
    for it in items:
        for k, (v, b) in it["dims"].items():
            stats[k][b].append(it)
    bin_mean = {(k, b): sum(i["x"]["ret"] for i in v) / len(v) for k, bs in stats.items() for b, v in bs.items()}
    dim_src = {k: sources_for(d[2], ledger, snames) for k, d in DIMS.items()}
    cat_src = {c: sources_for(rx, ledger, snames, 5) for c, rx in CAT_RX.items()}
    index = defaultdict(list)
    for it in items:
        bot, x, a, cat, nm = it["bot"], it["x"], it["a"], it["cat"], it["nm"]
        title, meaning, exps, themes, issues = CATS[cat]
        label, bot_exps, bot_note = BOTS[bot]
        t = dt.datetime.fromtimestamp(x["t"])
        bet, mlinks = better(a, cat, label)
        rnotes = [f for f, txt in notes_r.items() if x.get("name") and len(x["name"]) > 3
                  and x["name"].lower() in txt.lower()]
        ilinks = [n for n in issue_files if any(n.startswith(i + " ") for i in issues)]
        md = head(x, a, cat, label, t) + ["## Why it " + ("won" if x["ret"] > 0 else "lost")]
        md += [f"- {s}" for s in why(a, cat)]
        md += ["", "## What could have been better"] + [f"- {s}" for s in bet]
        md += ["", "## Data points",
               "Each links to a data-point note with the P/L of every trade sharing that range (all bots, *measured*, "
               "descriptive only).", "", "| Data point | This trade | Range | Mean P/L in range (n) |",
               "| --- | --- | --- | --- |"]
        for k, (v, b) in it["dims"].items():
            md.append(f"| [[{dp_name(k)}]] | {v} | {b} | {bin_mean[(k, b)]:+.1%} ({len(stats[k][b])}) |")
        md += ["", "## Decision record (what the bot saw)"]
        if it["sc"]:
            s_ = it["sc"]
            md.append(f"- **Scorer** ({s_.get('model', 'model not logged')}): score {s_.get('score')}"
                      + (f", author followers {s_['followers']:,}" if isinstance(s_.get("followers"), int) else "")
                      + f": “{(s_.get('why') or '').strip()}”" + (f" · [X link]({s_['link']})" if s_.get("link") else ""))
        if it["de"]:
            d_ = {k: v for k, v in it["de"].items() if k not in ("t", "m", "stage", "name", "buy", "ok", "why")}
            md.append("- **Buy check** (" + ("M5 model" if it["de"].get("stage") == "m5" else "M1–M4 checks") + "): "
                      + ", ".join(f"{k} {v}" for k, v in d_.items()))
        if not it["sc"] and not it["de"]:
            md.append("- No scorer or check record on file for this trade.")
        res = list(dict.fromkeys(cat_src.get(cat, []) + [s for k, (v, b) in it["dims"].items()
                                                        if bin_mean[(k, b)] < -0.05 for s in dim_src[k][:2]]))[:10]
        if res:
            md += ["", "## Research behind this trade",
                   "Sources whose claims match this outcome or this trade's weak data points (source claims, not "
                   "verified by us):"] + [f"- [[{s}]]" for s in res]
        if rnotes:
            md += ["", "## Rohit's notes on this coin"]
            for f in rnotes:
                txt = notes_r[f]
                i = txt.lower().find(x["name"].lower())
                md.append(f"- In [[{f}]]: “{txt[i:i + 450].strip()}…”")
        others = [o for o in by_mint[x["m"]] if o != nm]
        # only the experiments behind this trade's telling data points (range mean <= -5% for a loss, >= +5% for a win)
        tell = [k for k, (v, b) in it["dims"].items()
                if (bin_mean[(k, b)] <= -0.05 if x["ret"] <= 0 else bin_mean[(k, b)] >= 0.05)]
        dexps = list(dict.fromkeys(e for k in tell for e in DIMS[k][3])) or ["2026-10-22 trade patterns (TF2)"]
        md += ["", "## Links",
               "- **Bot experiments:** " + ", ".join(f"[[{e}]]" for e in bot_exps) + f" · Rohit: [[{bot_note}]]",
               "- **Why-category experiments:** " + ", ".join(f"[[{e}]]" for e in exps),
               "- **Data-point experiments:** " + ", ".join(f"[[{e}]]" for e in dexps),
               "- **Research themes:** " + ", ".join(f"[[{th}]]" for th in themes) + " · [[Runner research hub]]",
               "- **Map:** " + ", ".join(f"[[{m_}]]" for m_ in sorted(mlinks | {"Map - Paper bots M1-M5"}))]
        if ilinks:
            md.append("- **Known issues:** " + ", ".join(f"[[{i}]]" for i in ilinks))
        md.append("- **Ideas and write-ups:** " + ", ".join(f"[[{n}]]" for n in IDEAS[cat]))
        if others:
            md.append("- **Same coin, other trades:** " + ", ".join(f"[[{o}]]" for o in others))
        d = OUT / label
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{nm}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        index[label].append((x["t"], nm, x.get("name"), x["ret"], cat, title))
    dd = OUT / "Data points"
    dd.mkdir(parents=True, exist_ok=True)
    for k, (desc, _, rx, exps) in DIMS.items():
        bs = stats[k]
        n_all = sum(len(v) for v in bs.values())
        lines = [f"---\ntags: [trades, data-point]\n---\n# {dp_name(k)}\n", f"{desc}\n",
                 f"{n_all} of {len(items)} trades have this data point. *Measured*, all bots pooled, descriptive only: "
                 f"a range that looks good here is a hypothesis to pre-register, not a rule. Part of [[Trades index]].\n",
                 "| Range | Trades | Mean P/L | Median | Win | By bot (n, mean) |",
                 "| --- | --- | --- | --- | --- | --- |"]
        for b, v in sorted(bs.items(), key=lambda kv: str(kv[0])):
            r_ = sorted(i["x"]["ret"] for i in v)
            byb = defaultdict(list)
            for i in v:
                byb[BOTS[i["bot"]][0]].append(i["x"]["ret"])
            lines.append(f"| {b} | {len(v)} | {sum(r_) / len(r_):+.1%} | {r_[len(r_) // 2]:+.1%} | "
                         f"{sum(z > 0 for z in r_) / len(r_):.0%} | "
                         + ", ".join(f"{bb} {len(z)}, {sum(z) / len(z):+.0%}" for bb, z in sorted(byb.items())) + " |")
        lines += ["", "**Experiments:** " + ", ".join(f"[[{e}]]" for e in exps),
                  "**Research sources (claims about this data point):** " + ", ".join(f"[[{s}]]" for s in dim_src[k])]
        for b, v in sorted(bs.items(), key=lambda kv: str(kv[0])):
            lines += ["", f"## {b}"] + [f"- [[{i['nm']}]] {i['x']['ret']:+.1%}"
                                        for i in sorted(v, key=lambda i: i["x"]["t"])]
        (dd / f"{dp_name(k)}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    cats_all = Counter()
    by_cat = defaultdict(list)
    for label, rows in index.items():
        rows.sort()
        c = Counter(r[4] for r in rows)
        cats_all.update(c)
        for r in rows:
            by_cat[r[5]].append((label,) + r)
        n = len(rows)
        lines = [f"---\ntags: [trades]\n---\n# Trades - {label}\n",
                 f"{n} closed paper trades, mean **{sum(r[3] for r in rows) / n:+.1%}** per trade, total "
                 f"{sum(r[3] for r in rows) * SIZE:+.3f} SOL at 0.1 SOL. Part of [[Trades index]] · "
                 f"[[{BOTS[label.lower()][2]}]] · " + ", ".join(f"[[{e}]]" for e in BOTS[label.lower()][1]) + "\n",
                 "| Category | Trades | Mean |", "| --- | --- | --- |"]
        for k, v in c.most_common():
            rr = [r[3] for r in rows if r[4] == k]
            lines.append(f"| [[Trades - {CATS[k][0]}]] | {v} | {sum(rr) / len(rr):+.1%} |")
        lines += ["", "| Entered | Trade | P/L | Why |", "| --- | --- | --- | --- |"]
        for tt, nm, cn, r, k, title in rows:
            lines.append(f"| {dt.datetime.fromtimestamp(tt):%m-%d %H:%M} | [[{nm}]] | {r:+.1%} | {title} |")
        (OUT / f"Trades - {label}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for k, (title, meaning, exps, themes, issues) in CATS.items():
        rows = sorted(by_cat[title], key=lambda r: r[1])
        lines = [f"---\ntags: [trades]\n---\n# Trades - {title}\n", f"{meaning}\n",
                 f"{len(rows)} trades across M1–M5. Experiments: " + ", ".join(f"[[{e}]]" for e in exps)
                 + ". Themes: " + ", ".join(f"[[{t}]]" for t in themes) + ". Research: "
                 + ", ".join(f"[[{s}]]" for s in cat_src.get(k, [])) + ". Part of [[Trades index]].\n",
                 "| Bot | Entered | Trade | P/L |", "| --- | --- | --- | --- |"]
        lines += [f"| {lb} | {dt.datetime.fromtimestamp(tt):%m-%d %H:%M} | [[{nm}]] | {r:+.1%} |"
                  for lb, tt, nm, cn, r, kk, tl in rows]
        (OUT / f"Trades - {title}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    top = ["---\ntags: [trades, moc]\n---\n# Trades index\n",
           "Every closed M1–M5 paper trade has its own note: why it won or lost, what could have been better, its data "
           "points (each linked to a data-point note with the P/L of every trade sharing that range), the bot's "
           "decision record, the research sources behind it, and links to experiments, known issues and Rohit's notes. "
           "Generated by `uv run python -m tradeslave.trade_notes` from our own records; part of "
           "[[Memecoin bot knowledge map]].\n", "## By bot"]
    for label, rows in sorted(index.items()):
        top.append(f"- [[Trades - {label}]]: {len(rows)} trades, mean {sum(r[3] for r in rows) / len(rows):+.1%}")
    top += ["", "## By why", "| Category | Trades | Share |", "| --- | --- | --- |"]
    tot = sum(cats_all.values())
    for k, v in cats_all.most_common():
        top.append(f"| [[Trades - {CATS[k][0]}]] | {v} | {v / tot:.0%} |")
    top += ["", "## Data points"] + [f"- [[{dp_name(k)}]]: {d[0]}" for k, d in DIMS.items()]
    (OUT / "Trades index.md").write_text("\n".join(top) + "\n", encoding="utf-8")
    print(f"{tot} trade notes:", {k: len(v) for k, v in index.items()}, dict(cats_all))


if __name__ == "__main__":
    main()
