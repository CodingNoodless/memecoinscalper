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
    ("M11 = leader wave rider (decu copy, fast exit)", "paper bot", "m11_bot.log", 600,
     "m11-v1: watch decu's wallet live; on his first buy in a coin, paper-buy 1 s after detection; TP +50% / stop -20% (+1 s fills)"),
    ("M10 = calm-entry rule (flagship forward test)", "paper bot", "m10_bot.out", 15,
     "m10-v2: every 15 s from 15-180 s, buy if avg buy <= 0.51 SOL, top-10 <= 23%, >= 30 s old, buy/sell <= 1.6; -10% stop, 1 s polls"),
    ("M9 = M5 or M1 entries + PX rule", "paper bot", "m9_bot.out", 15,
     "m9-v2: M5's or M1's signal, then 30 s net flow >= 12.3 SOL and a creator funder new to us; -10% stop (twin M9e: -30%)"),
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



def m11_view() -> dict:
    """M11: live copy trades of decu with detection lag, his own sells, and the backtest record."""
    try:
        tr = jsonl(DATA_DIR / "m11" / "trades.jsonl")
    except OSError:
        tr = []
    lags = [r["lag"] for r in tr if r.get("lag") is not None]
    rs = [r["ret"] for r in tr]
    return dict(stats=stats(rs), lag_median=statistics.median(lags) if lags else None, lag_max=max(lags) if lags else None,
                tp=sum(r["exit"] == "TP" for r in tr), sl=sum(r["exit"] == "SL" for r in tr), timeout=sum(r["exit"] == "timeout" for r in tr),
                deep=(sum(x <= -0.25 for x in rs) / len(rs)) if rs else None,
                trades=[dict(t=r["t"], m=r["m"], exit=r["exit"], ret=r["ret"], lag=r.get("lag"), peak=r.get("peak_ret"),
                             held=round((r["t_exit"] - r["t"]) / 60, 1)) for r in tr[-30:]][::-1])


M10_FROM = time.mktime(time.strptime("2026-10-23 23:45", "%Y-%m-%d %H:%M"))   # M10 v2 forward paper start


def m10_view() -> dict:
    """M10: rule decisions (every 15-s step), trades, and M5 over the same hours."""
    def load(v, name):
        try:
            return [r for r in jsonl(DATA_DIR / v / name) if r["t"] >= M10_FROM]
        except OSError:
            return []

    cands = [c for c in load("m10", "cands.jsonl") if c.get("stage") == "m5" and "avg_buy_60" in c]
    names = {c["m"]: c.get("name") for c in load("m10", "cands.jsonl") if c.get("name")}
    tr = load("m10", "trades.jsonl")
    names.update({r["m"]: r.get("name") for r in tr})
    m5 = load("m5", "trades.jsonl")
    rs = [r["ret"] for r in tr]
    r5 = [r["ret"] for r in m5]
    keys = ("avg_buy_60", "top10", "t_to_n", "buy_ratio_60")
    return dict(since=M10_FROM, checked=len(cands), bought=sum(bool(c.get("buy")) for c in cands),
                decisions=[dict(t=c["t"], m=c["m"], name=names.get(c["m"]), step=c.get("step"), buy=bool(c.get("buy")),
                                **{k: c.get(k) for k in keys}) for c in cands[-40:]][::-1],
                stats=stats(rs), deep=(sum(x <= -0.25 for x in rs) / len(rs)) if rs else None,
                m5=stats(r5), m5_deep=(sum(x <= -0.25 for x in r5) / len(r5)) if r5 else None)


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
        by_src={k: dict(stats([r["ret"] for r in trades if r.get("src9") == k]),
                        two_x=(sum(1 for r in trades if r.get("src9") == k and r.get("peak", 0) >= 2 * r["p0"])
                               / max(1, sum(1 for r in trades if r.get("src9") == k))))
                for k in ("m5", "m1")} if variant == "m9" else None,
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


# every model book: (key, name, one-line idea, group, current rules label, verdict text, verdict class)
MODELS = [
    ("m12", "M12", "M8 + hype gate: a top pump.fun caller bought, or livestream / DexScreener boost", "active", "m12-v1+1s", "forward test, judged at 60", "done"),
    ("m12r", "M12r", "M12's entries, ratcheting runner exit (1.25x at 2x, then N-1 at each new N)", "active", "m12r-v1+1s", "forward vs M12, at 60", "done"),
    ("m8", "M8", "m6-v3 entries (cluster + serial-funder skips), -10% stop", "active", "m8-v2+1s", "exits ADOPTED (weak, t~0.7)", "pass"),
    ("m7", "M7", "m6-v3 entries, M5 exits with -30% stop", "active", "m7-v1+1s", "entries PASS (weak)", "pass"),
    ("m9", "M9", "M5/M1 signal + PX flow rule (30 s net flow >= 12.3 SOL), -10% stop", "active", "m9-v2+1s", "forward test, judged at 60", "done"),
    ("m9e", "M9e", "M9's fills, -30% stop (twin)", "active", "m9e-v2+1s", "forward test, judged at 60", "done"),
    ("m11", "M11", "copy decu's first buy 1 s late, TP +50% / stop -20% (feed-driven)", "active", "m11-v1+1s", "forward test, judged at 60", "done"),
    ("m10", "M10", "calm-entry rule from the M10 backtest", "retired", "m10-v2+1s", "FAIL (stopped Oct 24)", "fail"),
    ("m6", "M6", "M5 + funding-cluster skip (baseline for M7)", "baseline", "m6-v2+1s", "baseline", "quiet"),
    ("m5", "M5", "runner model on post-band momentum (baseline)", "baseline", "m5-v1+1s", "baseline", "quiet"),
    ("m4", "M4", "M1 + watchlist to 20 holders, holder-drop exit", "baseline", "m4-v3+1s", "baseline", "quiet"),
    ("m3", "M3", "M2 + dead-coin / dev filters, rides runners", "baseline", "m3-v3+1s", "baseline", "quiet"),
    ("m2", "M2", "M1 + sniper-bag rug filter", "baseline", "m2-v1+1s", "baseline", "quiet"),
    ("m1", "M1", "Rohit's process automated: narrative score, $4-7k entry", "baseline", "exits-v2+1s", "baseline", "quiet"),
]


