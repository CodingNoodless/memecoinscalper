"""Blind labeling (LB): Rohit judges coins at the moment the bot would buy them, the future hidden until he decides.

Served by the dashboard at /label. Coins come from the NC dataset (research/nc_data.py) in a fixed random order, so
no coin is picked by its outcome. Labels go to data/m1/labels.jsonl.
"""

from __future__ import annotations

import gzip
import json
import random
import time

from .paths import DATA_DIR
from .trade_view import jsonl

NC = DATA_DIR / "m1" / "nc.json"
LABELS = DATA_DIR / "m1" / "labels.jsonl"
SHOWN = ("age", "n_buyers", "n_sellers", "buy_sol", "sell_sol", "spike", "from_open", "slot0_buyers", "slot0_sol",
         "dev_buy", "dev_sold", "flow5", "flow10", "buyers10", "sellers10", "top1_share", "top3_share", "big_sells",
         "max_sell", "sold_out_frac")


def _nc() -> dict:
    try:
        return {r["m"]: r for r in json.loads(NC.read_text()) if r.get("r_m1") is not None}
    except (OSError, ValueError):
        return {}


def _early(m: str) -> list:
    with gzip.open(DATA_DIR / "m1" / "early_trades" / f"{m}.json.gz", "rt", encoding="utf-8") as fh:
        tr = json.load(fh)["trades"]
    return [[x[2], x[5] / x[6], x[4], x[5], x[3]] for x in tr if not x[7] and x[6] > 0]


def label_stats() -> dict:
    nc, rows = _nc(), jsonl(LABELS)
    out = {"n": len(rows), "left": len(nc) - len({r["m"] for r in rows})}
    for k, sel in (("buy", True), ("skip", False)):
        v = [nc[r["m"]]["r_m1"] for r in rows if r["buy"] == sel and r["m"] in nc]
        out[k] = {"n": len(v), "mean": sum(v) / len(v) if v else None, "wins": sum(x > 0 for x in v)}
    return out


def label_next() -> dict:
    nc = _nc()
    done = {r["m"] for r in jsonl(LABELS)}
    todo = sorted(m for m in nc if m not in done)
    if not todo:
        return {"done": True, "stats": label_stats()}
    m = random.Random(len(done) * 7919 + 11).choice(todo)
    r = nc[m]
    info = {k: r.get(k) for k in ("name", "sym", "desc", "link", "score", "crit", "why", "first", "after", "mc")}
    info.update(m=m, t0=r["t0"], te=r["te"], feats={k: round(r[k], 3) for k in SHOWN if r.get(k) is not None})
    return {"info": info, "trades": [x for x in _early(m) if x[0] <= r["te"]], "stats": label_stats()}


def label_save(body: dict) -> dict:
    nc = _nc()
    m = str(body.get("m", ""))
    if m not in nc:
        return {"error": "unknown coin"}
    if m not in {r["m"] for r in jsonl(LABELS)}:
        rec = {"t": time.time(), "m": m, "buy": bool(body.get("buy")), "narr": body.get("narr"),
               "note": str(body.get("note") or "")[:500]}
        with LABELS.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    r = nc[m]
    try:
        c = json.loads((DATA_DIR / "m1" / "pm_candles" / f"{m}.json").read_text())
    except (OSError, ValueError):
        c = []
    return {"outcome": {k: r.get(k) for k in ("r_m1", "r_m2", "r_tp3", "x10", "x30", "x60", "max10m", "max60m")},
            "after": [x for x in _early(m) if x[0] > r["te"]],
            "candles": [y for y in c if r["te"] - 60 <= y[0] <= r["te"] + 1800], "stats": label_stats()}


