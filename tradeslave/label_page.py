"""The blind-label page (dashboard "/label"); data from label_view (/api/label/next, POST /api/label)."""

from .ui_theme import THEME, nav

LABEL_HTML = (r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Blind Coin Labels</title><style>""" + THEME + r"""
main{max-width:1320px;margin:0 auto;padding:16px;display:grid;gap:14px}
.score{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
.lay{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:14px;align-items:start}
.coin .name{font-size:20px;font-weight:750}.coin .sym{color:var(--mute);font-weight:600;margin-left:6px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.chip{background:var(--card2);border:1px solid var(--line);border-radius:999px;padding:2px 10px;font-size:12px;color:var(--mute)}
.chip b{color:var(--ink)}
.why{margin-top:10px;font-size:13px;color:#c9d1dc;border-left:3px solid var(--acc);padding:6px 10px;background:#7c9cff0d;border-radius:0 8px 8px 0}
#wrap{position:relative;height:430px}canvas{position:absolute;inset:0;width:100%;height:100%}
.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--mute);font-size:12px;margin-top:8px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px;vertical-align:-1px}
.grp{margin-bottom:12px}.grp:last-child{margin-bottom:0}
.grp h3{margin:0 0 6px;font-size:11.5px;color:var(--mute);text-transform:uppercase;letter-spacing:.06em}
.row{display:flex;justify-content:space-between;gap:10px;padding:4px 0;border-bottom:1px dashed #252d3890;font-size:13px}
.row span{color:var(--mute)}.row b{font-weight:650;font-variant-numeric:tabular-nums}
.warn b{color:var(--warn)}.bad b{color:var(--bad)}.ok b{color:var(--good)}
.narr{display:flex;gap:6px}.narr button{flex:1;padding:8px 0}
.narr button.sel{background:#1c2440;border-color:var(--acc);color:var(--ink)}
.act{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}
.act button{padding:14px;font-size:16px;border-radius:10px}
#b{background:#05966922;border-color:#34d39977;color:#a7f3d0}#b:hover{background:#05966944}
#s{background:#dc262622;border-color:#f8717177;color:#fecaca}#s:hover{background:#dc262644}
#note{width:100%;margin-top:10px}
.out{margin-top:12px;padding:12px;border-radius:10px;background:var(--card2);border:1px solid var(--line);display:none}
.out .big{font-size:24px;font-weight:750}
.keys{color:var(--mute);font-size:12px;margin-top:10px}kbd{background:var(--card2);border:1px solid var(--line);border-bottom-width:2px;border-radius:5px;padding:0 5px;font-size:11px}
@media (max-width:980px){.lay{grid-template-columns:1fr}.score{grid-template-columns:repeat(2,1fr)}#wrap{height:340px}}
</style></head><body>""" + nav("label", "Decide like you would live; the future stays hidden until you click") + r"""
<main>
<section class="score" id="score"></section>
<section class="lay">
<div class="card"><div class="coin"><span class="name" id="name">Loading...</span><span class="sym" id="sym"></span></div>
<div class="chips" id="chips"></div><div class="why" id="why"></div>
<div id="wrap" style="margin-top:12px"><canvas id="cv"></canvas></div>
<div class="legend"><span><i class="dot" style="background:#34d399"></i>buy</span><span><i class="dot" style="background:#f87171"></i>sell (dot size = SOL)</span>
<span><i class="dot" style="background:#7c9cff"></i>bot entry</span><span>after you decide: the next 2 min trade by trade, then 1-min candles to 30 min</span></div></div>
<div style="display:grid;gap:14px">
<div class="card"><h2>What the bot saw at entry</h2><div id="feats"></div></div>
<div class="card"><h2>Your call</h2>
<div class="mute small" style="margin-bottom:6px">Narrative strength</div><div class="narr" id="narr"></div>
<div class="act"><button id="b">Buy</button><button id="s">Skip</button></div>
<input id="note" placeholder="Why? e.g. rug chart, dev dumped, weak story">
<div class="out" id="out"></div>
<div class="keys"><kbd>B</kbd> buy &nbsp;<kbd>S</kbd> skip &nbsp;<kbd>1</kbd>&ndash;<kbd>5</kbd> narrative &nbsp;<kbd>N</kbd> next coin</div></div>
</div></section></main>
<script>
const $=id=>document.getElementById(id);let D=null,NARR=null,DONE=false;
const usd=x=>x>=1e6?'$'+(x/1e6).toFixed(2)+'M':x>=1e3?'$'+(x/1e3).toFixed(1)+'k':'$'+x.toFixed(0);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const pct=x=>x==null?'-':(x>=0?'+':'')+(100*x).toFixed(1)+'%';
const F={activity:[['age','Seconds since launch',v=>v.toFixed(0)+' s'],['n_buyers','Unique buyers',v=>v],['n_sellers','Unique sellers',v=>v],
 ['buy_sol','SOL bought / sold',(v,f)=>v.toFixed(1)+' / '+(f.sell_sol||0).toFixed(1)],['spike','High so far vs entry',v=>v.toFixed(2)+'x',v=>v>1.25?'warn':''],
 ['from_open','Entry vs launch price',v=>v.toFixed(2)+'x',v=>v>2?'warn':'']],
 'last seconds':[['flow5','Net SOL flow, last 5 s',v=>(v>=0?'+':'')+v.toFixed(2),v=>v<0?'bad':'ok'],['flow10','Net SOL flow, last 10 s',v=>(v>=0?'+':'')+v.toFixed(2),v=>v<0?'bad':'ok'],
 ['buyers10','Buyers / sellers, last 10 s',(v,f)=>v+' / '+(f.sellers10??0)],['max_sell','Biggest sell so far',v=>v.toFixed(2)+' SOL',v=>v>=1.5?'warn':''],['big_sells','Sells of 0.5 SOL or more',v=>v]],
 'who holds it':[['dev_buy','Dev buy',v=>v.toFixed(2)+' SOL'],['dev_sold','Dev sold (share of their buy)',v=>(100*v).toFixed(0)+'%',v=>v>0.5?'bad':v>0?'warn':''],
 ['slot0_buyers','Wallets in the launch block',(v,f)=>v+' ('+(f.slot0_sol||0).toFixed(1)+' SOL)',v=>v>=6?'warn':''],['top1_share','Biggest buyer / top 3 (of buy SOL)',(v,f)=>(100*v).toFixed(0)+'% / '+(100*(f.top3_share||0)).toFixed(0)+'%',v=>v>0.35?'warn':''],
 ['sold_out_frac','Buyers already fully out',v=>(100*v).toFixed(0)+'%',v=>v>0.3?'warn':'']]};
$('narr').innerHTML=[1,2,3,4,5].map(i=>`<button data-n="${i}">${i}</button>`).join('');
$('narr').onclick=e=>{const b=e.target.closest('button');if(!b)return;NARR=+b.dataset.n;document.querySelectorAll('#narr button').forEach(x=>x.classList.toggle('sel',x==b))};
function score(s){const c=(k,lab)=>{const z=s[k];return `<div class="kpi"><span>${lab}</span><b class="${z.mean>0?'pos':z.mean<0?'neg':''}">${z.n?pct(z.mean):'&ndash;'}</b><div class="mute small">${z.n} coins, ${z.wins} wins</div></div>`};
 $('score').innerHTML=`<div class="kpi"><span>Labeled</span><b>${s.n}</b><div class="mute small">${s.left} left &middot; verdict at 100</div></div>${c('buy','Your buys (mean, M1 exits)')}${c('skip','Your skips (mean)')}`+
 `<div class="kpi"><span>Edge (buys &minus; skips)</span><b>${s.buy.n&&s.skip.n?pct(s.buy.mean-s.skip.mean):'&ndash;'}</b><div class="mute small">pass: buys &gt; 0, t &ge; 2, edge &ge; 15 pts</div></div>`}
async function next(){DONE=false;NARR=null;$('note').value='';$('out').style.display='none';document.querySelectorAll('#narr button').forEach(x=>x.classList.remove('sel'));
 D=await (await fetch('/api/label/next')).json();score(D.stats);if(D.done){$('name').textContent='All coins labeled.';return}
 const I=D.info;$('name').textContent=I.name;$('sym').textContent='$'+(I.sym||'');
 $('chips').innerHTML=[`entry <b>${usd(I.mc)}</b>`,`<b>${Math.round(I.te-I.t0)} s</b> after launch`,`bot score <b>${I.score}</b>`,`criterion <b>${I.crit??'-'}</b>`,I.after?'<b>copy</b> of an earlier coin':'<b>first</b> coin on this post',
  I.link?`<a href="${esc(I.link)}" target="_blank" rel="noopener">open linked post &#8599;</a>`:'no link'].map(x=>`<span class="chip">${x}</span>`).join('');
 $('why').innerHTML=esc(I.why||'')+(I.desc?`<div class="mute small" style="margin-top:4px">${esc(I.desc)}</div>`:'');
 const f=I.feats;$('feats').innerHTML=Object.entries(F).map(([g,rows])=>`<div class="grp"><h3>${g}</h3>${rows.filter(([k])=>f[k]!=null).map(([k,lab,fmt,cls])=>`<div class="row ${cls?cls(f[k]):''}"><span>${lab}</span><b>${fmt(f[k],f)}</b></div>`).join('')}</div>`).join('');
 draw([],[])}
function draw(after,candles){const cv=$('cv'),W=cv.clientWidth,H=cv.clientHeight,dpr=devicePixelRatio||1;cv.width=W*dpr;cv.height=H*dpr;const x=cv.getContext('2d');x.scale(dpr,dpr);
 const I=D.info,k=1e9*105,pts=D.trades.concat(after),cs=candles.filter(c=>c[0]>=I.te);const t0=I.t0-2;let t1=I.te+3;
 pts.forEach(p=>t1=Math.max(t1,p[0]));if(cs.length)t1=Math.max(t1,cs[cs.length-1][0]+60);
 let lo=1e99,hi=0;pts.forEach(p=>{lo=Math.min(lo,p[1]);hi=Math.max(hi,p[1])});cs.forEach(c=>{lo=Math.min(lo,c[3]);hi=Math.max(hi,c[2])});
 lo=Math.min(lo*k,I.mc);hi=Math.max(hi*k,I.mc);const L=58,R=10,T=12,B=22,yl=Math.log(lo*0.93),yh=Math.log(hi*1.07),ww=W-L-R,rev=after.length||cs.length,f=rev?0.5:1;
 const X=t=>t<=I.te?L+(t-t0)/(I.te-t0||1)*ww*f:L+ww*f+(t-I.te)/(t1-I.te||1)*ww*(1-f);
 const Y=v=>T+(1-(Math.log(v)-yl)/(yh-yl||1))*(H-T-B);
 x.font='11px Segoe UI, system-ui';x.lineWidth=1;
 for(let i=0;i<=4;i++){const v=Math.exp(yl+(yh-yl)*i/4),y=Y(v);x.strokeStyle='#1f2630';x.beginPath();x.moveTo(L,y);x.lineTo(W-R,y);x.stroke();x.fillStyle='#8b95a5';x.fillText(usd(v),6,y+4)}
 if(rev){x.fillStyle='#7c9cff0d';x.fillRect(X(I.te),T,W-R-X(I.te),H-T-B)}
 x.fillStyle='#8b95a5';x.fillText('launch',L,H-6);x.fillText('entry +'+Math.round(I.te-I.t0)+'s',Math.max(L+40,X(I.te)-60),H-6);if(rev)x.fillText('+'+Math.round((t1-I.te)/60)+' min',W-60,H-6);
 [[2,'2x'],[1.5,'1.5x'],[0.7,'stop 0.7']].forEach(([m,l])=>{if(I.mc*m>lo*0.93&&I.mc*m<hi*1.07){x.strokeStyle=m>1?'#34d39940':'#f8717140';x.setLineDash([3,4]);x.beginPath();x.moveTo(X(I.te),Y(I.mc*m));x.lineTo(W-R,Y(I.mc*m));x.stroke();x.setLineDash([]);x.fillStyle=m>1?'#34d399aa':'#f87171aa';x.fillText(l,W-R-44,Y(I.mc*m)-3)}});
 x.strokeStyle='#7c9cff';x.setLineDash([5,4]);x.beginPath();x.moveTo(L,Y(I.mc));x.lineTo(W-R,Y(I.mc));x.stroke();x.beginPath();x.moveTo(X(I.te),T);x.lineTo(X(I.te),H-B);x.stroke();x.setLineDash([]);
 cs.forEach(c=>{const up=c[4]>=c[1];x.strokeStyle=x.fillStyle=up?'#34d399':'#f87171';const cx=X(c[0]+30);x.beginPath();x.moveTo(cx,Y(c[2]*k));x.lineTo(cx,Y(c[3]*k));x.stroke();const a=Y(c[1]*k),b=Y(c[4]*k);x.fillRect(cx-2.5,Math.min(a,b),5,Math.max(1,Math.abs(a-b)))});
 x.strokeStyle='#cbd5e1aa';x.lineWidth=1.2;x.beginPath();pts.forEach((p,i)=>{const px=X(p[0]),py=Y(p[1]*k);i?x.lineTo(px,py):x.moveTo(px,py)});x.stroke();
 pts.forEach(p=>{x.fillStyle=p[2]?'#34d399':'#f87171';x.globalAlpha=.85;x.beginPath();x.arc(X(p[0]),Y(p[1]*k),Math.min(8,1.8+Math.sqrt(p[3])*1.8),0,7);x.fill()});x.globalAlpha=1;
 x.fillStyle='#7c9cff';x.beginPath();x.moveTo(X(I.te),Y(I.mc)+3);x.lineTo(X(I.te)-7,Y(I.mc)+15);x.lineTo(X(I.te)+7,Y(I.mc)+15);x.fill()}
async function decide(buy){if(DONE||!D||D.done)return;DONE=true;
 const r=await (await fetch('/api/label',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({m:D.info.m,buy,narr:NARR,note:$('note').value})})).json();
 if(r.error){$('out').style.display='block';$('out').textContent=r.error;return}score(r.stats);const o=r.outcome;draw(r.after,r.candles);
 const good=o.r_m1>0,right=(buy&&good)||(!buy&&!good);
 $('out').style.display='block';$('out').innerHTML=`<div class="mute small">You chose <b style="color:var(--ink)">${buy?'Buy':'Skip'}</b> &middot; ${right?'<span class="pos">good call</span>':'<span class="neg">would have been the other way</span>'}</div>`+
 `<div class="big ${good?'pos':'neg'}">${pct(o.r_m1)}</div><div class="mute small">with M1's exits (2x / stop 0.7 / 10 min)</div>`+
 `<div class="chips"><span class="chip">after 30 s <b>${o.x30?o.x30.toFixed(2)+'x':'-'}</b></span><span class="chip">best in 10 min <b>${o.max10m?o.max10m.toFixed(2)+'x':'-'}</b></span><span class="chip">best in 60 min <b>${o.max60m?o.max60m.toFixed(2)+'x':'-'}</b></span></div>`+
 `<button id="nx" style="margin-top:10px;width:100%">Next coin &rarr;</button>`;$('nx').onclick=next}
$('b').onclick=()=>decide(true);$('s').onclick=()=>decide(false);
document.onkeydown=e=>{if(e.target.tagName=='INPUT')return;const k=e.key.toLowerCase();if(k=='b')decide(true);if(k=='s')decide(false);if(k=='n'&&DONE)next();
 if('12345'.includes(k)&&k.length==1)document.querySelector(`#narr button[data-n="${k}"]`).click()};
window.onresize=()=>D&&!D.done&&!DONE&&draw([],[]);
next();
</script></body></html>""")
