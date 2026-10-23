"""MV idea 1: score coins on whether they will run, learned from examples (vault "2026-10-22 M4 variants backtest (MV)").

    uv run python -m tradeslave.research.run_score posts     # fetch each coin's linked post (fxtwitter) -> data/m1/post_info.json
    uv run python -m tradeslave.research.run_score score     # run scores (Gemini flash-lite) -> data/m1/run_scores.json

"Ran" = from a realistic entry (first trade in the $4-7k band >= 5 s after create, filled 1 s later) the price reached
2x within 30 min. Examples come only from B-train (live-feed coins created before 20:00 Oct 22); the coins used as
examples are left out of every evaluation.
"""

from __future__ import annotations

import gzip
import json
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from ..paths import DATA_DIR
from .m1_bot import Llm
from .m4_bt import B_SPLIT, OUT as COINS, SOL_USD

POSTS = DATA_DIR / "m1" / "post_info.json"
SCORES = DATA_DIR / "m1" / "run_scores.json"
MODEL = "gemini-3.5-flash-lite"


def ran(co) -> bool | None:
    rows = co["rows"]
    t_ok = co["t0"] + 5
    e = next((r for r in rows if r[0] >= t_ok and 4000 <= r[1] * 1e9 * SOL_USD <= 7000), None)
    if e is None:
        return None
    fill = [r[1] for r in rows if r[0] <= e[0] + 1][-1]
    later = [r[1] for r in rows if e[0] + 1 <= r[0] <= e[0] + 1800]
    return bool(later) and max(later) >= 2 * fill


def posts() -> None:
    with gzip.open(COINS, "rt", encoding="utf-8") as fh:
        coins = json.load(fh)
    have = json.loads(POSTS.read_text()) if POSTS.exists() else {}
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0"})

    def one(co):
        link = co["c"].get("link") or ""
        m = re.search(r"(?:x|twitter)\.com/([^/?#]+)/status/(\d+)", link)
        if not m:
            return co["m"], {"kind": "profile/community" if link else "none"}
        for k in range(3):
            try:
                r = s.get(f"https://api.fxtwitter.com/{m.group(1)}/status/{m.group(2)}", timeout=10)
                if r.status_code == 404:
                    return co["m"], {"kind": "deleted"}
                if r.ok:
                    t = r.json().get("tweet") or {}
                    a = t.get("author") or {}
                    q = t.get("quote") or {}
                    return co["m"], {"kind": "post", "text": (t.get("text") or "")[:280], "author": a.get("screen_name"),
                                     "followers": a.get("followers"), "quote": (q.get("text") or "")[:160],
                                     "quote_author": (q.get("author") or {}).get("screen_name")}
                time.sleep(3 * (k + 1))
            except requests.RequestException:
                time.sleep(2)
        return co["m"], {"kind": "error"}

    todo = [co for co in coins if co["m"] not in have]
    with ThreadPoolExecutor(4) as ex:
        for i, (m, info) in enumerate(ex.map(one, todo), 1):
            have[m] = info
            if i % 200 == 0:
                POSTS.write_text(json.dumps(have))
                print(f"  {i}/{len(todo)}", flush=True)
    POSTS.write_text(json.dumps(have))
    print(len(have), "posts")


def card(co, info) -> str:
    c = co["c"]
    out = f"Name: {c.get('name')} (${c.get('sym')})\nDescription: {(c.get('desc') or '')[:160]}\n"
    if info.get("kind") == "post":
        out += f"Linked X post by @{info.get('author')} ({info.get('followers') or 0:,} followers): \"{info.get('text')}\"\n"
        if info.get("quote"):
            out += f"It quotes @{info.get('quote_author')}: \"{info.get('quote')}\"\n"
    else:
        out += f"Link: {info.get('kind')}\n"
    return out


PROMPT = """You predict which brand-new pump.fun memecoins will at least DOUBLE from a ~$5k market cap within 30 minutes.
Most do not (about 1 in 8 does). Below are real examples from earlier tonight: coins that doubled, then coins that did
not. Learn what separates them (themes people are piling into, how the name/ticker lands, who posted, timing), not
whether the story is "good".

COINS THAT DOUBLED:
{yes}
COINS THAT DID NOT:
{no}
NEW COIN:
{coin}
Return JSON only: {{"run": <0-10, 10 = very likely to double>, "why": "<one short sentence>"}}"""


def score() -> None:
    with gzip.open(COINS, "rt", encoding="utf-8") as fh:
        coins = json.load(fh)
    info = json.loads(POSTS.read_text())
    train = [co for co in coins if co["set"] == "B" and co["t0"] < B_SPLIT]
    lab = [(co, ran(co)) for co in train]
    yes = [co for co, r in lab if r]
    no = [co for co, r in lab if r is False]
    rnd = random.Random(7)
    ex_yes, ex_no = rnd.sample(yes, min(15, len(yes))), rnd.sample(no, min(25, len(no)))
    ex_ids = {co["m"] for co in ex_yes + ex_no}
    head = dict(yes="\n".join(card(co, info.get(co["m"], {})) for co in ex_yes),
                no="\n".join(card(co, info.get(co["m"], {})) for co in ex_no))
    llm = Llm()
    out = json.loads(SCORES.read_text()) if SCORES.exists() else {}
    out["_examples"] = sorted(ex_ids)

    def one(co):
        if co["m"] in ex_ids or co["m"] in out:
            return co["m"], out.get(co["m"])
        text = PROMPT.format(coin=card(co, info.get(co["m"], {})), **head)
        for k in range(4):
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
                              headers={"x-goog-api-key": llm.key}, timeout=30, json={
                                  "contents": [{"parts": [{"text": text}]}],
                                  "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}})
            if r.ok:
                try:
                    return co["m"], int(json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])["run"])
                except (KeyError, IndexError, ValueError, TypeError):
                    return co["m"], None
            time.sleep(10 * (k + 1) if r.status_code == 429 else 2)
        return co["m"], None

    todo = [co for co in coins if co["m"] not in ex_ids and co["m"] not in out]
    print(f"examples: {len(ex_yes)} doubled, {len(ex_no)} did not (B-train has {len(yes)} / {len(no)}); scoring {len(todo)}",
          flush=True)
    with ThreadPoolExecutor(3) as ex:
        for i, (m, v) in enumerate(ex.map(one, todo), 1):
            out[m] = v
            if i % 100 == 0:
                SCORES.write_text(json.dumps(out))
                print(f"  {i}/{len(todo)}", flush=True)
    SCORES.write_text(json.dumps(out))
    print("done;", sum(v is not None for k, v in out.items() if not k.startswith("_")), "scored")


if __name__ == "__main__":
    {"posts": posts, "score": score}[sys.argv[1]]()