LABEL_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Blind Coin Labels</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--text:#e6e8ee;--mute:#8b93a7;--up:#22c55e;--down:#ef4444}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,Segoe UI,sans-serif}
header{padding:10px 16px;border-bottom:1px solid var(--line);display:flex;gap:14px;align-items:center;flex-wrap:wrap}
header h1{font-size:15px;margin:0}a{color:#93c5fd}.mute{color:var(--mute)}.pos{color:var(--up)}.neg{color:var(--down)}
main{max-width:1100px;margin:0 auto;padding:12px 16px;display:grid;gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px}
#wrap{position:relative;height:42vh;min-height:280px}canvas{position:absolute;inset:0;width:100%;height:100%}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:4px 14px;font-size:13px}.kv span{color:var(--mute)}
button{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:10px 18px;font:inherit;cursor:pointer}
button.buy{border-color:var(--up)}button.skip{border-color:var(--down)}button.sel{background:#2b3550}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
input{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:8px;font:inherit;flex:1;min-width:200px}
</style></head><body>
<header><h1>Blind coin labels</h1><span class="mute">Each coin is shown at the moment the bot would buy it. Decide, then see what happened. Keys: b buy, s skip, 1-5 narrative, n next.</span>
<span id="st" class="mute"></span><a href="/trades">trade charts</a></header>
<main><div class="card"><div id="title" style="font-size:16px"></div><div id="nar" class="mute" style="margin-top:4px"></div></div>
<div class="card"><div id="wrap"><canvas id="cv"></canvas></div><div class="mute" style="font-size:12px">Every curve trade since create (green buy, red sell, dot size = SOL). Blue dashed = entry. After you decide, the rest appears to the right of the entry line (ticks to 2 min, then 1-min candles to 30 min).</div></div>
<div class="card"><div id="feats" class="kv"></div></div>
<div class="card"><div class="row"><span>Narrative (1 weak - 5 strong):</span><span id="narr"></span></div>
<div class="row" style="margin-top:10px"><button class="buy" id="b">Buy</button><button class="skip" id="s">Skip</button>
<input id="note" placeholder="optional: why (rug chart, dev dumped, weak story...)"></div>
<div id="out" style="margin-top:10px"></div></div></main>
<script>
const $=id=>document.getElementById(id);let D=null,NARR=null,DONE=false;
const usd=x=>x>=1e6?'$'+(x/1e6).toFixed(2)+'M':x>=1e3?'$'+(x/1e3).toFixed(1)+'k':'$'+x.toFixed(0);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const pct=x=>x==null?'-':(x>=0?'+':'')+(100*x).toFixed(1)+'%';
$('narr').innerHTML=[1,2,3,4,5].map(i=>`<button data-n="${i}" style="padding:6px 12px">${i}</button>`).join(' ');
$('narr').onclick=e=>{const b=e.target.closest('button');if(!b)return;NARR=+b.dataset.n;document.querySelectorAll('#narr button').forEach(x=>x.classList.toggle('sel',x==b))};
function stats(s){const f=k=>s[k].n?`${s[k].n} (mean ${pct(s[k].mean)}, ${s[k].wins} wins)`:'0';$('st').textContent=`labeled ${s.n}, left ${s.left} | your buys ${f('buy')} | skips ${f('skip')}`}
async function next(){DONE=false;NARR=null;$('note').value='';$('out').innerHTML='';document.querySelectorAll('button.sel').forEach(x=>x.classList.remove('sel'));
 D=await (await fetch('/api/label/next')).json();stats(D.stats);if(D.done){$('title').textContent='All coins labeled.';return}
 const I=D.info;$('title').innerHTML=`<b>${esc(I.name)}</b> <span class="mute">$${esc(I.sym)}</span> &middot; entry mc ${usd(I.mc)} &middot; ${Math.round(I.te-I.t0)} s after create`;
 $('nar').innerHTML=`${I.link?'<a href="'+esc(I.link)+'" target="_blank" rel="noopener">linked post</a> &middot; ':''}${esc(I.desc||'')}<br>bot score ${I.score} (criterion ${I.crit}) ${I.after?'&middot; copy of an earlier coin':'&middot; first coin on this post'}: <i>${esc(I.why||'')}</i>`;
 $('feats').innerHTML=Object.entries(I.feats).map(([k,v])=>`<div><span>${esc(k)}</span> ${esc(v)}</div>`).join('');draw([],[])}
function draw(after,candles){const cv=$('cv'),W=cv.clientWidth,H=cv.clientHeight,dpr=devicePixelRatio||1;cv.width=W*dpr;cv.height=H*dpr;const x=cv.getContext('2d');x.scale(dpr,dpr);
 const I=D.info,k=1e9*105,pts=D.trades.concat(after),cs=candles.filter(c=>c[0]>=I.te);const t0=I.t0-2;let t1=I.te+3;
 pts.forEach(p=>t1=Math.max(t1,p[0]));if(cs.length)t1=Math.max(t1,cs[cs.length-1][0]+60);
 let lo=1e99,hi=0;pts.forEach(p=>{lo=Math.min(lo,p[1]);hi=Math.max(hi,p[1])});cs.forEach(c=>{lo=Math.min(lo,c[3]);hi=Math.max(hi,c[2])});
 lo*=k;hi*=k;const L=60,R=8,T=8,B=20,yl=Math.log(lo*0.95),yh=Math.log(hi*1.05),ww=W-L-R,rev=after.length||cs.length,f=rev?0.55:1;
 const X=t=>t<=I.te?L+(t-t0)/(I.te-t0||1)*ww*f:L+ww*f+(t-I.te)/(t1-I.te||1)*ww*(1-f);
 const Y=v=>T+(1-(Math.log(v)-yl)/(yh-yl||1))*(H-T-B);
 x.fillStyle='#8b93a7';x.font='11px system-ui';x.strokeStyle='#262b36';
 for(let i=0;i<=4;i++){const v=Math.exp(yl+(yh-yl)*i/4),y=Y(v);x.beginPath();x.moveTo(L,y);x.lineTo(W-R,y);x.stroke();x.fillText(usd(v),4,y+4)}
 x.fillText('create',L,H-5);x.fillText('entry +'+Math.round(I.te-I.t0)+'s',X(I.te)-30,H-5);if(rev)x.fillText('+'+Math.round((t1-I.te)/60)+' min',W-60,H-5);
 x.strokeStyle='#3b82f6';x.setLineDash([4,4]);x.beginPath();x.moveTo(L,Y(I.mc));x.lineTo(W-R,Y(I.mc));x.stroke();x.beginPath();x.moveTo(X(I.te),T);x.lineTo(X(I.te),H-B);x.stroke();x.setLineDash([]);
 cs.forEach(c=>{x.strokeStyle=x.fillStyle=c[4]>=c[1]?'#22c55e':'#ef4444';const cx=X(c[0]+30);x.beginPath();x.moveTo(cx,Y(c[2]*k));x.lineTo(cx,Y(c[3]*k));x.stroke();const a=Y(c[1]*k),b=Y(c[4]*k);x.fillRect(cx-2,Math.min(a,b),4,Math.max(1,Math.abs(a-b)))});
 x.strokeStyle='#cbd5e1';x.beginPath();pts.forEach((p,i)=>{const px=X(p[0]),py=Y(p[1]*k);i?x.lineTo(px,py):x.moveTo(px,py)});x.stroke();
 pts.forEach(p=>{x.fillStyle=p[2]?'#22c55e':'#ef4444';x.beginPath();x.arc(X(p[0]),Y(p[1]*k),Math.min(7,1.5+Math.sqrt(p[3])*1.6),0,7);x.fill()})}
async function decide(buy){if(DONE||!D||D.done)return;DONE=true;
 const r=await (await fetch('/api/label',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({m:D.info.m,buy,narr:NARR,note:$('note').value})})).json();
 if(r.error){$('out').textContent=r.error;return}stats(r.stats);const o=r.outcome;draw(r.after,r.candles);
 $('out').innerHTML=`You chose <b>${buy?'Buy':'Skip'}</b>. With M1's exits (2x / stop 0.7 / 10 min) this coin returned <b class="${o.r_m1>0?'pos':'neg'}">${pct(o.r_m1)}</b>. Price after 30 s ${o.x30?o.x30.toFixed(2)+'x':'-'}; best within 10 min ${o.max10m?o.max10m.toFixed(2)+'x':'-'}, within 60 min ${o.max60m?o.max60m.toFixed(2)+'x':'-'}. <button id="nx" style="margin-left:10px">Next coin</button>`;$('nx').onclick=next}
$('b').onclick=()=>decide(true);$('s').onclick=()=>decide(false);
document.onkeydown=e=>{if(e.target.tagName=='INPUT')return;if(e.key=='b')decide(true);if(e.key=='s')decide(false);if(e.key=='n'&&DONE)next();if('12345'.includes(e.key)&&e.key.length==1)document.querySelector(`#narr button[data-n="${e.key}"]`).click()};
window.onresize=()=>D&&!D.done&&draw([],[]);
next();
</script></body></html>"""
