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


def m1() -> dict:
    d = DATA_DIR / "m1"
    trades = jsonl(d / "trades.jsonl")
    cands = jsonl(d / "cands.jsonl")
    try:
        status = json.loads((d / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {}
    day0 = time.time() - 86400
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
    eq, curve = 0.0, []
    for r in trades:
        eq += r["ret"] * 0.1
        curve.append([r["t_exit"], round(eq, 4)])
    for o in status.get("open", []):
        o["ret_now"] = (o["last"] / o["p0"] - 1) if o.get("last") and o.get("p0") else None
    return dict(
        stats=stats([r["ret"] for r in trades]),
        by_rules={k: stats([r["ret"] for r in trades if r.get("rules", "exits-v1") == k])
                  for k in dict.fromkeys(r.get("rules", "exits-v1") for r in trades)}, narratives=stats([statistics.mean(v) for v in groups.values()]), sol=round(sum(r["ret"] for r in trades) * 0.1, 4), curve=curve,
        exits=Counter(r["exit"] for r in trades), open=status.get("open", []),
        status={k: status.get(k) for k in ("t", "llm", "calls", "fails", "watching", "sol_usd")},
        recent=[dict(t=r["t"], name=r.get("name"), score=r.get("score"), exit=r["exit"], ret=r["ret"],
                     narr=(r.get("nkey") or key.get(r["m"], ""))[-6:],
                     mc=r.get("mc"), held=round((r["t_exit"] - r["t"]) / 60, 1)) for r in trades[-25:]][::-1],
        funnel=dict(scored=len(scored), passed=sum((c["score"] or 0) >= 7 for c in scored),
                    checked=len(checks), checks_ok=sum(c["ok"] for c in checks),
                    blocked=Counter(w for c in checks for w in c.get("why", [])),
                    scores=Counter(str(c["score"]) for c in scored),
                    models=Counter(str(c.get("model")) for c in scored)),
        top=[dict(t=c["t"], name=c.get("name"), score=c["score"], why=c.get("why"), link=c.get("link"))
             for c in sorted((c for c in scored if c["score"] is not None), key=lambda c: c["t"])
             if c["score"] >= 5][-12:][::-1])


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
    return dict(t=time.time(), jobs=jobs(), m1=m1(), c1=c1(), kalshi=kalshi(), experiments=experiments())


LAB_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tradeslave Lab</title><style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1b1f24;--mute:#66707c;--line:#e3e6ea;--good:#13804b;--bad:#c23b32;--warn:#a86a00;--acc:#3456d1}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#171b21;--ink:#e6e9ee;--mute:#8d97a3;--line:#262c34;--good:#3fbf7f;--bad:#ef6b62;--warn:#e0a63a;--acc:#7c95ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
h1{font-size:20px;margin:0}h2{font-size:15px;margin:0 0 10px}.mute{color:var(--mute)}main{padding:18px 24px;display:grid;gap:16px;max-width:1400px}
.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;min-width:0}
.kpis{display:flex;gap:18px;flex-wrap:wrap}.kpi b{display:block;font-size:20px}.kpi span{color:var(--mute);font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:5px 6px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--mute);font-weight:600}
.scroll{overflow:auto;max-height:420px}.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;font-weight:600;border:1px solid}
.running,.pass{color:var(--good);border-color:var(--good)}.quiet,.missing{color:var(--warn);border-color:var(--warn)}.fail{color:var(--bad);border-color:var(--bad)}.done{color:var(--acc);border-color:var(--acc)}
.pos{color:var(--good)}.neg{color:var(--bad)}.small{font-size:12px}code{font-size:12px;word-break:break-all}svg{width:100%;height:90px}
a{color:var(--acc)}.bar{display:flex;gap:4px;align-items:flex-end;height:60px}.bar div{background:var(--acc);flex:1;min-height:1px;border-radius:2px 2px 0 0;position:relative}
.bar div span{position:absolute;bottom:-17px;left:0;right:0;text-align:center;font-size:11px;color:var(--mute)}
@media (max-width:600px){main,header{padding:12px 16px}}
</style></head><body>
<header><h1>Tradeslave Lab</h1><span class="mute">Paper only. Refreshes every 15 s.</span><span id="upd" class="mute small"></span><a href="/scalper" class="small">old scalper view</a></header>
<main>
<section class="card"><h2>M1: automated manual process (paper)</h2><div class="kpis" id="m1k"></div>
<div class="grid" style="margin-top:12px"><div><div class="mute small">Equity (SOL, 0.1 SOL per trade)</div><svg id="eq" viewBox="0 0 300 90" preserveAspectRatio="none"></svg></div>
<div><div class="mute small">Narrative scores, last 24 h</div><div class="bar" id="sc"></div></div></div>
<div class="grid" style="margin-top:22px"><div><h2>Open positions</h2><div id="open" class="scroll"></div></div>
<div><h2>Funnel, last 24 h</h2><div id="fun"></div></div></div>
<div class="grid" style="margin-top:8px"><div><h2>Recent paper trades</h2><div id="rules" style="margin-bottom:10px"></div><div id="trades" class="scroll"></div></div>
<div><h2>Best-scored narratives</h2><div id="top" class="scroll"></div></div></div></section>
<section class="grid"><div class="card"><h2>Running jobs</h2><div id="jobs" class="scroll"></div></div>
<div class="card"><h2>Kalshi paper bots</h2><div id="kal"></div><h2 style="margin-top:16px">C1 recorder</h2><div id="c1"></div></div></section>
<section class="card"><h2>Experiments <span class="mute small" id="exn"></span></h2><div id="exp" class="scroll" style="max-height:520px"></div></section>
</main><script>
const $=id=>document.getElementById(id),esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const pct=(x,d=1)=>x==null?"–":`<span class="${x>0?"pos":x<0?"neg":""}">${(x>0?"+":"")+(100*x).toFixed(d)}%</span>`;
const ago=t=>{const s=Date.now()/1e3-t;return s<90?`${s|0}s ago`:s<5400?`${(s/60)|0}m ago`:`${(s/3600).toFixed(1)}h ago`};
const tm=t=>new Date(t*1e3).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"});
function table(h,rows){return rows.length?`<table><tr>${h.map(x=>`<th>${x}</th>`).join("")}</tr>${rows.map(r=>`<tr>${r.map(c=>`<td>${c}</td>`).join("")}</tr>`).join("")}</table>`:'<span class="mute">none yet</span>'}
async function tick(){let d;try{d=await (await fetch("/api/lab",{cache:"no-store"})).json()}catch(e){$("upd").textContent="dashboard server unreachable";return}
$("upd").textContent="updated "+new Date().toLocaleTimeString();const m=d.m1,s=m.stats;
$("m1k").innerHTML=[["Trades",s.n],["Mean / trade",s.n?pct(s.mean):"–"],["Win rate",s.n?(100*s.win).toFixed(0)+"%":"–"],["t-stat",s.n>1?s.t.toFixed(2):"–"],
["Narratives",m.narratives.n],["Mean / narrative",m.narratives.n?pct(m.narratives.mean):"–"],["P&L",`${m.sol>=0?"+":""}${m.sol} SOL`],["2x hits",m.exits.TP||0],["Open",m.open.length],["Watching",m.status.watching??"–"],
["Model",esc(m.status.llm||"–")]].map(([a,b])=>`<div class="kpi"><b>${b}</b><span>${a}</span></div>`).join("")+
`<div class="kpi"><b class="small">${s.n>=100?"verdict due":"verdict at 100 trades or Nov 1"}</b><span>bar: mean ≥ +3%, t ≥ 3</span></div>`;
const c=m.curve;if(c.length>1){const ys=c.map(p=>p[1]),lo=Math.min(0,...ys),hi=Math.max(0,...ys),r=hi-lo||1;
$("eq").innerHTML=`<line x1="0" x2="300" y1="${90-(0-lo)/r*85}" y2="${90-(0-lo)/r*85}" stroke="var(--line)"/><polyline fill="none" stroke="var(--acc)" stroke-width="2" points="${c.map((p,i)=>`${i/(c.length-1)*300},${90-(p[1]-lo)/r*85}`).join(" ")}"/>`}
else $("eq").innerHTML='<text x="4" y="40" fill="var(--mute)" font-size="11">no closed trades yet</text>';
const sc=m.funnel.scores,mx=Math.max(1,...Object.values(sc));$("sc").innerHTML=[...Array(11).keys()].map(k=>`<div style="height:${(sc[k]||0)/mx*100}%;${k>=7?"background:var(--good)":""}" title="score ${k}: ${sc[k]||0}"><span>${k}</span></div>`).join("");
$("open").innerHTML=table(["Coin","Score","Entry","Now","Since"],m.open.map(o=>[esc(o.name),o.score,"$"+(o.mc||0).toLocaleString(),pct(o.ret_now),ago(o.t)]));
const f=m.funnel;$("fun").innerHTML=table(["Stage","Count"],[["X-linked launches scored",f.scored],["Narrative ≥ 7",f.passed],["Reached $5–7k + checks",f.checked],["Passed checks (bought)",f.checks_ok],
...Object.entries(f.blocked).map(([k,v])=>[`<span class="mute">blocked: ${esc(k)}</span>`,v]),[`<span class="mute">models</span>`,esc(Object.entries(f.models).map(([k,v])=>k+" "+v).join(", "))],
[`<span class="mute">LLM calls today</span>`,esc(Object.entries(m.status.calls||{}).map(([k,v])=>k.split(" ")[1]+" "+v).join(", ")||"–")]]);
$("rules").innerHTML=table(["Rule version","Trades","Mean","Win","t"],Object.entries(m.by_rules).map(([k,v])=>[esc(k),v.n,v.n?pct(v.mean):"–",v.n?(100*v.win).toFixed(0)+"%":"–",v.n>1?v.t.toFixed(2):"–"]));
$("trades").innerHTML=table(["Time","Coin","Narrative","Score","Exit","Return","Held"],m.recent.map(r=>[tm(r.t),esc(r.name),`<span class="mute small">…${esc(r.narr)}</span>`,r.score,esc(r.exit),pct(r.ret),r.held+"m"]));
$("top").innerHTML=table(["Time","Coin","Score","Why"],m.top.map(r=>[tm(r.t),r.link?`<a href="${esc(r.link)}" target="_blank" rel="noopener">${esc(r.name)}</a>`:esc(r.name),r.score,`<span class="small">${esc(r.why)}</span>`]));
$("jobs").innerHTML=table(["Job","Status","Last update"],d.jobs.map(j=>[`<b>${esc(j.name)}</b><div class="mute small">${esc(j.desc)}</div>`,`<span class="pill ${j.status}">${j.status}</span><div class="mute small">${j.age_min==null?"":j.age_min<90?j.age_min+" min":(j.age_min/60).toFixed(1)+" h"}</div>`,`<code>${esc(j.last)}</code>`]));
$("kal").innerHTML=table(["Bot","Hours","Rewards","Settled P&L","Net","Open"],d.kalshi.map(k=>[k.name,k.hours,"$"+k.rewards.toFixed(2),`<span class="${k.settled>=0?"pos":"neg"}">$${k.settled.toFixed(2)}</span>`,`<span class="${k.rewards+k.settled>=0?"pos":"neg"}">$${(k.rewards+k.settled).toFixed(2)}</span>`,k.open]));
$("c1").innerHTML=table(["Launches logged","Reached R 40","Trade files","Window"],[[d.c1.creates.toLocaleString(),d.c1.hot,d.c1.files,esc(d.c1.window)]]);
const e=d.experiments;$("exn").textContent=`${e.total} experiment rows · ${e.running} running`;
$("exp").innerHTML=table(["Date","Experiment","What","Result","Verdict"],e.rows.map(r=>[r.date,`<b>${esc(r.note)}</b>`,`<span class="small">${esc(r.strategy)}</span>`,`<span class="small">${esc(r.result)}</span>`,`<span class="pill ${r.state}">${r.state}</span><div class="small mute">${esc(r.verdict)}</div>`]))}
tick();setInterval(tick,15000);
</script></body></html>"""
