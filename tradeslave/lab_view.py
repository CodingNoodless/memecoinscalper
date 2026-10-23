"""Research lab page for the dashboard: every running bot / test / paper trade at a glance (read-only).

Sources are the jobs' own logs and data files; nothing here writes anything or touches a wallet.
"""

from __future__ import annotations

import json
import math
import re
import statistics
import time
from collections import Counter
from pathlib import Path

from .paths import DATA_DIR, PROJECT_ROOT

LOGS = PROJECT_ROOT / "logs"
VAULT = Path(r"D:\vault\tradeslave")

# name, kind, log file, minutes of silence before it counts as stalled, one-line description
JOBS = [
    ("M1 automated manual process", "paper bot", "m1_bot.out", 15,
     "Rohit's new-pairs process: X-linked launches, Gemini narrative score, bundle/chart checks, 2x / -5%"),
    ("M4 = M1 + TF fixes", "paper bot", "m4_bot.out", 15,
     "Watchlist until >= 20 holders and >= 90% of their max, sell when holders fall 10%, skip a top; live feed"),
    ("M5 runner model", "paper bot", "m5_bot.out", 15,
     "Any X-linked coin; buys only when the runner model (post-band momentum) says p >= 0.373; forward test"),
    ("M6 = M5 + bad-buy skip", "paper bot", "m6_bot.out", 15,
     "M5 v1, but skips the buy when one funder controls >= 15% of the top-20 holders (Helius funding lookups); m6-v2 from 13:10"),
    ("M7 = m6-v3, M5 exits", "paper bot", "m7_bot.out", 15,
     "M6 + skip when the creator's funder funded an earlier creator; stop -30% (side by side with M8)"),
    ("M8 = M7 twin book, -10% stop", "paper bot", "m7_bot.out", 15,
     "Runs inside the M7 process (m8-v2): same fills and polls as M7, -10% stop instead of -30% (max-loss test, vault XA)"),
    ("Live trade feed", "data feed", "live_feed.log", 15,
     "Every pump.fun trade from a Helius websocket; real-time trades for the coins the bots watch (data/feed)"),
    ("C1 curve reach model", "forward recorder", "c1_curve.out", 15,
     "Records every launch reaching curve R 40 (Oct 21-28); logistic model + 40->75 trade judged once after"),
    ("Launch watcher (J1/J2)", "forward recorder", "launch_watch.out", 15,
     "Every pump.fun create; J1/J2 creator-launch forward test"),
    ("Y19 liquidation map", "forward scanner", "liq_map.out", 45,
     "Hyperliquid liquidation clusters every 30 min; judged at 30 fills / 100 signals or Nov 10"),
    ("R3 perp shock reversal", "forward test", "perp_forward.out", 1500,
     "Buy 1-min memecoin perp crashes, hold 2 h (paper)"),
    ("W1 Kalshi rewards (base)", "paper bot", "kalshi_lip_paper.out", 15, "Kalshi liquidity-incentive quoting"),
    ("W2 Kalshi rewards (front)", "paper bot", "kalshi_lip_front.out", 15, "Kalshi liquidity-incentive, front of book"),
    ("W4 Kalshi market making", "paper bot", "kalshi_lip_mm.out", 15, "Two-sided quoting"),
    ("W5 Kalshi MM (join)", "paper bot", "kalshi_lip_mmjoin.out", 15, "Two-sided, joining best price; judged at 48 h"),
    ("Y11-DEX candle fetch", "data job", "major_wick_fetch.out", 90, "1-min DEX candles for 19 large memecoins"),
]


def tail(path: Path, n: int = 4000) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - n))
            return [x for x in fh.read().decode("utf-8", "replace").splitlines() if x.strip()][-6:]
    except OSError:
        return []


