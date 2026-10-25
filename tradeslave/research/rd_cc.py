"""RD case-control data (vault "2026-10-24 runner detection on 1,000 real runners (RD)", amendment 1).

  random     random pump.fun launches spread over the same months as the runners: random moments -> pump.fun program
             transactions in a 30 s window (Helius) -> the coin created there -> its first 300 curve trades
  graduated  coins that graduated (>= 60 s on the curve) but never reached $300k, sampled from data/runners/migrations.jsonl,
             with their first 300 curve trades (a start under $10k is applied when the features are built)
  positives  the curve runners' first 300 curve trades (data/runners/runners.json)
Output: data/runners/cc/<kind>/<mint>.json = dict(m, created, rows=[[slot, ts, user, buy, sol, tok, vs, vt], ...])

    uv run python -u -m tradeslave.research.rd_cc random 3000 | graduated 1500 | positives
"""

from __future__ import annotations

import json
import random
import sys
import time

from ..paths import DATA_DIR
from ..scalper.trade_events import parse_trade_events
from .bw_test import gtfa
from .fl_test import helius_url
from .rn_collect import ATH_MIN, _pump_headers, ath_batch, created_times

OUT = DATA_DIR / "runners" / "cc"
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PAGES = 3


def first_trades(url, m, created):
    txs, err = gtfa(url, m, created - 5, created + 3 * 86400, max_pages=PAGES)
    rows = []
    for tx in txs:
        for e in parse_trade_events((tx.get("meta") or {}).get("logMessages") or []):
            if e["mint"] == m and e["vt"]:
                rows.append([tx.get("slot"), e["ts"], e["user"], e["buy"], e["sol"] / 1e9, e["tok"] / 1e6, e["vs"] / 1e9, e["vt"] / 1e6])
    return rows


def save(kind, m, created, rows, **extra):
    d = OUT / kind
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{m}.json").write_text(json.dumps(dict(m=m, created=created, rows=rows, **extra)))


def span():
    R = json.loads((DATA_DIR / "runners" / "runners.json").read_text())
    ts = [r["created"] for r in R if r.get("created")]
    return min(ts), max(ts)


def sample_random(n):
    url = helius_url()
    lo, hi = span()
    rng = random.Random(11)
    have = {p.stem for p in (OUT / "random").glob("*.json")} if (OUT / "random").exists() else set()
    tries = 0
    while len(have) < n and tries < 4 * n:
        tries += 1
        t = rng.uniform(lo, hi)
        txs, _ = gtfa(url, PUMP, t, t + 30, "full", 100, 1)
        made = []
        for tx in txs:
            logs = (tx.get("meta") or {}).get("logMessages") or []
            if any("Instruction: Create" in x for x in logs):
                ev = [e for e in parse_trade_events(logs)]
                mints = {b["mint"] for b in (tx["meta"].get("postTokenBalances") or []) if b["mint"].endswith("pump")}
                m = ev[0]["mint"] if ev else (next(iter(mints)) if mints else None)
                if m:
                    made.append((m, tx.get("blockTime") or t))
        if not made:
            continue
        m, created = rng.choice(made)
        if m in have:
            continue
        save("random", m, created, first_trades(url, m, created))
        have.add(m)
        if len(have) % 100 == 0:
            print(f"random launches: {len(have)}/{n}", flush=True)
    # label them, so a random launch that became a runner is counted as one
    H = _pump_headers()
    ms = sorted(have)
    ath = ath_batch(ms, H)
    (OUT / "random_ath.json").write_text(json.dumps(ath))
    print(f"random done: {len(ms)} launches, {sum((v or 0) >= ATH_MIN for v in ath.values())} reached $300k")


def sample_graduated(n):
    url, H = helius_url(), _pump_headers()
    lo, hi = span()
    rows = [json.loads(x) for x in (DATA_DIR / "runners" / "migrations.jsonl").open()]
    pool = [r for r in rows if r.get("t") and lo <= r["t"] <= hi + 86400 and (r.get("ath") or 0) < ATH_MIN]
    rng = random.Random(12)
    rng.shuffle(pool)
    have = {p.stem for p in (OUT / "graduated").glob("*.json")} if (OUT / "graduated").exists() else set()
    for i in range(0, len(pool), 50):
        if len(have) >= n:
            break
        chunk = [r for r in pool[i:i + 50] if r["m"] not in have]
        cr = created_times([r["m"] for r in chunk], H)
        for r in chunk:
            c = cr.get(r["m"])
            if c is None or r["t"] - c < 60:
                continue
            save("graduated", r["m"], c, first_trades(url, r["m"], c), ath=r.get("ath"), t_mig=r["t"])
            have.add(r["m"])
        print(f"graduated negatives: {len(have)}/{n}", flush=True)
    print("graduated done")


def positives():
    url = helius_url()
    R = json.loads((DATA_DIR / "runners" / "runners.json").read_text())
    have = {p.stem for p in (OUT / "positive").glob("*.json")} if (OUT / "positive").exists() else set()
    for i, r in enumerate(R):
        if r["m"] in have or not r.get("created"):
            continue
        save("positive", r["m"], r["created"], first_trades(url, r["m"], r["created"]), ath=r.get("ath"), t_mig=r["t"])
        if i % 100 == 0:
            print(f"positives {i}/{len(R)}", flush=True)
    print("positives done")


if __name__ == "__main__":
    k = sys.argv[1]
    if k == "random":
        sample_random(int(sys.argv[2]) if len(sys.argv) > 2 else 3000)
    elif k == "graduated":
        sample_graduated(int(sys.argv[2]) if len(sys.argv) > 2 else 1500)
    else:
        positives()
    time.sleep(0)
