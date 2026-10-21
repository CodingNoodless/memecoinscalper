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
3. Market filters: market cap >= $4.5k, volume >= $1.5k, entry while market cap is $5k-7k.
4. Top-5 holder check: no bundle (several wallets buying in the create slot, shared or simultaneous funding,
   near-identical balances).
5. Chart check: organic (no single buy above 40% of buy volume besides the dev's create buy).
6. Exits: 2x take profit; stop at -5% (-30% when really sure); uptrend break (after +30%, 20% off the peak);
   60 min max.
Fills: curve state at the next poll after each decision (~2 s), 1.25%/side curve fee, own price impact,
TX_SOL per transaction.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
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

OUT = DATA_DIR / "m1"
CANDS, TRADES_LOG = OUT / "cands.jsonl", OUT / "trades.jsonl"
SWAP = "https://swap-api.pump.fun/v2/coins/{}/trades"
GT = "https://api.geckoterminal.com/api/v2/networks/solana/pools/" + SOL_USDC
MC_MIN, VOL_MIN, MC_BUY = 4500.0, 1500.0, (5000.0, 7000.0)
SCORE_MIN, SCORE_HIGH = 7, 9
GEMINI = (("gemini-3.8-flash", 9_000), ("gemini-3.5-flash-lite", 140_000))   # (model, daily budget): free-tier RPD
                                                 # 10k / 150k (Rohit's AI Studio limits, 2026-10-21); resets at midnight PT
TP, STOP, STOP_HIGH, TREND_ARM, TREND_GIVE, MAX_HOLD = 2.0, 0.95, 0.70, 1.30, 0.80, 3600
WATCH_S, POLL_S, RECHECK_S = 20 * 60, 2.0, 30
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

Coin: {name} (${sym})
Description: {desc}
X link: {link}
{tweet}
Reply with JSON only: {{"score": <0-10 integer>, "criterion": <1-4 or 0>, "why": "<one short sentence>"}}"""


def jlog(path, row):
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


class Llm:
    def __init__(self):
        self.key = os.environ.get("GEMINI_API_KEY") or self._env("GEMINI_API_KEY")
        self.name = "+".join(m for m, _ in GEMINI) if self.key else f"ollama:{OLLAMA_MODEL}"
        self.used, self.fails, self.calls = None, defaultdict(int), defaultdict(int)   # calls: (PT date, model) -> n

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
            d = json.loads(out[out.find("{"):out.rfind("}") + 1])
            return int(d.get("score", 0)), d.get("criterion"), str(d.get("why", ""))[:200]
        except Exception as exc:  # noqa: BLE001 - unscored = no buy
            return None, None, f"llm error: {type(exc).__name__}"

    def _gemini(self, text):
        day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 7 * 3600))
        for model, budget in GEMINI:
            if self.calls[(day, model)] >= budget:
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
                if r.status_code == 429:                        # quota hit early: spend the rest of the day on the next
                    self.calls[(day, model)] = budget
            except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
                self.fails[(model, type(exc).__name__)] += 1
        return None


def tweet_info(s, link):
    """Author, followers and text of the linked X post / profile (fxtwitter, free). Communities: link only."""
    m = re.search(r"(?:x|twitter)\.com/([^/?#]+)(?:/status/(\d+))?", link)
    if not m or m.group(1) in ("i", "home", "search"):
        return "X community / other link (contents not fetched)"
    try:
        if m.group(2):
            t = s.get(f"https://api.fxtwitter.com/{m.group(1)}/status/{m.group(2)}", timeout=8).json()["tweet"]
            a = t["author"]
            out = (f"Post by @{a['screen_name']} ({a.get('name', '')}, {a.get('followers', 0):,} followers): "
                   f"\"{t.get('text', '')[:500]}\" ({t.get('likes', 0)} likes, {t.get('retweets', 0)} reposts)")
            if t.get("replying_to"):
                out += f"\nIt replies to @{t['replying_to']}."
            q = t.get("quote")
            if q:
                qa = q.get("author", {})
                out += (f"\nIt quotes a post by @{qa.get('screen_name')} ({qa.get('name', '')}, "
                        f"{qa.get('followers', 0):,} followers): \"{q.get('text', '')[:300]}\"")
            return out
        u = s.get(f"https://api.fxtwitter.com/{m.group(1)}", timeout=8).json()["user"]
        return f"Profile @{u['screen_name']} ({u.get('followers', 0):,} followers): {u.get('description', '')[:300]}"
    except Exception:  # noqa: BLE001
        return "X link could not be read"


class Rpc:
    def __init__(self):
        self.url, self.s = env_rpc(), requests.Session()

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
    if len(early) >= 3:
        why.append("bundle: create-slot buyers")
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
    buys = [x[4] for x in tr[1:] if x[3] and x[2] != dev] if tr and tr[0][2] == dev else [x[4] for x in tr if x[3]]
    if buys and max(buys) > 0.40 * sum(buys):
        why.append("chart: one buy dominates")
    return not why, why, det


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    s.headers.update({"Origin": "https://pump.fun", "Accept": "application/json"})
    rpc, llm, pace = Rpc(), Llm(), Pace(0.3)
    print(time.strftime("%H:%M"), f"M1 paper bot, narrative model {llm.name}", flush=True)
    sol = {"usd": 0.0, "t": 0.0}
    watch, scored, opened = {}, {}, {}           # mint -> create row; mint -> (score, crit); mint -> position
    done, lock = set(), threading.Lock()
    pool = ThreadPoolExecutor(1)                 # one LLM call at a time (local GPU)

    def narrate(c, link):
        meta = c.get("_meta") or {}
        text = PROMPT.format(name=c.get("name"), sym=c.get("sym"), desc=str(meta.get("description", ""))[:400],
                             link=link, tweet=tweet_info(s, link))
        sc, crit, why = llm.score(text)
        with lock:
            scored[c["m"]] = (sc, crit)
        jlog(CANDS, {"t": time.time(), "m": c["m"], "name": c.get("name"), "link": link, "score": sc,
                     "crit": crit, "why": why, "age": round(time.time() - c["t"]), "model": llm.used})

    def intake(c):
        if not c["m"].endswith("pump") or c.get("mh"):
            return
        try:
            meta = s.get(c["uri"], timeout=6).json() if c.get("uri") else {}
        except Exception:  # noqa: BLE001
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
                manage(opened, m, p, cs, done)
                continue
            if p is None or m in done or len(opened) >= MAX_OPEN:
                continue
            mc = p * 1e9 * sol["usd"]
            sc = scored.get(m, (None, None))[0]
            if sc is None or sc < SCORE_MIN or not (MC_MIN <= mc and MC_BUY[0] <= mc <= MC_BUY[1]):
                continue
            if t_loop - last_check[m] < RECHECK_S:
                continue
            last_check[m] = t_loop
            tr = trades(s, pace, m)
            if not tr:
                continue
            ok, why, det = checks(rpc, tr, c.get("dev"), sol["usd"])
            jlog(CANDS, {"t": time.time(), "m": m, "stage": "checks", "mc": round(mc), "ok": ok, "why": why, **det})
            if not ok:
                if "volume" not in why:
                    done.add(m)
                continue
            fresh = rpc.curves([bc]).get(bc)                     # fill at the state after our decision
            if not fresh or fresh.complete:
                continue
            v = fresh.virtual_sol
            q = V0 * T0 / v - V0 * T0 / (v + SIZE_SOL * (1 - CURVE_FEE))
            opened[m] = dict(m=m, name=c.get("name"), score=sc, t=time.time(), p0=price(fresh), q=q, peak=price(fresh),
                             stop=STOP_HIGH if sc >= SCORE_HIGH else STOP, mc=round(mc), sol_usd=sol["usd"])
            print(time.strftime("%H:%M"), f"BUY  {c.get('name')!r} score {sc} mc ${mc:,.0f}", flush=True)
        if time.time() - last_note > 600:
            last_note = time.time()
            print(time.strftime("%H:%M"), f"watching {len(watch)} with X links, scored {len(scored)}, "
                  f"open {len(opened)}, closed {summary()[0]}, llm calls {dict(llm.calls)} fails {dict(llm.fails)}",
                  flush=True)
        time.sleep(max(0.0, POLL_S - (time.time() - t_loop)))


def manage(opened, m, p, cs, done):
    """Exit decided on one poll, filled at the curve state of the next poll (~2 s later)."""
    o = opened[m]
    if p is None:                                               # graduated: sell at the last price seen
        why, p = o.get("pending") or "GRAD", o.get("last", o["p0"])
    elif o.get("pending"):
        why = o["pending"]
    else:
        o["peak"], o["last"] = max(o["peak"], p), p
        r = p / o["p0"]
        o["pending"] = ("TP" if r >= TP else "STOP" if r <= o["stop"] else
                        "TREND" if o["peak"] >= o["p0"] * TREND_ARM and p <= o["peak"] * TREND_GIVE else
                        "TIME" if time.time() - o["t"] > MAX_HOLD else None)
        return
    out = sell_curve(o["q"], p)
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
    for r in rows[-15:]:
        print(f"  {time.strftime('%m-%d %H:%M', time.localtime(r['t']))} {str(r['name'])[:24]:24s} "
              f"score {r['score']} {r['exit']:5s} {r['ret']:+.1%}")


def main() -> None:
    {"run": run, "report": report}[sys.argv[1]]()


if __name__ == "__main__":
    main()
