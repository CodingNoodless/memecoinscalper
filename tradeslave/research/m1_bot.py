"""M1: Rohit's manual new-pairs process, automated (PAPER only: no wallet, no signing, no order path).
Vault: "2026-10-21 M1 automated manual process".

    uv run python -m tradeslave.research.m1_bot run      # live paper loop
    uv run python -m tradeslave.research.m1_bot report   # results so far

Process (Rohit, 2026-10-20), step by step:
1. New pump.fun pairs (launch_watch logs every create to data/c1/creates.jsonl) with an X post / profile /
   community link in the metadata.
2. Narrative read by an LLM against his four criteria (influential poster, project with real potential,
   community cause like charity, correcting an existing narrative). NVIDIA GPT-OSS 20B if NVIDIA_API_KEY is set,
   else local Ollama. Buy only if score >= SCORE_MIN; score >= SCORE_HIGH is "really sure".
3. Market filters: volume >= $1.5k; entry while market cap is $4k-7k ($5-7k best, $4-5k ok), up to $10k when the
   narrative scores >= SCORE_HIGH.
4. Top-5 holder check: no bundle (shared or simultaneous funding, near-identical balances).
5. Chart check: organic (no single buy above 40% of buy volume besides the dev's create buy).
6. Exits (Rohit, 2026-10-21 02:00), by conviction. Activity = transactions on the bonding curve in the last 60 s.
   Normal (score 7-8): 2x; uptrend break (after +30%, 20% off the peak); stall (no new high for 2 min and activity
   < 50% of its peak since entry); 10 min max; disaster stop -30%. Dips are held while activity holds up.
   High (score >= 9): no 2x cap; hold until the uptrend is over (once >= +50%, 40% off the peak); situation change
   (activity < 20% of its peak while below entry); disaster stop -30%; 2 h max.
Fills: curve state at the next poll after each decision (~2 s), 1.25%/side curve fee, own price impact,
TX_SOL per transaction.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
import sys
import threading
import time
from collections import defaultdict
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor

import requests
from openai import OpenAI

OLLAMA_MODEL = "qwen2.5:7b"                    # the 14b model needs more free RAM than this PC has
from ..paths import DATA_DIR, PROJECT_ROOT
from ..scalper.curve import decode_bonding_curve
from .c1_curve import CREATES, CURVE_FEE, SOL_USDC, T0, V0, sell_curve
from .chain import env_rpc
from .full_trades import Pace, ts as iso_ts
from .live_feed import DB as FEED_DB, feed_rows, feed_trades
from .fl_test import LiveFunding

VARIANT = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] in ("m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8", "m9", "m10") else "m1"
M6 = VARIANT in ("m6", "m7", "m8", "m9")             # M6 (2026-10-23 17:00): M5 v1 + bad-buy skip filters (see below)
M7 = VARIANT in ("m7", "m8", "m9")                   # M7/M8 (2026-10-23 16:00): m6-v3 entries, side by side with two exit sets
M8 = VARIANT == "m8"
M10 = VARIANT == "m10"                           # M10 (2026-10-23 23:45): calm-entry rule from the M10 backtest
M9 = VARIANT == "m9"                             # M9 (2026-10-23 16:53): M5 or M1 entries, loss controls (below)
TWIN = VARIANT in ("m7", "m9")                   # twin exit book on the same fills: M7 -> M8 (16:15), M9 -> M9e (17:20)
M5 = VARIANT in ("m5", "m6", "m7", "m8", "m9", "m10")                     # M5 (2026-10-22 23:00): trade only predicted runners (see below)
M4 = VARIANT == "m4"                             # M4 (2026-10-22 15:10): M1 + the fixes accepted by the TF sweep (see below)
M3 = VARIANT == "m3"                             # M3 (2026-10-21 16:00): M2 + Rohit's post-mortem changes (see below)
M2 = VARIANT in ("m2", "m3")                     # M2 (2026-10-21 09:40): M1 + sniper-bag filter, -10% stop, 1 s polls
OUT = DATA_DIR / VARIANT
CANDS, TRADES_LOG, OPEN = OUT / "cands.jsonl", OUT / "trades.jsonl", OUT / "status.json"
SWAP = "https://swap-api.pump.fun/v2/coins/{}/trades"
GT = "https://api.geckoterminal.com/api/v2/networks/solana/pools/" + SOL_USDC
MC_MIN, VOL_MIN, MC_BUY, MC_HIGH = 4000.0, 1500.0, (4000.0, 7000.0), 10000.0   # Rohit: 5-7k best, 4-5k ok,
                                                                         # up to 10k if the narrative is really good
SCORE_MIN, SCORE_HIGH = 7, 9
NVIDIA_MODEL = "openai/gpt-oss-20b"
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
TP, STOP, TREND_ARM, TREND_GIVE, STALL_S, STALL_ACT, MAX_HOLD = 2.0, 0.70, 1.30, 0.80, 120, 0.50, 600
HI_ARM, HI_GIVE, HI_DEAD, HI_HOLD = 1.50, 0.60, 0.20, 7200
if M2 or M8 or M9 or M10:
    STOP = 0.90                                  # Rohit: losses ~15-20% even when rugged (a -10% stop fills ~-17..-20%)
SNIP_SLOTS, SNIP_MAX = 2, 8.0                    # M2: wallets buying within 2 slots of the create, % of supply still held
# M3 (assumptions, not fitted): dead coin = < 15 holders and < 8 new buyers in 2 min; rug = dev holds > 10% or
# top-10 wallets funded in the last 6 h hold > 25%; a copy needs volume >= $3k; at 2x, if the coin has traction
# (>= 25 buyers and buys >= 1.5x sells in the last 60 s, or the linked post's engagement doubled since entry, +20),
# m3-v2 (Rohit 15:50): instead of any exit signal while >= 1.3x (2x, trend break, stall, time), a coin with traction
# is ridden with the WHOLE position until support fades (buyers/min < 40% of their peak since the ride began and
# sells >= buys), or a floor (0.8x the ride-start price, at most 1.4x entry), a crash (50% off the peak), or 2 h.
DEAD_HOLDERS, DEAD_GROWTH, DEV_MAX, FRESH_H, FRESH_MAX, VOL_COPY = 15, 8, 10.0, 6 * 3600, 25.0, 3000.0
RUN_BUYERS, RUN_FLOW, RUN_SOCIAL, RUN_GIVE, RUN_FLOOR, RUN_HOLD = 25, 1.5, 2.0, 0.50, 1.4, 7200
RIDE_MIN, SUP_EVERY, SUP_FADE = 1.3, 10.0, 0.40
# M4 (TF sweep, vault "2026-10-22 trade forensics and fix sweep (TF)"): Rohit's watchlist (buy only once >= 20 wallets
# hold it, re-checked every 10 s while in the buy band), Rohit's holder exit (sell when holders fall 10% from their max
# since entry; holders re-read every 5 s), and skip a coin bought at a local top (>= 1.3x its 60 s low into net selling).
# m4-v2 (15:20, Rohit: "it literally bought into a dying token"): the watchlist also needs holders >= 90% of their max
# so far (never buy what the holder exit would sell at once), and M4 reads trades from the real-time feed
# (research.live_feed) instead of the swap-api list, which lags 20 s to minutes.
# m4-v3 (17:05, Rohit's M4 notes, vault "2026-10-22 Rohit M4 notes (TF3)"): skip a coin back near its launch price in
# a downtrend (<= 1.15x launch and <= 0.8x its high; TF3 accepted), and skip posts whose author has < 100 or >= 10k
# followers (found while testing his deleted-posts idea: -18% vs -7%/-9% in both halves; forward test decides).
H_MIN, H_DROP, H_EVERY, TOP_UP = 20, 0.10, 2.0, 1.3
LAUNCH_X, LAUNCH_HIGH, FOLL = 1.15, 0.80, (100, 10_000)
# M5 (vault "2026-10-22 M5 runner bot (M5)"): no narrative gate (any X-linked coin; the score is still fetched from
# the shared cache only so the coin is watched). From a coin's first trade in the $4-7k band >= 5 s after create, every
# 10 s up to +180 s, a logistic runner model on post-band momentum (data/m1/m5_model.json, trained on Oct 21 + Oct 22
# evening, frozen) gives p; buy at the first p >= its threshold (0.373). Trades come from the real-time feed. Exits:
# M1's plus the holder exit (-10% from max) after a 15 s grace. The backtest FAILED held out (-5.0%/trade; all data
# -1.6%, M4 replay -6.3%): this is a forward test.
M5_GRACE, M5_MAX_S, M5_EVERY = 15.0, 180.0, 10.0
# M6 (vault "2026-10-23 M6 bad-buy filters (M6)"): M5 v1 unchanged, but its buy is skipped when one funder (Helius
# funding lookups, vault "2026-10-23 funding links (FL)") controls >= 15% of the top-20 holders' tokens at the decision.
# m6-v1 (12:05-13:10, score >= 5 and holders >= 60) took 1 trade; m6-v2 swaps in the funding-cluster skip, the only rug
# signal that separated deep losses in the same direction on train, validation and live (post hoc; forward test).
M6_CLUSTER = 0.15
# M7 / M8 (vault "M6 proposals (m6-v3)", "2026-10-23 coordinated-exit and serial-funder tests (CX)", "Max profit and max
# 25% loss - what the vault says"): m6-v2 plus the serial-funder skip (CX2): skip when the creator's funder already funded
# the creator of an earlier M5-signalled coin (program-owned funders don't count; a failed lookup skips). Seen funders
# start from data/m1/serial_seed.json (dataset entries + M5 live trades) and grow with every M5 buy signal. M7 keeps
# M5's exits (stop -30%); M8 uses a -10% stop (XA L5). Forward test, judged once at >= 60 trades each.
SERIAL_SEED = DATA_DIR / "m1" / "serial_seed.json"
# m8-v2 (2026-10-23 16:15, Rohit: "the comparison data isn't accurate"): two separate processes buying the same coin fill
# and poll at slightly different moments (M5 and M6 on the same coin at the same second: +18.6% vs +15.9%), so the M8 - M7
# gap mixed timing noise into the stop effect. M8 now runs inside the M7 process as a twin book: same fill, same polls,
# only the stop differs (-10%). It writes data/m8/trades.jsonl and data/m8/status.json; `run m8` alone is retired.
TWIN_STOP, TWIN_DIR, TWIN_RULES = ((0.70, DATA_DIR / "m9e", "m9e-v3+1s") if VARIANT == "m9"
                                   else (0.90, DATA_DIR / "m8", "m8-v3+1s"))
TWIN_LABEL = "M9e " if VARIANT == "m9" else "M8 "
# M12 (vault "2026-10-24 M12 - M8 with hype context (M12)", Rohit: "watch pump.fun callouts ... use it as an indicator"): a
# third book in the M7 process with M8's fill and exits (-10% stop). It takes M8's entry only with hype at that moment: a top
# pump.fun caller's wallet (callout leaderboard, read with Rohit's own login token) bought the coin before now, or the coin was
# a >= 20-viewer pump.fun livestream / DexScreener-boosted in the last 4 h (HY recorder). Every M8 entry's hype is logged.
# v2 (amendment 2026-10-24 22:08, Rohit: "widen M12's hype readings"; 1 trade in 3 h): the top 50 callers instead of 5
# (leaderboard limit=100; the site-wide latest-callouts list refuses non-site clients), their wallets added to the feed's
# leader capture, and a caller buy counts from either the feed DB or leader_trades.jsonl within HYPE_S. v1 and v2 trades
# are judged separately (rules label).
M12 = VARIANT == "m7"
M12_DIR, M12_RULES, M12_STOP, M12_LABEL = DATA_DIR / "m12", "m12-v3+1s", 0.90, "M12 "
HYPE_S = 4 * 3600
# Rohit's elite list (v2): callers with >= 31% of callouts reaching 2x and a median multiple >= 1.40 (18 of the top 50 on
# 2026-10-24). Recorded on every entry and compared at the verdict; the gate itself takes any of the 50.
ELITE_2X, ELITE_MED = 0.31, 1.40
# M12r (same vault note, Rohit's design): M12's entries with a ratcheting runner exit. Below 2x: M8's exits without the 2x
# take-profit. From the first 2x touch the stop only moves up: 1.25x at 2x, then N - 1 at each new whole multiple N >= 3; take
# profit when the run looks done (STALLED: no new high for 120 s and 60 s net SOL flow <= 0; DYING: 60 s curve txs < 25% of the
# busiest 60 s since entry) or after 60 min in runner mode.
M12R_DIR, M12R_RULES, M12R_LABEL = DATA_DIR / "m12r", "m12r-v3+1s", "M12r "
LAD_ARM, LAD_FIRST, LAD_STALL_S, LAD_DYING, LAD_HOLD = 2.0, 1.25, 120, 0.25, 3600


class Hype:
    """Top-caller wallets (refreshed every 6 h in a thread) and HY's recent livestream/boost signals."""

    def __init__(self):
        self.callers, self.elite, self.sig, self.t_sig = set(), set(), {}, 0.0
        self._load_callers()
        threading.Thread(target=self._refresh, daemon=True, name="callouts").start()

    def _load_callers(self):
        for f in sorted((DATA_DIR / "callouts").glob("leaderboard_*.json"), reverse=True):   # newest readable one
            try:
                lb = json.loads(f.read_text(encoding="utf-8")).get("callouts", [])
            except (OSError, ValueError):
                continue
            self.callers = {w for u in lb for w in u.get("wallets", []) if not w.startswith("0x")}
            self.elite = {w for u in lb if u.get("pct2xOrMore", 0) >= ELITE_2X and u.get("medianMultiple", 0) >= ELITE_MED
                          for w in u.get("wallets", []) if not w.startswith("0x")}
            try:                                 # the feed's leader capture also records these wallets' buys on any coin
                lp = DATA_DIR / "feed" / "leaders.json"
                cur = set(json.loads(lp.read_text())) if lp.exists() else set()
                if not self.callers <= cur:
                    lp.write_text(json.dumps(sorted(cur | self.callers)))
            except (OSError, ValueError):
                pass
            return

    def _refresh(self):
        import requests
        from dotenv import dotenv_values
        while True:
            tok = (dotenv_values(PROJECT_ROOT / ".env").get("PUMP_JWT") or "").strip().strip('"').strip("'")
            if tok:
                try:
                    r = requests.get("https://frontend-api-v3.pump.fun/callout/leaderboard?limit=100", timeout=20,
                                     headers={"User-Agent": "tradeslave-research/0.1 (personal research; low rate)",
                                              "Accept": "application/json", "Authorization": f"Bearer {tok}"})
                    if r.status_code == 200:
                        (DATA_DIR / "callouts").mkdir(exist_ok=True)
                        (DATA_DIR / "callouts" / f"leaderboard_{int(time.time())}.json").write_text(r.text, encoding="utf-8")
                        self._load_callers()
                    else:
                        print(time.strftime("%H:%M"), f"M12 callout leaderboard HTTP {r.status_code} (token expired?)", flush=True)
                except Exception as exc:  # noqa: BLE001 - keep the last caller list
                    print(time.strftime("%H:%M"), f"M12 callout refresh failed: {type(exc).__name__}", flush=True)
            time.sleep(6 * 3600)

    def signals(self):
        if time.time() - self.t_sig > 60:
            self.t_sig, out = time.time(), {}
            try:
                with (DATA_DIR / "hype" / "signals.jsonl").open() as fh:
                    for line in fh:
                        j = json.loads(line)
                        if time.time() - j["t"] < HYPE_S:
                            out.setdefault(j["m"], set()).add(j["src"])
            except (OSError, ValueError):
                pass
            self.sig = out
        return self.sig

    def gate(self, m):
        callers_in = []
        if self.callers:
            try:
                q = ",".join("?" * len(self.callers))
                db = sqlite3.connect(FEED_DB, timeout=5)
                callers_in = [w for (w,) in db.execute(f"SELECT DISTINCT w FROM tr WHERE m=? AND b=1 AND w IN ({q})", (m, *self.callers))]
                db.close()
            except sqlite3.Error:
                pass
        try:                                     # caller buys the feed DB missed (bought before the coin was written)
            with (DATA_DIR / "feed" / "leader_trades.jsonl").open(encoding="utf-8") as fh:
                fh.seek(max(0, fh.seek(0, 2) - 3_000_000))
                for line in fh:
                    if m in line:
                        e = json.loads(line)
                        if e.get("m") == m and e.get("buy") and e.get("w") in self.callers and time.time() - e.get("recv", 0) < HYPE_S:
                            callers_in.append(e["w"])
        except (OSError, ValueError):
            pass
        callers_in = sorted(set(callers_in))
        src = sorted(self.signals().get(m, ()))
        return dict(caller=len(callers_in), elite=len(set(callers_in) & self.elite), live_or_boost=src, go=bool(callers_in) or bool(src))
