"""Trade replay for the M paper bots: every trade, click to replay it tick by tick with buyers, sellers and the bot's moves.

Served by the dashboard at /trades. Candles and trades come from pump.fun's swap-api on demand and are cached in
data/m1/chart_cache once the coin is old enough not to change (2 h after our exit).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import requests

from .paths import DATA_DIR
from .ui_theme import THEME, nav
from .research.full_trades import ts as iso_ts

BOTS = ("m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8")
CACHE = DATA_DIR / "m1" / "chart_cache"
CANDLES = "https://swap-api.pump.fun/v2/coins/{}/candles"
TRADES = "https://swap-api.pump.fun/v2/coins/{}/trades"
S = requests.Session()
S.headers.update({"Origin": "https://pump.fun", "Accept": "application/json"})


def jsonl(p: Path) -> list[dict]:
    out = []
    try:
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


_REASON: dict = {}


def reason(v: str, r: dict) -> str:
    """Why a closed trade won or lost (the TF taxonomy, vault "2026-10-22 trade forensics and fix sweep (TF)").
    'Shaken out' / 'cut winner' need the coin's trades after our exit (data/m1/ticks); without them a trade falls
    through to the record-only categories."""
    key = (v, r["m"], r["t"])
    if key in _REASON:
        return _REASON[key]
    p0 = r.get("p0") or 0
    pk = r["peak"] / p0 if p0 else 1.0
    stop = r.get("stop", 0.7 if v == "m1" else 0.9)
    if r["ret"] > 0:
        out = "win"
    elif pk >= 1.3:
        out = "gave_back"
    elif r.get("exit") == "STOP" and p0 and r["p_exit"] / p0 < stop - 0.10:
        out = "rug_gap"
    else:
        out = None
        tp = DATA_DIR / "m1" / "ticks" / f"{r['m']}.json.gz"
        if p0 and tp.exists():
            import gzip
            from .research.tf_replay import path
            try:
                with gzip.open(tp, "rt", encoding="utf-8") as fh:
                    tr = sorted(json.load(fh)["trades"], key=lambda x: (x[0], x[1]))
                _, post = path(r, tr)
                after = max((y[1] for y in post if r["t_exit"] < y[0] <= r["t"] + 900), default=0) / p0
                if after >= 2:
                    out = "shaken_out" if r.get("exit") == "STOP" else "cut_winner"
            except (OSError, ValueError, KeyError, ZeroDivisionError):
                pass
        if out is None:
            out = "instant_dump" if pk < 1.05 else "slow_bleed"
    _REASON[key] = out
    return out


def trade_list() -> list[dict]:
    rows = []
    for v in BOTS:
        d = DATA_DIR / v
        for r in jsonl(d / "trades.jsonl"):
            if "ret" in r:
                rows.append(dict(v=v, m=r["m"], t=r["t"], name=r.get("name"), score=r.get("score"), mc=r.get("mc"),
                                 exit=r.get("exit"), ret=r["ret"], held=r["t_exit"] - r["t"],
                                 peak=r["peak"] / r["p0"] if r.get("p0") else None, open=False, why=reason(v, r)))
        try:
            st = json.loads((d / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            st = {}
        for o in st.get("open", []):
            rows.append(dict(v=v, m=o["m"], t=o["t"], name=o.get("name"), score=o.get("score"), mc=o.get("mc"),
                             exit=None, ret=(o["last"] / o["p0"] - 1) if o.get("last") and o.get("p0") else None,
                             held=time.time() - o["t"], peak=o["peak"] / o["p0"] if o.get("p0") else None, open=True))
    rows.sort(key=lambda r: -r["t"])
    return rows


def _get(url: str, params: dict):
    for k in range(3):
        try:
            r = S.get(url, params=params, timeout=15)
        except requests.RequestException:
            time.sleep(1 + k)
            continue
        if r.ok:
            return r.json()
        if r.status_code in (400, 404):
            return None
        time.sleep(2 + 2 * k)
    return None


def candles(m: str, t0: float, interval: str) -> list:
    d = _get(CANDLES.format(m), {"interval": interval, "limit": 1000, "currency": "SOL", "createdTs": int(t0 * 1000)})
    return [[x["timestamp"] / 1000, float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]),
             float(x["volume"])] for x in d or []]


def trades_between(m: str, a: float, b: float, pages: int = 40) -> tuple[list, bool]:
    """Every curve trade in [a, b] (newest-first API, paged back from b): [t, price, is_buy, sol, wallet, tokens].
    Returns (rows, complete); complete is False when the page cap cut off the start."""
    rows, cur, complete = [], f"9999999999990000000000-{int(b * 1000)}", False
    for _ in range(pages):
        d = _get(TRADES.format(m), {"limit": 100, "cursor": cur})
        if not d:
            break
        for x in d["trades"]:
            tok = float(x["baseAmount"])
            if tok > 0 and x.get("program", "pump") == "pump":
                rows.append([iso_ts(x["timestamp"]), float(x["amountSol"]) / tok, x["type"] == "buy",
                             float(x["amountSol"]), x["userAddress"], tok])
        cur = d["pagination"].get("nextCursor")
        if not d["pagination"].get("hasMore") or not cur or (rows and rows[-1][0] < a):
            complete = True
            break
    return sorted(r for r in rows if a <= r[0] <= b), complete


def find(v: str, m: str) -> tuple[dict | None, list[dict], dict | None]:
    d = DATA_DIR / v
    trade = next((r for r in reversed(jsonl(d / "trades.jsonl")) if r["m"] == m), None)
    if trade is None:
        try:
            trade = next((o for o in json.loads((d / "status.json").read_text(encoding="utf-8")).get("open", [])
                          if o["m"] == m), None)
        except (OSError, ValueError):
            trade = None
    cand, checks = None, []
    for c in jsonl(d / "cands.jsonl"):
        if c.get("m") != m:
            continue
        if c.get("stage") == "checks":
            checks.append(c)
        elif "score" in c:
            cand = c
    return trade, checks, cand


def chart(v: str, m: str) -> dict:
    trade, checks, cand = find(v, m)
    if trade is None:
        return {"error": "no such trade"}
    t_in = trade["t"]
    t_out = trade.get("t_exit") or time.time()
    t0 = (cand["t"] - cand.get("age", 0)) if cand else t_in - 60
    CACHE.mkdir(parents=True, exist_ok=True)
    cp = CACHE / f"{m}.json"
    data = None
    if cp.exists():
        data = json.loads(cp.read_text())
        if data.get("v") != 2:
            data = None
    if data is None:
        c1 = candles(m, t0 - 60, "1m")
        if not c1 or c1[0][0] > t_in - 300:                # newest-1000 limit: prepend 5m candles for the start
            c5 = candles(m, t0 - 60, "5m")
            c1 = [x for x in c5 if not c1 or x[0] < c1[0][0]] + c1
        tr, complete = trades_between(m, t0 - 5, t_out + 300)
        data = {"v": 2, "candles": c1, "trades": tr, "complete": complete}
        if time.time() > t_out + 7200 and trade.get("t_exit"):
            cp.write_text(json.dumps(data))
    sol = trade.get("sol_usd") or 105.0
    marks = [{"t": t_in, "p": trade["p0"], "kind": "buy", "label": f"BOT BUY at mc ${trade.get('mc') or 0:,.0f}"}]
    if trade.get("half_at"):
        th = trade.get("t_run") or t_in
        marks.append({"t": th, "p": trade["p0"] * trade["half_at"], "kind": "half",
                      "label": f"BOT SELLS HALF at {trade['half_at']:.2f}x"})
    if trade.get("t_exit"):
        marks.append({"t": trade["t_exit"], "p": trade["p_exit"], "kind": "sell",
                      "label": f"BOT SELL ({trade['exit']}) {trade['ret']:+.1%}"})
    info = {k: trade.get(k) for k in ("name", "score", "mc", "exit", "ret", "rules", "link", "copy", "stop", "high",
                                      "act", "act_max", "traction", "tp_buyers", "tp_flow", "half_at", "eng0", "eng_tp")}
    info.update(v=v, m=m, t=t_in, t_exit=trade.get("t_exit"), held=t_out - t_in, peak_x=trade["peak"] / trade["p0"],
                created=t0, sol_usd=sol, p0=trade["p0"], complete=data.get("complete", True))
    if cand:
        info.update({k: cand.get(k) for k in ("crit", "why", "desc", "sym", "first", "model", "improves", "nkeys")})
    info["checks"] = [{k: c[k] for k in c if k not in ("stage", "m")} for c in checks[-3:]]
    if trade.get("t_exit") and "ret" in trade:
        info["outcome"] = reason(v, trade)
    return {"info": info, "marks": marks, "candles": data["candles"], "trades": data["trades"], "sol_usd": sol}


TRADES_HTML = (r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trade Replay</title>
<style>""" + THEME + r"""
:root{--panel:var(--card);--text:var(--ink);--up:var(--good);--down:var(--bad)}
body{font-size:13px}
.filters{padding:8px 14px;border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:center;flex-wrap:wrap;background:var(--card)}
main{display:grid;grid-template-columns:minmax(420px,480px) 1fr;height:calc(100vh - 146px)}
.reasons{display:flex;gap:6px;flex-wrap:wrap;padding:8px 14px;border-bottom:1px solid var(--line);background:var(--bg)}
.rc{border:1px solid var(--line);background:var(--card2);border-radius:999px;padding:4px 11px;font-size:12px;cursor:pointer;font-weight:600;color:var(--mute)}
.rc b{color:var(--ink);margin:0 3px}.rc.on{border-color:var(--acc);background:#1c2440;color:var(--ink)}
.rtag{display:inline-block;font-size:10.5px;padding:0 6px;border-radius:4px;margin-left:4px;font-weight:650}
#list{overflow:auto;border-right:1px solid var(--line)}
table{width:100%;border-collapse:collapse}td,th{padding:5px 6px;border-bottom:1px solid var(--line);white-space:nowrap;text-align:left}
th{position:sticky;top:0;background:var(--panel);color:var(--mute);font-weight:500}
tr.row{cursor:pointer}tr.row:hover{background:#1d212b}tr.sel{background:#22283a}
.pos{color:var(--up)}.neg{color:var(--down)}.mute{color:var(--mute)}
#right{display:flex;flex-direction:column;min-width:0;overflow:auto}
#bar{padding:8px 12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--line)}
#slider{flex:1;min-width:160px;accent-color:var(--buy)}
#stage{display:grid;grid-template-columns:1fr 290px;min-height:430px;height:62vh}
#wrap{position:relative;min-width:0}canvas{position:absolute;inset:0;width:100%;height:100%}
#side{border-left:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
#live{padding:8px 10px;border-bottom:1px solid var(--line);display:grid;grid-template-columns:auto 1fr;gap:2px 10px;font-size:12.5px}
#live div:nth-child(odd){color:var(--mute)}#live .big{font-size:18px;font-weight:600}
#tape{overflow:hidden;flex:1;font:12px/1.5 ui-monospace,Consolas,monospace;padding:4px 8px}
#tape div{display:grid;grid-template-columns:52px 34px 58px 1fr;gap:4px;white-space:nowrap}
.tag{font-size:10px;padding:0 4px;border-radius:3px;margin-left:3px}.t-dev{background:#7c2d12}.t-top{background:#1e3a8a}.t-new{background:#14532d}.t-out{background:#450a0a}
#banner{position:absolute;left:70px;top:10px;padding:6px 10px;border-radius:6px;font-weight:600;display:none}
#info{padding:10px 12px;display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:10px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px}
.card h3{margin:0 0 6px;font-size:12px;color:var(--mute);font-weight:500;text-transform:uppercase;letter-spacing:.04em}
.kv{display:grid;grid-template-columns:auto 1fr;gap:2px 10px}.kv div:nth-child(odd){color:var(--mute)}
@media (max-width:900px){main{grid-template-columns:1fr;height:auto}#list{max-height:35vh}#stage{grid-template-columns:1fr;height:auto}#wrap{height:55vh}#side{max-height:40vh}}
</style></head><body>""" + nav("trades", "Click a trade, then press Play (space)") + r"""
<header class="filters">
<select id="bot"><option value="">all bots</option><option>m1</option><option>m2</option><option>m3</option><option>m4</option><option>m5</option><option>m6</option><option>m7</option><option>m8</option></select>
<select id="res"><option value="">all results</option><option value="w">wins</option><option value="l">losses</option><option value="o">open</option></select>
<input id="q" placeholder="search name" size="14"><span id="sum" class="mute"></span></header>
<div class="reasons" id="reasons"></div>
<main><div id="list"><table><thead><tr><th>time</th><th>bot</th><th>coin</th><th>sc</th><th>exit</th><th>ret</th></tr></thead><tbody id="rows"></tbody></table></div>
<div id="right">
<div id="bar"><button id="play" title="space">&#9654; Play</button>
<select id="spd"><option value="1">1x</option><option value="3" selected>3x</option><option value="10">10x</option><option value="30">30x</option><option value="120">120x</option></select>
<button id="jbuy">&#8676; bot buy</button><button id="jsell">bot sell &#8677;</button><button id="jend">full</button>
<input id="slider" type="range" min="0" max="1000" value="0"><span id="clock" class="mute"></span>
<button id="ov">1m overview</button></div>
<div id="stage"><div id="wrap"><canvas id="cv"></canvas><div id="banner"></div></div>
<div id="side"><div id="live"><div class="mute">Click a trade on the left, then Play.</div></div><div id="tape"></div></div></div>
<div id="info"></div></div></main>
<script>
let ALL=[],D=null,SEL=null,CUR=0,PLAY=false,LAST=0,OV=false,S=null;
const $=id=>document.getElementById(id);
const fmtT=t=>{const d=new Date(t*1000);return d.toTimeString().slice(0,8)};
const fmtD=t=>{const d=new Date(t*1000);return (d.getMonth()+1)+'/'+d.getDate()+' '+d.toTimeString().slice(0,5)};
const pct=x=>x==null||isNaN(x)?'-':(x>=0?'+':'')+(100*x).toFixed(1)+'%';
const usd=x=>x>=1e6?'$'+(x/1e6).toFixed(2)+'M':x>=1e3?'$'+(x/1e3).toFixed(1)+'k':'$'+x.toFixed(0);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const FEE=0.0175;
const WHY={instant_dump:['Instant dump','never above 1.05x after we bought','#ef4444'],rug_gap:['Rug gap','stop filled 10+ pts below its level','#dc2626'],
 slow_bleed:['Slow bleed','drifted down, no rug','#f97316'],gave_back:['Gave back a gain','was up 30%+, sold at a loss','#eab308'],
 shaken_out:['Shaken out','stopped out, then the coin did 2x','#a855f7'],cut_winner:['Cut a winner','sold early, then the coin did 2x','#8b5cf6'],win:['Win','closed in profit','#22c55e']};
let WHYSEL='';
async function load(){ALL=await (await fetch('/api/trades')).json();list()}
function chips(base){const c={};base.filter(x=>!x.open&&x.why).forEach(x=>{(c[x.why]=c[x.why]||{n:0,s:0}).n++;c[x.why].s+=x.ret});
 $('reasons').innerHTML='<span class="mute small" style="align-self:center;margin-right:4px">Why:</span>'+Object.keys(WHY).filter(k=>c[k]).sort((a,b)=>c[a].s-c[b].s).map(k=>
  `<span class="rc${WHYSEL==k?' on':''}" data-w="${k}" title="${WHY[k][1]}"><i class="dot" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${WHY[k][2]}"></i> ${WHY[k][0]}<b>${c[k].n}</b><span class="${c[k].s>0?'pos':'neg'}">${(c[k].s*0.1>=0?'+':'')+(c[k].s*0.1).toFixed(2)} SOL</span></span>`).join('')+
  (WHYSEL?'<span class="rc" data-w="">show all</span>':'')}
$('reasons').onclick=e=>{const c=e.target.closest('.rc');if(!c)return;WHYSEL=WHYSEL==c.dataset.w?'':c.dataset.w;list()};
function list(){const b=$('bot').value,r=$('res').value,q=$('q').value.toLowerCase();
 const base=ALL.filter(x=>(!b||x.v==b)&&(!q||(x.name||'').toLowerCase().includes(q))&&(!r||(r=='o'?x.open:r=='w'?(!x.open&&x.ret>0):(!x.open&&x.ret<=0))));
 chips(base);const rows=base.filter(x=>!WHYSEL||x.why==WHYSEL);
 const c=rows.filter(x=>!x.open),s=c.reduce((a,x)=>a+x.ret,0);
 $('sum').textContent=c.length+' closed, mean '+pct(c.length?s/c.length:0)+', '+c.filter(x=>x.ret>0).length+' wins';
 $('rows').innerHTML=rows.slice(0,1500).map(x=>`<tr class="row${SEL==x.v+x.m?' sel':''}" data-k="${x.v}|${x.m}"><td class="mute" title="${fmtD(x.t)}">${fmtD(x.t).split(' ')[1]}</td><td>${x.v}</td><td>${esc((x.name||'').slice(0,18))}${x.why&&x.why!='win'?`<span class="rtag" style="background:${WHY[x.why][2]}33;color:${WHY[x.why][2]}">${WHY[x.why][0]}</span>`:''}</td><td>${x.score??''}</td><td>${x.open?'<i>open</i>':esc(x.exit)}</td><td class="${x.ret>0?'pos':'neg'}">${pct(x.ret)}</td></tr>`).join('')}
['bot','res'].forEach(i=>$(i).onchange=list);$('q').oninput=list;
$('rows').onclick=e=>{const tr=e.target.closest('tr');if(!tr)return;document.querySelectorAll('tr.sel').forEach(x=>x.classList.remove('sel'));tr.classList.add('sel');
 const [v,m]=tr.dataset.k.split('|');history.replaceState(null,'','#'+v+'|'+m);openTrade(v,m)};
async function openTrade(v,m,at){SEL=v+m;stop();
 $('live').innerHTML='<div class="mute">Loading trades...</div><div></div>';$('tape').innerHTML='';
 const d=await (await fetch('/api/chart?v='+v+'&m='+m)).json();if(d.error){$('live').innerHTML='<div>'+esc(d.error)+'</div><div></div>';return}
 prep(d);CUR=at!=null?D.info.t+at:Math.max(S.t0,D.info.t-20);render();info()}
function prep(d){D=d;const I=d.info,tr=d.trades,dev=tr.length?tr[0][4]:null;const t0=tr.length?Math.min(tr[0][0],I.t-30):I.t-60;
 const t1=Math.max(tr.length?tr[tr.length-1][0]:0,(I.t_exit||I.t)+60);
 // running state per trade: holders, dev %, top10 %, first-seen and sold-out flags
 const bal={},seen={};let hold=0;const st=[];
 tr.forEach((x,i)=>{const w=x[4],b0=bal[w]||0,b1=b0+(x[2]?x[5]:-x[5]);bal[w]=b1;if(b0<=1&&b1>1)hold++;if(b0>1&&b1<=1)hold--;
  const tags=[];if(w==dev)tags.push('dev');if(!seen[w]){seen[w]=1;if(x[2])tags.push('new')}if(!x[2]&&b1<=1)tags.push('out');
  st.push({hold,tags,devp:Math.max(0,bal[dev]||0)/1e7});
  if(i%25==0||i==tr.length-1){const top=Object.entries(bal).filter(([k,v])=>k!=dev&&v>0).map(([k,v])=>v).sort((a,b)=>b-a).slice(0,10).reduce((a,v)=>a+v,0);st[i].top10=top/1e7}});
 let lt=0;st.forEach(s=>{if(s.top10!=null)lt=s.top10;else s.top10=lt});
 const topW=new Set(Object.entries(bal).sort((a,b)=>b[1]-a[1]).slice(0,10).map(x=>x[0]));
 S={t0,t1,dev,st,topW,k:1e9*d.sol_usd};$('slider').value=0}
function idx(t){const tr=D.trades;let lo=0,hi=tr.length;while(lo<hi){const m=(lo+hi)>>1;if(tr[m][0]<=t)lo=m+1;else hi=m}return lo}
function render(){if(!D)return;const I=D.info,cv=$('cv'),W=cv.clientWidth,H=cv.clientHeight,dpr=devicePixelRatio||1;cv.width=W*dpr;cv.height=H*dpr;
 const x=cv.getContext('2d');x.scale(dpr,dpr);x.clearRect(0,0,W,H);const k=S.k,tr=D.trades,n=idx(CUR);
 $('slider').value=Math.round((CUR-S.t0)/(S.t1-S.t0)*1000);$('clock').textContent=`${fmtT(CUR)}  (+${Math.round(CUR-I.created)} s from create)`;
 if(OV){overview(x,W,H);live(n);return}
 // fixed axes over the whole window so the chart builds like a live screen
 let lo=1e99,hi=0;tr.forEach(p=>{lo=Math.min(lo,p[1]);hi=Math.max(hi,p[1])});D.marks.forEach(m=>{lo=Math.min(lo,m.p);hi=Math.max(hi,m.p)});
 if(!tr.length){lo=hi=I.p0}lo*=k;hi*=k;
 const L=62,R=10,T=10,P1=Math.round(H*0.58),P2=Math.round(H*0.78),B=20;
 const X=t=>L+(t-S.t0)/(S.t1-S.t0||1)*(W-L-R),yl=Math.log(lo*0.93),yh=Math.log(hi*1.07),Y=v=>T+(1-(Math.log(v)-yl)/(yh-yl||1))*(P1-T);
 x.font='11px system-ui';x.lineWidth=1;
 for(let i=0;i<=4;i++){const v=Math.exp(yl+(yh-yl)*i/4),y=Y(v);x.strokeStyle='#262b36';x.beginPath();x.moveTo(L,y);x.lineTo(W-R,y);x.stroke();x.fillStyle='#8b93a7';x.fillText(usd(v),4,y+4)}
 const pe=I.p0*k;x.setLineDash([5,4]);x.strokeStyle='#3b82f6aa';x.beginPath();x.moveTo(L,Y(pe));x.lineTo(W-R,Y(pe));x.stroke();
 x.fillStyle='#60a5fa';x.fillText('our entry',W-R-58,Y(pe)-4);
 [[2,'2x'],[1.5,'1.5x']].forEach(([m,l])=>{if(pe*m<hi*1.07){x.strokeStyle='#22c55e55';x.beginPath();x.moveTo(L,Y(pe*m));x.lineTo(W-R,Y(pe*m));x.stroke();x.fillStyle='#22c55e99';x.fillText(l,W-R-22,Y(pe*m)-3)}});
 if(I.stop&&I.stop<1){x.strokeStyle='#ef444477';x.beginPath();x.moveTo(L,Y(pe*I.stop));x.lineTo(W-R,Y(pe*I.stop));x.stroke();x.fillStyle='#ef4444aa';x.fillText('stop',W-R-26,Y(pe*I.stop)+12)}
 x.setLineDash([]);
 if(CUR>=I.t){x.fillStyle='#3b82f614';x.fillRect(X(I.t),T,Math.max(1,X(Math.min(CUR,I.t_exit||CUR))-X(I.t)),P1-T)}
 // price line + trade dots up to the cursor
 x.strokeStyle='#cbd5e1';x.beginPath();for(let i=0;i<n;i++){const px=X(tr[i][0]),py=Y(tr[i][1]*k);i?x.lineTo(px,py):x.moveTo(px,py)}x.stroke();
 for(let i=Math.max(0,n-400);i<n;i++){const p=tr[i];x.fillStyle=p[2]?'#34d399':'#f87171';x.globalAlpha=0.8;x.beginPath();x.arc(X(p[0]),Y(p[1]*k),Math.min(7,1.4+Math.sqrt(p[3])*1.7),0,7);x.fill()}x.globalAlpha=1;
 // volume pane: buy SOL up, sell SOL down, per bucket
 const bw=Math.max(2,(S.t1-S.t0)/160),nb=Math.ceil((S.t1-S.t0)/bw),bu=new Array(nb).fill(0),se=new Array(nb).fill(0),by=new Array(nb).fill(0);
 const seenB=[];for(let i=0;i<n;i++){const b=Math.floor((tr[i][0]-S.t0)/bw);if(b<0||b>=nb)continue;if(tr[i][2])bu[b]+=tr[i][3];else se[b]+=tr[i][3]}
 const vmax=Math.max(0.5,...bu,...se),mid=(P1+P2)/2+4,hh=(P2-P1)/2-6,pw=Math.max(1,(W-L-R)/nb-1);
 x.fillStyle='#8b93a7';x.fillText('buy / sell SOL',4,P1+16);x.strokeStyle='#262b36';x.beginPath();x.moveTo(L,mid);x.lineTo(W-R,mid);x.stroke();
 for(let b=0;b<nb;b++){const px=L+b*(W-L-R)/nb;if(bu[b]){x.fillStyle='#34d399';x.fillRect(px,mid-bu[b]/vmax*hh,pw,bu[b]/vmax*hh)}if(se[b]){x.fillStyle='#f87171';x.fillRect(px,mid,pw,se[b]/vmax*hh)}}
 // buyers pane: unique buyers and sellers in the last 20 s
 const step=Math.max(1,(S.t1-S.t0)/240);let bmax=4;const pts=[];
 for(let t=S.t0;t<=Math.min(CUR,S.t1);t+=step){const a=idx(t-20),z=idx(t);const B1=new Set(),S1=new Set();for(let i=a;i<z;i++)(tr[i][2]?B1:S1).add(tr[i][4]);pts.push([t,B1.size,S1.size]);bmax=Math.max(bmax,B1.size,S1.size)}
 const Yb=v=>H-B-(v/bmax)*(H-B-P2-6);x.fillStyle='#8b93a7';x.fillText('wallets / 20 s',4,P2+14);x.fillText('max '+bmax,4,P2+28);
 [[1,'#34d399'],[2,'#f87171']].forEach(([j,c])=>{x.strokeStyle=c;x.lineWidth=1.5;x.beginPath();pts.forEach((p,i)=>{const px=X(p[0]),py=Yb(p[j]);i?x.lineTo(px,py):x.moveTo(px,py)});x.stroke()});x.lineWidth=1;
 for(let i=0;i<=4;i++){const t=S.t0+(S.t1-S.t0)*i/4;x.fillStyle='#8b93a7';x.fillText('+'+Math.round(t-I.created)+'s',X(t)-12,H-5)}
 // bot actions appear when the replay reaches them
 let ban=null;D.marks.forEach(m=>{if(m.t>CUR)return;const px=X(m.t),py=Y(m.p*k);x.fillStyle=m.kind=='buy'?'#60a5fa':m.kind=='sell'?'#f59e0b':'#a855f7';
  x.beginPath();if(m.kind=='buy'){x.moveTo(px,py+3);x.lineTo(px-8,py+17);x.lineTo(px+8,py+17)}else if(m.kind=='sell'){x.moveTo(px,py-3);x.lineTo(px-8,py-17);x.lineTo(px+8,py-17)}else{x.moveTo(px,py-8);x.lineTo(px+8,py);x.lineTo(px,py+8);x.lineTo(px-8,py)}x.fill();
  x.strokeStyle=x.fillStyle;x.setLineDash([2,3]);x.beginPath();x.moveTo(px,T);x.lineTo(px,H-B);x.stroke();x.setLineDash([]);
  x.fillStyle='#e6e8ee';x.fillText(m.label,Math.min(px+10,W-190),m.kind=='sell'?py-20:py+30);if(CUR-m.t<4)ban=m});
 const bn=$('banner');if(ban){bn.style.display='block';bn.textContent=ban.label;bn.style.background=ban.kind=='buy'?'#1e3a8a':ban.kind=='sell'?'#78350f':'#581c87'}else bn.style.display='none';
 x.strokeStyle='#e6e8ee55';x.beginPath();x.moveTo(X(CUR),T);x.lineTo(X(CUR),H-B);x.stroke();
 live(n)}
function overview(x,W,H){const I=D.info,k=S.k,c=D.candles.filter(y=>y[0]>=I.created-120&&y[0]<=(I.t_exit||I.t)+3600);if(!c.length)return;
 let lo=1e99,hi=0;c.forEach(y=>{lo=Math.min(lo,y[3]);hi=Math.max(hi,y[2])});lo*=k;hi*=k;const L=62,R=10,T=10,B=20,a=c[0][0],b=c[c.length-1][0]+60;
 const X=t=>L+(t-a)/(b-a)*(W-L-R),yl=Math.log(lo*0.95),yh=Math.log(hi*1.05),Y=v=>T+(1-(Math.log(v)-yl)/(yh-yl))*(H-T-B);
 x.font='11px system-ui';for(let i=0;i<=4;i++){const v=Math.exp(yl+(yh-yl)*i/4);x.fillStyle='#8b93a7';x.fillText(usd(v),4,Y(v)+4)}
 const w=Math.max(1,(X(a+60)-X(a))*0.7);c.forEach(y=>{x.strokeStyle=x.fillStyle=y[4]>=y[1]?'#34d399':'#f87171';const cx=X(y[0]+30);x.beginPath();x.moveTo(cx,Y(y[2]*k));x.lineTo(cx,Y(y[3]*k));x.stroke();const o=Y(y[1]*k),z=Y(y[4]*k);x.fillRect(cx-w/2,Math.min(o,z),w,Math.max(1,Math.abs(o-z)))});
 D.marks.forEach(m=>{x.fillStyle=m.kind=='buy'?'#60a5fa':m.kind=='sell'?'#f59e0b':'#a855f7';x.beginPath();x.arc(X(m.t),Y(m.p*k),5,0,7);x.fill();x.fillStyle='#e6e8ee';x.fillText(m.label,Math.min(X(m.t)+8,W-190),Y(m.p*k)-8)});
 x.fillStyle='#8b93a7';x.fillText('1-min candles from create to 1 h after the exit',L,H-5)}
function live(n){const I=D.info,tr=D.trades,k=S.k;const p=n?tr[n-1][1]:I.p0,s=n?S.st[n-1]:{hold:0,devp:0,top10:0};
 const a=idx(CUR-20),f=idx(CUR-15);const B1=new Set(),S1=new Set();for(let i=a;i<n;i++)(tr[i][2]?B1:S1).add(tr[i][4]);
 let flow=0;for(let i=f;i<n;i++)flow+=tr[i][2]?tr[i][3]:-tr[i][3];
 const inPos=CUR>=I.t&&(!I.t_exit||CUR<I.t_exit),xr=p/I.p0;let pk=0;if(CUR>=I.t)for(let i=idx(I.t);i<n;i++)pk=Math.max(pk,tr[i][1]/I.p0);
 const pnl=xr*(1-FEE)/(1+FEE)-1;
 const status=CUR<I.t?'<span class="mute">watching</span>':inPos?'<b style="color:#93c5fd">HOLDING</b>':`<b style="color:#fbbf24">SOLD (${esc(I.exit)})</b> ${pct(I.ret)}`;
 $('live').innerHTML=`<div>market cap</div><div class="big">${usd(p*k)}</div><div>bot</div><div>${status}</div>
 <div>vs our entry</div><div class="${xr>=1?'pos':'neg'}">${xr.toFixed(2)}x ${CUR>=I.t?'('+pct(pnl)+' after fees)':''}</div>
 <div>peak since buy</div><div>${pk?pk.toFixed(2)+'x':'-'}</div>
 <div>buyers / sellers 20 s</div><div><span class="pos">${B1.size}</span> / <span class="neg">${S1.size}</span></div>
 <div>net flow 15 s</div><div class="${flow>=0?'pos':'neg'}">${flow>=0?'+':''}${flow.toFixed(2)} SOL</div>
 <div>holders</div><div>${s.hold}</div><div>dev holds</div><div>${s.devp.toFixed(1)}%</div><div>top 10 (ex dev)</div><div>${(s.top10||0).toFixed(1)}%</div>
 ${I.complete?'':'<div class="neg">note</div><div class="neg">busy coin: trades before the window are missing</div>'}`;
 const rows=[];for(let i=n-1;i>=Math.max(0,n-30);i--){const q=tr[i],tg=S.st[i].tags.slice();if(S.topW.has(q[4])&&q[4]!=S.dev)tg.push('top');
  rows.push(`<div><span class="mute">${fmtT(q[0])}</span><span class="${q[2]?'pos':'neg'}">${q[2]?'BUY':'SELL'}</span><span>${q[3].toFixed(3)}</span><span>${esc(q[4].slice(0,5))}${tg.map(t=>`<span class="tag t-${t}">${t}</span>`).join('')}</span></div>`)}
 $('tape').innerHTML=rows.join('')}
function info(){const I=D.info,ck=(I.checks||[]).map(c=>`<div class="card"><h3>check ${fmtT(c.t)} &middot; ${c.ok?'<span class=pos>passed</span>':'<span class=neg>blocked</span>'}</h3>${kv(Object.fromEntries(Object.entries(c).filter(([a])=>a!='t'&&a!='ok')))}</div>`).join('');
 $('info').innerHTML=`<div class="card"><h3>${esc(I.v)} &middot; ${esc(I.name)}</h3>${kv({'why':I.outcome&&WHY[I.outcome]?WHY[I.outcome][0]+' — '+WHY[I.outcome][1]:null,result:I.exit?I.exit+' '+pct(I.ret):'open '+pct(I.ret),'entry mc':I.mc?usd(I.mc):null,bought:fmtD(I.t)+' ('+Math.round(I.t-I.created)+' s after create)',held:(I.held/60).toFixed(1)+' min',peak:I.peak_x.toFixed(2)+'x',rules:I.rules})}
 <a href="https://pump.fun/coin/${esc(I.m)}" target="_blank" rel="noopener">pump.fun</a>${I.link?' &middot; <a href="'+esc(I.link)+'" target="_blank" rel="noopener">X post</a>':''}</div>
 <div class="card"><h3>narrative score ${I.score??''} &middot; criterion ${I.crit??'-'}</h3>${kv({why:I.why,improves:I.improves,'first coin':I.first,copy:I.copy,model:I.model,desc:(I.desc||'').slice(0,300)})}</div>
 <div class="card"><h3>exit data</h3>${kv({stop:I.stop,'conviction high':I.high,activity:I.act!=null?I.act+' / max '+I.act_max:null,traction:I.traction,'buyers/min at 2x':I.tp_buyers,'buy/sell flow':I.tp_flow,'half at':I.half_at})}</div>${ck}
 <div class="card mute">Tape tags: <span class="tag t-dev">dev</span> creator, <span class="tag t-new">new</span> first buy of a wallet, <span class="tag t-top">top</span> a top-10 holder by the end, <span class="tag t-out">out</span> wallet sold everything. Volume pane: green = SOL bought, red = SOL sold. Bottom pane: unique buyers (green) and sellers (red) in the last 20 s.</div>`}
function kv(o){return '<div class="kv">'+Object.entries(o).filter(([a,b])=>b!==undefined&&b!==null&&b!=='').map(([a,b])=>`<div>${esc(a)}</div><div>${typeof b=='object'?esc(JSON.stringify(b)):esc(b)}</div>`).join('')+'</div>'}
function tick(ts){if(!PLAY)return;const dt=LAST?(ts-LAST)/1000:0;LAST=ts;CUR=Math.min(S.t1,CUR+dt*+$('spd').value);render();if(CUR>=S.t1){stop();return}requestAnimationFrame(tick)}
function stop(){PLAY=false;LAST=0;$('play').innerHTML='&#9654; Play'}
$('play').onclick=()=>{if(!D)return;if(PLAY){stop();return}if(CUR>=S.t1)CUR=S.t0;OV=false;$('ov').classList.remove('on');PLAY=true;$('play').innerHTML='&#10074;&#10074; Pause';requestAnimationFrame(tick)};
$('slider').oninput=e=>{if(!D)return;CUR=S.t0+(S.t1-S.t0)*e.target.value/1000;render()};
$('jbuy').onclick=()=>{if(D){CUR=D.info.t-5;render()}};$('jsell').onclick=()=>{if(D){CUR=(D.info.t_exit||D.info.t)+1;render()}};$('jend').onclick=()=>{if(D){CUR=S.t1;render()}};
$('ov').onclick=()=>{OV=!OV;$('ov').classList.toggle('on',OV);render()};
document.onkeydown=e=>{if(e.target.tagName=='INPUT'&&e.target.type!='range')return;if(e.code=='Space'){e.preventDefault();$('play').click()}
 if(e.key=='ArrowRight'&&D){CUR=Math.min(S.t1,CUR+2);render()}if(e.key=='ArrowLeft'&&D){CUR=Math.max(S.t0,CUR-2);render()}};
window.onresize=()=>render();load().then(()=>{const h=decodeURIComponent(location.hash.slice(1)).split('|');if(h.length>=2)openTrade(h[0],h[1],h[2]!=null?+h[2]:null)});setInterval(()=>{if(!PLAY)load()},30000);
</script></body></html>""")