def jsonl(path: Path) -> list[dict]:
    out = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def jobs() -> list[dict]:
    now = time.time()
    rows = []
    for name, kind, log, quiet, desc in JOBS:
        p = LOGS / log
        try:
            age = (now - p.stat().st_mtime) / 60
        except OSError:
            age = None
        lines = tail(p)
        last = next((x for x in reversed(lines) if not x.startswith((" ", "Traceback", "  File"))), lines[-1] if lines else "")
        status = "missing" if age is None else "running" if age <= quiet else "quiet"
        if kind == "data job" and status == "quiet":
            status = "done"
        rows.append(dict(name=name, kind=kind, desc=desc, status=status, age_min=None if age is None else round(age, 1),
                         last=last[-260:]))
    return rows


def stats(v: list[float]) -> dict:
    if not v:
        return dict(n=0)
    m = statistics.mean(v)
    sd = statistics.stdev(v) if len(v) > 1 else 0.0
    return dict(n=len(v), mean=m, median=statistics.median(v), win=sum(x > 0 for x in v) / len(v),
                t=m / sd * math.sqrt(len(v)) if sd else 0.0, best=max(v), worst=min(v))


M5_FROM = time.mktime(time.strptime("2026-10-22 23:00", "%Y-%m-%d %H:%M"))   # M5 start: M1 over the same hours
M4_FROM = time.mktime(time.strptime("2026-10-22 15:01", "%Y-%m-%d %H:%M"))   # M4 start: M1 over the same hours
FRESH_FROM = time.mktime(time.strptime("2026-10-21 09:20", "%Y-%m-%d %H:%M"))   # = M2 start, so M1 vs M2 compare   # fresh daytime count (Rohit)


def m5_view(cands: list[dict]) -> dict:
    """M5's runner-model decisions: one row per coin (its best p so far), newest first."""
    names = {c["m"]: c.get("name") for c in cands if "score" in c}
    ev = [c for c in cands if c.get("stage") == "m5" and "p" in c]
    best: dict = {}
    for c in ev:
        b = best.get(c["m"])
        if b is None or c["p"] > b["p"] or c.get("buy"):
            best[c["m"]] = dict(t=c["t"], m=c["m"], name=names.get(c["m"]), p=c["p"], holders=c.get("holders"),
                                mc=c.get("mc"), buy=bool(c.get("buy")) or bool(b and b.get("buy")), steps=c.get("step"))
    rows = sorted(best.values(), key=lambda r: -r["t"])
    try:
        P = json.loads((DATA_DIR / "m1" / "m5_model.json").read_text())["P"]
    except (OSError, ValueError, KeyError):
        P = None
    return dict(P=P, evals=len(ev), coins=len(best), buys=sum(r["buy"] for r in rows),
                near=sum(1 for r in rows if P and r["p"] >= 0.8 * P), recent=rows[:40])


M6V2_FROM = time.mktime(time.strptime("2026-10-23 13:03", "%Y-%m-%d %H:%M"))   # m6-v2 (funding-cluster skip) start


def m6_view(cands: list[dict]) -> dict:
    """M6's funding checks: every M5 buy signal M6 looked up, with its cluster share and whether it bought."""
    names = {c["m"]: c.get("name") for c in cands if "score" in c}
    ev = [c for c in cands if c.get("stage") == "m5" and "m6_cluster" in c and c["t"] >= M6V2_FROM]
    rows = [dict(t=c["t"], m=c["m"], name=names.get(c["m"]), cluster=c["m6_cluster"], creator=c.get("m6_creator"),
                 fresh=c.get("m6_fresh"), serial=c.get("m7_serial"), buy=bool(c.get("buy")), mc=c.get("mc"),
                 why=c.get("why")) for c in ev]
    try:
        m5 = jsonl(DATA_DIR / "m5" / "trades.jsonl")
    except OSError:
        m5 = []
    same = [r["ret"] for r in m5 if r["t"] >= M6V2_FROM]
    return dict(checked=len(rows), bought=sum(r["buy"] for r in rows), skipped=sum(not r["buy"] for r in rows),
                recent=sorted(rows, key=lambda r: -r["t"])[:40], m5_same=stats(same),
                m5_deep=(sum(x <= -0.25 for x in same) / len(same)) if same else None)