def scoreboard() -> list[dict]:
    out, now = [], time.time()
    for key, name, idea, group, rules, verdict, vclass in MODELS:
        try:
            tr = [r for r in jsonl(DATA_DIR / key / "trades.jsonl") if r.get("rules") == rules]
        except OSError:
            tr = []
        try:
            st_t = json.loads((DATA_DIR / key / "status.json").read_text(encoding="utf-8")).get("t", 0)
        except (OSError, ValueError):
            st_t = 0
        rs = [r["ret"] for r in tr]
        r24 = [r["ret"] for r in tr if r["t"] >= now - 86400]
        out.append(dict(key=key, name=name, idea=idea, group=group, rules=rules, verdict=verdict, vclass=vclass,
                        live=now - st_t < 300, stats=stats(rs), day=stats(r24),
                        deep=(sum(x <= -0.25 for x in rs) / len(rs)) if rs else None,
                        big=(sum(x >= 0.5 for x in rs) / len(rs)) if rs else None,
                        last=max((r.get("t_exit", r["t"]) for r in tr), default=None)))
    return out


def m12_view() -> dict:
    """M12 vs M12r vs M8 on the same coins, and the hype gate's log."""
    def load(v, rules):
        try:
            return [r for r in jsonl(DATA_DIR / v / "trades.jsonl") if r.get("rules") == rules]
        except OSError:
            return []
    try:
        cands = jsonl(DATA_DIR / "m12" / "cands.jsonl")
    except OSError:
        cands = []
    t12, t12r, t8 = load("m12", "m12-v1+1s"), load("m12r", "m12r-v1+1s"), load("m8", "m8-v2+1s")
    by8, by12r = {r["m"]: r for r in t8}, {r["m"]: r for r in t12r}
    gate = {c["m"]: c for c in cands}
    since = min((c["t"] for c in cands), default=None)
    paired = [(r["ret"], by12r[r["m"]]["ret"], by8[r["m"]]["ret"]) for r in t12 if r["m"] in by12r and r["m"] in by8]
    taken8 = [by8[m]["ret"] for m, c in gate.items() if c.get("go") and m in by8]
    skipped8 = [by8[m]["ret"] for m, c in gate.items() if not c.get("go") and m in by8]
    try:
        op = json.loads((DATA_DIR / "m12r" / "status.json").read_text(encoding="utf-8")).get("open", [])
    except (OSError, ValueError):
        op = []
    return dict(since=since, checked=len(cands), passed=sum(bool(c.get("go")) for c in cands),
                caller=sum((c.get("caller") or 0) > 0 for c in cands), live=sum(bool(c.get("live_or_boost")) for c in cands),
                paired=dict(n=len(paired), m12=stats([a for a, _, _ in paired]), m12r=stats([b for _, b, _ in paired]),
                            m8=stats([c for _, _, c in paired])),
                taken8=stats(taken8), skipped8=stats(skipped8),
                open12r=[dict(m=o["m"], name=o.get("name"), t=o.get("t"), r=(o["last"] / o["p0"]) if o.get("last") and o.get("p0") else None,
                              peak=(o["peak"] / o["p0"]) if o.get("peak") and o.get("p0") else None, lad_on=o.get("lad_on"),
                              lad_stop=o.get("lad_stop")) for o in op],
                recent=[dict(t=r["t"], m=r["m"], name=r.get("name"), hype=gate.get(r["m"], {}), exit12=r["exit"], ret12=r["ret"],
                             exit12r=by12r.get(r["m"], {}).get("exit"), ret12r=by12r.get(r["m"], {}).get("ret"),
                             ret8=by8.get(r["m"], {}).get("ret")) for r in t12[-30:]][::-1],
                gate_log=[dict(t=c["t"], m=c["m"], name=c.get("name"), go=c.get("go"), caller=c.get("caller"),
                               live=c.get("live_or_boost"), ret8=by8.get(c["m"], {}).get("ret")) for c in cands[-30:]][::-1])


def build_lab_snapshot() -> dict:
    return dict(t=time.time(), jobs=jobs(), m1=m1(), m2=m1("m2"), m3=m1("m3"), m4=m1("m4"), m5=m1("m5"), m6=m1("m6"), m7=m1("m7"), m8=m1("m8"), m9=m1("m9"), m9e=m1("m9e"), m10=m1("m10"), m10x=m10_view(), m11=m1("m11"), m11x=m11_view(), m12=m1("m12"), m12r=m1("m12r"), m12x=m12_view(), board=scoreboard(), m78=m78_view(), c1=c1(), kalshi=kalshi(), experiments=experiments())


