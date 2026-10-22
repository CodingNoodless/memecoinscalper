"""Trade charts for the M paper bots: every trade, click for its chart with buy / sell markers and the bot's reasons.

Served by the dashboard at /trades. Candles and trades come from pump.fun's swap-api on demand and are cached in
data/m1/chart_cache once the coin is old enough not to change (2 h after our exit).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import requests

from .paths import DATA_DIR
from .research.full_trades import ts as iso_ts

BOTS = ("m1", "m2", "m3")
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


def trade_list() -> list[dict]:
    rows = []
    for v in BOTS:
        d = DATA_DIR / v
        for r in jsonl(d / "trades.jsonl"):
            if "ret" in r:
                rows.append(dict(v=v, m=r["m"], t=r["t"], name=r.get("name"), score=r.get("score"), mc=r.get("mc"),
                                 exit=r.get("exit"), ret=r["ret"], held=r["t_exit"] - r["t"],
                                 peak=r["peak"] / r["p0"] if r.get("p0") else None, open=False))
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


def trades_between(m: str, a: float, b: float, pages: int = 15) -> list:
    """Every curve trade in [a, b] (newest-first API, paged back from b): [t, price, is_buy, sol, wallet]."""
    rows, cur = [], f"9999999999990000000000-{int(b * 1000)}"
    for _ in range(pages):
        d = _get(TRADES.format(m), {"limit": 100, "cursor": cur})
        if not d:
            break
        for x in d["trades"]:
            tok = float(x["baseAmount"])
            if tok > 0:
                rows.append([iso_ts(x["timestamp"]), float(x["amountSol"]) / tok, x["type"] == "buy",
                             float(x["amountSol"]), x["userAddress"]])
        cur = d["pagination"].get("nextCursor")
        if not d["pagination"].get("hasMore") or not cur or (rows and rows[-1][0] < a):
            break
    return sorted(r for r in rows if a <= r[0] <= b)


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
    if data is None:
        c1 = candles(m, t0 - 60, "1m")
        if not c1 or c1[0][0] > t_in - 300:                # newest-1000 limit: prepend 5m candles for the start
            c5 = candles(m, t0 - 60, "5m")
            c1 = [x for x in c5 if not c1 or x[0] < c1[0][0]] + c1
        tr = trades_between(m, t_in - 180, t_out + 300)
        data = {"candles": c1, "trades": tr}
        if time.time() > t_out + 7200 and trade.get("t_exit"):
            cp.write_text(json.dumps(data))
    sol = trade.get("sol_usd") or 105.0
    marks = [{"t": t_in, "p": trade["p0"], "kind": "buy", "label": f"BUY mc ${trade.get('mc') or 0:,.0f}"}]
    if trade.get("half_at"):
        th = trade.get("t_run") or t_in
        marks.append({"t": th, "p": trade["p0"] * trade["half_at"], "kind": "half",
                      "label": f"HALF sold at {trade['half_at']:.2f}x"})
    if trade.get("t_exit"):
        marks.append({"t": trade["t_exit"], "p": trade["p_exit"], "kind": "sell",
                      "label": f"SELL {trade['exit']} {trade['ret']:+.1%}"})
    if trade.get("t_peak"):
        marks.append({"t": trade["t_peak"], "p": trade["peak"], "kind": "peak",
                      "label": f"peak {trade['peak'] / trade['p0']:.2f}x"})
    info = {k: trade.get(k) for k in ("name", "score", "mc", "exit", "ret", "rules", "link", "copy", "stop", "high",
                                      "act", "act_max", "traction", "tp_buyers", "tp_flow", "half_at", "eng0", "eng_tp")}
    info.update(v=v, m=m, t=t_in, t_exit=trade.get("t_exit"), held=t_out - t_in, peak_x=trade["peak"] / trade["p0"],
                created=t0, sol_usd=sol)
    if cand:
        info.update({k: cand.get(k) for k in ("crit", "why", "desc", "sym", "first", "model", "improves", "nkeys")})
    info["checks"] = [{k: c[k] for k in c if k not in ("stage", "m")} for c in checks[-3:]]
    return {"info": info, "marks": marks, "candles": data["candles"], "trades": data["trades"], "sol_usd": sol}


TRADES_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trade Charts</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--text:#e6e8ee;--mute:#8b93a7;--up:#22c55e;--down:#ef4444;
--buy:#3b82f6;--sell:#f59e0b;--half:#a855f7;--peak:#94a3b8}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px/1.4 system-ui,Segoe UI,sans-serif}
header{padding:10px 16px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header h1{font-size:15px;margin:0}header a{color:var(--mute)}
select,button,input{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:4px 8px;font:inherit}
button.on{border-color:var(--buy)}
main{display:grid;grid-template-columns:minmax(300px,420px) 1fr;height:calc(100vh - 49px)}
#list{overflow:auto;border-right:1px solid var(--line)}
table{width:100%;border-collapse:collapse}td,th{padding:5px 8px;border-bottom:1px solid var(--line);white-space:nowrap;text-align:left}
th{position:sticky;top:0;background:var(--panel);color:var(--mute);font-weight:500}
tr.row{cursor:pointer}tr.row:hover{background:#1d212b}tr.sel{background:#22283a}
.pos{color:var(--up)}.neg{color:var(--down)}.mute{color:var(--mute)}
#right{display:flex;flex-direction:column;min-width:0;overflow:auto}
#tools{padding:8px 12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--line)}
#wrap{position:relative;height:56vh;min-height:320px}canvas{position:absolute;inset:0;width:100%;height:100%}
#tip{position:absolute;pointer-events:none;background:#000c;padding:4px 6px;border-radius:4px;font-size:12px;display:none}
#info{padding:10px 14px;display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px}
.card h3{margin:0 0 6px;font-size:12px;color:var(--mute);font-weight:500;text-transform:uppercase;letter-spacing:.04em}
.kv{display:grid;grid-template-columns:auto 1fr;gap:2px 10px}.kv div:nth-child(odd){color:var(--mute)}
.legend span{margin-right:10px}
@media (max-width:800px){main{grid-template-columns:1fr;height:auto}#list{max-height:40vh}}
</style></head><body>
<header><h1>Trade charts</h1>
<select id="bot"><option value="">all bots</option><option>m1</option><option>m2</option><option>m3</option></select>
<select id="res"><option value="">all results</option><option value="w">wins</option><option value="l">losses</option><option value="o">open</option></select>
<input id="q" placeholder="search name" size="14"><span id="sum" class="mute"></span>
<a href="/">lab</a></header>
<main><div id="list"><table><thead><tr><th>time</th><th>bot</th><th>coin</th><th>sc</th><th>exit</th><th>ret</th></tr></thead><tbody id="rows"></tbody></table></div>
<div id="right"><div id="tools"><button id="vc" class="on">1m candles</button><button id="vt">trades (tick)</button>
<button id="lg">log scale</button><span class="legend mute"><span style="color:var(--buy)">&#9650; buy</span><span style="color:var(--sell)">&#9660; sell</span><span style="color:var(--half)">&#9670; half</span><span style="color:var(--peak)">&#9679; peak</span> &middot; prices as market cap (USD)</span></div>
<div id="wrap"><canvas id="cv"></canvas><div id="tip"></div></div><div id="info"><div class="card mute">Click a trade on the left.</div></div></div></main>
<script>
let ALL=[],CUR=null,VIEW='c',LOG=false,SEL=null;
const $=id=>document.getElementById(id);
const fmtT=t=>{const d=new Date(t*1000);return (d.getMonth()+1)+'/'+d.getDate()+' '+d.toTimeString().slice(0,8)};
const pct=x=>x==null?'-':(x>=0?'+':'')+(100*x).toFixed(1)+'%';
const usd=x=>x>=1e6?'$'+(x/1e6).toFixed(2)+'M':x>=1e3?'$'+(x/1e3).toFixed(1)+'k':'$'+x.toFixed(0);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function load(){ALL=await (await fetch('/api/trades')).json();draw()}
function draw(){const b=$('bot').value,r=$('res').value,q=$('q').value.toLowerCase();
 const rows=ALL.filter(x=>(!b||x.v==b)&&(!q||(x.name||'').toLowerCase().includes(q))&&(!r||(r=='o'?x.open:r=='w'?(!x.open&&x.ret>0):(!x.open&&x.ret<=0))));
 const closed=rows.filter(x=>!x.open);const s=closed.reduce((a,x)=>a+x.ret,0);
 $('sum').textContent=closed.length+' closed, mean '+pct(closed.length?s/closed.length:0)+', wins '+closed.filter(x=>x.ret>0).length;
 $('rows').innerHTML=rows.slice(0,1500).map((x,i)=>`<tr class="row${SEL==x.v+x.m+x.t?' sel':''}" data-k="${x.v}|${x.m}|${x.t}"><td class="mute">${fmtT(x.t)}</td><td>${x.v}</td><td>${esc((x.name||'').slice(0,22))}</td><td>${x.score??''}</td><td>${x.open?'<i>open</i>':esc(x.exit)}</td><td class="${x.ret>0?'pos':'neg'}">${pct(x.ret)}</td></tr>`).join('');}
$('rows').onclick=async e=>{const tr=e.target.closest('tr');if(!tr)return;const [v,m,t]=tr.dataset.k.split('|');SEL=v+m+t;
 document.querySelectorAll('tr.sel').forEach(x=>x.classList.remove('sel'));tr.classList.add('sel');
 $('info').innerHTML='<div class="card mute">Loading chart...</div>';
 const d=await (await fetch('/api/chart?v='+v+'&m='+m)).json();if(d.error){$('info').innerHTML='<div class="card">'+esc(d.error)+'</div>';return}
 CUR=d;render();info(d.info)};
['bot','res'].forEach(i=>$(i).onchange=draw);$('q').oninput=draw;
$('vc').onclick=()=>{VIEW='c';$('vc').classList.add('on');$('vt').classList.remove('on');render()};
$('vt').onclick=()=>{VIEW='t';$('vt').classList.add('on');$('vc').classList.remove('on');render()};
$('lg').onclick=()=>{LOG=!LOG;$('lg').classList.toggle('on',LOG);render()};
window.onresize=()=>CUR&&render();
let G=null;
function render(){const d=CUR,cv=$('cv'),W=cv.clientWidth,H=cv.clientHeight,dpr=devicePixelRatio||1;cv.width=W*dpr;cv.height=H*dpr;
 const x=cv.getContext('2d');x.scale(dpr,dpr);x.clearRect(0,0,W,H);const k=1e9*d.sol_usd,I=d.info;
 let t0,t1,pts=[];if(VIEW=='c'){const a=I.created-120,b=(I.t_exit||Date.now()/1000)+3600;pts=d.candles.filter(c=>c[0]>=a&&c[0]<=b);
   if(!pts.length)pts=d.candles;t0=pts.length?pts[0][0]:I.t-60;t1=pts.length?pts[pts.length-1][0]+60:I.t+60}
 else{pts=d.trades;t0=pts.length?pts[0][0]:I.t-60;t1=pts.length?pts[pts.length-1][0]:I.t+60}
 t0=Math.min(t0,I.t-30);t1=Math.max(t1,(I.t_exit||I.t)+30);
 let lo=1e99,hi=0;if(VIEW=='c')pts.forEach(c=>{lo=Math.min(lo,c[3]);hi=Math.max(hi,c[2])});else pts.forEach(p=>{lo=Math.min(lo,p[1]);hi=Math.max(hi,p[1])});
 d.marks.forEach(m=>{lo=Math.min(lo,m.p);hi=Math.max(hi,m.p)});lo*=k;hi*=k;if(hi<=lo){hi=lo*1.1+1}
 const L=64,R=10,T=10,B=24,f=v=>LOG?Math.log(v):v,yl=f(lo*0.97),yh=f(hi*1.03);
 const X=t=>L+(t-t0)/(t1-t0||1)*(W-L-R),Y=v=>T+(1-(f(v)-yl)/(yh-yl||1))*(H-T-B);
 x.strokeStyle='#262b36';x.fillStyle='#8b93a7';x.font='11px system-ui';x.lineWidth=1;
 for(let i=0;i<=5;i++){const v=LOG?Math.exp(yl+(yh-yl)*i/5):lo*0.97+(hi*1.03-lo*0.97)*i/5,yy=Y(v);x.beginPath();x.moveTo(L,yy);x.lineTo(W-R,yy);x.stroke();x.fillText(usd(v),4,yy+4)}
 for(let i=0;i<=4;i++){const t=t0+(t1-t0)*i/4;x.fillText(fmtT(t).split(' ')[1],X(t)-24,H-6)}
 const pe=I.t?d.marks[0].p*k:0;x.setLineDash([4,4]);x.strokeStyle='#3b82f680';x.beginPath();x.moveTo(L,Y(pe));x.lineTo(W-R,Y(pe));x.stroke();
 if(I.stop&&I.stop<1){x.strokeStyle='#ef444480';x.beginPath();x.moveTo(L,Y(pe*I.stop));x.lineTo(W-R,Y(pe*I.stop));x.stroke()}x.setLineDash([]);
 x.fillStyle='#3b82f620';x.fillRect(X(I.t),T,Math.max(1,X(I.t_exit||Date.now()/1000)-X(I.t)),H-T-B);
 if(VIEW=='c'){const w=Math.max(1,(X(t0+60)-X(t0))*0.7);pts.forEach(c=>{const up=c[4]>=c[1];x.strokeStyle=x.fillStyle=up?'#22c55e':'#ef4444';
   const cx=X(c[0]+30);x.beginPath();x.moveTo(cx,Y(c[2]*k));x.lineTo(cx,Y(c[3]*k));x.stroke();const a=Y(c[1]*k),b=Y(c[4]*k);x.fillRect(cx-w/2,Math.min(a,b),w,Math.max(1,Math.abs(a-b)))})}
 else{x.strokeStyle='#cbd5e1';x.beginPath();pts.forEach((p,i)=>{const px=X(p[0]),py=Y(p[1]*k);i?x.lineTo(px,py):x.moveTo(px,py)});x.stroke();
   pts.forEach(p=>{x.fillStyle=p[2]?'#22c55e':'#ef4444';const r=Math.min(6,1.5+Math.sqrt(p[3])*1.5);x.beginPath();x.arc(X(p[0]),Y(p[1]*k),r,0,7);x.fill()})}
 const col={buy:'#3b82f6',sell:'#f59e0b',half:'#a855f7',peak:'#94a3b8'};
 d.marks.forEach(m=>{const px=X(m.t),py=Y(m.p*k);x.fillStyle=col[m.kind];x.beginPath();
  if(m.kind=='buy'){x.moveTo(px,py+2);x.lineTo(px-7,py+14);x.lineTo(px+7,py+14)}else if(m.kind=='sell'){x.moveTo(px,py-2);x.lineTo(px-7,py-14);x.lineTo(px+7,py-14)}
  else if(m.kind=='half'){x.moveTo(px,py-7);x.lineTo(px+7,py);x.lineTo(px,py+7);x.lineTo(px-7,py)}else x.arc(px,py,4,0,7);x.fill();
  x.fillStyle='#e6e8ee';x.fillText(m.label,Math.min(px+9,W-150),m.kind=='sell'?py-16:py+24)});
 G={X,Y,t0,t1,L,R,W,k,pts,yl,yh,T,H,B}}
$('cv').onmousemove=e=>{if(!G)return;const r=e.target.getBoundingClientRect(),mx=e.clientX-r.left,t=G.t0+(mx-G.L)/(G.W-G.L-G.R)*(G.t1-G.t0);
 let best=null,bd=1e99;G.pts.forEach(p=>{const dd=Math.abs((VIEW=='c'?p[0]+30:p[0])-t);if(dd<bd){bd=dd;best=p}});if(!best){return}
 const tip=$('tip');tip.style.display='block';tip.style.left=Math.min(mx+12,G.W-200)+'px';tip.style.top='8px';
 tip.innerHTML=VIEW=='c'?`${fmtT(best[0])}<br>O ${usd(best[1]*G.k)} H ${usd(best[2]*G.k)}<br>L ${usd(best[3]*G.k)} C ${usd(best[4]*G.k)}<br>vol ${best[5].toFixed(2)} SOL`
 :`${fmtT(best[0])} ${best[2]?'<span class=pos>buy</span>':'<span class=neg>sell</span>'} ${best[3].toFixed(3)} SOL<br>mc ${usd(best[1]*G.k)}<br><span class=mute>${esc(best[4].slice(0,6))}...</span>`};
$('cv').onmouseleave=()=>$('tip').style.display='none';
function kv(o){return '<div class="kv">'+Object.entries(o).filter(([a,b])=>b!==undefined&&b!==null&&b!=='').map(([a,b])=>`<div>${esc(a)}</div><div>${typeof b=='object'?esc(JSON.stringify(b)):esc(b)}</div>`).join('')+'</div>'}
function info(I){const ck=(I.checks||[]).map(c=>`<div class="card"><h3>check ${fmtT(c.t)} &middot; ${c.ok?'<span class=pos>passed</span>':'<span class=neg>blocked</span>'}</h3>${kv(Object.fromEntries(Object.entries(c).filter(([a])=>a!='t'&&a!='ok')))}</div>`).join('');
 $('info').innerHTML=`<div class="card"><h3>${esc(I.v)} &middot; ${esc(I.name)}</h3>${kv({result:I.exit?I.exit+' '+pct(I.ret):'open '+pct(I.ret),'entry mc':I.mc?usd(I.mc):null,
  'bought':fmtT(I.t)+' ('+Math.round(I.t-I.created)+' s after create)',held:(I.held/60).toFixed(1)+' min',peak:I.peak_x.toFixed(2)+'x',rules:I.rules,mint:I.m})}
  <a href="https://pump.fun/coin/${esc(I.m)}" target="_blank" rel="noopener">pump.fun</a>${I.link?' &middot; <a href="'+esc(I.link)+'" target="_blank" rel="noopener">X post</a>':''}</div>
  <div class="card"><h3>narrative score ${I.score??''} &middot; criterion ${I.crit??'-'}</h3>${kv({why:I.why,'improves':I.improves,'first coin':I.first,copy:I.copy,model:I.model,desc:(I.desc||'').slice(0,300)})}</div>
  <div class="card"><h3>exit data</h3>${kv({stop:I.stop,'conviction high':I.high,activity:I.act!=null?I.act+' / max '+I.act_max:null,traction:I.traction,'buyers/min at 2x':I.tp_buyers,'buy/sell flow':I.tp_flow,'half at':I.half_at,'post engagement entry':I.eng0,'at 2x':I.eng_tp})}</div>${ck}`}
load();setInterval(load,30000);
</script></body></html>"""