# M9 v2 (vault "2026-10-23 pattern search for M9 (PX)", 17:20): the one PX rule that held on holdout replaces M9 v1's
# skips on both entry sources: net SOL flow over the 30 s before the last feed trade >= M9_FLOW, and the creator's funder
# funded no earlier creator (counts continue from the PX table; a program-owned or unknown funder skips). Main book L5
# (-10% stop), twin book M9e E2 (-30% stop) on the same fills.
M9_FLOW, SERIAL_SEED_PX = 12.2787, DATA_DIR / "m1" / "serial_seed_px.json"
# M10 v2 (vault "2026-10-23 M10 flagship model - backtest (M10)"): a forward test, NOT a passed model (backtest -4.6% a trade on
# both halves, deep losses 3-4%, 1 s delay ~0.5 pt). Every 15 s from 15 to 180 s after a coin's first trade (with >= 5 trades
# in the last 60 s), buy if: average buy (60 s) <= 0.511 SOL, top-10 share <= 23.3%, >= 30.25 s since the first trade, and
# buy/sell SOL ratio (60 s) <= 1.593 (thresholds from design data only). Exits: E2 with a -10% stop, 1 s polls (L5).
M10_RULE = (("avg_buy_60", "<=", 0.511), ("top10", "<=", 0.233), ("t_to_n", ">=", 30.25), ("buy_ratio_60", "<=", 1.593))
# M9 (vault "2026-10-23 M9 design test - M1 + M5 entries with loss controls (M9)", passed 16:52 on 21 kept M1 coins):
# buy on M5 v1's runner signal OR, when the model doesn't buy, on M1's signal (score >= 7, $4-7k band, M1's checks).
# Both sources then need the M6 cluster skip and the serial-funder skip (as M7; M1-path checks don't add to the counts,
# as in the test). Every position uses M5's exits with a -10% stop on 1 s polls (XA L5). Forward test.
# 1 s polls on every bot from 2026-10-23 16:00 (Rohit: simulate a 1 s delay, not 2-4 s): an exit decided on one poll
# fills at the next, 1 s later; rules carry "+1s" so the dashboard splits results from before the change.
RULES = ("m10-v2" if M10 else "m9-v3" if M9 else "m8-v1" if M8 else "m7-v2" if M7 else "m6-v2" if M6 else "m5-v1" if M5 else "m4-v3" if M4 else "m3-v3" if M3 else "m2-v1" if M2 else "exits-v2") + "+1s"                                # bump on every rule change; the dashboard splits results by it
ACT_EVERY = 6.0
WATCH_S, POLL_S, RECHECK_S = 20 * 60, 1.0, (5 if M4 else 30)
SIZE_SOL, TX_SOL, MAX_OPEN = 0.1, 0.0005, 5
LINK = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/\S+", re.I)

