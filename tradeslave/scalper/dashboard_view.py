"""Scalper page for the local dashboard (served at / ; the momentum view moved to /momentum)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from ..state import STATE_DIR, load_json


def _trades() -> list[dict]:
    path = STATE_DIR / "scalp_trades.jsonl"
    rows, seen = [], set()
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            trade_id = row.get("trade_id")
            if not isinstance(row, dict) or trade_id in seen:
                continue
            seen.add(trade_id)
            rows.append(row)
    rows.sort(key=lambda r: r.get("exit_t", 0))
    return rows


def _summary(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"trades": 0}
    pnls = [r["pnl_sol"] for r in rows]
    wins = [r for r in rows if r["pnl_sol"] > 0]
    losses = [r for r in rows if r["pnl_sol"] <= 0]
    gross_loss = -sum(r["pnl_sol"] for r in losses)
    span_h = max((rows[-1]["exit_t"] - rows[0]["entry_t"]) / 3600, 1 / 60)
    return {
        "trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / n,
        "pnl_sol": sum(pnls),
        "avg_pnl_pct": sum(r["pnl_pct"] for r in rows) / n,
        "avg_win_pct": sum(r["pnl_pct"] for r in wins) / len(wins) if wins else None,
        "avg_loss_pct": sum(r["pnl_pct"] for r in losses) / len(losses) if losses else None,
        "expectancy_sol": sum(pnls) / n,
        "profit_factor": sum(r["pnl_sol"] for r in wins) / gross_loss if gross_loss > 0 else None,
        "avg_hold_s": sum(r["held_s"] for r in rows) / n,
        "trades_per_hour": n / span_h,
    }


def _group(rows: list[dict], key: str) -> list[dict]:
    groups: dict[str, dict] = {}
    for r in rows:
        g = groups.setdefault(str(r.get(key)), {"name": str(r.get(key)), "n": 0, "wins": 0, "pnl_sol": 0.0})
        g["n"] += 1
        g["wins"] += r["pnl_sol"] > 0
        g["pnl_sol"] += r["pnl_sol"]
    return sorted(groups.values(), key=lambda g: -g["n"])


def build_scalper_snapshot() -> dict:
    now = time.time()
    wallet = load_json("scalp_wallet.json", {}) or {}
    rows = _trades()
    today = datetime.now(timezone.utc).date().isoformat()
    today_rows = [r for r in rows if datetime.fromtimestamp(r["exit_t"], tz=timezone.utc).date().isoformat() == today]
    start = wallet.get("paper_start_sol") or (rows[0]["balance_sol"] - rows[0]["pnl_sol"] if rows else 1.0)
    first_balance = rows[0]["balance_sol"] - rows[0]["pnl_sol"] if rows else start
    series = [{"t": rows[0]["entry_t"], "balance": first_balance}] if rows else []
    series += [{"t": r["exit_t"], "balance": r["balance_sol"], "pnl_sol": r["pnl_sol"], "pnl_pct": r["pnl_pct"],
                "symbol": r.get("symbol"), "reason": r.get("exit_reason")} for r in rows]
    positions = []
    for p in wallet.get("open_positions", []):
        positions.append({**p, "held_s": round(now - p.get("entry_t", now), 1)})
    updated = wallet.get("updated_at")
    equity = wallet.get("equity_sol", wallet.get("balance_sol"))
    return {
        "now": now,
        "engine_online": bool(updated and now - updated < 15),
        "updated_at": updated,
        "started_at": wallet.get("started_at"),
        "wallet": {
            "balance_sol": wallet.get("balance_sol"), "equity_sol": equity, "start_sol": start,
            "return_pct": (equity / start - 1.0) if equity is not None and start else None,
            "day_pnl_sol": wallet.get("day_pnl_sol"), "fees_paid_sol": wallet.get("fees_paid_sol"),
            "failed_tx": wallet.get("failed_tx"), "watched": wallet.get("watched"),
        },
        "all": _summary(rows),
        "today": _summary(today_rows),
        "by_exit": _group(rows, "exit_reason"),
        "series": series[-2000:],
        "trades": [{k: r.get(k) for k in ("exit_t", "symbol", "mint", "kind", "pnl_pct", "pnl_sol", "held_s",
                                          "exit_reason", "entry_details")} for r in rows[-60:]][::-1],
        "positions": positions,
        "watchlist": wallet.get("watchlist", [])[:40],
        "blocks": wallet.get("entry_block_reasons", {}),
        "engine": wallet.get("stats", {}),
        "settings": wallet.get("settings", {}),
    }


def scalper_html(base_css: str) -> str:
    return SCALPER_HTML.replace("/*BASE_CSS*/", base_css)


SCALPER_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scalper Monitor</title><style>/*BASE_CSS*/
:root{--gain:#2ea676;--loss:#ea5f55;--line-series:#7dc9ff}
.nav{display:flex;gap:8px;align-items:center}.nav a{color:var(--muted);text-decoration:none;font-size:12px;border:1px solid var(--line);border-radius:999px;padding:6px 10px}.nav a.on{color:var(--text);background:var(--panel2)}
.cards6{display:grid;grid-template-columns:repeat(6,minmax(120px,1fr));gap:12px;margin-bottom:14px}
.gain{color:var(--gain)}.loss{color:var(--loss)}.off{color:var(--amber)}
.charts{display:grid;grid-template-columns:1.4fr 1fr;gap:14px}.charts>*,.three>*,.lower>*,.cards6>*{min-width:0}
.chartbox{position:relative}.tip{position:absolute;pointer-events:none;background:#0b1110ee;border:1px solid var(--line);border-radius:8px;padding:7px 9px;font-size:12px;display:none;white-space:nowrap;z-index:2}
.tip b{font-weight:600}.three{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-top:14px}
.tag{display:inline-block;border:1px solid var(--line);border-radius:6px;padding:1px 6px;font-size:11px;color:var(--muted)}
.tag.bad{color:var(--loss);border-color:#5a2e2b}.tag.ok{color:var(--gain);border-color:#24493b}
.kv{display:grid;grid-template-columns:1fr auto;gap:6px 12px;font-size:12px}.kv div:nth-child(odd){color:var(--muted)}
a.mint{color:var(--blue);text-decoration:none}
@media(max-width:1100px){.cards6{grid-template-columns:repeat(3,1fr)}.charts,.three{grid-template-columns:1fr}}
@media(max-width:600px){.cards6{grid-template-columns:repeat(2,1fr)}.top{flex-wrap:wrap;gap:10px}.nav{flex-wrap:wrap}}
</style></head><body><main>
<header class="top"><div class="brand"><span id="orb" class="orb"></span><div><div class="eyebrow">Tradeslave · launch-dip scalper · paper</div><h1>Scalper monitor</h1></div></div>
<div><div class="nav"><a class="on" href="/">Scalper</a><a href="/momentum">Momentum bot</a></div><div id="updated" class="updated" style="margin-top:7px">Connecting…</div></div></header>
<section class="cards6">
<div class="card"><div class="label">Equity</div><div id="equity" class="value">—</div><div id="ret" class="sub">—</div></div>
<div class="card"><div class="label">Realized P&amp;L</div><div id="pnl" class="value">—</div><div id="today" class="sub">Today: —</div></div>
<div class="card"><div class="label">Trades · win rate</div><div id="trades" class="value">—</div><div id="wl" class="sub">—</div></div>
<div class="card"><div class="label">Avg trade (net)</div><div id="avg" class="value">—</div><div id="avgwl" class="sub">—</div></div>
<div class="card"><div class="label">Pace</div><div id="pace" class="value">—</div><div id="hold" class="sub">—</div></div>
<div class="card"><div class="label">Watching now</div><div id="watching" class="value">—</div><div id="openpos" class="sub">—</div></div>
</section>
<section class="charts">
<div class="panel"><div class="panel-head"><h2>Paper balance (SOL) after each trade</h2><span class="eyebrow" id="pf"></span></div><div class="chartbox"><canvas id="bal"></canvas><div class="tip" id="balTip"></div></div></div>
<div class="panel"><div class="panel-head"><h2>Net result per trade (last 80)</h2><span class="eyebrow">above line = gain · below = loss</span></div><div class="chartbox"><canvas id="bars"></canvas><div class="tip" id="barTip"></div></div></div>
</section>
<section class="three">
<div class="panel"><div class="panel-head"><h2>Open positions</h2></div><div id="positions" class="event"></div></div>
<div class="panel"><div class="panel-head"><h2>Exits by reason</h2></div><div id="exits" class="table-wrap"></div></div>
<div class="panel"><div class="panel-head"><h2>Why it isn't buying</h2><span class="eyebrow">entry checks that said no</span></div><div id="blocks" class="table-wrap"></div></div>
</section>
<section class="lower">
<div class="panel"><div class="panel-head"><h2>Watchlist</h2><span class="eyebrow">price vs opening · safety</span></div><div id="watch" class="table-wrap"></div></div>
<div class="panel"><div class="panel-head"><h2>Recent trades</h2></div><div id="tradeTable" class="table-wrap"></div></div>
</section>
<section class="lower"><div class="panel"><div class="panel-head"><h2>Rules in force</h2></div><div id="settings" class="kv"></div></div>
<div class="panel"><div class="panel-head"><h2>Engine health</h2></div><div id="health" class="kv"></div></div></section>
<div class="footer">Refreshes every 2 seconds · paper fills: pump.fun curve math, 1.25% fee per side, network fee, ~0.8 s latency, simulated failed transactions</div></main>
<script>
const $=id=>document.getElementById(id);let snap=null;
const sol=(n,d=4)=>n==null?'—':`${n>=0?'+':'−'}${Math.abs(n).toFixed(d)} SOL`;
const solp=(n,d=4)=>n==null?'—':`${n.toFixed(d)} SOL`;
const pct=(n,d=1)=>n==null?'—':`${n>=0?'+':'−'}${Math.abs(n*100).toFixed(d)}%`;
const cls=n=>n==null?'':n>0?'gain':n<0?'loss':'';
function safe(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
const ago=t=>{if(!t)return'—';const s=Math.max(0,Date.now()/1000-t);return s<60?`${s.toFixed(0)}s`:s<3600?`${Math.floor(s/60)}m`:`${Math.floor(s/3600)}h ${Math.floor(s%3600/60)}m`};
const hhmm=t=>new Date(t*1000).toLocaleTimeString([], {hour:'numeric',minute:'2-digit',second:'2-digit'});
const reasonText={take_profit:'Take profit',stop_loss:'Stop loss',time_stop:'Time limit',trailing_giveback:'Gave back gains',dump_detected:'Sudden dump',dev_sold:'Dev sold',support_broken:'Fell below opening',curve_complete:'Graduated'};
const blockText={too_young:'Token too new',too_old:'Token too old',no_launch_spike_yet:'No launch spike yet',above_buy_zone:'Price above buy zone',below_buy_zone:'Price below buy zone',still_selling:'Sellers still active',waiting_for_uptick:'Waiting for uptick',sniper_window_open:'Sniper window open',snipers_hold_too_much:'Snipers hold too much',dev_holds_too_much:'Dev holds too much',dev_sold:'Dev sold',floor_broken:'Fell below opening',serial_launcher:'Serial launcher',market_inactive:'Token went quiet',curve_complete:'Graduated',token_entry_limit:'Token entry limit',token_loss_limit:'Token loss limit',reentry_cooldown:'Cooldown',max_open_positions:'Max positions open',daily_loss_limit:'Daily loss limit',max_exposure:'Max exposure',insufficient_balance:'Low balance'};
function canvasCtx(c){const r=c.getBoundingClientRect(),d=devicePixelRatio||1;c.width=Math.max(1,r.width*d);c.height=Math.max(1,r.height*d);const x=c.getContext('2d');x.scale(d,d);return[x,r.width,r.height]}
function grid(x,w,h,p,lo,hi,fmt){x.font='11px system-ui';x.textAlign='right';for(let i=0;i<4;i++){const y=p.t+i*(h-p.t-p.b)/3,v=hi-i*(hi-lo)/3;x.strokeStyle='#22322d';x.lineWidth=1;x.beginPath();x.moveTo(p.l,y);x.lineTo(w-p.r,y);x.stroke();x.fillStyle='#93a89e';x.fillText(fmt(v),p.l-8,y+4)}}
let balGeom=null,barGeom=null;
function drawBalance(){const c=$('bal'),[x,w,h]=canvasCtx(c),p={l:62,r:14,t:12,b:24},pts=snap?.series||[];x.clearRect(0,0,w,h);balGeom=null;
if(pts.length<2){x.fillStyle='#93a89e';x.font='13px system-ui';x.textAlign='center';x.fillText('No closed trades yet',w/2,h/2);return}
let lo=Math.min(...pts.map(q=>q.balance)),hi=Math.max(...pts.map(q=>q.balance));const pad=Math.max((hi-lo)*.15,.002);lo-=pad;hi+=pad;
const t0=pts[0].t,t1=pts[pts.length-1].t||t0+1,px=t=>p.l+(t-t0)/Math.max(t1-t0,1)*(w-p.l-p.r),py=v=>p.t+(hi-v)/(hi-lo)*(h-p.t-p.b);
grid(x,w,h,p,lo,hi,v=>v.toFixed(3));const start=snap.wallet.start_sol;if(start>lo&&start<hi){x.setLineDash([4,4]);x.strokeStyle='#5b7068';x.beginPath();x.moveTo(p.l,py(start));x.lineTo(w-p.r,py(start));x.stroke();x.setLineDash([])}
x.strokeStyle=getComputedStyle(document.documentElement).getPropertyValue('--line-series');x.lineWidth=2;x.lineJoin='round';x.beginPath();pts.forEach((q,i)=>{const X=px(q.t),Y=py(q.balance);i?x.lineTo(X,Y):x.moveTo(X,Y)});x.stroke();
x.fillStyle='#93a89e';x.textAlign='left';x.fillText(hhmm(t0),p.l,h-6);x.textAlign='right';x.fillText(hhmm(t1),w-p.r,h-6);balGeom={pts,px,py,p,w,h}}
function drawBars(){const c=$('bars'),[x,w,h]=canvasCtx(c),p={l:46,r:10,t:12,b:14},rows=(snap?.series||[]).filter(q=>q.pnl_pct!=null).slice(-80);x.clearRect(0,0,w,h);barGeom=null;
if(!rows.length){x.fillStyle='#93a89e';x.font='13px system-ui';x.textAlign='center';x.fillText('No closed trades yet',w/2,h/2);return}
const m=Math.max(.06,...rows.map(r=>Math.abs(r.pnl_pct)))*1.1,lo=-m,hi=m,py=v=>p.t+(hi-v)/(hi-lo)*(h-p.t-p.b);grid(x,w,h,p,lo,hi,v=>`${(v*100).toFixed(0)}%`);
const zero=py(0);x.strokeStyle='#5b7068';x.beginPath();x.moveTo(p.l,zero);x.lineTo(w-p.r,zero);x.stroke();
const slot=(w-p.l-p.r)/Math.max(rows.length,30),bw=Math.max(2,Math.min(12,slot-2)),cs=getComputedStyle(document.documentElement);
rows.forEach((r,i)=>{const X=p.l+i*slot+(slot-bw)/2,Y=py(r.pnl_pct),top=Math.min(Y,zero),ht=Math.max(1,Math.abs(Y-zero));x.fillStyle=r.pnl_pct>=0?cs.getPropertyValue('--gain'):cs.getPropertyValue('--loss');x.beginPath();x.roundRect?x.roundRect(X,top,bw,ht,r.pnl_pct>=0?[3,3,0,0]:[0,0,3,3]):x.rect(X,top,bw,ht);x.fill()});barGeom={rows,slot,p}}
$('bal').addEventListener('mousemove',e=>{const g=balGeom,tip=$('balTip');if(!g){tip.style.display='none';return}const r=e.target.getBoundingClientRect(),mx=e.clientX-r.left;let best=g.pts[0],bd=1e9;for(const q of g.pts){const d=Math.abs(g.px(q.t)-mx);if(d<bd){bd=d;best=q}}
tip.style.display='block';tip.innerHTML=`<b>${solp(best.balance)}</b><br>${hhmm(best.t)}${best.symbol?`<br>${safe(best.symbol)} ${pct(best.pnl_pct)} · ${safe(reasonText[best.reason]||best.reason)}`:''}`;tip.style.left=Math.min(g.px(best.t)+12,r.width-170)+'px';tip.style.top=Math.max(0,g.py(best.balance)-40)+'px'});
$('bal').addEventListener('mouseleave',()=>$('balTip').style.display='none');
$('bars').addEventListener('mousemove',e=>{const g=barGeom,tip=$('barTip');if(!g){tip.style.display='none';return}const r=e.target.getBoundingClientRect(),i=Math.floor((e.clientX-r.left-g.p.l)/g.slot);const q=g.rows[i];if(!q){tip.style.display='none';return}
tip.style.display='block';tip.innerHTML=`<b>${safe(q.symbol)}</b> ${pct(q.pnl_pct)}<br>${sol(q.pnl_sol,5)}<br>${safe(reasonText[q.reason]||q.reason)} · ${hhmm(q.t)}`;tip.style.left=Math.min(e.clientX-r.left+12,r.width-170)+'px';tip.style.top='10px'});
$('bars').addEventListener('mouseleave',()=>$('barTip').style.display='none');
function table(head,rows){return rows.length?`<table><thead><tr>${head.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table>`:'<div class="event">Nothing yet.</div>'}
function render(d){snap=d;const w=d.wallet,a=d.all,t=d.today;
$('orb').style.background=d.engine_online?'var(--green)':'var(--amber)';
$('updated').innerHTML=d.engine_online?`Engine live · updated ${ago(d.updated_at)} ago · up ${ago(d.started_at)}`:`<span class="off">Engine offline${d.updated_at?` · last update ${ago(d.updated_at)} ago`:''} · run <code>uv run python scalp.py</code></span>`;
$('equity').textContent=solp(w.equity_sol);$('ret').innerHTML=`<span class="${cls(w.return_pct)}">${pct(w.return_pct,2)}</span> vs ${solp(w.start_sol,2)} start`;
$('pnl').textContent=sol(a.pnl_sol);$('pnl').className='value '+cls(a.pnl_sol);$('today').textContent=`Today: ${sol(t.pnl_sol)} · ${t.trades||0} trades`;
$('trades').textContent=a.trades?`${a.trades} · ${(a.win_rate*100).toFixed(0)}%`:'0';$('wl').textContent=a.trades?`${a.wins} wins · ${a.losses} losses`:'No closed trades yet';
$('avg').textContent=pct(a.avg_pnl_pct,2);$('avg').className='value '+cls(a.avg_pnl_pct);$('avgwl').textContent=a.trades?`wins ${pct(a.avg_win_pct)} · losses ${pct(a.avg_loss_pct)}`:'—';
$('pace').textContent=a.trades?`${a.trades_per_hour.toFixed(1)}/h`:'—';$('hold').textContent=a.trades?`avg hold ${a.avg_hold_s.toFixed(1)}s · fees ${solp(w.fees_paid_sol)}`:'—';
$('watching').textContent=w.watched??'—';$('openpos').textContent=`${d.positions.length} open position${d.positions.length===1?'':'s'}`;
$('pf').textContent=a.profit_factor==null?'':`profit factor ${a.profit_factor.toFixed(2)} · expectancy ${sol(a.expectancy_sol,5)}/trade`;
$('positions').innerHTML=d.positions.length?table(['Token','Net now','Peak','vs opening','Held'],d.positions.map(p=>`<tr><td>${safe(p.symbol)}</td><td class="${cls(p.last_pnl_pct)}">${pct(p.last_pnl_pct)}</td><td>${pct(p.peak_pnl_pct)}</td><td>${pct(p.vs_opening)}</td><td>${p.held_s.toFixed(0)}s</td></tr>`)):'<div class="event">Flat. Waiting for a token to return to its opening price.</div>';
$('exits').innerHTML=table(['Reason','Trades','Won','Net'],d.by_exit.map(g=>`<tr><td>${safe(reasonText[g.name]||g.name)}</td><td>${g.n}</td><td>${g.wins}</td><td class="${cls(g.pnl_sol)}">${sol(g.pnl_sol)}</td></tr>`));
const bl=Object.entries(d.blocks||{});const total=bl.reduce((s,[,n])=>s+n,0)||1;$('blocks').innerHTML=table(['Check','Times','Share'],bl.slice(0,10).map(([k,n])=>`<tr><td>${safe(blockText[k]||k)}</td><td>${n}</td><td>${(n/total*100).toFixed(0)}%</td></tr>`));
$('watch').innerHTML=table(['Token','Age','vs opening','Spike','Dev','Snipers','Status'],(d.watchlist||[]).slice(0,25).map(r=>`<tr><td><a class="mint" href="https://pump.fun/coin/${encodeURIComponent(r.mint)}" target="_blank" rel="noopener">${safe(r.symbol)}</a></td><td>${r.age_s.toFixed(0)}s</td><td class="${cls(r.vs_opening)}">${pct(r.vs_opening)}</td><td>${pct(r.rise,0)}</td><td>${(r.dev_supply_pct*100).toFixed(1)}%</td><td>${r.early_supply_pct==null?'…':(r.early_supply_pct*100).toFixed(0)+'%'}</td><td>${r.safety?`<span class="tag bad">${safe(blockText[r.safety]||r.safety)}</span>`:r.status==='in position'?'<span class="tag ok">In position</span>':`<span class="tag">${safe(blockText[r.status]||r.status||'—')}</span>`}</td></tr>`));
$('tradeTable').innerHTML=table(['Closed','Token','Net','SOL','Held','Exit'],d.trades.slice(0,30).map(r=>`<tr><td>${hhmm(r.exit_t)}</td><td>${safe(r.symbol)}</td><td class="${cls(r.pnl_pct)}">${pct(r.pnl_pct)}</td><td class="${cls(r.pnl_sol)}">${sol(r.pnl_sol,5)}</td><td>${r.held_s.toFixed(1)}s</td><td>${safe(reasonText[r.exit_reason]||r.exit_reason)}</td></tr>`));
const s=d.settings||{};$('settings').innerHTML=Object.keys(s).length?[
['Entry',`${s.entry_mode} · confirm: ${s.entry_confirm}`],['Launch spike needed',pct(s.min_initial_rise_pct,0)],['Buy zone vs opening',`${pct(-s.zone_below_pct,0)} to ${pct(s.zone_above_pct,0)}`],['Support broken below',pct(-s.rug_floor_pct,0)],
['Max dev bag',`${(s.max_dev_supply_pct*100).toFixed(0)}% of supply`],['Max sniper share (first 3s)',`${(s.max_early_supply_pct*100).toFixed(0)}%`],['Take profit / stop (net)',`${pct(s.take_profit_pct,0)} / ${pct(-s.stop_loss_pct,0)}`],['Time limit',`${s.max_hold_s}s`],['Size · max open',`${s.trade_size_sol} SOL · ${s.max_open_positions}`]].map(([k,v])=>`<div>${k}</div><div>${safe(v)}</div>`).join(''):'<div>Start the scalper to see its rules.</div><div></div>';
const e=d.engine||{};$('health').innerHTML=[['New tokens seen',e.created_seen],['Curve updates',e.curve_msgs],['Decode failures',e.decode_fail],['Dev dumps confirmed',e.dev_sells],['Creator-wallet sells seen',e.creator_sell_logs],['Skipped (watchlist full)',e.skipped_full],['Reconnects (PumpPortal / RPC)',`${e.pp_reconnects??'—'} / ${e.rpc_reconnects??'—'}`],['Failed transactions (sim)',w.failed_tx]].map(([k,v])=>`<div>${k}</div><div>${v??'—'}</div>`).join('');
drawBalance();drawBars()}
async function refresh(){try{const r=await fetch('/api/scalper',{cache:'no-store'});if(!r.ok)throw Error(`HTTP ${r.status}`);render(await r.json())}catch(e){$('updated').textContent=`Dashboard data unavailable · ${e.message}`}}
refresh();setInterval(refresh,2000);window.addEventListener('resize',()=>snap&&(drawBalance(),drawBars()));
</script></body></html>"""
