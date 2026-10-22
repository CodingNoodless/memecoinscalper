"""The paper-trading page (dashboard "/label"): Rohit trades replayed coins; data from paper_view (/api/paper)."""

from .ui_theme import THEME, nav

LABEL_HTML = (r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paper Trade Replays</title><style>""" + THEME + r"""
main{max-width:1440px;margin:0 auto;padding:14px 16px;display:grid;gap:12px}
.score{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px}
.lay{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:12px;align-items:start}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.clock{font-variant-numeric:tabular-nums;color:var(--mute);margin-left:auto}
#wrap{position:relative;height:500px}canvas{position:absolute;inset:0;width:100%;height:100%}
.side{display:grid;gap:12px}
.coin .name{font-size:19px;font-weight:750;line-height:1.2}.coin .sym{color:var(--mute);font-weight:600;font-size:13px}
.badges{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}
.badge{border-radius:999px;padding:2px 10px;font-size:12px;font-weight:650;background:var(--card2);border:1px solid var(--line)}
.badge.hi{color:#a7f3d0;border-color:#34d39966}.badge.mid{color:#fde68a;border-color:#fbbf2466}
.why{font-size:13px;color:#c9d1dc;line-height:1.45}.desc{font-size:12px;color:var(--mute);margin-top:6px;max-height:54px;overflow:hidden}
.post{display:inline-block;margin-top:8px;font-size:13px;font-weight:600}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.stat{background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:8px 10px}
.stat>span{display:block;font-size:11px;color:var(--mute);text-transform:uppercase;letter-spacing:.05em}
.stat b span{font-size:inherit}.stat b .mute{font-size:12px;font-weight:600}
.stat b{font-size:17px;font-variant-numeric:tabular-nums}
.act{display:grid;gap:8px}
.big{padding:16px;font-size:18px;border-radius:12px;font-weight:750;letter-spacing:.02em}
#buy{background:#05966933;border-color:#34d39999;color:#a7f3d0}#buy:hover{background:#05966955}
#sell{background:#b4530933;border-color:#f59e0b99;color:#fde68a;display:none}#sell:hover{background:#b4530955}
#skip{background:transparent;color:var(--mute)}
.pos-box{display:none;background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:10px;text-align:center}
.pos-box .pnl{font-size:28px;font-weight:800;font-variant-numeric:tabular-nums}
.narr{display:flex;gap:6px}.narr button{flex:1;padding:6px 0}.narr button.sel{background:#1c2440;border-color:var(--acc)}
#note{width:100%}
#result{display:none}
.vs{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:8px 0}
.keys{color:var(--mute);font-size:12px}kbd{background:var(--card2);border:1px solid var(--line);border-bottom-width:2px;border-radius:5px;padding:0 5px;font-size:11px}
@media (max-width:1100px){.lay{grid-template-columns:1fr}.score{grid-template-columns:repeat(3,1fr)}#wrap{height:380px}}
</style></head><body>""" + nav("label", "Trade it like it's live: the future stays hidden") + r"""
<main>
<section class="score" id="score"></section>
<section class="lay">
<div class="card"><div class="bar"><button id="play">&#9654; Play</button>
<select id="spd"><option value="1">1x (live)</option><option value="2" selected>2x</option><option value="5">5x</option><option value="10">10x</option><option value="30">30x</option></select>
<span class="mute small">a quiet stretch? speed up, or pause to think</span><span class="clock" id="clock"></span></div>
<div id="wrap"><canvas id="cv"></canvas></div>
<div class="mute small" style="margin-top:6px">Market cap line with every trade (green buy, red sell, dot size = SOL). Middle: SOL bought / sold. Bottom: holders (yellow) and buyers in the last 20 s (green).</div></div>
<div class="side">
<div class="card coin"><div class="name" id="name">Loading...</div><div class="sym" id="sym"></div><div class="badges" id="badges"></div>
<div class="why" id="why"></div><div class="desc" id="desc"></div><a class="post" id="post" target="_blank" rel="noopener"></a></div>
<div class="card"><h2>Right now</h2><div class="stats" id="live"></div></div>
<div class="card act"><div class="pos-box" id="posbox"><div class="mute small">your position (0.1 SOL, after fees)</div><div class="pnl" id="pnl">+0.0%</div><div class="mute small" id="posinfo"></div></div>
<button class="big" id="buy">BUY 0.1 SOL</button><button class="big" id="sell">SELL</button><button id="skip">Skip this coin</button>
<div class="mute small">Narrative (1 weak &ndash; 5 strong)</div><div class="narr" id="narr"></div>
<input id="note" placeholder="Why? (optional)">
<div class="keys"><kbd>Space</kbd> play/pause &nbsp;<kbd>B</kbd> buy &nbsp;<kbd>S</kbd> sell &nbsp;<kbd>K</kbd> skip &nbsp;<kbd>N</kbd> next</div></div>
<div class="card" id="result"></div>
</div></section></main>
<script>
const $=id=>document.getElementById(id);let D=null,S=null,CUR=0,PLAY=false,LAST=0,BUY=null,SELL=null,DONE=false,NARR=null;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const usd=x=>x>=1e6?'$'+(x/1e6).toFixed(2)+'M':x>=1e3?'$'+(x/1e3).toFixed(1)+'k':'$'+x.toFixed(0);
const pct=x=>x==null||isNaN(x)?'&ndash;':(x>=0?'+':'')+(100*x).toFixed(1)+'%';
const cls=x=>x==null||x==0?'':x>0?'pos':'neg';
const K=30*1073000000,FEE=0.0125;
function estRet(pb,ps){const v=Math.sqrt(pb*K),q=K/v-K/(v+0.1*(1-FEE));const v2=Math.sqrt(ps*K),t=K/v2;return ((K/Math.max(t-q,1)-v2)*(1-FEE)-0.001)/0.1-1}
$('narr').innerHTML=[1,2,3,4,5].map(i=>`<button data-n="${i}">${i}</button>`).join('');
$('narr').onclick=e=>{const b=e.target.closest('button');if(!b)return;NARR=+b.dataset.n;document.querySelectorAll('#narr button').forEach(x=>x.classList.toggle('sel',x==b))};
function score(s){const k=(lab,val,sub,c)=>`<div class="kpi"><span>${lab}</span><b class="${c||''}">${val}</b><div class="mute small">${sub}</div></div>`;
 $('score').innerHTML=k('Your trades',s.trades,`${s.skips} skipped &middot; ${s.left} coins left`)+k('Your mean / trade',pct(s.mean),s.t!=null?'t '+s.t.toFixed(2):'need 3+ trades',cls(s.mean))+
 k('Your wins',s.trades?s.wins+' / '+s.trades:'&ndash;',s.trades?Math.round(100*s.wins/s.trades)+'% win rate':'')+k('Your P&amp;L',(s.sol>=0?'+':'')+s.sol.toFixed(3)+' SOL',s.ex2!=null?'without best 2: '+(s.ex2>=0?'+':'')+s.ex2.toFixed(3):'',cls(s.sol))+
 k('Bots on your coins',pct(s.bot_same),'M1 exits at the bots&rsquo; entry',cls(s.bot_same))+k('Bots on every coin seen',pct(s.bot_all),'incl. the ones you skipped',cls(s.bot_all))}
function idx(t){const tr=D.trades;let lo=0,hi=tr.length;while(lo<hi){const m=(lo+hi)>>1;if(tr[m][0]<=t)lo=m+1;else hi=m}return lo}
function spot(t){const n=idx(t);return n?D.trades[n-1][1]:D.trades[0][1]}
async function next(){stop();BUY=SELL=null;DONE=false;NARR=null;$('note').value='';$('result').style.display='none';
 document.querySelectorAll('#narr button').forEach(x=>x.classList.remove('sel'));$('buy').style.display='block';$('sell').style.display='none';$('posbox').style.display='none';$('skip').style.display='block';
 D=await (await fetch('/api/paper/next')).json();score(D.stats);if(D.done){$('name').textContent='Every coin traded or skipped.';return}
 const I=D.info;S={t0:I.t0-1,t1:D.trades[D.trades.length-1][0]+1,k:1e9*I.sol_usd};CUR=S.t0;
 $('name').textContent=I.name||'';$('sym').textContent='$'+(I.sym||'');
 const sc=I.score>=9?'hi':I.score>=8?'mid':'';
 $('badges').innerHTML=`<span class="badge ${sc}">narrative ${I.score}/10</span><span class="badge">${I.after?'copy of an earlier coin':'first coin on this post'}</span>`+(I.crit?`<span class="badge">${({1:'influential poster',2:'potential',3:'real cause',4:'correction'})[I.crit]||'criterion '+I.crit}</span>`:'');
 $('why').textContent=I.why||'';$('desc').textContent=I.desc||'';
 if(I.link){$('post').style.display='';$('post').href=I.link;$('post').textContent='Open the linked X post ↗'}else $('post').style.display='none';
 const at=/t=(\d+)/.exec(location.hash);if(at){CUR=Math.min(S.t1,I.t0+(+at[1]));render();if(/buy/.test(location.hash))buy();render();return}
 render();play()}
function render(){if(!D||D.done)return;const cv=$('cv'),W=cv.clientWidth,H=cv.clientHeight,dpr=devicePixelRatio||1;cv.width=W*dpr;cv.height=H*dpr;
 const x=cv.getContext('2d');x.scale(dpr,dpr);const tr=D.trades,k=S.k,n=idx(CUR),reveal=DONE;const upto=reveal?tr.length:n;
 const tEnd=reveal?S.t1:Math.max(CUR+20,S.t0+90);const tStart=S.t0;
 let lo=1e99,hi=0;for(let i=0;i<upto;i++){lo=Math.min(lo,tr[i][1]);hi=Math.max(hi,tr[i][1])}if(!upto){lo=hi=tr[0][1]}lo*=k;hi*=k;
 const L=60,R=12,T=10,P1=Math.round(H*0.62),P2=Math.round(H*0.80),B=20;
 const X=t=>L+(t-tStart)/(tEnd-tStart||1)*(W-L-R),yl=Math.log(lo*0.92),yh=Math.log(hi*1.08),Y=v=>T+(1-(Math.log(v)-yl)/(yh-yl||1))*(P1-T);
 x.font='11px Segoe UI, system-ui';x.lineWidth=1;
 for(let i=0;i<=4;i++){const v=Math.exp(yl+(yh-yl)*i/4),y=Y(v);x.strokeStyle='#1f2630';x.beginPath();x.moveTo(L,y);x.lineTo(W-R,y);x.stroke();x.fillStyle='#8b95a5';x.fillText(usd(v),6,y+4)}
 for(let i=0;i<=4;i++){const t=tStart+(tEnd-tStart)*i/4;x.fillStyle='#8b95a5';x.fillText(Math.round(t-D.info.t0)+'s',X(t)-10,H-5)}
 if(BUY){const pb=BUY.p*k;x.setLineDash([5,4]);x.strokeStyle='#34d39988';x.beginPath();x.moveTo(L,Y(pb));x.lineTo(W-R,Y(pb));x.stroke();x.setLineDash([]);
  x.fillStyle='#34d39914';x.fillRect(X(BUY.t),T,Math.max(1,X(SELL?SELL.t:CUR)-X(BUY.t)),P1-T)}
 x.strokeStyle='#cbd5e1';x.beginPath();for(let i=0;i<upto;i++){const px=X(tr[i][0]),py=Y(tr[i][1]*k);i?x.lineTo(px,py):x.moveTo(px,py)}x.stroke();
 for(let i=Math.max(0,upto-600);i<upto;i++){const p=tr[i];x.fillStyle=p[2]?'#34d399':'#f87171';x.globalAlpha=.85;x.beginPath();x.arc(X(p[0]),Y(p[1]*k),Math.min(7,1.6+Math.sqrt(p[3])*1.7),0,7);x.fill()}x.globalAlpha=1;
 const nb=120,bw=(tEnd-tStart)/nb,bu=new Array(nb).fill(0),se=new Array(nb).fill(0);
 for(let i=0;i<upto;i++){const b=Math.floor((tr[i][0]-tStart)/bw);if(b>=0&&b<nb){if(tr[i][2])bu[b]+=tr[i][3];else se[b]+=tr[i][3]}}
 const vm=Math.max(.5,...bu,...se),mid=(P1+P2)/2+4,hh=(P2-P1)/2-6,pw=Math.max(1,(W-L-R)/nb-1);
 x.strokeStyle='#252d38';x.beginPath();x.moveTo(L,mid);x.lineTo(W-R,mid);x.stroke();
 for(let b=0;b<nb;b++){const px=L+b*(W-L-R)/nb;if(bu[b]){x.fillStyle='#34d399';x.fillRect(px,mid-bu[b]/vm*hh,pw,bu[b]/vm*hh)}if(se[b]){x.fillStyle='#f87171';x.fillRect(px,mid,pw,se[b]/vm*hh)}}
 let hmx=5;for(let i=0;i<upto;i++)hmx=Math.max(hmx,tr[i][5]||0);const Yh=v=>H-B-(v/hmx)*(H-B-P2-6);
 x.strokeStyle='#fbbf24';x.lineWidth=1.5;x.beginPath();let st=false;for(let i=0;i<upto;i++){if(tr[i][5]==null)continue;const px=X(tr[i][0]),py=Yh(tr[i][5]);st?x.lineTo(px,py):x.moveTo(px,py);st=true}x.stroke();
 const step=Math.max(1,(tEnd-tStart)/200);x.strokeStyle='#34d399aa';x.beginPath();let first=true;
 for(let t=tStart;t<=(reveal?S.t1:CUR);t+=step){const a=idx(t-20),z=idx(t),w=new Set();for(let i=a;i<z;i++)if(tr[i][2])w.add(tr[i][4]);const py=Yh(Math.min(hmx,w.size));first?x.moveTo(X(t),py):x.lineTo(X(t),py);first=false}x.stroke();x.lineWidth=1;
 x.textAlign='right';x.fillStyle='#fbbf24';x.fillText('holders (max '+hmx+')',W-R-4,P2+12);x.fillStyle='#34d399';x.fillText('buyers / 20 s',W-R-4,P2+26);x.textAlign='left';
 const mark=(t,p,c,lab,up)=>{const px=X(t),py=Y(p*k);x.fillStyle=c;x.beginPath();if(up){x.moveTo(px,py+3);x.lineTo(px-8,py+17);x.lineTo(px+8,py+17)}else{x.moveTo(px,py-3);x.lineTo(px-8,py-17);x.lineTo(px+8,py-17)}x.fill();x.fillStyle='#e7ebf1';x.fillText(lab,Math.min(px+10,W-120),up?py+30:py-20)};
 if(BUY)mark(BUY.t,BUY.p,'#34d399','YOU BUY',true);if(SELL)mark(SELL.t,SELL.p,'#f59e0b','YOU SELL',false);
 if(reveal&&D.bot){mark(D.bot.te,spot(D.bot.te),'#7c9cff','BOT BUY',true);(D.bot.trades||[]).forEach(b=>{mark(b.t_exit,spot(b.t_exit),'#a78bfa',b.v.toUpperCase()+' SELL '+pct(b.ret).replace(/&ndash;/,'-'),false)})}
 if(!reveal){x.strokeStyle='#e7ebf155';x.beginPath();x.moveTo(X(CUR),T);x.lineTo(X(CUR),H-B);x.stroke()}
 live(n)}
function live(n){const tr=D.trades,k=S.k,p=spot(CUR),h=n?tr[n-1][5]:0;let hm=0;for(let i=0;i<n;i++)hm=Math.max(hm,tr[i][5]||0);
 const a=idx(CUR-20),h10=idx(CUR-10);const B1=new Set(),S1=new Set();let flow=0,vol=0;for(let i=a;i<n;i++){(tr[i][2]?B1:S1).add(tr[i][4]);if(tr[i][0]>CUR-15)flow+=tr[i][2]?tr[i][3]:-tr[i][3]}
 for(let i=0;i<n;i++)vol+=tr[i][3];const hPrev=h10?tr[h10-1][5]:0,dh=(h??0)-(hPrev??0);
 const st=(lab,val,c)=>`<div class="stat"><span>${lab}</span><b class="${c||''}">${val}</b></div>`;
 $('live').innerHTML=st('Market cap',usd(p*k))+st('Since launch',Math.round(CUR-D.info.t0)+' s')+st('Holders',`${h??'&ndash;'} <span class="mute small">/ high ${hm}</span>`)+
  st('Holders, last 10 s',(dh>=0?'+':'')+dh,dh>0?'pos':dh<0?'neg':'')+st('Buyers / sellers 20 s',`<span class="pos">${B1.size}</span> / <span class="neg">${S1.size}</span>`)+
  st('Net SOL flow 15 s',(flow>=0?'+':'')+flow.toFixed(2),flow>=0?'pos':'neg')+st('Volume so far',(vol*D.info.sol_usd/1000).toFixed(1)+'k $')+st('Trades so far',n);
 $('clock').textContent=`${Math.round(CUR-D.info.t0)} s after launch`;
 if(BUY&&!SELL){const r=estRet(BUY.p,p);$('pnl').innerHTML=pct(r);$('pnl').className='pnl '+cls(r);$('posinfo').textContent=`bought at ${usd(BUY.p*k)} · held ${Math.round(CUR-BUY.t)} s`}}
function tick(ts){if(!PLAY)return;const dt=LAST?(ts-LAST)/1000:0;LAST=ts;CUR=Math.min(S.t1,CUR+dt*+$('spd').value);render();
 if(CUR>=S.t1){stop();if(BUY&&!SELL)sell();else if(!BUY&&!DONE)skip();return}requestAnimationFrame(tick)}
function play(){if(!D||D.done||DONE)return;PLAY=true;LAST=0;$('play').innerHTML='&#10074;&#10074; Pause';requestAnimationFrame(tick)}
function stop(){PLAY=false;LAST=0;$('play').innerHTML='&#9654; Play'}
function buy(){if(!D||DONE||BUY)return;BUY={t:CUR,p:spot(CUR+1)};$('buy').style.display='none';$('sell').style.display='block';$('skip').style.display='none';$('posbox').style.display='block';render()}
function sell(){if(!BUY||SELL)return;SELL={t:CUR,p:spot(CUR+1)};finish()}
function skip(){if(DONE||BUY)return;finish()}
async function finish(){DONE=true;stop();$('sell').style.display='none';$('buy').style.display='none';$('skip').style.display='none';
 const r=await (await fetch('/api/paper',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({m:D.info.m,t_buy:BUY?BUY.t:null,t_sell:SELL?SELL.t:null,narr:NARR,note:$('note').value})})).json();
 if(r.error){$('result').style.display='block';$('result').textContent=r.error;return}
 D.bot=r.bot;score(r.stats);render();const me=r.rec,b=r.bot;
 const bots=(b.trades||[]).map(t=>`<div class="stat"><span>${t.v.toUpperCase()} (live)</span><b class="${cls(t.ret)}">${pct(t.ret)}</b></div>`).join('');
 $('result').style.display='block';$('result').innerHTML=`<h2>Result</h2><div class="vs"><div class="stat"><span>You</span><b class="${cls(me.ret)}">${me.action=='trade'?pct(me.ret):'skipped'}</b></div>`+
 `<div class="stat"><span>Bot rule (M1 exits)</span><b class="${cls(b.r_m1)}">${pct(b.r_m1)}</b></div>${bots}</div>`+
 (me.action=='trade'?`<div class="mute small">bought ${Math.round(me.buy_after_create)} s after launch at ${usd(me.mc_buy)}, held ${Math.round(me.held)} s</div>`:'')+
 `<div class="mute small" style="margin-top:4px">The full chart is now shown, with the bots' buy and sells.</div><button style="width:100%;margin-top:10px" onclick="next()">Next coin &rarr;</button>`}
$('play').onclick=()=>PLAY?stop():play();$('buy').onclick=buy;$('sell').onclick=sell;$('skip').onclick=skip;
document.onkeydown=e=>{if(e.target.tagName=='INPUT')return;const k=e.key.toLowerCase();if(e.code=='Space'){e.preventDefault();PLAY?stop():play()}
 if(k=='b')buy();if(k=='s')sell();if(k=='k')skip();if(k=='n'&&DONE)next();if('12345'.includes(k)&&k.length==1)document.querySelector(`#narr button[data-n="${k}"]`).click()};
window.onresize=()=>render();next();
</script></body></html>""")