PROMPT = """You screen brand-new pump.fun memecoins for a trader. He buys only when the narrative is something people
are about to buy into. Any ONE of these is enough for him to be interested (a coin does not need several):
1) posted by an influential person,
2) a project with lots of potential for success,
3) a token aiming to achieve something with community support, like donating to charity,
4) a token correcting an existing narrative for being incorrect.
"Influential" is about WHO the person is: known memecoin KOLs and traders (e.g. Orangie, Cupsy), big crypto
accounts, or celebrities (e.g. Elon Musk). Follower counts are a hint only; an account you don't recognise with a few
thousand followers is NOT influential (criterion 1 fails, though the coin can still qualify on 2, 3 or 4). The coin's
own post often quotes or replies to someone else's post: if that person is influential, criterion 1 can count.
Judge the coin on the single criterion it fits best, and do not mark it down for missing the others (for example, a
strong charity or correction narrative posted by a small account can still score high). Then ask: is this
something people are about to buy into? Low-effort copies, jokes nobody will care about, or scams score low.
Judge ONLY the content and who posted it. Ignore engagement completely: likes, reposts, views, replies and the
coin's own traction do not matter (the coin is minutes old, so zero traction is normal and is never a reason to
score low). An unknown poster only means criterion 1 fails; it says nothing about criteria 2-4.
Timing: if the narrative is built on something time-sensitive (an announcement, event, launch, hackathon, news),
the post it relies on must be recent (hours to a day or two); a time-sensitive post from weeks or months ago has no
hype left, so score it low. Timeless meme content (a funny clip, an animal, a classic quote) can be old.
Verification: anyone can paste any link. Only credit criterion 1 when the post below was actually read and shows the
influential person posting it; if the link could not be read, judge only what can be seen (the name, the
description) and never credit criterion 1 from the handle in the URL alone.
Scale: 9-10 = an influential person posted it, or an exceptional narrative people will clearly pile into;
7-8 = clearly fits one criterion with content people would buy into; 4-6 = fits a criterion but the idea is weak or
forced; 0-3 = fits none, low effort, offensive, or a scam.

Coin: {name} (${sym})
Description: {desc}
X link: {link}
{tweet}
{earlier}Reply with JSON only: {{"score": <0-10 integer>, "criterion": <1-4 or 0>, "why": "<one short sentence>"{improves}}}"""

EARLIER = """
An EARLIER coin already launched on this same post / narrative: {name} (${sym}). Its description: {desc}
The trader prefers the first coin of a narrative. A later coin is only worth buying if it fixes the earlier coin's
narrative or credibly claims to be the better version with the right reasoning (e.g. the original got the name,
ticker or facts wrong, or this one is the official / correct one). A plain copy is not.
"""
CAUSE_M3 = """Community causes (criterion 3) are the narratives that hold up best, but only when the story is REAL and
BACKED: a real, identifiable person, animal, organisation or event (in the news, or the organisation / person itself
posting), or a real fundraiser. Score those 8-9. A vague promise ("we will donate", "for the community") with no real
story behind it scores 4-6.
A brand-new post by a famous account or big brand (Elon, OpenAI, toly, pump.fun...) gets many copy coins within
minutes and their devs often dump: being posted by a famous account alone scores 7 at most; 8+ needs a real story or
an exceptional idea on top.
"""
if M3:
    PROMPT = PROMPT.replace("Scale: 9-10 = an influential person posted it, or an exceptional narrative",
                            CAUSE_M3 + "Scale: 9-10 = a real, backed story people will rally behind, or an exceptional "
                            "narrative")
IMPROVES = ', "improves": <true if this coin fixes or credibly improves on the earlier one, else false>'


def jlog(path, row):
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


SHARED = DATA_DIR / "llm_scores_nvidia_gpt_oss_20b.sqlite"  # isolated from Gemini-era cached answers


class Llm:
    def __init__(self):
        self.key = os.environ.get("NVIDIA_API_KEY") or self._env("NVIDIA_API_KEY")
        self.client = OpenAI(base_url=NVIDIA_BASE_URL, api_key=self.key, timeout=30) if self.key else None
        self.name = NVIDIA_MODEL if self.client else f"ollama:{OLLAMA_MODEL}"
        self.used, self.fails, self.calls = None, defaultdict(int), defaultdict(int)
        self.db = sqlite3.connect(SHARED, timeout=30, check_same_thread=False, isolation_level=None)
        self.db.execute("CREATE TABLE IF NOT EXISTS s (h TEXT PRIMARY KEY, t REAL, model TEXT, out TEXT)")

    @staticmethod
    def _env(k):
        try:
            for line in (PROJECT_ROOT / ".env").open(encoding="utf-8", errors="ignore"):
                if line.strip().startswith(k + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'") or None
        except OSError:
            pass
        return None

    def score(self, text):
        """Identical prompts (M1 and M2 see the same coins with the same prompt) are scored once."""
        h = hashlib.sha1(text.encode("utf-8")).hexdigest()
        try:
            if self.db.execute("INSERT OR IGNORE INTO s VALUES (?, ?, NULL, NULL)", (h, time.time())).rowcount == 0:
                for _ in range(40):
                    row = self.db.execute("SELECT model, out FROM s WHERE h = ?", (h,)).fetchone()
                    if row and row[1]:
                        self.used = row[0]
                        return self._parse(row[1])
                    time.sleep(0.5)
        except sqlite3.Error:
            pass
        return self._score(text)

    def _parse(self, out):
        d = json.loads(out[out.find("{"):out.rfind("}") + 1])
        return int(d.get("score", 0)), d.get("criterion"), str(d.get("why", ""))[:200], bool(d.get("improves"))

    def _score(self, text):
        h = hashlib.sha1(text.encode("utf-8")).hexdigest()
        try:
            out = None
            out = self.generate(text)
            if out is None:
                self.used = OLLAMA_MODEL
                r = requests.post("http://localhost:11434/api/generate", timeout=120, json={
                    "model": OLLAMA_MODEL, "prompt": text, "stream": False, "format": "json",
                    "options": {"temperature": 0}})
                out = r.json()["response"]
            res = self._parse(out)
            try:
                self.db.execute("UPDATE s SET model = ?, out = ? WHERE h = ?", (self.used, out, h))
            except sqlite3.Error:
                pass
            return res
        except Exception as exc:  # noqa: BLE001 - unscored = no buy
            return None, None, f"llm error: {type(exc).__name__}", False

    def generate(self, text):
        """Generate with NVIDIA; return None on provider failure so callers can fall back to local Ollama."""
        if not self.client:
            return None
        try:
            day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 7 * 3600))
            self.calls[(day, NVIDIA_MODEL)] += 1
            completion = self.client.chat.completions.create(
                model=NVIDIA_MODEL,
                messages=[{"role": "user", "content": text}],
                temperature=1,
                top_p=1,
                max_tokens=4096,
                stream=False,
            )
            self.used = NVIDIA_MODEL
            return completion.choices[0].message.content
        except Exception as exc:  # noqa: BLE001 - avoid logging provider response text or credentials
            self.fails[(NVIDIA_MODEL, type(exc).__name__)] += 1
            return None


def age_text(ts):
    if not ts:
        return "age unknown"
    d = max(0.0, time.time() - float(ts))
    return (f"posted {d / 60:.0f} min ago" if d < 3600 else f"posted {d / 3600:.1f} h ago" if d < 172800 else
            f"posted {d / 86400:.0f} days ago")


def nkeys(link):
    """Narrative keys of an X link: the post id. A profile or community link names a person or group, not a
    narrative, so it gives no key (every coin linking one is judged on its own)."""
    sid = re.search(r"status/(\d+)", link)
    return [sid.group(1)] if sid else []