M78_FROM = time.mktime(time.strptime("2026-10-23 16:00", "%Y-%m-%d %H:%M"))   # M7 / M8 start (m6-v3 entries, 1 s polls)


def m78_view() -> dict:
    """M7 vs M8 side by side: same m6-v3 entries, stop -30% vs -10% (vault "2026-10-23 M7 vs M8 forward test")."""
    def load(v):
        try:
            rs = [r for r in jsonl(DATA_DIR / v / "trades.jsonl") if r["t"] >= M78_FROM]
            return [r for r in rs if r.get("rules") == "m8-v2+1s"] if v == "m8" else rs   # m8-v2: twin book in M7
        except OSError:
            return []

    tr = {v: load(v) for v in ("m5", "m6", "m7", "m8")}

    def card(v):
        rs = [r["ret"] for r in tr[v]]
        st = stats(rs)
        st.update(deep=(sum(x <= -0.25 for x in rs) / len(rs)) if rs else None,
                  sd=statistics.pstdev(rs) if len(rs) > 1 else None, sol=round(sum(rs) * 0.1, 4),
                  ex2=round(sum(sorted(rs)[:-2]) * 0.1, 4) if len(rs) > 2 else None,
                  exits=Counter(r["exit"] for r in tr[v]))
        return st

    try:
        cands = jsonl(DATA_DIR / "m7" / "cands.jsonl")
    except OSError:
        cands = []
    names = {c["m"]: c.get("name") for c in cands if c.get("name")}
    for v in ("m7", "m8"):
        names.update({r["m"]: r.get("name") for r in tr[v]})
    ev = [c for c in cands if c.get("stage") == "m5" and c["t"] >= M78_FROM and ("m6_cluster" in c or "M6 skip" in str(c.get("why")))]

    def reason(c):
        w = str(c.get("why") or "")
        return ("bought" if c.get("buy") else "cluster" if "funding cluster" in w else "serial" if "M7 skip: creator's" in w
                else "lookup" if "lookup failed" in w else "other")
    why = Counter(reason(c) for c in ev)
    decisions = [dict(t=c["t"], m=c["m"], name=names.get(c["m"]), cluster=c.get("m6_cluster"), serial=c.get("m7_serial"),
                      decision=reason(c), mc=c.get("mc")) for c in ev[-40:]][::-1]
    by8 = {r["m"]: r for r in tr["m8"]}
    pairs = [dict(t=r["t"], m=r["m"], name=r.get("name"), r7=r["ret"], x7=r["exit"], r8=by8[r["m"]]["ret"], x8=by8[r["m"]]["exit"])
             for r in tr["m7"] if r["m"] in by8][::-1]
    recent = sorted([dict(bot=v, t=r["t"], m=r["m"], name=r.get("name"), score=r.get("score"), exit=r["exit"], ret=r["ret"],
                          mc=r.get("mc"), held=round((r["t_exit"] - r["t"]) / 60, 1)) for v in ("m7", "m8") for r in tr[v]],
                    key=lambda x: -x["t"])[:30]
    opened = []
    for v in ("m7", "m8"):
        try:
            st = json.loads((DATA_DIR / v / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for o in st.get("open", []):
            opened.append(dict(bot=v, name=o.get("name"), mc=o.get("mc"), t=o.get("t"), holders=o.get("holders"),
                               h_max=o.get("h_max"), stop=o.get("stop"),
                               ret_now=(o["last"] / o["p0"] - 1) if o.get("last") and o.get("p0") else None))
    dec = {c["m"]: reason(c) for c in ev}                       # what the m6-v3 skip did to M5's own trades
    took = {r["m"]: r for r in tr["m7"]}
    skip_rows = [dict(t=r["t"], m=r["m"], name=r.get("name"), r5=r["ret"], x5=r["exit"],
                      decision=dec.get(r["m"], "no signal seen"), r7=(took[r["m"]]["ret"] if r["m"] in took else None))
                 for r in tr["m5"]][::-1]
    kept5 = [x["r5"] for x in skip_rows if x["decision"] == "bought"]
    skipped5 = [x["r5"] for x in skip_rows if x["decision"] in ("cluster", "serial", "lookup")]
    return dict(since=M78_FROM, m7=card("m7"), m8=card("m8"), m5=card("m5"), m6=card("m6"), skip_rows=skip_rows,
                kept5=stats(kept5), skipped5=stats(skipped5), checks=dict(
                signals=len(ev), **why), decisions=decisions, pairs=pairs,
                pair_diff=statistics.mean(p["r8"] - p["r7"] for p in pairs) if pairs else None, recent=recent, open=opened)

def m1(variant: str = "m1") -> dict:
    d = DATA_DIR / variant
    trades = jsonl(d / "trades.jsonl")
    cands = jsonl(d / "cands.jsonl")
    try:
        status = json.loads((d / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {}
    day0 = time.time() - 86400
    lt = time.localtime()
    day_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    scored = [c for c in cands if "score" in c and c["t"] >= day0]
    checks = [c for c in cands if c.get("stage") == "checks" and c["t"] >= day0]
    key = {}
    for c in cands:
        if "link" in c:
            sid = re.search(r"status/(\d+)", c["link"])
            key.setdefault(c["m"], (c.get("nkeys") or [sid.group(1) if sid else c["link"].lower()])[0])
    groups = {}
    for r in trades:
        groups.setdefault(r.get("nkey") or key.get(r["m"], r["m"]), []).append(r["ret"])
    last_check = {c["m"]: ("passed" if c["ok"] else ", ".join(c.get("why", []))) for c in checks}
    bought = {r["m"] for r in trades} | {o.get("m") for o in status.get("open", [])}
    eq, curve = 0.0, []
    for r in trades:
        eq += r["ret"] * 0.1
        curve.append([r["t_exit"], round(eq, 4)])
    for o in status.get("open", []):
        o["ret_now"] = (o["last"] / o["p0"] - 1) if o.get("last") and o.get("p0") else None
    return dict(
        stats=stats([r["ret"] for r in trades]),
        by_window={lab: stats([r["ret"] for r in trades if lo <= time.localtime(r["t"]).tm_hour < hi])
                   for lab, lo, hi in (("<b>17-24 Rohit's hours (pre-registered test)</b>", 17, 24),
                                       ("00-04", 0, 4), ("04-07 (Rohit: suspect)", 4, 7), ("07-17 daytime", 7, 17))},
        fresh=stats([r["ret"] for r in trades if r["t"] >= FRESH_FROM]),
        by_rules={k: stats([r["ret"] for r in trades if r.get("rules", "exits-v1") == k])
                  for k in dict.fromkeys(r.get("rules", "exits-v1") for r in trades)}, narratives=stats([statistics.mean(v) for v in groups.values()]), sol=round(sum(r["ret"] for r in trades) * 0.1, 4), curve=curve,
        exits=Counter(r["exit"] for r in trades), open=status.get("open", []),
        status={k: status.get(k) for k in ("t", "llm", "calls", "fails", "watching", "sol_usd")},
        rides=sum(1 for r in trades if r.get("half_at")),
        ex2=round(sum(sorted(r["ret"] for r in trades)[:-2]) * 0.1, 4) if len(trades) > 2 else None,
        fresh4=stats([r["ret"] for r in trades if r["t"] >= M4_FROM]),
        fresh5=stats([r["ret"] for r in trades if r["t"] >= M5_FROM]),
        m5=m5_view(cands) if variant in ("m5", "m6", "m7", "m8") else None,
        m6=m6_view(cands) if variant in ("m6", "m7", "m8") else None,
        deep=(sum(r["ret"] <= -0.25 for r in trades) / len(trades)) if trades else None,
        recent=[dict(t=r["t"], m=r["m"], name=r.get("name"), score=r.get("score"), exit=r["exit"], ret=r["ret"],
                     narr=(r.get("nkey") or key.get(r["m"], ""))[-6:],
                     mc=r.get("mc"), held=round((r["t_exit"] - r["t"]) / 60, 1)) for r in trades[-25:]][::-1],
        funnel=dict(scored=len(scored), passed=sum((c["score"] or 0) >= 7 for c in scored),
                    checked=len(checks), checks_ok=sum(c["ok"] for c in checks),
                    blocked=Counter(w for c in checks for w in c.get("why", [])),
                    scores=Counter(str(c["score"]) for c in scored),
                    models=Counter(str(c.get("model")) for c in scored)),
        top=[dict(t=c["t"], name=c.get("name"), score=c["score"], why=c.get("why"), link=c.get("link"),
                  crit=c.get("crit"), after=c.get("after"), improves=c.get("improves"), model=c.get("model"),
                  check=last_check.get(c["m"]), bought=c["m"] in bought)
             for c in sorted(scored, key=lambda c: c["t"])][-300:][::-1])


def c1() -> dict:
    d = DATA_DIR / "c1"
    hot, maxr = set(), Counter()
    for c in jsonl(d / "checks.jsonl"):
        if c.get("t40"):
            hot.add(c["m"])
    files = len(list((d / "trades").glob("*.json.gz"))) if (d / "trades").exists() else 0
    try:
        with (d / "creates.jsonl").open("rb") as fh:
            creates = sum(1 for _ in fh)
    except OSError:
        creates = 0
    return dict(creates=creates, hot=len(hot), files=files, window="Oct 21 - Oct 28 PDT, verdict after")


def kalshi() -> list[dict]:
    out = []
    for name, log in (("W1", "kalshi_lip_paper.out"), ("W2", "kalshi_lip_front.out"), ("W4", "kalshi_lip_mm.out"),
                      ("W5", "kalshi_lip_mmjoin.out")):
        line = next((x for x in reversed(tail(LOGS / log)) if "rewards" in x), "")
        m = re.search(r"([\d.]+) h: rewards \$([\d.\-]+), settled P&L \$([+\-\d.]+), open positions (\d+)", line)
        if m:
            out.append(dict(name=name, hours=float(m.group(1)), rewards=float(m.group(2)), settled=float(m.group(3)),
                            open=int(m.group(4))))
    return out


def experiments() -> dict:
    rows = []
    try:
        text = (VAULT / "Experiments" / "Experiments.md").read_text(encoding="utf-8")
    except OSError:
        return dict(rows=[], total=0)
    clean = lambda s: re.sub(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", r"\1", s).replace("**", "").strip()   # noqa: E731
    for line in text.splitlines():
        if not line.startswith("| 20"):
            continue
        cells = [clean(c) for c in line.strip().strip("|").split("|")]
        if len(cells) >= 7:
            v = cells[6].lower()
            state = ("running" if any(w in v for w in ("running", "recording", "pending", "planned", "⏳")) else
                     "pass" if any(w in v for w in ("pass", "✅", "edge found")) and "no edge" not in v else "fail")
            rows.append(dict(date=cells[0], note=cells[1], strategy=cells[2], data=cells[3], trades=cells[4],
                             result=cells[5], verdict=cells[6], state=state))
    return dict(rows=rows[::-1], total=len(rows), running=sum(r["state"] == "running" for r in rows))


def build_lab_snapshot() -> dict:
    return dict(t=time.time(), jobs=jobs(), m1=m1(), m2=m1("m2"), m3=m1("m3"), m4=m1("m4"), m5=m1("m5"), m6=m1("m6"), m7=m1("m7"), m8=m1("m8"), m78=m78_view(), c1=c1(), kalshi=kalshi(), experiments=experiments())


