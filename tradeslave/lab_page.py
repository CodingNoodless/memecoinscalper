"""The lab page (dashboard "/"): the three M bots side by side, their details, jobs, Kalshi, C1 and experiments.
Data comes from /api/lab (lab_view.build_lab_snapshot)."""

from .ui_theme import THEME, nav

LAB_HTML = (r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tradeslave Lab</title><style>""" + THEME + r"""
main{padding:18px;display:grid;gap:16px;max-width:1440px;margin:0 auto}
.bots{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(310px,1fr))}
.bot .top{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
.bot .name{font-size:15px;font-weight:700}.bot .desc{color:var(--mute);font-size:12px;margin-top:2px}
.bot .pnl{font-size:24px;font-weight:750;text-align:right;white-space:nowrap}
.bot .pnl small{display:block;font-size:10.5px;color:var(--mute);font-weight:600;text-transform:uppercase;letter-spacing:.05em}
.kgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px}
.spark{width:100%;height:56px;margin-top:10px;display:block}
.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}
details.card{padding:0}
details.card>summary{list-style:none;cursor:pointer;padding:14px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
details.card>summary::-webkit-details-marker{display:none}
details.card>summary .chev{transition:transform .15s;color:var(--mute);font-size:11px}
details[open]>summary .chev{transform:rotate(90deg)}
details.card>summary b{font-size:14px}details .body{padding:0 16px 16px;display:grid;gap:16px}
.lbl{color:var(--mute);font-size:11.5px;font-weight:650;letter-spacing:.06em;text-transform:uppercase;margin:0 0 8px}
.scroll{overflow:auto;max-height:420px;border:1px solid var(--line);border-radius:10px}
.bar{display:flex;gap:5px;align-items:flex-end;height:72px;padding-bottom:18px}
.bar div{background:#334155;flex:1;min-height:2px;border-radius:4px 4px 0 0;position:relative}
.bar div.hot{background:linear-gradient(180deg,var(--good),#059669)}
.bar div span{position:absolute;bottom:-18px;left:0;right:0;text-align:center;font-size:11px;color:var(--mute)}
.running,.pass{color:var(--good);border-color:#34d39966}.quiet,.missing{color:var(--warn);border-color:#fbbf2466}
.fail{color:var(--bad);border-color:#f8717166}.done{color:var(--acc);border-color:#7c9cff66}
tr.hl td{background:#7c9cff12}code{font-size:11.5px;word-break:break-all;color:#b6c0cf}
a.coin{color:var(--ink)}a.coin:hover{color:var(--acc)}
@media (max-width:640px){main{padding:12px}.kgrid{grid-template-columns:repeat(2,1fr)}}
</style></head><body>""" + nav("lab", "Paper only &middot; refreshes every 15 s") + r"""
<main>
<section class="bots" id="bots"></section>
<details class="card" open><summary><span class="chev">&#9654;</span><b>M1 details</b><span class="mute small">slices, recent trades, funnel, every scored coin</span></summary><div class="body">
<div class="grid"><div><div class="lbl">Results by slice</div><div id="rules" class="scroll"></div></div>
<div><div class="lbl">Narrative scores, last 24 h</div><div class="bar" id="sc"></div><div class="lbl" style="margin-top:14px">Open positions</div><div id="open"></div></div></div>
<div class="grid"><div><div class="lbl">Recent trades &middot; click a coin to replay it</div><div id="trades" class="scroll"></div></div>
<div><div class="lbl">Funnel, last 24 h</div><div id="fun" class="scroll"></div></div></div>
<div><div class="lbl">Every scored coin <select id="minsc"><option value="0">all scores</option><option value="5">5+</option><option value="7">7+ (buy zone)</option></select></div><div id="top" class="scroll" style="max-height:520px"></div></div>
</div></details>
<details class="card"><summary><span class="chev">&#9654;</span><b>M2 details</b><span class="mute small">M1 + sniper-bag rug filter, &minus;10% stop, 1 s price checks</span></summary><div class="body">
<div class="grid"><div><div class="lbl">Recent trades</div><div id="m2t" class="scroll"></div></div><div><div class="lbl">Funnel, last 24 h</div><div id="m2f" class="scroll"></div></div></div></div></details>
<details class="card"><summary><span class="chev">&#9654;</span><b>M3 details</b><span class="mute small">M2 + dead-coin and dev filters, real-cause scoring, rides runners</span></summary><div class="body">
<div class="grid"><div><div class="lbl">Recent trades</div><div id="m3t" class="scroll"></div></div><div><div class="lbl">Funnel, last 24 h</div><div id="m3f" class="scroll"></div></div></div></div></details>
<details class="card" open><summary><span class="chev">&#9654;</span><b>M4 details</b><span class="mute small">M1 + TF fixes: watchlist until 20 holders, sell when holders fall 10%, skip buying a top &middot; forward paper test from Oct 22 15:01</span></summary><div class="body">
<div class="grid"><div><div class="lbl">Open positions (holders now / max since buy)</div><div id="m4o"></div><div class="lbl" style="margin-top:14px">Exit reasons</div><div id="m4x"></div></div>
<div><div class="lbl">Verdict bar</div><div id="m4v"></div></div></div>
<div class="grid"><div><div class="lbl">Recent trades &middot; click a coin to replay it</div><div id="m4t" class="scroll"></div></div><div><div class="lbl">Funnel, last 24 h (watch = waiting for 20 holders)</div><div id="m4f" class="scroll"></div></div></div></div></details>
<details class="card" open><summary><span class="chev">&#9654;</span><b>M5 details</b><span class="mute small">runner model on post-band momentum, any X-linked coin &middot; forward paper test from Oct 22 23:00 (backtest held out &minus;5%, so it must prove itself here)</span></summary><div class="body">
<div class="grid"><div><div class="lbl">Runner model decisions</div><div id="m5s"></div><div class="lbl" style="margin-top:14px">Open positions (holders now / max since buy)</div><div id="m5o"></div><div class="lbl" style="margin-top:14px">Exit reasons</div><div id="m5x"></div></div>
<div><div class="lbl">Verdict bar</div><div id="m5v"></div></div></div>
<div class="grid"><div><div class="lbl">Recent trades &middot; click a coin to replay it</div><div id="m5t" class="scroll"></div></div>
<div><div class="lbl">Coins the model evaluated (best runner probability so far)</div><div id="m5e" class="scroll"></div></div></div></div></details>
<details class="card" open><summary><span class="chev">&#9654;</span><b>M6 details</b><span class="mute small">M5 v1 + funding-cluster skip: no buy when one funder controls &ge; 15% of the top-20 holders (Helius funding lookups) &middot; m6-v2 forward paper test from Oct 23 13:03 (FL backtest failed narrowly; this is its clean test)</span></summary><div class="body">
<div class="grid"><div><div class="lbl">Funding checks (M5 buy signals M6 looked up)</div><div id="m6s"></div><div class="lbl" style="margin-top:14px">Open positions</div><div id="m6o"></div><div class="lbl" style="margin-top:14px">Exit reasons</div><div id="m6x"></div></div>
<div><div class="lbl">Verdict bar (judged once at 50 m6-v2 trades or Nov 1)</div><div id="m6v"></div></div></div>
<div class="grid"><div><div class="lbl">Recent trades &middot; click a coin to replay it</div><div id="m6t" class="scroll"></div></div>
<div><div class="lbl">Recent funding checks (largest same-funder share of the top-20)</div><div id="m6e" class="scroll"></div></div></div></div></details>
<details class="card" open><summary><span class="chev">&#9654;</span><b>M11 details</b><span class="mute small">leader wave rider (m11-v1): copy decu's first buy in a coin 1 s after we see it, exit TP +50% / stop &minus;20% &middot; the only copy design that passed a backtest (DC part C: +3.3% a trade on his later 70 trades, narrow: 90% CI &minus;4.8% to +12.2%) &middot; M11 v1&ndash;v3 model searches all FAILED</span></summary><div class="body">
<div class="grid"><div><div class="lbl">Scorecard vs backtest</div><div id="m11v"></div><div class="lbl" style="margin-top:14px">Open positions</div><div id="m11o"></div></div>
<div><div class="lbl">How M11 was chosen (all pre-registered, judged on later data)</div><div id="m11b"></div></div></div>
<div><div class="lbl">Recent M11 trades</div><div id="m11t" class="scroll"></div></div></div></details>
<details class="card" open><summary><span class="chev">&#9654;</span><b>M10 details</b><span class="mute small">flagship forward test (m10-v2): calm-entry rule learned from 20,848 backtest moments &middot; every 15 s from 15&ndash;180 s after a coin's first trade &middot; &minus;10% stop, 1 s polls &middot; backtest did NOT pass (&minus;4.6% a trade both halves, 4% deep losses)</span></summary><div class="body">
<div class="grid"><div><div class="lbl">Rule (all four must hold; thresholds from design data only)</div><div id="m10r"></div><div class="lbl" style="margin-top:14px">Open positions</div><div id="m10o"></div><div class="lbl" style="margin-top:14px">Exit reasons</div><div id="m10x"></div></div>
<div><div class="lbl">Scorecard vs backtest and M5 (same hours)</div><div id="m10v"></div></div></div>
<div class="grid"><div><div class="lbl">Recent trades &middot; click a coin to replay it</div><div id="m10t" class="scroll"></div></div>
<div><div class="lbl">Recent rule checks (one per 15-s step)</div><div id="m10d" class="scroll"></div></div></div></div></details>
<details class="card" open><summary><span class="chev">&#9654;</span><b>M7 vs M8 details</b><span class="mute small">same m6-v3 entries (M6 cluster skip + skip creators whose funder funded an earlier creator) &middot; M7 stop &minus;30%, M8 stop &minus;10% &middot; 1 s polls &middot; forward paper test from Oct 23 16:00, judged once at &ge; 60 trades each</span></summary><div class="body">
<div class="grid"><div><div class="lbl">Entry checks (M5 buy signals since 16:00)</div><div id="m78c"></div><div class="lbl" style="margin-top:14px">Open positions</div><div id="m78o"></div><div class="lbl" style="margin-top:14px">Exit reasons</div><div id="m78x"></div></div>
<div><div class="lbl">Side by side (same hours) &middot; verdict bars</div><div id="m78v"></div></div></div>
<div class="grid"><div><div class="lbl">Same coin, both bots &middot; the cleanest stop comparison</div><div id="m78p" class="scroll"></div></div>
<div><div class="lbl">Recent trades &middot; click a coin to replay it</div><div id="m78t" class="scroll"></div></div></div>
<div class="lbl" style="margin-top:14px">What the m6-v3 skip did &middot; every M5 trade since 16:00 and M7's decision on that coin</div><div id="m78s" class="scroll"></div>
<div class="lbl" style="margin-top:14px">Recent entry decisions (cluster = largest same-funder share of the top-20; serial = earlier creators this funder funded)</div><div id="m78d" class="scroll"></div></div></details>
<section class="grid"><div class="card"><h2>Running jobs</h2><div id="jobs" class="scroll"></div></div>
<div class="card"><h2>Kalshi paper bots</h2><div id="kal"></div><h2 style="margin-top:18px">C1 recorder</h2><div id="c1"></div></div></section>
<section class="card"><h2>Experiments <span class="mute small" id="exn" style="text-transform:none;letter-spacing:0"></span></h2><div id="exp" class="scroll" style="max-height:560px"></div></section>
</main><script>
const $=id=>document.getElementById(id),esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const DASH="–";
const pct=(x,d=1)=>x==null?DASH:`<span class="${x>0?"pos":x<0?"neg":""}">${(x>0?"+":"")+(100*x).toFixed(d)}%</span>`;
const ago=t=>{const s=Date.now()/1e3-t;return s<90?`${s|0}s ago`:s<5400?`${(s/60)|0}m ago`:`${(s/3600).toFixed(1)}h ago`};
const tm=t=>new Date(t*1e3).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"});
function table(h,rows,hl){return rows.length?`<table><tr>${h.map(x=>`<th>${x}</th>`).join("")}</tr>${rows.map((r,i)=>`<tr${hl&&hl(i)?' class="hl"':""}>${r.map(c=>`<td>${c}</td>`).join("")}</tr>`).join("")}</table>`:'<div class="mute small" style="padding:10px">none yet</div>'}
function spark(c){if(!c||c.length<2)return '<svg class="spark"></svg>';const ys=c.map(p=>p[1]),lo=Math.min(0,...ys),hi=Math.max(0,...ys),r=hi-lo||1,
 col=ys[ys.length-1]>=0?"#34d399":"#f87171",y=v=>54-(v-lo)/r*50,pts=c.map((p,i)=>`${(i/(c.length-1)*300).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" ");
 return `<svg class="spark" viewBox="0 0 300 56" preserveAspectRatio="none"><line x1="0" x2="300" y1="${y(0)}" y2="${y(0)}" stroke="#2a3340" stroke-dasharray="3 3"/>`+
 `<polygon fill="${col}" opacity=".12" points="0,${y(0)} ${pts} 300,${y(0)}"/><polyline fill="none" stroke="${col}" stroke-width="1.8" points="${pts}"/></svg>`}
function bot(title,desc,m,extra){const s=m.stats;return `<div class="card bot"><div class="top"><div><div class="name">${title}</div><div class="desc">${desc}</div></div>`+
 `<div class="pnl ${m.sol>=0?"pos":"neg"}">${m.sol>=0?"+":""}${m.sol.toFixed(3)} SOL<small>paper P&amp;L, 0.1 SOL a trade</small></div></div>${spark(m.curve)}`+
 `<div class="kgrid">${[["Trades",s.n],["Mean / trade",s.n?pct(s.mean):DASH],["Win rate",s.n?(100*s.win).toFixed(0)+"%":DASH],["t-stat",s.n>1?s.t.toFixed(2):DASH],
 ["2x hits",m.exits.TP||0],["Open now",m.open.length],...(extra||[])].map(([a,b])=>`<div class="kpi"><span>${a}</span><b>${b}</b></div>`).join("")}</div></div>`}
const trow=(v,r)=>[tm(r.t),`<a class="coin" href="/trades#${v}|${esc(r.m)}">${esc(r.name)}</a>`,r.score,esc(r.exit),pct(r.ret),r.held+"m"];
const funnel=ff=>table(["Stage","Count"],[["Narrative &ge; 7",ff.passed],["Reached $4&ndash;7k + checks",ff.checked],["Passed checks (bought)",ff.checks_ok],
 ...Object.entries(ff.blocked).sort((a,b)=>b[1]-a[1]).map(([k,v])=>[`<span class="mute">blocked: ${esc(k)}</span>`,v])]);
async function tick(){let d;try{d=await (await fetch("/api/lab",{cache:"no-store"})).json()}catch(e){$("navnote").textContent="dashboard server unreachable";return}
 $("navnote").textContent="Paper only · updated "+new Date().toLocaleTimeString();const m=d.m1,m2=d.m2,m3=d.m3,m4=d.m4,m5=d.m5;
 $("bots").innerHTML=bot("M1","Rohit's process automated: narrative score, $4&ndash;7k entry, bundle checks",m,[["Watching",m.status.watching??DASH],["Narratives",m.narratives.n],["Model",`<span class="small">${esc((m.status.llm||DASH).replace(/gemini-/g,""))}</span>`]])+
  bot("M2","M1 + sniper-bag rug filter, &minus;10% stop, 1 s checks",m2,[["Narratives",m2.narratives.n],["Mean / narrative",m2.narratives.n?pct(m2.narratives.mean):DASH],["M1 since 09:20",m.fresh.n?pct(m.fresh.mean):DASH]])+
  bot("M3","M2 + dead-coin and dev filters, real-cause scoring, rides runners",m3,[["Half-sold, rode",m3.rides||0],["Narratives",m3.narratives.n],["Mean / narrative",m3.narratives.n?pct(m3.narratives.mean):DASH]])+
  bot("M4 <span class='pill done' style='margin-left:6px'>new</span>","M1 + TF fixes: wait for 20 holders, sell when holders fall 10%, skip buying a top",m4,[["Narratives",m4.narratives.n],["Mean / narrative",m4.narratives.n?pct(m4.narratives.mean):DASH],["Rules",esc(Object.keys(m4.by_rules||{}).join(", ")||"m4-v1")]])+
  bot("M5 <span class='pill done' style='margin-left:6px'>new</span>","Runner model: buys only coins whose post-band momentum says runner (backtest: held out -5%, all -1.6%; forward test)",m5,[["Narratives",m5.narratives.n],["Mean / narrative",m5.narratives.n?pct(m5.narratives.mean):DASH],["Since",esc("Oct 22 23:00")]])+
  (d.m6?bot("M6 <span class='pill done' style='margin-left:6px'>new</span>","M5 + funding-cluster skip: no buy when one funder holds &ge; 15% of the top-20 (forward test; FL backtest failed narrowly)",d.m6,[["Narratives",d.m6.narratives.n],["Mean / narrative",d.m6.narratives.n?pct(d.m6.narratives.mean):DASH],["Since",esc("Oct 23 17:00")]]):"")+
  (d.m7?bot("M7 <span class='pill done' style='margin-left:6px'>new</span>","m6-v3 entries (cluster + serial-funder skip), M5 exits (stop &minus;30%); side by side with M8",d.m7,[["Checked",(d.m7.m6||{}).checked??0],["Skipped",(d.m7.m6||{}).skipped??0],["Since",esc("Oct 23 16:00")]]):"")+
  (d.m11?bot("M11 <span class='pill done' style='margin-left:6px'>newest</span>","Leader wave rider: copies decu's first buy 1 s after we detect it, exits TP +50% / stop &minus;20% (only copy design that passed a backtest, narrowly)",d.m11,[["Open",(d.m11.open||[]).length],["Lag",(d.m11x||{}).lag_median!=null?(+d.m11x.lag_median).toFixed(1)+" s":DASH],["Since",esc("Oct 24 02:23")]]):"")+
  (d.m10?bot("M10 <span class='pill done' style='margin-left:6px'>flagship</span>","Calm-entry rule from the M10 backtest: many small buys, low top-10, 30+ s old, no buying burst &middot; &minus;10% stop, 1 s polls (forward test; backtest &minus;4.6%)",d.m10,[["Checks",(d.m10x||{}).checked??0],["Bought",(d.m10x||{}).bought??0],["Since",esc("Oct 23 23:45")]]):"")+
  (d.m9?bot("M9 <span class='pill done' style='margin-left:6px'>new</span>","m9-v2: M5's or M1's signal, then the PX rule (30 s net flow &ge; 12.3 SOL, creator funder new to us) &middot; &minus;10% stop, 1 s polls &middot; twin M9e: same fills, &minus;30% stop (judged at 60 trades)",d.m9,[["Twin M9e (&minus;30%)",d.m9e&&d.m9e.stats.n?d.m9e.stats.n+" &middot; "+pct(d.m9e.stats.mean):"0"],["From M5 signal",d.m9.by_src&&d.m9.by_src.m5.n?d.m9.by_src.m5.n+" &middot; "+pct(d.m9.by_src.m5.mean):"0"],["From M1 signal",d.m9.by_src&&d.m9.by_src.m1.n?d.m9.by_src.m1.n+" &middot; "+pct(d.m9.by_src.m1.mean):"0"],["Since",esc("Oct 23 17:21 (v2)")]]):"")+
  (d.m8?bot("M8 <span class='pill done' style='margin-left:6px'>new</span>","Twin exit book inside M7: same fills, &minus;10% stop (max-loss test)",d.m8,[["Checked",((d.m7||{}).m6||{}).checked??0],["Skipped",((d.m7||{}).m6||{}).skipped??0],["Since",esc("Oct 23 16:00")]]):"");
 const sc=m.funnel.scores,mx=Math.max(1,...Object.values(sc));
 $("sc").innerHTML=[...Array(11).keys()].map(k=>`<div class="${k>=7?"hot":""}" style="height:${(sc[k]||0)/mx*100}%" title="score ${k}: ${sc[k]||0}"><span>${k}</span></div>`).join("");
 $("open").innerHTML=table(["Coin","Score","Entry","Now","Since"],m.open.map(o=>[esc(o.name),o.score,"$"+(o.mc||0).toLocaleString(),pct(o.ret_now),ago(o.t)]));
 const f=m.funnel;$("fun").innerHTML=table(["Stage","Count"],[["X-linked launches scored",f.scored],["Narrative &ge; 7",f.passed],["Reached $4&ndash;7k + checks",f.checked],["Passed checks (bought)",f.checks_ok],
  ...Object.entries(f.blocked).sort((a,b)=>b[1]-a[1]).map(([k,v])=>[`<span class="mute">blocked: ${esc(k)}</span>`,v]),[`<span class="mute">models</span>`,esc(Object.entries(f.models).map(([k,v])=>k+" "+v).join(", "))],
  [`<span class="mute">LLM calls today</span>`,esc(Object.entries(m.status.calls||{}).map(([k,v])=>k.split(" ")[1]+" "+v).join(", ")||DASH)]]);
 const srow=(k,v)=>[k,v.n,v.n?pct(v.mean):DASH,v.n?(100*v.win).toFixed(0)+"%":DASH,v.n>1?v.t.toFixed(2):DASH];
 $("rules").innerHTML=table(["Slice","Trades","Mean","Win","t"],[srow("fresh: since 08:45",m.fresh),...Object.entries(m.by_window).map(([k,v])=>srow(esc(k.replace(/<\/?b>/g,"")),v)),
  ...Object.entries(m.by_rules).map(([k,v])=>srow("rules "+esc(k),v))],i=>i<2);
 $("trades").innerHTML=table(["Time","Coin","Score","Exit","Return","Held"],m.recent.map(r=>trow("m1",r)));
 const ms=+$("minsc").value,crit={1:"influential",2:"potential",3:"cause",4:"correction"};
 $("top").innerHTML=table(["Time","Coin","Score","Why","Outcome"],m.top.filter(r=>(r.score??-1)>=ms).map(r=>[tm(r.t),
  (r.link?`<a href="${esc(r.link)}" target="_blank" rel="noopener">${esc(r.name)}</a>`:esc(r.name))+(r.after?`<div class="mute small">copy of ${esc(r.after)}${r.improves?" &middot; improves":""}</div>`:""),
  `<b class="${r.score>=7?"pos":r.score==null?"neg":""}">${r.score??"err"}</b><div class="mute small">${esc(crit[r.crit]||"")}</div>`,`<span class="small">${esc(r.why)}</span>`,
  `<span class="small">${r.bought?'<b class="pos">bought</b>':r.check?esc(r.check):r.score>=7?(r.after&&!r.improves?"skipped: copy":"waiting for $4&ndash;7k"):"&mdash;"}</span>`]));
 $("m2t").innerHTML=table(["Time","Coin","Score","Exit","Return","Held"],m2.recent.map(r=>trow("m2",r)));
 $("m3t").innerHTML=table(["Time","Coin","Score","Exit","Return","Held"],m3.recent.map(r=>trow("m3",r)));
 $("m2f").innerHTML=funnel(m2.funnel);$("m3f").innerHTML=funnel(m3.funnel);$("m4f").innerHTML=funnel(m4.funnel);
 const v5=m5.m5||{},P5=v5.P;
 $("m5s").innerHTML=table(["Measure","Value"],[["Buy threshold (runner probability)",P5!=null?P5.toFixed(3):DASH],["Coins evaluated",v5.coins??0],["Evaluations (every 10 s for 3 min after the band touch)",v5.evals??0],
  ["Coins within 80% of the threshold",v5.near??0],["Bought",v5.buys??0]]);
 $("m5e").innerHTML=table(["Time","Coin","Best p","","Holders","mc"],(v5.recent||[]).map(r=>[tm(r.t),`<a class="coin" href="/watch">${esc((r.name||r.m.slice(0,6)).slice(0,22))}</a>`,
  `<b class="${r.buy?"pos":P5&&r.p>=0.8*P5?"":"mute"}">${r.p.toFixed(3)}</b>`,
  `<div style="background:#252d38;border-radius:4px;height:8px;width:90px;position:relative"><div style="position:absolute;left:0;top:0;bottom:0;border-radius:4px;width:${Math.min(100,P5?r.p/P5*100:0)}%;background:${r.buy?"var(--good)":r.p>=0.8*(P5||1)?"var(--warn)":"#475569"}"></div></div>`,
  r.holders??DASH,r.mc?"$"+(r.mc/1000).toFixed(1)+"k":DASH]));
 $("m5t").innerHTML=table(["Time","Coin","Score","Exit","Return","Held"],m5.recent.map(r=>trow("m5",r)));
 $("m5o").innerHTML=table(["Coin","Entry","Now","Holders","Since"],m5.open.map(o=>[esc(o.name),"$"+(o.mc||0).toLocaleString(),pct(o.ret_now),o.holders!=null?o.holders+" / "+o.h_max:DASH,ago(o.t)]));
 $("m5x").innerHTML=table(["Exit","Trades"],Object.entries(m5.exits).sort((a,b)=>b[1]-a[1]).map(([k,v])=>[esc(k)+(k=="HDECL"?' <span class="mute small">holders fell 10% (after 15 s)</span>':""),v]));
 const s5=m5.stats,vs5=(m.fresh5||{});
 $("m5v").innerHTML=table(["Test","Now","Bar"],[["Trades",s5.n||0,"60 (or Nov 1)"],["Mean / trade",s5.n?pct(s5.mean):DASH,"&ge; +5%"],["t-stat",s5.n>1?s5.t.toFixed(2):DASH,"&ge; 3"],
  ["P&amp;L without best 2",m5.ex2!=null?(m5.ex2>=0?"+":"")+m5.ex2.toFixed(3)+" SOL":DASH,"&gt; 0"],["M1 over the same hours",vs5.n?pct(vs5.mean)+` (${vs5.n})`:DASH,"M5 must beat M1&ndash;M4"]]);
 const m6=d.m6||{stats:{},recent:[],open:[],exits:{}},v6=m6.m6||{},s6=m6.stats||{},m5same=v6.m5_same||{};
 $("m6s").innerHTML=table(["Measure","Value"],[["Rule","skip if one funder holds &ge; 15% of the top-20"],["Signals checked",v6.checked??0],["Bought",v6.bought??0],["Skipped (funding cluster)",v6.skipped??0],["Rules",esc(Object.keys(m6.by_rules||{}).join(", ")||"m6-v2")]]);
 $("m6e").innerHTML=table(["Time","Coin","Cluster","Creator-linked","Fresh","Decision"],(v6.recent||[]).map(r=>[tm(r.t),`<a class="coin" href="/watch">${esc((r.name||r.m.slice(0,6)).slice(0,22))}</a>`,
  `<b class="${r.cluster>=0.15?"neg":"mute"}">${(r.cluster*100).toFixed(0)}%</b>`,r.creator!=null?(r.creator*100).toFixed(0)+"%":DASH,r.fresh!=null?(r.fresh*100).toFixed(0)+"%":DASH,
  r.buy?'<b class="pos">bought</b>':'<span class="neg">skipped</span>']));
 $("m6t").innerHTML=table(["Time","Coin","Score","Exit","Return","Held"],(m6.recent||[]).map(r=>trow("m6",r)));
 $("m6o").innerHTML=table(["Coin","Entry","Now","Holders","Since"],(m6.open||[]).map(o=>[esc(o.name),"$"+(o.mc||0).toLocaleString(),pct(o.ret_now),o.holders!=null?o.holders+" / "+o.h_max:DASH,ago(o.t)]));
 $("m6x").innerHTML=table(["Exit","Trades"],Object.entries(m6.exits||{}).sort((a,b)=>b[1]-a[1]).map(([k,v])=>[esc(k),v]));
 $("m6v").innerHTML=table(["Test","M6","Bar"],[["Trades",s6.n||0,"50 (or Nov 1)"],["Mean / trade",s6.n?pct(s6.mean):DASH,m5same.n?"&ge; M5 same hours + 5 pts ("+pct(m5same.mean)+", "+m5same.n+")":"M5 same hours + 5 pts"],
  ["Share &le; &minus;25%",m6.deep!=null?pct(m6.deep):DASH,v6.m5_deep!=null?"&lt; M5 same hours ("+pct(v6.m5_deep)+")":"&lt; M5 same hours"],["t-stat",s6.n>1?s6.t.toFixed(2):DASH,"report only"],
  ["P&amp;L without best 2",m6.ex2!=null?(m6.ex2>=0?"+":"")+m6.ex2.toFixed(3)+" SOL":DASH,"&gt; 0"]]);
 const x_m=d.m11||{stats:{},open:[]},x_x=d.m11x||{stats:{},trades:[]},x_s=x_x.stats||{};
 $("m11v").innerHTML=table(["Measure","M11 (live paper)","Backtest (DC part C, holdout)"],[["Trades",x_s.n||0,"70"],["Mean / trade",x_s.n?pct(x_s.mean):DASH,pct(0.033)],["Median",x_s.n?pct(x_s.median):DASH,pct(-0.186)],["Win rate",x_s.n?pct(x_s.win,0):DASH,DASH],["TP / stop / timeout",(x_x.tp||0)+" / "+(x_x.sl||0)+" / "+(x_x.timeout||0),"23 / 39 / 8"],["Share &le; &minus;25%",x_x.deep!=null?pct(x_x.deep,0):DASH,DASH],["Our detection lag (median / max)",x_x.lag_median!=null?(+x_x.lag_median).toFixed(1)+" s / "+(+x_x.lag_max).toFixed(1)+" s":DASH,"1 s assumed"],["Pass bar","mean &gt; 0, both halves, &ge; 60 trades","&mdash;"]]);
 $("m11o").innerHTML=table(["Coin","Since","Entry price"],(x_m.open||[]).map(o=>[esc(o.name||o.m.slice(0,6)),ago(o.t),o.p0?(+o.p0).toExponential(3):"waiting for +1 s"]));
 $("m11b").innerHTML=table(["Version","Idea","Holdout","Verdict"],[["DC part C","copy decu +1 s, exit picked on his first half (TP50/SL20)","+3.3% (n 70)","PASS (narrow)"],["M11 v1","follow wallets profitable on their own trades","&minus;11.7% (n 196)","FAIL"],["M11 v2","follow wallets whose 1 s copies were profitable","none qualified (411 wallets, copies &minus;11%)","FAIL"],["M11 v3","M9 flow entry + acceleration cap","&minus;10.8% (n 437)","FAIL"],["Copy decu, his own exit","plain copy at +1 s","&minus;10.0% (n 129)","FAIL"]]);
 $("m11t").innerHTML=table(["Time","Coin","Exit","Return","Peak","Our lag","Held"],(x_x.trades||[]).map(r=>[tm(r.t),esc(r.m.slice(0,6)),esc(r.exit),pct(r.ret),r.peak!=null?pct(r.peak):DASH,r.lag!=null?(+r.lag).toFixed(1)+" s":DASH,r.held+"m"]));
 const y_m=d.m10||{stats:{},recent:[],open:[],exits:{}},y_x=d.m10x||{stats:{},m5:{},decisions:[]},y_s=y_x.stats||{},y_5=y_x.m5||{};
 const y_ok=(v,op,c)=>v==null?DASH:(op=="<="?v<=c:v>=c)?'<b class="pos">'+(+v).toFixed(3)+'</b>':'<span class="neg">'+(+v).toFixed(3)+'</span>';
 $("m10r").innerHTML=table(["Condition","Threshold","Why"],[["Average buy, last 60 s","&le; 0.511 SOL","winners were bought on many small buys"],["Top-10 holders","&le; 23.3%","less concentrated supply"],["Since first trade","&ge; 30.25 s","after the launch burst"],["Buy/sell SOL ratio, 60 s","&le; 1.593","not into a buying burst (1 s delay costs ~0.5 pt here vs 3-4 pts in bursts)"],["Checks so far",y_x.checked??0,"bought "+(y_x.bought??0)]]);
 $("m10o").innerHTML=table(["Coin","Entry","Now","Holders","Since"],(y_m.open||[]).map(o=>[esc(o.name),"$"+(o.mc||0).toLocaleString(),pct(o.ret_now),o.holders!=null?o.holders+" / "+o.h_max:DASH,ago(o.t)]));
 $("m10x").innerHTML=table(["Exit","Trades"],Object.entries(y_m.exits||{}).sort((a,b)=>b[1]-a[1]).map(([k,v])=>[esc(k),v]));
 $("m10v").innerHTML=table(["Measure","M10 (live paper)","Backtest holdout","M5 same hours"],[["Trades",y_s.n||0,"922",y_5.n||0],["Mean / trade",y_s.n?pct(y_s.mean):DASH,pct(-0.046),y_5.n?pct(y_5.mean):DASH],["Median",y_s.n?pct(y_s.median):DASH,pct(-0.047),y_5.n?pct(y_5.median):DASH],["Share &le; &minus;25%",y_x.deep!=null?pct(y_x.deep,0):DASH,"4%",y_x.m5_deep!=null?pct(y_x.m5_deep,0):DASH],["Win rate",y_s.n?pct(y_s.win,0):DASH,DASH,y_5.n?pct(y_5.win,0):DASH],["Worst trade",y_s.n?pct(y_s.worst):DASH,DASH,y_5.n?pct(y_5.worst):DASH],["Pass bar","mean &gt; 0 over &ge; 60 trades","&mdash;","&mdash;"]]);
 $("m10t").innerHTML=table(["Time","Coin","Score","Exit","Return","Held"],(y_m.recent||[]).map(r=>trow("m10",r)));
 $("m10d").innerHTML=table(["Time","Coin","Step","Avg buy","Top-10","Age s","Buy/sell","Decision"],(y_x.decisions||[]).map(r=>[tm(r.t),esc((r.name||r.m.slice(0,6)).slice(0,22)),r.step??DASH,y_ok(r.avg_buy_60,"<=",0.511),y_ok(r.top10,"<=",0.233),y_ok(r.t_to_n,">=",30.25),y_ok(r.buy_ratio_60,"<=",1.593),r.buy?'<b class="pos">bought</b>':'<span class="mute">no</span>']));
 const z_q=d.m78||{m7:{},m8:{},m5:{},m6:{},checks:{},decisions:[],pairs:[],recent:[],open:[]},z_c78=z_q.checks||{},z_B={m7:"M7",m8:"M8"};
 const z_DEC={bought:'<b class="pos">bought</b>',cluster:'<span class="neg">skip: cluster</span>',serial:'<span class="neg">skip: serial funder</span>',lookup:'<span class="neg">skip: lookup failed</span>',other:'<span class="mute">skip</span>'};
 $("m78c").innerHTML=table(["Measure","Count"],[["M5 buy signals checked",z_c78.signals??0],["Bought (both bots)",z_c78.bought??0],["Skipped: funding cluster &ge; 15%",z_c78.cluster??0],["Skipped: serial-funder creator",z_c78.serial??0],["Skipped: funder lookup failed",z_c78.lookup??0]]);
 $("m78o").innerHTML=table(["Bot","Coin","Entry","Now","Stop","Holders","Since"],(z_q.open||[]).map(o=>[z_B[o.bot],esc(o.name),"$"+(o.mc||0).toLocaleString(),pct(o.ret_now),o.stop!=null?pct(o.stop-1,0):DASH,o.holders!=null?o.holders+" / "+o.h_max:DASH,ago(o.t)]));
 const z_xk=[...new Set([...Object.keys(z_q.m7.exits||{}),...Object.keys(z_q.m8.exits||{})])];
 $("m78x").innerHTML=table(["Exit","M7","M8"],z_xk.map(k=>[esc(k),(z_q.m7.exits||{})[k]||0,(z_q.m8.exits||{})[k]||0]));
 const z_f=(s,k,fn)=>s&&s.n?fn(s[k]):DASH,z_P=x=>pct(x),z_S=x=>x==null?DASH:(x>=0?"+":"")+x.toFixed(3)+" SOL",z_N=x=>x==null?DASH:(100*x).toFixed(0)+"%";
 const z_M7=z_q.m7,z_M8=z_q.m8,z_M5=z_q.m5,z_M6=z_q.m6;
 $("m78v").innerHTML=table(["Measure","M7 (&minus;30%)","M8 (&minus;10%, same fills)","M5 (all its trades)","M6 (all its trades)","Bar"],[
  ["Trades",z_M7.n||0,z_M8.n||0,z_M5.n||0,z_M6.n||0,"&ge; 60 each (or Nov 1)"],
  ["Mean / trade",z_f(z_M7,"mean",z_P),z_f(z_M8,"mean",z_P),z_f(z_M5,"mean",z_P),z_f(z_M6,"mean",z_P),"M7: &gt; 0, &ge; M5 + 3 pts, &ge; M6"],
  ["Share &le; &minus;25%",z_f(z_M7,"deep",z_N),z_f(z_M8,"deep",z_N),z_f(z_M5,"deep",z_N),z_f(z_M6,"deep",z_N),"M8 adopted if lower than M7&hellip;"],
  ["Mean gap M8 &minus; M7",DASH,z_M7.n&&z_M8.n?pct(z_M8.mean-z_M7.mean):DASH,DASH,DASH,"&hellip;and within 2 pts of M7"],
  ["Median",z_f(z_M7,"median",z_P),z_f(z_M8,"median",z_P),z_f(z_M5,"median",z_P),z_f(z_M6,"median",z_P),"report only"],
  ["Win rate",z_f(z_M7,"win",z_N),z_f(z_M8,"win",z_N),z_f(z_M5,"win",z_N),z_f(z_M6,"win",z_N),"report only"],
  ["Worst trade",z_f(z_M7,"worst",z_P),z_f(z_M8,"worst",z_P),z_f(z_M5,"worst",z_P),z_f(z_M6,"worst",z_P),"report only"],
  ["Spread (sd)",z_f(z_M7,"sd",z_N),z_f(z_M8,"sd",z_N),z_f(z_M5,"sd",z_N),z_f(z_M6,"sd",z_N),"report only"],
  ["P&amp;L (0.1 SOL / trade)",z_f(z_M7,"sol",z_S),z_f(z_M8,"sol",z_S),z_f(z_M5,"sol",z_S),z_f(z_M6,"sol",z_S),"report only"],
  ["P&amp;L without best 2",z_S(z_M7.ex2),z_S(z_M8.ex2),z_S(z_M5.ex2),z_S(z_M6.ex2),"report only"]]);
 $("m78p").innerHTML=(z_q.pairs&&z_q.pairs.length?`<div class="small mute" style="padding:4px 0 8px">${z_q.pairs.length} coins in both &middot; mean M8 &minus; M7 ${pct(z_q.pair_diff)}</div>`:"")+
  table(["Time","Coin","M7 exit","M7","M8 exit","M8","M8 &minus; M7"],(z_q.pairs||[]).map(r=>[tm(r.t),`<a class="coin" href="/trades#m7|${esc(r.m)}">${esc(r.name)}</a>`,esc(r.x7),pct(r.r7),esc(r.x8),pct(r.r8),pct(r.r8-r.r7)]));
 $("m78t").innerHTML=table(["Bot","Time","Coin","Exit","Return","Held"],(z_q.recent||[]).map(r=>{const c=trow(r.bot,r);return [z_B[r.bot],c[0],c[1],c[3],c[4],c[5]]}));
 const z_k5=z_q.kept5||{},z_s5=z_q.skipped5||{};
 $("m78s").innerHTML=`<div class="small mute" style="padding:4px 0 8px">M5 on coins M7 took: ${z_k5.n?pct(z_k5.mean)+" ("+z_k5.n+")":DASH} &middot; M5 on coins M7 skipped: ${z_s5.n?pct(z_s5.mean)+" ("+z_s5.n+")":DASH} &middot; the skip helps if the skipped side is worse</div>`+
  table(["Time","Coin","M5 exit","M5","M7 decision","M7"],(z_q.skip_rows||[]).map(r=>[tm(r.t),`<a class="coin" href="/trades#m5|${esc(r.m)}">${esc(r.name)}</a>`,esc(r.x5),pct(r.r5),z_DEC[r.decision]||esc(r.decision),r.r7!=null?pct(r.r7):DASH]));
 $("m78d").innerHTML=table(["Time","Coin","Market cap","Cluster","Serial","Decision"],(z_q.decisions||[]).map(r=>[tm(r.t),esc((r.name||r.m.slice(0,6)).slice(0,22)),r.mc?"$"+r.mc.toLocaleString():DASH,
  r.cluster!=null?`<b class="${r.cluster>=0.15?"neg":"mute"}">${(r.cluster*100).toFixed(0)}%</b>`:DASH,r.serial!=null?(r.serial>=1?`<b class="neg">${r.serial}</b>`:"0"):DASH,z_DEC[r.decision]||esc(r.decision)]));
 $("m4t").innerHTML=table(["Time","Coin","Score","Exit","Return","Held"],m4.recent.map(r=>trow("m4",r)));
 $("m4o").innerHTML=table(["Coin","Score","Entry","Now","Holders","Since"],m4.open.map(o=>[esc(o.name),o.score,"$"+(o.mc||0).toLocaleString(),pct(o.ret_now),o.holders!=null?o.holders+" / "+o.h_max:DASH,ago(o.t)]));
 $("m4x").innerHTML=table(["Exit","Trades"],Object.entries(m4.exits).sort((a,b)=>b[1]-a[1]).map(([k,v])=>[esc(k)+(k=="HDECL"?' <span class="mute small">holders fell 10%</span>':""),v]));
 const s4=m4.stats,vs=(m.fresh4||{});
 $("m4v").innerHTML=table(["Test","Now","Bar"],[["Trades",s4.n||0,"60 (or Nov 1)"],["Mean / trade",s4.n?pct(s4.mean):DASH,"&ge; +5%"],["t-stat",s4.n>1?s4.t.toFixed(2):DASH,"&ge; 3"],
  ["P&amp;L without best 2",m4.ex2!=null?(m4.ex2>=0?"+":"")+m4.ex2.toFixed(3)+" SOL":DASH,"&gt; 0"],["M1 over the same hours",vs.n?pct(vs.mean)+` (${vs.n})`:DASH,"M4 must beat it"]]);
 $("jobs").innerHTML=table(["Job","Status","Last line"],d.jobs.map(j=>[`<b>${esc(j.name)}</b><div class="mute small">${esc(j.desc)}</div>`,
  `<span class="pill ${j.status}">${j.status}</span><div class="mute small">${j.age_min==null?"":j.age_min<90?j.age_min+" min":(j.age_min/60).toFixed(1)+" h"}</div>`,`<code>${esc(j.last)}</code>`]));
 $("kal").innerHTML=table(["Bot","Hours","Rewards","Settled","Net","Open"],d.kalshi.map(k=>[k.name,k.hours,"$"+k.rewards.toFixed(2),
  `<span class="${k.settled>=0?"pos":"neg"}">$${k.settled.toFixed(2)}</span>`,`<span class="${k.rewards+k.settled>=0?"pos":"neg"}">$${(k.rewards+k.settled).toFixed(2)}</span>`,k.open]));
 $("c1").innerHTML=table(["Launches logged","Reached R 40","Trade files","Window"],[[d.c1.creates.toLocaleString(),d.c1.hot,d.c1.files,esc(d.c1.window)]]);
 const e=d.experiments;$("exn").textContent=`${e.total} rows · ${e.running} running`;
 $("exp").innerHTML=table(["Date","Experiment","What","Result","Verdict"],e.rows.map(r=>[`<span class="mute small">${r.date}</span>`,`<b>${esc(r.note)}</b>`,
  `<span class="small">${esc(r.strategy)}</span>`,`<span class="small">${esc(r.result)}</span>`,`<span class="pill ${r.state}">${r.state}</span><div class="small mute">${esc(r.verdict)}</div>`]))}
tick();setInterval(tick,15000);$("minsc").onchange=tick;
</script></body></html>""")