def tweet_info(s, link):
    """Author, followers and text of the linked X post / profile (fxtwitter, free), and the narrative keys (the
    post plus the post it quotes). Communities: link only."""
    keys = nkeys(link)
    m = re.search(r"(?:x|twitter)\.com/([^/?#]+)(?:/status/(\d+))?", link)
    if not m or m.group(1) in ("i", "home", "search"):
        return "X community / other link (contents not fetched)", keys
    try:
        if m.group(2):
            t = s.get(f"https://api.fxtwitter.com/{m.group(1)}/status/{m.group(2)}", timeout=8).json()["tweet"]
            a = t["author"]
            out = (f"Post by @{a['screen_name']} ({a.get('name', '')}, {a.get('followers', 0):,} followers, "
                   f"{age_text(t.get('created_timestamp'))}): "
                   f"\"{t.get('text', '')[:500]}\"")
            if t.get("replying_to"):
                out += f"\nIt replies to @{t['replying_to']}."
            q = t.get("quote")
            if q and q.get("id"):
                keys.append(str(q["id"]))
            if q:
                qa = q.get("author", {})
                out += (f"\nIt quotes a post by @{qa.get('screen_name')} ({qa.get('name', '')}, "
                        f"{qa.get('followers', 0):,} followers, {age_text(q.get('created_timestamp'))}): "
                        f"\"{q.get('text', '')[:300]}\"")
            return out, keys
        u = s.get(f"https://api.fxtwitter.com/{m.group(1)}", timeout=8).json()["user"]
        return (f"Profile @{u['screen_name']} ({u.get('followers', 0):,} followers): "
                f"{u.get('description', '')[:300]}"), keys
    except Exception:  # noqa: BLE001
        return "X link could not be read", keys


SOC = requests.Session()
SOC.headers.update({"Origin": "https://pump.fun", "Accept": "application/json"})


def engagement(link):
    """Likes + reposts + replies + quotes of the linked X post now (fxtwitter), or None."""
    m = re.search(r"(?:x|twitter)\.com/([^/?#]+)/status/(\d+)", link or "")
    if not m:
        return None
    try:
        t = SOC.get(f"https://api.fxtwitter.com/{m.group(1)}/status/{m.group(2)}", timeout=6).json()["tweet"]
        return sum(int(t.get(k) or 0) for k in ("likes", "retweets", "replies", "quotes"))
    except Exception:  # noqa: BLE001
        return None


def support(m):
    """(unique buyers, buy SOL / sell SOL) over the coin's last 60 s of trades; (0, 0.0) if unreadable."""
    now = time.time()
    try:
        tr = SOC.get(SWAP.format(m), params={"limit": 100}, timeout=8).json()["trades"]
    except Exception:  # noqa: BLE001
        return 0, 0.0
    last = [x for x in tr if iso_ts(x["timestamp"]) >= now - 60]
    buy = sum(float(x["amountSol"]) for x in last if x["type"] == "buy")
    sell = sum(float(x["amountSol"]) for x in last if x["type"] != "buy")
    return len({x["userAddress"] for x in last if x["type"] == "buy"}), (round(buy / sell, 2) if sell else 99.0)


def traction(o):
    """M3 at an exit signal in profit: is the coin still pulling in buyers, or is its post spreading?"""
    o["tp_buyers"], o["tp_flow"] = support(o["m"])
    o["eng_tp"] = engagement(o.get("link"))
    chain = o["tp_buyers"] >= RUN_BUYERS and o["tp_flow"] >= RUN_FLOW
    e0, e1 = o.get("eng0"), o["eng_tp"]
    social = e0 is not None and e1 is not None and e1 >= RUN_SOCIAL * max(e0, 1) and e1 - e0 >= 20
    o["traction"] = "chain+social" if chain and social else "chain" if chain else "social" if social else None
    return o["traction"] is not None


class Rpc:
    """Round-robin over every working RPC endpoint (RESEARCH_RPC_URL + HISTORY_RPC_URLS + public). infra v5
    (2026-10-22 15:55): Helius hit "max usage reached" and every bot went blind; an endpoint that fails or rate-limits
    now rests 10 min while the others carry on, and no single free plan takes all the load."""

    def __init__(self):
        from .history_miner import endpoints
        urls = [env_rpc()] + [u for u, _ in endpoints(3.0)]
        self.urls = list(dict.fromkeys(u for u in urls if u))
        self.url, self.s = self.urls[0], requests.Session()
        self.rest, self.i, self.fcache = {}, 0, {}

    def call(self, method, params):
        for k in range(2 * len(self.urls)):
            now = time.time()
            live = [u for u in self.urls if self.rest.get(u, 0) <= now] or self.urls
            self.i += 1
            u = live[self.i % len(live)]
            try:
                r = self.s.post(u, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15)
                j = r.json() if r.status_code != 429 else {}
                if "result" in j:
                    return j["result"]
            except (requests.RequestException, ValueError):
                pass
            self.rest[u] = now + 600
            time.sleep(0.2 if k < len(self.urls) else 1.0)
        return None

    def curves(self, keys):
        out = {}
        for i in range(0, len(keys), 100):
            res = self.call("getMultipleAccounts", [keys[i:i + 100], {"encoding": "base64"}])
            for k, a in zip(keys[i:i + 100], (res or {}).get("value") or []):
                if a:
                    out[k] = decode_bonding_curve(a["data"][0])
        return out

    def funder(self, wallet):
        """(funder, unix time) of the wallet's first incoming SOL transfer, if it is a fresh wallet."""
        if wallet not in self.fcache:
            self.fcache[wallet] = self._funder(wallet)
        return self.fcache[wallet]

    def _funder(self, wallet):
        sigs = self.call("getSignaturesForAddress", [wallet, {"limit": 1000}])
        if not sigs or len(sigs) >= 1000:
            return None
        tx = self.call("getTransaction", [sigs[-1]["signature"], {"encoding": "jsonParsed",
                                                                  "maxSupportedTransactionVersion": 0}])
        for ins in ((tx or {}).get("transaction") or {}).get("message", {}).get("instructions", []):
            p = ins.get("parsed")
            if isinstance(p, dict) and p.get("type") == "transfer" and p["info"].get("destination") == wallet:
                return p["info"].get("source"), sigs[-1].get("blockTime")
        return None


def price(cs):
    return (cs.virtual_sol_reserves / 1e9) / (cs.virtual_token_reserves / 1e6)


def trades(s, pace, mint, pages=5):
    rows, cur = [], None
    for _ in range(pages):
        pace.wait()
        try:
            r = s.get(SWAP.format(mint), params={"limit": 100, **({"cursor": cur} if cur else {})}, timeout=20)
        except requests.RequestException:
            return rows or None
        if r.status_code == 429:
            pace.bad()
            return rows or None
        if not r.ok:
            return rows or None
        pace.good()
        d = r.json()
        for x in d["trades"]:
            sid = x["slotIndexId"]
            rows.append((int(sid[:12]), iso_ts(x["timestamp"]), x["userAddress"], x["type"] == "buy",
                         float(x["amountSol"]), float(x["baseAmount"])))
        cur = d["pagination"].get("nextCursor")
        if not d["pagination"].get("hasMore") or not cur:
            return rows[::-1]
    return rows[::-1]


def checks(rpc, tr, dev, sol_usd):
    """Volume, bundle and chart checks from the trade list; (ok, reasons, details)."""
    why, det = [], {}
    vol = sum(x[4] for x in tr) * sol_usd
    det["vol_usd"] = round(vol)
    if vol < VOL_MIN:
        return False, ["volume"], det
    slot0 = tr[0][0]
    early = {x[2] for x in tr if x[0] == slot0 and x[3] and x[2] != dev}
    det["create_slot_buyers"] = len(early)
    # create-slot buyers are logged only: sniper bots fill that slot on most launches; the bundle test is funding
    hold = defaultdict(float)
    for x in tr:
        hold[x[2]] += x[5] if x[3] else -x[5]
    top = sorted(((v, w) for w, v in hold.items() if v > 0), reverse=True)[:5]
    det["top5_pct"] = [round(100 * v / 1e9, 2) for v, _ in top]
    vs = [v for v, _ in top]
    if any(abs(a - b) / max(a, b) < 0.005 for i, a in enumerate(vs) for b in vs[i + 1:]):
        why.append("bundle: identical balances")
    fund = [f for f in (rpc.funder(w) for _, w in top if w != dev) if f]
    srcs = [f[0] for f in fund]
    times = sorted(f[1] for f in fund if f[1])
    det["funders"] = len(fund)
    if len(srcs) != len(set(srcs)):
        why.append("bundle: shared funder")
    if any(b - a <= 60 for a, b in zip(times, times[1:])):
        why.append("bundle: funded together")
    if M2:
        if tr[0][2] == dev:                       # full history from the create: measure the sniper bag
            snip = {x[2] for x in tr if x[3] and x[0] <= slot0 + SNIP_SLOTS and x[2] != dev}
            det["sniper_pct"] = round(sum(max(0.0, hold[w]) for w in snip) / 1e7, 2)
            if det["sniper_pct"] > SNIP_MAX:
                why.append("rug: snipers still hold > 8%")
        else:
            det["sniper_pct"] = None              # history starts after the create (very busy coin): not measurable
    if M3:
        now, full = time.time(), tr[0][2] == dev
        holders = sum(1 for w, v in hold.items() if v > 0 and w != dev)
        det["holders"] = holders if full else None              # history truncated = a busy coin, not a dead one
        det["new_buyers_2m"] = len({x[2] for x in tr if x[3] and x[1] >= now - 120})
        if full and holders < DEAD_HOLDERS and det["new_buyers_2m"] < DEAD_GROWTH:
            why.append("dead: < 15 holders, no growth")
        det["dev_pct"] = round(100 * max(0.0, hold.get(dev, 0.0)) / 1e9, 2) if full else None
        if (det["dev_pct"] or 0) > DEV_MAX:
            why.append("rug: dev holds > 10%")
        top10 = sorted(((v, w) for w, v in hold.items() if v > 0 and w != dev), reverse=True)[:10]
        fr = [(v, rpc.funder(w)) for v, w in top10]
        det["fresh_pct"] = round(100 * sum(v for v, f in fr if f and f[1] and f[1] >= now - FRESH_H) / 1e9, 2)
        if det["fresh_pct"] > FRESH_MAX:
            why.append("rug: fresh wallets hold > 25%")
    if M4:
        now, full = time.time(), tr[0][2] == dev
        det["holders"] = sum(1 for v in hold.values() if v > 1) if full else None   # TF definition: > 1 token, dev in
        if full:
            run_bal, h, hmax = defaultdict(float), 0, 0
            for x in tr:
                b0 = run_bal[x[2]]
                run_bal[x[2]] = b1 = b0 + (x[5] if x[3] else -x[5])
                h += (b0 <= 1 < b1) - (b1 <= 1 < b0)
                hmax = max(hmax, h)
            det["holders_max"] = hmax
        px = [(x[1], x[4] / x[5]) for x in tr if x[5] > 0]
        lo60 = min((q for t, q in px if t >= now - 60), default=None)
        flow10 = sum(x[4] if x[3] else -x[4] for x in tr if x[1] >= now - 10)
        if px and lo60 and px[-1][1] >= TOP_UP * lo60 and flow10 < 0:
            why.append("top: >= 1.3x the 60 s low into net selling")
        if full and px and px[-1][1] <= LAUNCH_X * px[0][1] and px[-1][1] <= LAUNCH_HIGH * max(q for _, q in px):
            why.append("launch: back near the launch price in a downtrend")
        if det["holders"] is not None and det["holders"] < H_MIN:
            why.append("watch: < 20 holders")
        elif det["holders"] is not None and det["holders"] < (1 - H_DROP) * det["holders_max"]:
            why.append("watch: holders falling (< 90% of their max)")
    buys = [x[4] for x in tr[1:] if x[3] and x[2] != dev] if tr and tr[0][2] == dev else [x[4] for x in tr if x[3]]
    if buys and max(buys) > 0.40 * sum(buys):
        why.append("chart: one buy dominates")
    return not why, why, det


