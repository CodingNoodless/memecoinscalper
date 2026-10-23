"""M4's watchlist (dashboard /watch): the coins M4 is watching and what each still needs, with a live chart per coin
from the real-time feed (research.live_feed, data/feed/trades.sqlite)."""

from __future__ import annotations

import json
import sqlite3
import time

from .paths import DATA_DIR
from .research.live_feed import DB
from .ui_theme import THEME, nav


def watch_list() -> dict:
    try:
        st = json.loads((DATA_DIR / "m4" / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"t": None, "rows": [], "open": []}
    rows = sorted(st.get("watch", []), key=lambda r: (not r.get("active"), -(r.get("t0") or 0)))
    return {"t": st.get("t"), "sol_usd": st.get("sol_usd"), "rows": rows, "open": st.get("open", [])}


def live(m: str) -> dict:
    """Every feed trade of a coin: [t, spot SOL/token, is_buy, sol, wallet (short), holders]; holders are counted only
    when the feed saw the coin from its create."""
    try:
        db = sqlite3.connect(DB, timeout=5)
        c = db.execute("SELECT complete FROM coin WHERE m=?", (m,)).fetchone()
        rows = db.execute("SELECT t, w, b, sol, tok, vs, vt FROM tr WHERE m=? ORDER BY slot, seq", (m,)).fetchall()
        beat = db.execute("SELECT t FROM beat WHERE k='feed'").fetchone()
        db.close()
    except sqlite3.Error as exc:
        return {"error": f"feed unavailable: {exc}"}
    full = bool(c and c[0])
    bal, h, out = {}, 0, []
    for t, w, b, sol, tok, vs, vt in rows:
        b0 = bal.get(w, 0.0)
        b1 = b0 + (tok if b else -tok)
        bal[w] = b1
        h += (b0 <= 1 < b1) - (b1 <= 1 < b0)
        out.append([t, vs / vt if vt else None, bool(b), round(sol, 4), w[:6], h if full else None])
    return {"m": m, "complete": full, "trades": out, "feed_age": time.time() - beat[0] if beat else None}


WATCH_HTML = (r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>M4 Watchlist</title><style>""" + THEME + r"""
main{display:grid;grid-template-columns:minmax(440px,560px) 1fr;height:calc(100vh - 53px)}
#left{overflow:auto;border-right:1px solid var(--line)}
#left .top{padding:10px 14px;border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:center;flex-wrap:wrap}
tr.row{cursor:pointer}tr.row:hover td{background:#ffffff08}tr.sel td{background:#1c2440}
tr.off td{opacity:.45}
.need{font-size:12px;color:var(--mute)}.need.ok{color:var(--good)}.need.warn{color:var(--warn)}
#right{display:flex;flex-direction:column;min-width:0;overflow:auto;padding:12px;gap:12px}
#wrap{position:relative;height:56vh;min-height:340px}canvas{position:absolute;inset:0;width:100%;height:100%}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}
.stat{background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:8px 10px}
.stat>span{display:block;font-size:11px;color:var(--mute);text-transform:uppercase;letter-spacing:.05em}.stat b{font-size:17px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
@media (max-width:1000px){main{grid-template-columns:1fr;height:auto}#left{max-height:45vh}}
</style></head><body>""" + nav("watch", "M4 watchlist &middot; live") + r"""
<main><div id="left"><div class="top"><label class="small"><input type="checkbox" id="all"> show ruled-out coins</label><span id="sum" class="mute small"></span></div>
<table><thead><tr><th>coin</th><th>sc</th><th>followers</th><th>mc now</th><th>holders</th><th>waiting for</th></tr></thead><tbody id="rows"></tbody></table></div>
<div id="right"><div class="card"><div style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap"><b id="title" style="font-size:17px">Click a coin on the left</b><span id="sub" class="mute small"></span><a id="pf" target="_blank" rel="noopener" class="small"></a><a id="xl" target="_blank" rel="noopener" class="small"></a></div>
<div id="wrap" style="margin-top:8px"><canvas id="cv"></canvas></div>
<div class="mute small" style="margin-top:6px">Live from the trade feed, refreshed every 2 s. Market cap with every trade (green buy, red sell), SOL bought / sold, holders (yellow) and buyers per 20 s (green). Blue band = M4's buy band.</div></div>
<div class="card"><h2>Right now</h2><div class="stats" id="live"></div></div></div></main>
<script>
const $=id=>document.getElementById(id);let W=null,SEL=null,L=null;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const usd=x=>x==null?'&ndash;':x>=1e6?'$'+(x/1e6).toFixed(2)+'M':x>=1e3?'$'+(x/1e3).toFixed(1)+'k':'$'+x.toFixed(0);
const fol=x=>x==null?'&ndash;':x>=1e6?(x/1e6).toFixed(1)+'M':x>=1e3?(x/1e3).toFixed(1)+'k':x;
async function loadList(){W=await (await fetch('/api/watch')).json();const all=$('all').checked;
 const rows=W.rows.filter(r=>all||r.active);const act=W.rows.filter(r=>r.active).length;
 $('sum').textContent=`${act} active, ${W.rows.length-act} ruled out`+(W.open.length?` · ${W.open.length} open position`:'')+(W.t?` · bot updated ${Math.round(Date.now()/1000-W.t)} s ago`:'');
 $('rows').innerHTML=rows.map(r=>{const c=/band, checking/.test(r.need)?'ok':/holders|top|launch/.test(r.need)?'warn':'';
  return `<tr class="row${SEL==r.m?' sel':''}${r.active?'':' off'}" data-m="${r.m}"><td><b>${esc((r.name||'').slice(0,20))}</b><div class="mute small">${Math.round(Date.now()/1000-(r.t0||0))} s old</div></td><td>${r.score}</td><td>${fol(r.followers)}</td><td>${usd(r.mc)}</td><td>${r.holders??'&ndash;'}${r.h_max?' / '+r.h_max:''}</td><td><span class="need ${c}">${esc(r.need)}</span></td></tr>`}).join('')||'<tr><td colspan="6" class="mute">Nothing on the watchlist right now.</td></tr>'}
$('rows').onclick=e=>{const tr=e.target.closest('tr.row');if(!tr)return;SEL=tr.dataset.m;document.querySelectorAll('tr.sel').forEach(x=>x.classList.remove('sel'));tr.classList.add('sel');loadLive()};
$('all').onchange=loadList;
async function loadLive(){if(!SEL)return;const r=W.rows.find(x=>x.m==SEL)||{};L=await (await fetch('/api/live?m='+SEL)).json();
 $('title').textContent=r.name||SEL.slice(0,8);$('sub').textContent=`score ${r.score??''} · ${r.need||''}`+(L.complete?'':' · feed missed its launch: holders unknown');
 $('pf').href='https://pump.fun/coin/'+SEL;$('pf').textContent='pump.fun ↗';if(r.link){$('xl').href=r.link;$('xl').textContent='X post ↗'}else $('xl').textContent='';draw(r)}
function draw(r){const cv=$('cv'),Wd=cv.clientWidth,H=cv.clientHeight,dpr=devicePixelRatio||1;cv.width=Wd*dpr;cv.height=H*dpr;const x=cv.getContext('2d');x.scale(dpr,dpr);
 const tr=(L&&L.trades||[]).filter(p=>p[1]);if(!tr.length){x.fillStyle='#8b95a5';x.fillText('No trades from the feed yet.',20,30);$('live').innerHTML='';return}
 const k=1e9*(W.sol_usd||105),t0=tr[0][0]-1,t1=Math.max(Date.now()/1000,tr[tr.length-1][0])+2;
 let lo=1e99,hi=0;tr.forEach(p=>{lo=Math.min(lo,p[1]*k);hi=Math.max(hi,p[1]*k)});lo=Math.min(lo,3800);hi=Math.max(hi,7500);
 const Lm=60,R=12,T=10,P1=Math.round(H*0.6),P2=Math.round(H*0.8),B=20,X=t=>Lm+(t-t0)/(t1-t0)*(Wd-Lm-R),yl=Math.log(lo*0.92),yh=Math.log(hi*1.08),Y=v=>T+(1-(Math.log(v)-yl)/(yh-yl))*(P1-T);
 x.font='11px Segoe UI, system-ui';
 const top=(r.score||0)>=9?10000:7000;x.fillStyle='#7c9cff14';x.fillRect(Lm,Y(top),Wd-Lm-R,Y(4000)-Y(top));
 for(let i=0;i<=4;i++){const v=Math.exp(yl+(yh-yl)*i/4),y=Y(v);x.strokeStyle='#1f2630';x.beginPath();x.moveTo(Lm,y);x.lineTo(Wd-R,y);x.stroke();x.fillStyle='#8b95a5';x.fillText(usd(v),6,y+4)}
 for(let i=0;i<=4;i++){const t=t0+(t1-t0)*i/4;x.fillStyle='#8b95a5';x.fillText(Math.round(t-t0)+'s',X(t)-10,H-5)}
 x.strokeStyle='#cbd5e1';x.beginPath();tr.forEach((p,i)=>{const a=X(p[0]),b=Y(p[1]*k);i?x.lineTo(a,b):x.moveTo(a,b)});x.stroke();
 tr.slice(-600).forEach(p=>{x.fillStyle=p[2]?'#34d399':'#f87171';x.globalAlpha=.85;x.beginPath();x.arc(X(p[0]),Y(p[1]*k),Math.min(7,1.6+Math.sqrt(p[3])*1.7),0,7);x.fill()});x.globalAlpha=1;
 const nb=120,bw=(t1-t0)/nb,bu=new Array(nb).fill(0),se=new Array(nb).fill(0);tr.forEach(p=>{const i=Math.floor((p[0]-t0)/bw);if(i>=0&&i<nb){if(p[2])bu[i]+=p[3];else se[i]+=p[3]}});
 const vm=Math.max(.5,...bu,...se),mid=(P1+P2)/2+4,hh=(P2-P1)/2-6,pw=Math.max(1,(Wd-Lm-R)/nb-1);
 for(let i=0;i<nb;i++){const a=Lm+i*(Wd-Lm-R)/nb;if(bu[i]){x.fillStyle='#34d399';x.fillRect(a,mid-bu[i]/vm*hh,pw,bu[i]/vm*hh)}if(se[i]){x.fillStyle='#f87171';x.fillRect(a,mid,pw,se[i]/vm*hh)}}
 let hm=5;tr.forEach(p=>hm=Math.max(hm,p[5]||0));const Yh=v=>H-B-(v/hm)*(H-B-P2-6);
 x.strokeStyle='#fbbf24';x.lineWidth=1.5;x.beginPath();let s=false;tr.forEach(p=>{if(p[5]==null)return;const a=X(p[0]),b=Yh(p[5]);s?x.lineTo(a,b):x.moveTo(a,b);s=true});x.stroke();
 x.strokeStyle='#34d399aa';x.beginPath();let f=true;for(let t=t0;t<=t1;t+=Math.max(1,(t1-t0)/200)){const w=new Set();tr.forEach(p=>{if(p[2]&&p[0]>t-20&&p[0]<=t)w.add(p[4])});const b=Yh(Math.min(hm,w.size));f?x.moveTo(X(t),b):x.lineTo(X(t),b);f=false}x.stroke();x.lineWidth=1;
 x.textAlign='right';x.fillStyle='#fbbf24';x.fillText('holders (max '+hm+')',Wd-R-4,P2+12);x.fillStyle='#34d399';x.fillText('buyers / 20 s',Wd-R-4,P2+26);x.textAlign='left';
 const now=Date.now()/1000,last=tr[tr.length-1],h=last[5];let hmax=0;tr.forEach(p=>hmax=Math.max(hmax,p[5]||0));
 const B20=new Set(),S20=new Set();let flow=0;tr.forEach(p=>{if(p[0]>now-20)(p[2]?B20:S20).add(p[4]);if(p[0]>now-15)flow+=p[2]?p[3]:-p[3]});
 const st=(a,b,c)=>`<div class="stat"><span>${a}</span><b class="${c||''}">${b}</b></div>`;
 $('live').innerHTML=st('Market cap',usd(last[1]*k))+st('Age',Math.round(now-tr[0][0])+' s')+st('Holders',h==null?'&ndash;':`${h} <span class="mute small">/ high ${hmax}</span>`,h!=null&&h<0.9*hmax?'neg':'')+
  st('Buyers / sellers 20 s',`<span class="pos">${B20.size}</span> / <span class="neg">${S20.size}</span>`)+st('Net SOL flow 15 s',(flow>=0?'+':'')+flow.toFixed(2),flow>=0?'pos':'neg')+
  st('Last trade',Math.round(now-last[0])+' s ago')+st('Feed',L.feed_age!=null&&L.feed_age<5?'live':'down',L.feed_age!=null&&L.feed_age<5?'pos':'neg')}
{const h=/m=([A-Za-z0-9]+)/.exec(location.hash);if(h){SEL=h[1];$('all').checked=true}}
loadList().then(()=>SEL&&loadLive());setInterval(loadList,5000);setInterval(loadLive,2000);window.onresize=()=>L&&draw(W.rows.find(x=>x.m==SEL)||{});
</script></body></html>""")
