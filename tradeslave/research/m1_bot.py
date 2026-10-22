"""M1: Rohit's manual new-pairs process, automated (PAPER only: no wallet, no signing, no order path).
Vault: "2026-10-21 M1 automated manual process".

    uv run python -m tradeslave.research.m1_bot run      # live paper loop
    uv run python -m tradeslave.research.m1_bot report   # results so far

Process (Rohit, 2026-10-20), step by step:
1. New pump.fun pairs (launch_watch logs every create to data/c1/creates.jsonl) with an X post / profile /
   community link in the metadata.
2. Narrative read by an LLM against his four criteria (influential poster, project with real potential,
   community cause like charity, correcting an existing narrative). Claude Haiku if ANTHROPIC_API_KEY is set,
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
from concurrent.futures import ThreadPoolExecutor

import requests

OLLAMA_MODEL = "qwen2.5:7b"                    # the 14b model needs more free RAM than this PC has
from ..paths import DATA_DIR
from ..scalper.curve import decode_bonding_curve
from .c1_curve import CREATES, CURVE_FEE, SOL_USDC, T0, V0, sell_curve
from .chain import env_rpc
from .full_trades import Pace, ts as iso_ts

VARIANT = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] in ("m1", "m2", "m3") else "m1"
M3 = VARIANT == "m3"                             # M3 (2026-10-21 16:00): M2 + Rohit's post-mortem changes (see below)
M2 = VARIANT in ("m2", "m3")                     # M2 (2026-10-21 09:40): M1 + sniper-bag filter, -10% stop, 1 s polls
OUT = DATA_DIR / VARIANT
CANDS, TRADES_LOG, OPEN = OUT / "cands.jsonl", OUT / "trades.jsonl", OUT / "status.json"
SWAP = "https://swap-api.pump.fun/v2/coins/{}/trades"
GT = "https://api.geckoterminal.com/api/v2/networks/solana/pools/" + SOL_USDC
MC_MIN, VOL_MIN, MC_BUY, MC_HIGH = 4000.0, 1500.0, (4000.0, 7000.0), 10000.0   # Rohit: 5-7k best, 4-5k ok,
                                                                         # up to 10k if the narrative is really good
SCORE_MIN, SCORE_HIGH = 7, 9
GEMINI = (("gemini-3.8-flash", 9_000), ("gemini-3.5-flash-lite", 140_000))   # (model, daily budget): free-tier RPD
                                                 # 10k / 150k (Rohit's AI Studio limits, 2026-10-21); resets at midnight PT
TP, STOP, TREND_ARM, TREND_GIVE, STALL_S, STALL_ACT, MAX_HOLD = 2.0, 0.70, 1.30, 0.80, 120, 0.50, 600
HI_ARM, HI_GIVE, HI_DEAD, HI_HOLD = 1.50, 0.60, 0.20, 7200
if M2:
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
RULES = "m3-v3" if M3 else "m2-v1" if M2 else "exits-v2"                                # bump on every rule change; the dashboard splits results by it
ACT_EVERY = 6.0
WATCH_S, POLL_S, RECHECK_S = 20 * 60, (1.0 if M2 else 2.0), 30
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


SHARED = DATA_DIR / "llm_scores.sqlite"   # infra v2 (2026-10-21 22:50): bots with the same prompt share one answer


class Llm:
    def __init__(self):
        self.key = os.environ.get("GEMINI_API_KEY") or self._env("GEMINI_API_KEY")
        self.name = "+".join(m for m, _ in GEMINI) if self.key else f"ollama:{OLLAMA_MODEL}"
        self.used, self.fails, self.calls = None, defaultdict(int), defaultdict(int)   # calls: (PT date, model) -> n
        self.cool = {}                                          # model -> unix time its per-minute 429 cools off
        self.db = sqlite3.connect(SHARED, timeout=30, check_same_thread=False, isolation_level=None)
        self.db.execute("CREATE TABLE IF NOT EXISTS s (h TEXT PRIMARY KEY, t REAL, model TEXT, out TEXT)")

    @staticmethod
    def _env(k):
        try:
            for line in open(".env", encoding="utf-8", errors="ignore"):
                if line.strip().startswith(k + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'") or None
        except OSError:
            pass
        return None

    def score(self, text):
        """Identical prompts (M1 and M2 see the same coins with the same prompt) are scored once: the first bot claims
        the prompt, the others wait up to 20 s for its answer instead of spending the shared Gemini quota again."""
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
            if self.key:
                out = self._gemini(text)
            if out is None:                                     # no key, or Gemini refused / rate-limited / slow
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

    def _gemini(self, text):
        day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 7 * 3600))
        for model, budget in GEMINI:
            if self.calls[(day, model)] >= budget or self.cool.get(model, 0) > time.time():
                continue
            self.calls[(day, model)] += 1
            try:
                r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                                  headers={"x-goog-api-key": self.key}, timeout=20, json={
                                      "contents": [{"parts": [{"text": text}]}],
                                      "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                                                           "thinkingConfig": {"thinkingLevel": "low"}}})
                if r.ok:
                    self.used = model
                    return r.json()["candidates"][0]["content"]["parts"][0]["text"]
                self.fails[(model, r.status_code)] += 1
                if r.status_code == 429 and "PerDay" in r.text:   # daily quota gone: the next model for the rest of the day
                    self.calls[(day, model)] = budget
                elif r.status_code == 429:                      # per-minute limit (infra v2): back off 2 min, not all day
                    self.cool[model] = time.time() + 120
            except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
                self.fails[(model, type(exc).__name__)] += 1
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
    def __init__(self):
        self.url, self.s = env_rpc(), requests.Session()
        self.fcache = {}

    def call(self, method, params):
        for k in range(3):
            try:
                r = self.s.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                timeout=20).json()
                if "result" in r:
                    return r["result"]
            except (requests.RequestException, ValueError):
                pass
            time.sleep(1 + k)
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
    first = {}                                   # narrative key -> the first coin seen on it (Rohit: prefer the first)
    for r in (json.loads(x) for x in CANDS.open(encoding="utf-8")) if CANDS.exists() else ():
        for k in r.get("nkeys") or []:
            if k.isdigit():                      # post ids only (older rows also keyed profiles)
                first.setdefault(k, r)
    done, lock = set(), threading.Lock()
    pool = ThreadPoolExecutor(1)                 # one LLM call at a time (local GPU)

    def narrate(c, link):
        meta = c.get("_meta") or {}
        desc = str(meta.get("description", ""))[:400]
        info, keys = tweet_info(s, link)
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
                     "improves": better if prev else None, "after": prev.get("name") if prev else None})

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
            for m in [m for m, c in watch.items() if t_loop - c["t"] > WATCH_S and m not in opened]:
                watch.pop(m)
            keys = {c["bc"]: m for m, c in watch.items() if c.get("bc")}
        states = rpc.curves(list(keys)) if keys and sol["usd"] else {}
        for bc, cs in states.items():
            m = keys[bc]
            c = watch[m]
            if cs is None:
                continue
            p = cs.price_sol if not cs.complete else None
            if m in opened:
                manage(opened, m, p, cs, done, rpc)
                continue
            if p is None or m in done or len(opened) >= MAX_OPEN:
                continue
            mc = p * 1e9 * sol["usd"]
            sc, narr_ok, nkey, copy, link = scored.get(m, (None, False, None, False, None))
            top = MC_HIGH if (sc or 0) >= SCORE_HIGH else MC_BUY[1]
            if sc is None or sc < SCORE_MIN or not narr_ok or not (MC_MIN <= mc and MC_BUY[0] <= mc <= top):
                continue
            if t_loop - last_check[m] < RECHECK_S:
                continue
            last_check[m] = t_loop
            tr = trades(s, pace, m)
            if not tr:
                continue
            ok, why, det = checks(rpc, tr, c.get("dev"), sol["usd"])
            if M3 and copy and det.get("vol_usd", 0) < VOL_COPY and "volume" not in why:
                ok, why = False, why + ["volume: copy coin < $3k"]
            jlog(CANDS, {"t": time.time(), "m": m, "stage": "checks", "mc": round(mc), "ok": ok, "why": why, **det})
            if not ok:
                if not any(w.startswith(("volume", "dead")) for w in why):   # these can still change: re-check
                    done.add(m)
                continue
            fresh = rpc.curves([bc]).get(bc)                     # fill at the state after our decision
            if not fresh or fresh.complete:
                continue
            v = fresh.virtual_sol
            q = V0 * T0 / v - V0 * T0 / (v + SIZE_SOL * (1 - CURVE_FEE))
            opened[m] = dict(m=m, name=c.get("name"), score=sc, t=time.time(), p0=price(fresh), q=q, peak=price(fresh),
                             stop=STOP, high=sc >= SCORE_HIGH, bc=bc, rules=RULES, mc=round(mc), sol_usd=sol["usd"],
                             nkey=nkey)
            if M3:                                               # exits by traction, not by score
                opened[m].update(high=False, link=link, copy=copy, eng0=engagement(link))
            print(time.strftime("%H:%M"), f"BUY  {c.get('name')!r} score {sc} mc ${mc:,.0f}", flush=True)
        try:                                                     # for the dashboard (tradeslave.lab_view)
            OPEN.write_text(json.dumps({"t": time.time(), "sol_usd": sol["usd"], "llm": llm.name,
                                        "calls": {f"{d} {m}": n for (d, m), n in llm.calls.items()},
                                        "fails": {str(k): n for k, n in llm.fails.items()},
                                        "watching": len(watch), "scored": len(scored),
                                        "open": [{k: o.get(k) for k in ("m", "name", "score", "t", "p0", "last", "peak",
                                                                        "stop", "mc")} for o in opened.values()]}))
        except OSError:
            pass
        if time.time() - last_note > 600:
            last_note = time.time()
            print(time.strftime("%H:%M"), f"watching {len(watch)} with X links, scored {len(scored)}, "
                  f"open {len(opened)}, closed {summary()[0]}, llm calls {dict(llm.calls)} fails {dict(llm.fails)}",
                  flush=True)
        time.sleep(max(0.0, POLL_S - (time.time() - t_loop)))


def manage(opened, m, p, cs, done, rpc=None):
    """Exit decided on one poll, filled at the curve state of the next poll (~2 s later)."""
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
            o["pending"] = ("TP" if r >= TP else "STOP" if r <= o["stop"] else
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
    row = {**o, "exit": why, "t_exit": time.time(), "p_exit": p, "ret": ret}
    jlog(TRADES_LOG, row)
    opened.pop(m)
    done.add(m)
    print(time.strftime("%H:%M"), f"SELL {o['name']!r} {why} {ret:+.1%}", flush=True)


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