def run():
    sys.stdout.reconfigure(errors="replace")    # coin names with emoji / non-Latin text must never crash the loop
    OUT.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    s.headers.update({"Origin": "https://pump.fun", "Accept": "application/json"})
    rpc, llm, pace = Rpc(), Llm(), Pace(0.3)
    print(time.strftime("%H:%M"), f"{VARIANT.upper()} paper bot ({RULES}), narrative model {llm.name}", flush=True)
    sol = {"usd": 0.0, "t": 0.0}
    watch, scored, opened = {}, {}, {}           # mint -> create row; mint -> (score, narrative ok, key); mint -> pos
    twins = {}                                   # M7 process only: mint -> M8 twin position (same fill, stop -10%)
    twins12, hype = {}, (Hype() if M12 else None)   # M7 process only: M12 book (M8 + hype gate)
    twins12r = {}                                # M7 process only: M12r book (M12 entries, ratcheting runner exit)
    m1_done = set()                              # M9: coins M1's checks rejected (M5's model can still buy them)
    m9_m5_done = set()                           # M9 v2: coins whose M5 signal already met the PX rule check
    first = {}                                   # narrative key -> the first coin seen on it (Rohit: prefer the first)
    for r in (json.loads(x) for x in CANDS.open(encoding="utf-8")) if CANDS.exists() else ():
        for k in r.get("nkeys") or []:
            if k.isdigit():                      # post ids only (older rows also keyed profiles)
                first.setdefault(k, r)
    done, lock = set(), threading.Lock()
    follow, lastwhy, mc_now = {}, {}, {}         # M4 watchlist view: author followers, last check, market cap
    m5_state = {}                                # M5: mint -> {"bucket": last 10-s step evaluated}
    m5_model = json.loads((DATA_DIR / "m1" / "m5_model.json").read_text()) if M5 else None
    m6_funding = LiveFunding() if M6 else None          # M6: Helius funding lookups for the cluster skip
    serial = SerialFunders() if M7 else None            # M7/M8: creator funders already seen
    pool = ThreadPoolExecutor(1)                 # one LLM call at a time (local GPU)

    def narrate(c, link):
        meta = c.get("_meta") or {}
        desc = str(meta.get("description", ""))[:400]
        info, keys = tweet_info(s, link)
        fm = re.search(r"([\d,]+) followers", info)              # first count = the post author's (or the profile's)
        foll = int(fm.group(1).replace(",", "")) if fm else None
        with lock:
            prev = next((first[k] for k in keys if k in first), None)
            if prev is None:
                for k in keys:
                    first[k] = {"m": c["m"], "name": c.get("name"), "sym": c.get("sym"), "desc": desc}
        earlier = EARLIER.format(name=prev.get("name"), sym=prev.get("sym"), desc=prev.get("desc") or "(none)")             if prev else ""
        sc, crit, why, better = llm.score(PROMPT.format(name=c.get("name"), sym=c.get("sym"), desc=desc, link=link,
                                                        tweet=info, earlier=earlier,
                                                        improves=IMPROVES if prev else ""))
        ok = prev is None or better
        with lock:
            scored[c["m"]] = (sc, ok, keys[0] if keys else c["m"], prev is not None, link)
        jlog(CANDS, {"t": time.time(), "m": c["m"], "name": c.get("name"), "sym": c.get("sym"), "desc": desc,
                     "link": link, "score": sc, "crit": crit, "why": why, "age": round(time.time() - c["t"]),
                     "model": llm.used, "nkeys": keys, "first": prev is None,
                     "improves": better if prev else None, "after": prev.get("name") if prev else None,
                     "followers": foll})
        follow[c["m"]] = foll
        if M4 and foll is not None and not FOLL[0] <= foll < FOLL[1]:
            with lock:
                done.add(c["m"])                         # m4-v3: author outside 100-10k followers

    def intake(c):
        if not c["m"].endswith("pump") or c.get("mh"):
            return
        meta = None
        for k in range(3):                       # infra v2: one failed fetch used to drop the coin silently (Remi Collet)
            try:
                meta = s.get(c["uri"], timeout=6).json() if c.get("uri") else {}
                break
            except Exception:  # noqa: BLE001
                time.sleep(1 + k)
        if meta is None:
            return
        link = next((str(meta[k]) for k in ("twitter", "website", "telegram") if LINK.match(str(meta.get(k) or ""))),
                    None)
        if not link:
            return
        c["_meta"] = meta
        with lock:
            watch[c["m"]] = c
        pool.submit(narrate, c, link)

    meta_pool = ThreadPoolExecutor(8)
    pos, last_note, last_check = 0, 0.0, defaultdict(float)
    with CREATES.open("rb") as fh:
        fh.seek(0, 2)
        pos = fh.tell()
    while True:
        t_loop = time.time()
        if t_loop - sol["t"] > 300:
            try:
                sol["usd"] = float(s.get(GT, timeout=15).json()["data"]["attributes"]["base_token_price_usd"])
                sol["t"] = t_loop
            except Exception:  # noqa: BLE001
                pass
        with CREATES.open("rb") as fh:
            fh.seek(pos)
            for line in fh:
                if not line.endswith(b"\n"):
                    break
                pos += len(line)
                if line.strip():
                    meta_pool.submit(intake, json.loads(line))
        with lock:
            for m in [m for m, c in watch.items() if t_loop - c["t"] > WATCH_S and m not in opened and m not in twins and m not in twins12 and m not in twins12r]:
                watch.pop(m)
            keys = {c["bc"]: m for m, c in watch.items() if c.get("bc")}
        states = rpc.curves(list(keys)) if keys and sol["usd"] else {}
        for bc, cs in states.items():
            m = keys[bc]
            c = watch[m]
            if cs is None:
                continue
            p = cs.price_sol if not cs.complete else None
            if p and sol["usd"]:
                mc_now[m] = p * 1e9 * sol["usd"]
            if m in opened or m in twins or m in twins12 or m in twins12r:
                if m in opened:
                    manage(opened, m, p, cs, done, rpc)
                if m in twins:                                   # M8 twin: same price read, its own stop
                    manage(twins, m, p, cs, done, rpc, log=TWIN_DIR / "trades.jsonl", label=TWIN_LABEL)
                if m in twins12:                                 # M12: same price read, M8's stop
                    manage(twins12, m, p, cs, done, rpc, log=M12_DIR / "trades.jsonl", label=M12_LABEL)
                if m in twins12r:                                # M12r: same price read, ratcheting runner exit
                    manage(twins12r, m, p, cs, done, rpc, log=M12R_DIR / "trades.jsonl", label=M12R_LABEL)
                continue
            if p is None or m in done or len(opened) >= MAX_OPEN:
                continue
            mc = p * 1e9 * sol["usd"]
            sc, narr_ok, nkey, copy, link = scored.get(m, (None, False, None, False, None))
            m1path = False                                       # M9: this coin is on M1's entry path this poll
            if M5:
                if m not in scored:
                    continue
                rows5, full5 = feed_rows(m)
                if not rows5 or not full5:
                    continue
                go, det5 = m10_decide(m, rows5, m5_state) if M10 else m5_decide(m, rows5, m5_state, m5_model)
                if M9 and m in m9_m5_done:
                    go, det5 = False, None                       # M9: the M5 signal is checked once per coin
                if go and M9:
                    m9_m5_done.add(m)
                    ok9, d9 = m9_check(m6_funding, serial, rows5)
                    det5.update(m9_src="m5", **d9)
                    go = ok9
                elif go and M6:
                    try:
                        fl = m6_funding.cluster_share(rows5, len(rows5) - 1)
                    except Exception as exc:  # noqa: BLE001 - lookup failed: no trade rather than an unchecked one
                        fl = None
                        det5.update(final=True, why=f"M6 skip: funding lookup failed ({type(exc).__name__})")
                        go = False
                    if fl is not None:
                        det5.update(m6_cluster=round(fl["cluster_share"], 3), m6_creator=round(fl["creator_linked"], 3),
                                    m6_fresh=round(fl["fresh_share"], 3))
                        seen_before = serial.check(m6_funding, rows5[0][4]) if M7 else 0
                        if M7:
                            det5.update(m7_serial=seen_before)
                        if fl["cluster_share"] >= M6_CLUSTER:
                            go = False
                            det5.update(final=True, why=f"M6 skip: funding cluster {fl['cluster_share']:.0%}")
                        elif M7 and seen_before is None:
                            go = False
                            det5.update(final=True, why="M7 skip: creator funder lookup failed")
                        elif M7 and seen_before >= 1:
                            go = False
                            det5.update(final=True, why=f"M7 skip: creator's funder funded {seen_before} earlier creator(s)")
                if det5:
                    jlog(CANDS, {"t": time.time(), "m": m, "stage": "m5", "mc": round(mc), "buy": go, **det5})
                skipped9 = bool(det5) and str(det5.get("why", "")).startswith(("M6 skip", "M7 skip")) and not M9
                if not go and not (M9 and not skipped9):
                    if det5 and det5.get("final"):
                        done.add(m)
                    continue
                m1path = M9 and not go                           # M9: the model said no; try M1's signal instead
                if m1path and m in m1_done:
                    continue
                if not m1path:
                    tr = [(0, r[0], r[4], r[2], r[3], r[5]) for r in rows5]
            top = MC_HIGH if (sc or 0) >= SCORE_HIGH else MC_BUY[1]
            if (not M5 or m1path) and (sc is None or sc < SCORE_MIN or not narr_ok or not (MC_MIN <= mc and MC_BUY[0] <= mc <= top)):
                continue
            if (not M5 or m1path) and t_loop - last_check[m] < RECHECK_S:
                continue
            last_check[m] = t_loop
            ok, why, det = True, [], {}
            if not M5 or m1path:
                tr = None
            src = "feed" if M5 and not m1path else "swap-api"
            if M4:                                               # real-time feed; the swap-api list lags
                tr, full = feed_trades(m)
                if not full:                                     # feed down, or it missed the create / had a gap
                    tr = None
                src = "feed" if tr else "swap-api"
            if not tr:
                tr = trades(s, pace, m)
            if not tr:
                continue
            if not M5 or m1path:
                ok, why, det = checks(rpc, tr, c.get("dev"), sol["usd"])
            if M3 and copy and det.get("vol_usd", 0) < VOL_COPY and "volume" not in why:
                ok, why = False, why + ["volume: copy coin < $3k"]
            if M4:
                det["src"] = src
            if ok and m1path:                                    # M9 v2: M1's signal passed; the PX rule decides
                ok9, d9 = m9_check(m6_funding, serial, rows5)
                det.update(m9_src="m1", **{k: v for k, v in d9.items() if k != "why"})
                if not ok9:
                    why = why + [d9["why"]]
                ok = ok9
            if not M5 or m1path:
                jlog(CANDS, {"t": time.time(), "m": m, "stage": "checks", "mc": round(mc), "ok": ok, "why": why, **det})
            lastwhy[m] = {"t": time.time(), "why": why, "holders": det.get("holders"), "h_max": det.get("holders_max")}
            if not ok:
                if m1path and not any(w.startswith("M9 skip") for w in why):
                    if not any(w.startswith(("volume", "dead", "watch")) for w in why):
                        m1_done.add(m)                           # M9: M1's checks said no; M5's model may still buy
                    continue
                if not any(w.startswith(("volume", "dead", "watch")) for w in why):   # these can change: re-check
                    done.add(m)
                continue
            fresh = rpc.curves([bc]).get(bc)                     # fill at the state after our decision
            if not fresh or fresh.complete:
                continue
            v = fresh.virtual_sol
            q = V0 * T0 / v - V0 * T0 / (v + SIZE_SOL * (1 - CURVE_FEE))
            opened[m] = dict(m=m, name=c.get("name"), score=sc, t=time.time(), p0=price(fresh), q=q, peak=price(fresh),
                             stop=STOP, high=(sc or 0) >= SCORE_HIGH and not M5, bc=bc, rules=RULES, mc=round(mc),
                             sol_usd=sol["usd"], nkey=nkey)
            if M9:
                opened[m]["src9"] = "m1" if m1path else "m5"
                if m1path:                                       # holder book from the feed, as on the M5 side
                    tr = [(0, r[0], r[4], r[2], r[3], r[5]) for r in rows5]
            if (M4 and tr[0][2] == c.get("dev")) or M5:        # holder book for the holder exit
                bal = defaultdict(float)
                for x in tr:
                    bal[x[2]] += x[5] if x[3] else -x[5]
                h0 = sum(1 for v in bal.values() if v > 1)
                opened[m].update(bal=dict(bal), seen=[list(x[:4]) for x in tr[-100:]], last_slot=tr[-1][0],
                                 holders=h0, h_max=h0, t_h=0.0)
            if M3:                                               # exits by traction, not by score
                opened[m].update(high=False, link=link, copy=copy, eng0=engagement(link))
            if TWIN:
                twins[m] = deepcopy(opened[m])
                twins[m].update(stop=TWIN_STOP, rules=TWIN_RULES, twin_of=VARIANT)
            if M12:
                g = hype.gate(m)
                M12_DIR.mkdir(exist_ok=True)
                with (M12_DIR / "cands.jsonl").open("a") as fh:
                    fh.write(json.dumps(dict(t=time.time(), m=m, name=c.get("name"), stage="m12", **g)) + "\n")
                if g["go"]:
                    twins12[m] = deepcopy(opened[m])
                    twins12[m].update(stop=M12_STOP, rules=M12_RULES, twin_of=VARIANT, hype=g)
                    twins12r[m] = deepcopy(twins12[m])
                    twins12r[m].update(rules=M12R_RULES, ladder=True)
                    print(time.strftime("%H:%M"), f"M12 BUY {c.get('name')!r} hype {g}", flush=True)
            print(time.strftime("%H:%M"), f"BUY  {c.get('name')!r} score {sc} mc ${mc:,.0f}", flush=True)
        if M12:
            try:
                M12_DIR.mkdir(exist_ok=True)
                (M12_DIR / "status.json").write_text(json.dumps({"t": time.time(), "sol_usd": sol["usd"], "twin_of": VARIANT,
                    "callers": len(hype.callers), "open": [{k: o.get(k) for k in ("m", "name", "score", "t", "p0", "last", "peak", "stop", "mc",
                                                    "holders", "h_max", "hype")} for o in twins12.values()]}))
                M12R_DIR.mkdir(exist_ok=True)
                (M12R_DIR / "status.json").write_text(json.dumps({"t": time.time(), "sol_usd": sol["usd"], "twin_of": VARIANT,
                    "open": [{k: o.get(k) for k in ("m", "name", "score", "t", "p0", "last", "peak", "stop", "mc", "holders", "h_max",
                                                    "hype", "lad_on", "lad_stop")} for o in twins12r.values()]}))
            except OSError:
                pass
        if TWIN:
            try:
                TWIN_DIR.mkdir(exist_ok=True)
                (TWIN_DIR / "status.json").write_text(json.dumps({"t": time.time(), "sol_usd": sol["usd"], "twin_of": VARIANT,
                    "open": [{k: o.get(k) for k in ("m", "name", "score", "t", "p0", "last", "peak", "stop", "mc",
                                                    "holders", "h_max")} for o in twins.values()]}))
            except OSError:
                pass
        try:                                                     # for the dashboard (tradeslave.lab_view)
            OPEN.write_text(json.dumps({"t": time.time(), "sol_usd": sol["usd"], "llm": llm.name,
                                        "calls": {f"{d} {m}": n for (d, m), n in llm.calls.items()},
                                        "fails": {str(k): n for k, n in llm.fails.items()},
                                        "watching": len(watch), "scored": len(scored),
                                        "open": [{k: o.get(k) for k in ("m", "name", "score", "t", "p0", "last", "peak",
                                                                        "stop", "mc", "holders", "h_max")}
                                                 for o in opened.values()],
                                        "watch": [watch_row(m, watch[m], scored[m], follow.get(m), lastwhy.get(m),
                                                            mc_now.get(m), m in done)
                                                  for m in list(watch) if m in scored and m not in opened
                                                  and (scored[m][0] or 0) >= SCORE_MIN] if M4 else []}))
        except OSError:
            pass
        if time.time() - last_note > 600:
            last_note = time.time()
            print(time.strftime("%H:%M"), f"watching {len(watch)} with X links, scored {len(scored)}, "
                  f"open {len(opened)}, closed {summary()[0]}, llm calls {dict(llm.calls)} fails {dict(llm.fails)}",
                  flush=True)
        time.sleep(max(0.0, POLL_S - (time.time() - t_loop)))


def m9_check(funding, serial, rows):
    """M9 v2 (PX rule): (ok, details) at the last feed trade."""
    t = rows[-1][0]
    flow = sum(r[3] if r[2] else -r[3] for r in rows if r[0] > t - 30)
    try:
        funding.cluster_share(rows, len(rows) - 1)              # looks up (and caches) the creator's funder
        n = serial.check(funding, rows[0][4], strict=True)
    except Exception as exc:  # noqa: BLE001 - lookup failed: no trade rather than an unchecked one
        return False, {"m9_flow": round(flow, 2), "why": f"M9 skip: funding lookup failed ({type(exc).__name__})"}
    det = {"m9_flow": round(flow, 2), "m7_serial": n}
    if n is None:
        return False, {**det, "why": "M9 skip: creator funder unknown or program-owned"}
    if n >= 1:
        return False, {**det, "why": f"M9 skip: creator's funder funded {n} earlier creator(s)"}
    if flow < M9_FLOW:
        return False, {**det, "why": f"M9 skip: 30 s net flow {flow:.1f} SOL < {M9_FLOW:.1f}"}
    return True, det


class SerialFunders:
    """M7/M8: how many earlier M5-signalled creators each funder funded (program-owned funders excluded)."""

    def __init__(self):
        from .fl_test import NONSYSTEM
        self.nonsystem = NONSYSTEM
        self.path = OUT / "serial_seen.json"
        seed = SERIAL_SEED_PX if M9 else SERIAL_SEED
        src = self.path if self.path.exists() else seed
        self.seen = defaultdict(int, json.loads(src.read_text()) if src.exists() else {})

    def service(self, f):
        """v3 (2026-10-25): a funder making >= 5 transactions a minute (last 1,000) is a service wallet (exchange, Axiom ...).
        Live, those piled up in `seen` and blocked ~90% of signals for 15 h. Cached in data/m1/service_funders.json."""
        p = DATA_DIR / "m1" / "service_funders.json"
        if not hasattr(self, "svc"):
            try:
                self.svc = json.loads(p.read_text())
            except (OSError, ValueError):
                self.svc = {}
        if f not in self.svc:
            try:
                from .m13_bot import rpc
                sig = rpc("getSignaturesForAddress", [f, {"limit": 1000}])
                span = (sig[0]["blockTime"] - sig[-1]["blockTime"]) / 60 if len(sig) > 1 else 0
                self.svc[f] = bool(len(sig) >= 1000 and span and len(sig) / span >= 5)
                p.write_text(json.dumps(self.svc))
            except Exception:  # noqa: BLE001 - unknown stays "not a service"
                return False
        return self.svc[f]

    def check(self, funding, creator, count=True, strict=False):
        """Times the creator's funder was seen before this coin (None if unknown); counts this coin unless count=False.
        strict (M9 v2, as the PX table): a program-owned funder is unknown too."""
        f = (funding.cache.get(creator) or {}).get("funder")
        if not f:
            return None
        if f in self.nonsystem:
            return None if strict else 0
        if self.service(f):                      # exchange / terminal hot wallet: funds everyone, says nothing about the dev
            return 0
        n = self.seen[f]
        if not count:
            return n
        self.seen[f] += 1
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.seen))
        os.replace(tmp, self.path)
        return n


def manage(opened, m, p, cs, done, rpc=None, log=None, label=""):
    """Exit decided on one poll, filled at the curve state of the next poll (1 s later)."""
    o = opened[m]
    now = time.time()
    if p is None:                                               # graduated: sell at the last price seen
        why, p = o.get("pending") if o.get("pending") not in (None, "HALF") else "GRAD", o.get("last", o["p0"])
    elif o.get("pending") == "HALF":                            # M3 v3: bank half at the ride start, ride the rest
        o["cash"] = sell_curve(o["q"] / 2, p) - TX_SOL
        o["q"] /= 2
        o.update(pending=None, half_at=p / o["p0"])
        return
    elif o.get("pending"):
        why = o["pending"]
    else:
        if p > o["peak"]:
            o["t_peak"] = now
        o["peak"], o["last"] = max(o["peak"], p), p
        if rpc and now - o.get("t_act", 0) >= ACT_EVERY:      # activity: curve txs in the last 60 s
            sigs = rpc.call("getSignaturesForAddress", [o["bc"], {"limit": 300}]) or []
            o["act"] = sum(1 for x in sigs if (x.get("blockTime") or 0) >= now - 60)
            o["act_max"], o["t_act"] = max(o.get("act_max", 0), o["act"]), now
        act, amax = o.get("act"), o.get("act_max") or 0
        r, held = p / o["p0"], now - o["t"]
        if (M4 or M5) and o.get("bal") is not None and now - o["t_h"] >= H_EVERY:
            holders_update(o, now)
        if o.get("ladder") and (o.get("lad_on") or r >= LAD_ARM):   # M12r runner mode: the stop only moves up
            if not o.get("lad_on"):
                o.update(lad_on=True, lad_stop=LAD_FIRST, t_lad=now)
                print(time.strftime("%H:%M"), f"{label}RUN {o['name']!r} hit {r:.2f}x: stop to {LAD_FIRST}x", flush=True)
            n = int(o["peak"] / o["p0"])
            if n >= 3 and n - 1 > o["lad_stop"]:
                o["lad_stop"] = float(n - 1)
                print(time.strftime("%H:%M"), f"{label}RUN {o['name']!r} peak {o['peak'] / o['p0']:.2f}x: stop to {n - 1}x", flush=True)
            if now - o.get("t_flow", 0) >= 5:                    # 60 s net SOL flow from the feed, every 5 s
                rows, _ = feed_rows(m)
                if rows:
                    w = [x for x in rows if x[0] > now - 60]
                    o["flow60"] = sum(x[3] if x[2] else -x[3] for x in w)
                o["t_flow"] = now
            o["pending"] = ("LSTOP" if r <= o["lad_stop"] else
                            "STALLED" if now - o.get("t_peak", o["t"]) > LAD_STALL_S and o.get("flow60", 1.0) <= 0 else
                            "DYING" if act is not None and amax and act < LAD_DYING * amax else
                            "TIME" if now - o["t_lad"] > LAD_HOLD else None)
            return
        if ((M4 or M5) and r > o["stop"] and o.get("bal") is not None and o["holders"] <= (1 - H_DROP) * o["h_max"]
                and (not M5 or now - o["t"] >= M5_GRACE)):
            o["pending"] = "HDECL"
            return
        if o.get("run"):                                        # M3 ride: hold the other half until support fades
            if now - o.get("t_sup", 0) >= SUP_EVERY:
                o["sup"], o["sup_flow"] = support(m)
                o["sup_max"], o["t_sup"] = max(o.get("sup_max", 0), o["sup"]), now
            fade = o["sup"] < SUP_FADE * o["sup_max"] and o["sup_flow"] < 1.0
            o["pending"] = ("FADE" if fade else "FLOOR" if r <= o["floor"] else
                            "CRASH" if p <= o["peak"] * RUN_GIVE else "TIME" if now - o["t_run"] > RUN_HOLD else None)
        elif o.get("high"):
            o["pending"] = ("STOP" if r <= o["stop"] else
                            "TREND" if o["peak"] >= o["p0"] * HI_ARM and p <= o["peak"] * HI_GIVE else
                            "DEAD" if act is not None and held > 60 and act < HI_DEAD * amax and r < 1 else
                            "TIME" if held > HI_HOLD else None)
        else:
            o["pending"] = ("TP" if r >= TP and not o.get("ladder") else "STOP" if r <= o["stop"] else
                            "TREND" if o["peak"] >= o["p0"] * TREND_ARM and p <= o["peak"] * TREND_GIVE else
                            "STALL" if (act is not None and now - o.get("t_peak", o["t"]) > STALL_S
                                        and act < STALL_ACT * amax) else
                            "TIME" if held > MAX_HOLD else None)
            if M3 and o["pending"] in ("TP", "TREND", "STALL", "TIME") and r >= RIDE_MIN and traction(o):
                o.update(run=True, t_run=now, ride_from=o["pending"], floor=min(RUN_FLOOR, 0.8 * r),
                         sup=o["tp_buyers"], sup_flow=o["tp_flow"], sup_max=o["tp_buyers"], t_sup=now, pending="HALF")
                print(time.strftime("%H:%M"), f"RIDE {o['name']!r} at {r:.2f}x (half sold) instead of {o['ride_from']} "
                      f"({o['traction']}: {o['tp_buyers']} buyers/min, flow {o['tp_flow']})", flush=True)
        return
    out = sell_curve(o["q"], p) + o.get("cash", 0.0)
    ret = (out - 2 * TX_SOL) / SIZE_SOL - 1
    row = {**{k: v for k, v in o.items() if k not in ("bal", "seen")}, "exit": why, "t_exit": time.time(), "p_exit": p,
           "ret": ret}
    jlog(log or TRADES_LOG, row)
    opened.pop(m)
    done.add(m)
    print(time.strftime("%H:%M"), f"{label}SELL {o['name']!r} {why} {ret:+.1%}", flush=True)


def m10_decide(m, rows, state):
    """M10 v2: (buy?, details), evaluated once per 15-s step from 15 s to 180 s after the coin's first trade."""
    from .lw_test import feats
    t0 = rows[0][0]
    age = time.time() - t0
    if age > 180:
        return False, {"final": True, "why": "no M10 signal within 180 s"}
    step = int(age // 15)
    if step < 1 or step <= state.get(m, -1):
        return False, None
    state[m] = step
    rows = [r for r in rows if r[1] and r[1] > 0]          # a few feed rows carry no curve state (price 0)
    if len(rows) < 5:
        return False, {"step": step, "why": "fewer than 5 priced trades"}
    k = len(rows) - 1
    t = rows[k][0]
    w60 = [r for r in rows if r[0] > t - 60]
    if len(w60) < 5:
        return False, {"step": step, "why": "fewer than 5 trades in 60 s"}
    f = feats(rows, k, [], defaultdict(lambda: [0, 0]))
    f["avg_buy_60"] = statistics.mean([r[3] for r in w60 if r[2]] or [0.0])
    go = all((f[n] >= c) if op == ">=" else (f[n] <= c) for n, op, c in M10_RULE)
    return go, {"step": step, **{n: round(f[n], 3) for n, _, _ in M10_RULE}}


def m5_decide(m, rows, state, model):
    """M5: (buy?, details) for one coin now. Evaluated once per 10-s step after its first band trade, up to +180 s."""
    from .m5_bt import Coin, identical_top5
    from .runners import decision
    t0 = rows[0][0]
    i = decision(rows, t0)
    if i is None:
        return False, None
    since = time.time() - rows[i][0]
    if since > M5_MAX_S:
        return False, {"final": True, "why": "no runner signal within 180 s"}
    step = int(since // M5_EVERY)
    if step <= state.get(m, -1):
        return False, None
    state[m] = step
    f, k = Coin({"rows": rows, "t0": t0}).feats(min(time.time(), rows[-1][0] + 60), i)
    x = [(math.log1p(max(0.0, f[n])) if n not in ("vs_band", "vs_high") else f[n]) for n in model["feats"]]
    z = model["intercept"] + sum(w * (v - mu) / sd for w, v, mu, sd in zip(model["coef"], x, model["mu"], model["sd"]))
    p = 1 / (1 + math.exp(-z))
    go = p >= model["P"] and f["mc_k"] <= 20 and not identical_top5(rows, k)
    return go, {"p": round(p, 3), "step": step, **{n: round(f[n], 3) for n in ("holders", "buy30", "mc_k", "vs_band")}}


def watch_row(m, c, sc, foll, last, mc, dropped):
    """One M4 watchlist entry for the dashboard: what the coin still needs before M4 would buy it."""
    score, narr_ok = sc[0], sc[1]
    if not narr_ok:
        need = "copy coin (no buy)"
    elif dropped and foll is not None and not FOLL[0] <= foll < FOLL[1]:
        need = "author followers outside 100-10k (no buy)"
    elif dropped:
        need = "ruled out: " + ", ".join((last or {}).get("why") or ["checks"])
    elif mc is None or mc < MC_BUY[0]:
        need = "market cap below $4k"
    elif mc > (MC_HIGH if (score or 0) >= SCORE_HIGH else MC_BUY[1]):
        need = "market cap above the buy band"
    elif last and last.get("why"):
        need = "; ".join(last["why"])
    else:
        need = "in the buy band, checking"
    return {"m": m, "name": c.get("name"), "sym": c.get("sym"), "score": score, "followers": foll, "mc": mc,
            "t0": c.get("t"), "holders": (last or {}).get("holders"), "h_max": (last or {}).get("h_max"),
            "need": need, "active": not dropped and narr_ok, "link": sc[4]}


def holders_update(o, now):
    """M4: holders now, from the real-time feed (all trades since create), else by folding the swap-api's newest
    trades into the holder book; a gap in the history switches the exit off."""
    o["t_h"] = now
    rows, full = feed_trades(o["m"])
    if rows and full:
        bal = defaultdict(float)
        for x in rows:
            bal[x[2]] += x[5] if x[3] else -x[5]
        o["holders"] = sum(1 for v in bal.values() if v > 1)
        o["h_max"] = max(o["h_max"], o["holders"])
        o["h_src"] = "feed"
        return
    try:
        rows = SOC.get(SWAP.format(o["m"]), params={"limit": 100}, timeout=8).json()["trades"]
    except Exception:  # noqa: BLE001
        return
    seen = {tuple(x) for x in o["seen"]}
    new = []
    for x in reversed(rows):                                    # oldest first
        if x.get("program", "pump") != "pump":
            continue
        k = [int(x["slotIndexId"][:12]), iso_ts(x["timestamp"]), x["userAddress"], x["type"] == "buy"]
        if k[0] < o["last_slot"] or tuple(k) in seen:
            continue
        new.append((k, float(x["baseAmount"])))
    if len(new) >= 100:
        o["bal"] = None                                         # more than one page since the last read: book broken
        return
    for k, tok in new:
        o["bal"][k[2]] = o["bal"].get(k[2], 0.0) + (tok if k[3] else -tok)
        o["seen"].append(k)
        o["last_slot"] = max(o["last_slot"], k[0])
    o["seen"] = o["seen"][-200:]
    o["holders"] = sum(1 for v in o["bal"].values() if v > 1)
    o["h_max"] = max(o["h_max"], o["holders"])


def summary():
    if not TRADES_LOG.exists():
        return 0, []
    v = [json.loads(x)["ret"] for x in TRADES_LOG.open(encoding="utf-8")]
    return len(v), v


def report():
    n, v = summary()
    rows = [json.loads(x) for x in TRADES_LOG.open(encoding="utf-8")] if n else []
    if n < 2:
        print(f"trades {n}", v)
        return
    m, sd = statistics.mean(v), statistics.stdev(v)
    print(f"trades {n} mean {m:+.2%} median {statistics.median(v):+.2%} win {sum(x > 0 for x in v) / n:.0%} "
          f"t {m / sd * math.sqrt(n) if sd else 0:+.2f} 2x {sum(r['exit'] == 'TP' for r in rows)} "
          f"sum {sum(v) * SIZE_SOL:+.3f} SOL")
    key = {}
    for c in (json.loads(x) for x in CANDS.open(encoding="utf-8")):
        if "link" in c:
            key.setdefault(c["m"], (c.get("nkeys") or nkeys(c["link"]) or [c["m"]])[0])
    g = defaultdict(list)
    for r in rows:
        g[r.get("nkey") or key.get(r["m"], r["m"])].append(r["ret"])
    nv = [statistics.mean(x) for x in g.values()]
    if len(nv) > 1:
        nm, nsd = statistics.mean(nv), statistics.stdev(nv)
        print(f"per narrative (trades on the same post averaged): n {len(nv)} mean {nm:+.2%} "
              f"t {nm / nsd * math.sqrt(len(nv)) if nsd else 0:+.2f}")
    for r in rows[-15:]:
        print(f"  {time.strftime('%m-%d %H:%M', time.localtime(r['t']))} {str(r['name'])[:24]:24s} "
              f"score {r['score']} {r['exit']:5s} {r['ret']:+.1%}")


def latency():
    """Fill sensitivity: re-price each closed trade as if entry and exit had landed d seconds later, from the coin's
    actual trade-by-trade prices (pump.fun trade API). Fees and own impact stay as recorded."""
    import bisect
    from .c1_curve import fetch_trades
    s = requests.Session()
    s.headers.update({"Origin": "https://pump.fun", "Accept": "application/json"})
    pace = Pace(0.3)
    creates = {}
    for x in CREATES.open(encoding="utf-8"):
        c = json.loads(x)
        creates[c["m"]] = c["t"]
    rows = [json.loads(x) for x in TRADES_LOG.open(encoding="utf-8")]
    out = defaultdict(list)
    for r in rows:
        tr, _ = fetch_trades(s, pace, r["m"], creates.get(r["m"], r["t"] - 120), r["t_exit"] + 90)
        tr = [x for x in tr or [] if not x[7]]
        if not tr:
            continue
        ts = [x[2] for x in tr]

        def px(t):
            i = bisect.bisect_right(ts, t) - 1
            return tr[max(i, 0)][8]
        base = px(r["t"]), px(r["t_exit"])
        line = []
        for d in (0, 1, 2, 3, 5):
            g = (1 + r["ret"]) * (px(r["t_exit"] + d) / base[1]) / (px(r["t"] + d) / base[0]) - 1
            out[d].append(g)
            line.append(f"{d}s {g:+.0%}")
        print(f"  {str(r['name'])[:20]:20s} {r['exit']:5s} recorded {r['ret']:+.0%} | " + " ".join(line), flush=True)
    for d, v in sorted(out.items()):
        m = statistics.mean(v)
        sd = statistics.stdev(v) if len(v) > 1 else 0
        print(f"both fills +{d}s: n {len(v)} mean {m:+.2%} median {statistics.median(v):+.2%} "
              f"t {m / sd * math.sqrt(len(v)) if sd else 0:+.2f}")


def main() -> None:
    sys.stdout.reconfigure(errors="replace")
    {"run": run, "report": report, "latency": latency}[sys.argv[1]]()


if __name__ == "__main__":
    main()
