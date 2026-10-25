"""Read-only local dashboard for the paper trader."""

from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
from pathlib import Path
import time
from collections import deque
from urllib.parse import parse_qs, urlsplit

from .config import PAPER_NETWORK_FEE_USD, PAPER_STARTING_BALANCE
from .state import STATE_DIR, STATE_LOCK, load_json
from .lab_page import LAB_HTML
from .lab_view import build_lab_snapshot
from .label_page import LABEL_HTML
from .label_view import label_next, label_save
from .paper_view import paper_next, paper_save
from .watch_view import WATCH_HTML, live, watch_list
from .assist import assist_json, hot_json, quick_json
from .trade_view import TRADES_HTML, chart, trade_list
from .scalper.dashboard_view import build_scalper_snapshot, scalper_html


HOST = "127.0.0.1"
PORT = 8765
logger = logging.getLogger("dashboard")


def _read_jsonl(name: str) -> list[dict]:
    path = STATE_DIR / name
    records: deque[dict] = deque(maxlen=5000)
    if not path.exists():
        return records
    try:
        with path.open("r", encoding="utf-8") as source:
            for line in source:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # A concurrently appended final line can be incomplete if
                    # the process is interrupted; ignore that line only.
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        logger.exception("Could not read dashboard data file %s", name)
    return records


def _number(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _unique_trades(rows: list[dict]) -> list[dict]:
    """Ignore duplicate close records by trade ID without rewriting user data."""
    unique: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        trade_id = row.get("trade_id")
        if isinstance(trade_id, str) and trade_id:
            if trade_id in seen:
                continue
            seen.add(trade_id)
        unique.append(row)
    return unique


def _timestamp(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def _chart_points(events: list[dict], observations: list[dict], history: list[dict], wallet: dict) -> list[dict]:
    entries: dict[str, dict] = {}
    points: list[dict] = []

    for event in events:
        kind = event.get("event") or event.get("event_type") or event.get("type")
        trade_id = event.get("trade_id")
        at = event.get("at") or event.get("timestamp") or event.get("created_at")
        x = _timestamp(at)
        if x is None:
            continue
        balance = _number(event.get("wallet_balance"))
        if kind == "paper_entry" and trade_id:
            entries[str(trade_id)] = event
            invested = _number(event.get("invested_usd")) or 0.0
            if balance is not None:
                points.append({"at": x, "balance": balance, "equity": balance + invested})
        elif kind == "position_observation" and trade_id:
            entry = entries.get(str(trade_id))
            entry_cash = _number(entry.get("wallet_balance")) if entry else None
            proceeds = _number(event.get("estimated_net_proceeds_usd"))
            if entry_cash is not None and proceeds is not None:
                points.append({"at": x, "balance": entry_cash, "equity": entry_cash + proceeds})
        elif kind == "paper_exit" and balance is not None:
            points.append({"at": x, "balance": balance, "equity": balance})

    for trade in history:
        x = _timestamp(trade.get("closed_at"))
        balance = _number(trade.get("wallet_balance"))
        if x is not None and balance is not None:
            points.append({"at": x, "balance": balance, "equity": balance})

    active = wallet.get("active_trade") if isinstance(wallet, dict) else None
    current_balance = _number(wallet.get("balance")) if isinstance(wallet, dict) else None
    if current_balance is not None:
        if isinstance(active, dict):
            exit_value = _number(active.get("last_estimated_exit_usd"))
            equity = current_balance + exit_value if exit_value is not None else None
        else:
            equity = current_balance
        now = time.time()
        points.append({"at": now, "balance": current_balance, "equity": equity})

    points.sort(key=lambda point: point["at"])
    # Coalesce identical timestamps and guard response size for a monitor.
    unique = {round(point["at"], 3): point for point in points}
    ordered = sorted(unique.values(), key=lambda point: point["at"])
    if not ordered:
        ordered = [{"at": time.time(), "balance": PAPER_STARTING_BALANCE, "equity": PAPER_STARTING_BALANCE}]
    return ordered[-2000:]


def _tracked_capital(history: list[dict], active: dict | None, events: list[dict]) -> float:
    """Estimate starting capital plus deposits from recorded paper trades."""
    entries = {
        str(event.get("trade_id")): event
        for event in events
        if event.get("event") == "paper_entry" and event.get("trade_id")
    }
    closed = sorted(history, key=lambda row: _timestamp(row.get("closed_at")) or 0)
    opening_cash: float | None = None
    net_cash_flows = 0.0
    previous_cash: float | None = None
    for trade in closed:
        ending_cash = _number(trade.get("wallet_balance"))
        profit = _number(trade.get("profit_usd"))
        if ending_cash is None or profit is None:
            continue
        cash_before_trade = ending_cash - profit
        if opening_cash is None:
            opening_cash = cash_before_trade
        elif previous_cash is not None:
            net_cash_flows += cash_before_trade - previous_cash
        previous_cash = ending_cash

    if active:
        event = entries.get(str(active.get("trade_id")))
        cash_after_entry = _number(event.get("wallet_balance")) if event else None
        invested = _number(active.get("invested_usd"))
        fee = _number(active.get("entry_network_fee_usd"))
        cash_before_trade = (
            cash_after_entry + invested + (fee or 0.0)
            if cash_after_entry is not None and invested is not None
            else None
        )
        if cash_before_trade is not None:
            if opening_cash is None:
                opening_cash = cash_before_trade
            elif previous_cash is not None:
                net_cash_flows += cash_before_trade - previous_cash

    tracked = (opening_cash or PAPER_STARTING_BALANCE) + net_cash_flows
    return tracked if tracked > 0 and math.isfinite(tracked) else PAPER_STARTING_BALANCE


def build_snapshot() -> dict:
    """Read a consistent snapshot of paper state without changing it."""
    with STATE_LOCK:
        wallet = load_json("paper_wallet.json", {"balance": PAPER_STARTING_BALANCE, "active_trade": None})
        history = load_json("trade_history.json", [])
        candidates = load_json("active_candidates.json", {"candidates": [], "llm_decisions": []})
        events = _read_jsonl("pipeline_events.jsonl")
        observations = _read_jsonl("paper_observations.jsonl")

    if not isinstance(wallet, dict):
        wallet = {"balance": PAPER_STARTING_BALANCE, "active_trade": None}
    if not isinstance(history, list):
        history = []
    history = [trade for trade in history if isinstance(trade, dict)]
    history = _unique_trades(history)
    if not isinstance(candidates, dict):
        candidates = {"candidates": [], "llm_decisions": []}

    balance = _number(wallet.get("balance"))
    active = wallet.get("active_trade") if isinstance(wallet.get("active_trade"), dict) else None
    account_id = wallet.get("account_id")
    session_history = [row for row in history if account_id and row.get("account_id") == account_id]
    session_events = [event for event in events if account_id and event.get("account_id") == account_id]
    session_observations = [row for row in observations if account_id and row.get("account_id") == account_id]
    realized = sum(_number(row.get("profit_usd")) or 0.0 for row in session_history)
    wins = sum(1 for row in session_history if (_number(row.get("profit_usd")) or 0.0) > 0)
    losses = sum(1 for row in session_history if (_number(row.get("profit_usd")) or 0.0) < 0)
    exit_value = _number(active.get("last_estimated_exit_usd")) if active else None
    invested = _number(active.get("invested_usd")) if active else None
    entry_fee = _number(active.get("entry_network_fee_usd")) if active else 0.0
    unrealized = exit_value - invested - (entry_fee or 0.0) if exit_value is not None and invested is not None else None
    current_equity = balance + exit_value if balance is not None and exit_value is not None else balance if not active else None
    capital_base = (
        _number(wallet.get("starting_balance"))
        or _tracked_capital(session_history, active, session_events)
        if account_id
        else balance or PAPER_STARTING_BALANCE
    )
    latest_event = events[-1] if events else None
    last_scan = next((event for event in reversed(events) if event.get("component") == "scanner"), None)

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "paper_only": True,
        "wallet": {
            "balance_usd": balance,
            "equity_usd": current_equity,
            "realized_pnl_usd": realized,
            "unrealized_pnl_usd": unrealized,
            "tracked_capital_usd": capital_base,
            "return_pct": (current_equity / capital_base - 1.0) if current_equity is not None and capital_base else None,
            "closed_trades": len(session_history),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": wins / len(session_history) * 100.0 if session_history else None,
        },
        "active_trade": active,
        "candidate_count": len(candidates.get("candidates", [])) if isinstance(candidates.get("candidates"), list) else 0,
        "latest_event": latest_event,
        "latest_event_at": (latest_event or {}).get("at") or (latest_event or {}).get("timestamp"),
        "account_id": account_id,
        "legacy_trade_records_excluded": len(history) - len(session_history),
        "chart": _chart_points(session_events, session_observations, session_history, wallet),
        "recent_trades": session_history[-10:][::-1],
        "recent_observations": list(session_observations)[-20:][::-1],
        "event_count": len(events),
        "last_scan": last_scan,
    }


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paper Trader Monitor</title><style>
:root{color-scheme:dark;--bg:#0b1110;--panel:#121b19;--panel2:#172320;--line:#283a34;--text:#e7f0eb;--muted:#93a89e;--green:#61d6a2;--red:#ff7777;--amber:#f6c66d;--blue:#7dc9ff}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 18% 0%,#162923 0,#0b1110 55%);color:var(--text);font:15px/1.4 system-ui,-apple-system,Segoe UI,sans-serif;min-height:100vh}
main{max-width:1500px;margin:auto;padding:26px 30px 36px}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}.brand{display:flex;align-items:center;gap:12px}.orb{width:12px;height:12px;border-radius:50%;background:var(--green);box-shadow:0 0 16px #61d6a2aa}.eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:.14em;color:var(--muted)}h1{font-size:24px;margin:1px 0 0}.updated{color:var(--muted);font-size:12px;text-align:right}.pill{border:1px solid var(--line);padding:6px 10px;border-radius:999px;color:var(--green);font-size:11px;letter-spacing:.07em;text-transform:uppercase}
.cards{display:grid;grid-template-columns:repeat(5,minmax(130px,1fr));gap:12px;margin-bottom:14px}.card,.panel{background:linear-gradient(145deg,#15211e,#101715);border:1px solid var(--line);border-radius:14px;box-shadow:0 10px 26px #0002}.card{padding:16px 18px;min-height:94px}.label{font-size:12px;color:var(--muted)}.value{font-size:25px;font-weight:650;letter-spacing:-.03em;margin-top:8px}.sub{font-size:11px;color:var(--muted);margin-top:3px}.green{color:var(--green)}.red{color:var(--red)}.amber{color:var(--amber)}
.layout{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(310px,1fr);gap:14px}.panel{padding:18px 20px}.panel-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}.panel h2{margin:0;font-size:15px;font-weight:600}.switch{display:flex;gap:6px}.switch button{border:1px solid var(--line);color:var(--muted);background:transparent;border-radius:7px;padding:5px 9px;cursor:pointer;font:inherit;font-size:11px}.switch button.active{color:var(--text);background:var(--panel2);border-color:#47655a}canvas{display:block;width:100%;height:260px}.trade-top{display:flex;justify-content:space-between;align-items:flex-start}.symbol{font-size:25px;font-weight:700}.address{font:11px ui-monospace,monospace;color:var(--muted);margin-top:3px;overflow-wrap:anywhere}.pnl{font-size:20px;font-weight:700;text-align:right}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:18px}.stat{border-top:1px solid var(--line);padding-top:10px}.stat .label{font-size:11px}.stat .v{font-size:14px;margin-top:4px;overflow-wrap:anywhere}.empty{height:230px;display:grid;place-items:center;text-align:center;color:var(--muted);padding:30px}.wide{grid-column:1/-1}.lower{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:12px}th{text-align:left;color:var(--muted);font-weight:500;padding:7px;border-bottom:1px solid var(--line)}td{padding:9px 7px;border-bottom:1px solid #26342f;white-space:nowrap}.event{color:var(--muted);font-size:12px}.footer{text-align:right;font-size:10px;color:#708078;margin-top:12px}@media(max-width:1000px){.cards{grid-template-columns:repeat(3,1fr)}.layout{grid-template-columns:1fr}.lower{grid-template-columns:1fr}}@media(max-width:600px){main{padding:16px}.cards{grid-template-columns:repeat(2,1fr)}.value{font-size:21px}}
</style></head><body><main>
<header class="top"><div class="brand"><span class="orb"></span><div><div class="eyebrow">Tradeslave · paper account</div><h1>Trading monitor</h1></div></div><div><div style="display:flex;gap:8px;justify-content:flex-end"><a class="pill" style="color:var(--muted);text-decoration:none" href="/">Scalper</a><div class="pill">Momentum bot · paper</div></div><div id="updated" class="updated" style="margin-top:7px">Connecting…</div></div></header>
<section class="cards">
<div class="card"><div class="label">Cash balance</div><div id="balance" class="value">—</div><div class="sub">Uninvested paper USD</div></div>
<div class="card"><div class="label">Net worth estimate</div><div id="equity" class="value">—</div><div class="sub">Cash + estimated net exit quote</div></div>
<div class="card"><div class="label">Total return</div><div id="return" class="value">—</div><div id="returnSub" class="sub">On tracked paper capital</div></div>
<div class="card"><div class="label">Realized P&amp;L</div><div id="realized" class="value">—</div><div id="unrealized" class="sub">Unrealized: —</div></div>
<div class="card"><div class="label">Closed trades · win rate</div><div id="trades" class="value">—</div><div id="wlt" class="sub">— wins · — losses</div></div>
</section>
<section class="layout"><div class="panel"><div class="panel-head"><h2>Paper wallet over time</h2><div class="switch"><button id="balanceBtn" class="active">Cash + equity</button><button id="equityBtn">Equity</button></div></div><canvas id="chart"></canvas></div>
<div class="panel"><div class="panel-head"><h2>Active trade</h2><span id="candidate" class="eyebrow"></span></div><div id="active"></div></div></section>
<section class="lower"><div class="panel"><div class="panel-head"><h2>Recent paper trades</h2></div><div id="tradeTable" class="table-wrap"></div></div><div class="panel"><div class="panel-head"><h2>Pipeline status</h2></div><div id="pipeline" class="event">Waiting for state…</div><div class="sub" id="eventCount" style="margin-top:14px"></div></div></section>
<div class="footer">Refreshes every 5 seconds · Market price is indicative; P&amp;L uses the paper sell quote</div></main>
<script>
const $=id=>document.getElementById(id);let snapshot=null,mode='both';
const money=n=>n==null?'—':new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2}).format(n);
const pct=n=>n==null?'—':`${n>=0?'+':''}${(n*100).toFixed(1)}%`;
const short=s=>s?`${s.slice(0,6)}…${s.slice(-5)}`:'—';
const cls=n=>n==null?'':n>=0?'green':'red';
function safe(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function duration(iso){if(!iso)return'—';const sec=Math.max(0,Date.now()-Date.parse(iso))/1000;if(!Number.isFinite(sec))return'—';return sec<60?`${Math.floor(sec)}s`:sec<3600?`${Math.floor(sec/60)}m ${Math.floor(sec%60)}s`:`${Math.floor(sec/3600)}h ${Math.floor(sec%3600/60)}m`}
function renderTrade(t){if(!t){$('active').innerHTML='<div class="empty">No active position<br>Waiting for the next paper signal.</div>';return}
const pnl=t.last_estimated_pnl_pct;const qc=t.last_quote_chart||{};const chart=t.entry_chart_analysis||{};
$('active').innerHTML=`<div class="trade-top"><div><div class="symbol">${safe(t.symbol||'Token')}</div><div class="address" title="${safe(t.address)}">${safe(short(t.address))}</div></div><div class="pnl ${cls(pnl)}">${pct(pnl)}<div class="sub">unrealized · quote based</div></div></div><div class="grid">
<div class="stat"><div class="label">Invested</div><div class="v">${money(t.invested_usd)}</div></div><div class="stat"><div class="label">Estimated net exit</div><div class="v">${money(t.last_estimated_exit_usd)}</div></div>
<div class="stat"><div class="label">Market price · indicative</div><div class="v">${t.last_market_price_usd==null?'—':`$${Number(t.last_market_price_usd).toPrecision(7)}`}</div></div><div class="stat"><div class="label">Held</div><div class="v">${duration(t.opened_at)}</div></div>
<div class="stat"><div class="label">Peak quote value</div><div class="v">${money(t.peak_estimated_exit_usd)}</div></div><div class="stat"><div class="label">Entry score / model confidence</div><div class="v">${t.selection_score??'—'} / ${t.selector==='score'||t.llm_confidence==null?'—':`${t.llm_confidence}%`}</div></div>
<div class="stat"><div class="label">Exit quote trend</div><div class="v">EMA${qc.fast_period??'3'} ${qc.fast_ema_net_exit_usd==null?'—':money(qc.fast_ema_net_exit_usd)} / EMA${qc.slow_period??'8'} ${qc.slow_ema_net_exit_usd==null?'—':money(qc.slow_ema_net_exit_usd)}</div></div><div class="stat"><div class="label">Chart entry check</div><div class="v">${chart.passed===true?'Passed':chart.passed===false?'Rejected':'—'} · ${chart.candle_count??'—'} bars</div></div>
<div class="stat wide"><div class="label">Exit rules</div><div class="v">Stop ${t.stop_loss_pct==null?'—':pct(t.stop_loss_pct)} · trailing ${t.trailing_active?'active':'inactive'} · bearish quotes ${qc.bearish_confirmations??0}</div></div></div>`}
function chart(){const c=$('chart'),r=c.getBoundingClientRect(),dpr=devicePixelRatio||1;c.width=Math.max(1,r.width*dpr);c.height=Math.max(1,r.height*dpr);const x=c.getContext('2d');x.scale(dpr,dpr);const w=r.width,h=r.height,p={l:56,r:12,t:12,b:24},pts=snapshot?.chart||[];x.clearRect(0,0,w,h);if(!pts.length)return;const series=mode==='both'?['balance','equity']:['equity'];const vals=pts.flatMap(v=>series.map(k=>v[k])).filter(v=>Number.isFinite(v));if(vals.length<1)return;let lo=Math.min(...vals),hi=Math.max(...vals);if(hi-lo<.01){lo-=1;hi+=1}else{const pad=(hi-lo)*.12;lo-=pad;hi+=pad}const t0=pts[0].at,t1=pts[pts.length-1].at;const px=(t)=>p.l+(t1===t0 ? 0.5 :(t-t0)/(t1-t0))*(w-p.l-p.r),py=v=>p.t+(hi-v)/(hi-lo)*(h-p.t-p.b);
x.font='11px system-ui';x.textAlign='right';for(let i=0;i<4;i++){let y=p.t+i*(h-p.t-p.b)/3,val=hi-i*(hi-lo)/3;x.strokeStyle='#263a33';x.beginPath();x.moveTo(p.l,y);x.lineTo(w-p.r,y);x.stroke();x.fillStyle='#93a89e';x.fillText(money(val),p.l-8,y+4)}
for(const [index,key] of series.entries()){x.strokeStyle=key==='equity'?'#61d6a2':'#7dc9ff';x.lineWidth=2.2;x.beginPath();let started=false;for(const point of pts){const v=point[key];if(!Number.isFinite(v))continue;const xx=px(point.at),yy=py(v);if(!started){x.moveTo(xx,yy);started=true}else x.lineTo(xx,yy)}x.stroke();}
x.fillStyle='#93a89e';x.textAlign='left';x.fillText(new Date(t0*1000).toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}),p.l,h-5);x.textAlign='right';x.fillText(new Date(t1*1000).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'}),w-p.r,h-5);
}
function render(d){snapshot=d;const w=d.wallet;$('balance').textContent=money(w.balance_usd);$('equity').textContent=money(w.equity_usd);$('return').textContent=pct(w.return_pct);$('return').className='value '+cls(w.return_pct);$('returnSub').textContent=`On ${money(w.tracked_capital_usd)} tracked paper capital`;$('realized').textContent=money(w.realized_pnl_usd);$('realized').className='value '+cls(w.realized_pnl_usd);$('unrealized').textContent=`Unrealized: ${money(w.unrealized_pnl_usd)}`;$('trades').textContent=`${w.closed_trades} · ${w.win_rate_pct==null?'—':w.win_rate_pct.toFixed(0)+'%'}`;$('wlt').textContent=`${w.wins} wins · ${w.losses} losses`;$('candidate').textContent=`${d.candidate_count} queued`;$('updated').textContent=`Updated ${new Date(d.updated_at).toLocaleTimeString()}`;renderTrade(d.active_trade);chart();const e=d.latest_event,scan=d.last_scan,scanAge=scan?.at?Math.max(0,Math.floor((Date.now()-Date.parse(scan.at))/1000)):null,scanStatus=scanAge==null?'No scanner events':scanAge>90?'Scanner stale':'Scanner active';$('pipeline').innerHTML=`<span class="${scanAge>90?'amber':'green'}">${safe(scanStatus)}</span>${scanAge==null?'':` · ${scanAge<60?`${scanAge}s`:`${Math.floor(scanAge/60)}m`} ago`}<br>${e?`Latest: <b>${safe(e.event||e.event_type||'event')}</b>${e.symbol?` · ${safe(e.symbol)}`:''}<br>${safe(e.at||e.timestamp||'time unavailable')}<br>${e.message?safe(e.message):''}`:'No pipeline events recorded yet.'}`;$('eventCount').textContent=`${d.event_count} events · stats scoped to this paper account · ${d.paper_only?'paper state is read-only here':''}`;
const rows=d.recent_trades||[];$('tradeTable').innerHTML=rows.length?`<table><thead><tr><th>Closed</th><th>Token</th><th>P&amp;L</th><th>Reason</th></tr></thead><tbody>${rows.map(t=>`<tr><td>${safe(new Date(t.closed_at).toLocaleTimeString())}</td><td>${safe(t.symbol||'—')}</td><td class="${cls(t.profit_usd)}">${money(t.profit_usd)}</td><td>${safe(t.exit_reason||'—')}</td></tr>`).join('')}</tbody></table>`:'<div class="event">No closes recorded in this paper-account run.</div>'}
$('balanceBtn').onclick=()=>{mode='both';$('balanceBtn').classList.add('active');$('equityBtn').classList.remove('active');if(snapshot)chart()};$('equityBtn').onclick=()=>{mode='equity';$('equityBtn').classList.add('active');$('balanceBtn').classList.remove('active');if(snapshot)chart()};window.addEventListener('resize',()=>snapshot&&chart());
async function refresh(){try{const response=await fetch('/api/state',{cache:'no-store'});if(!response.ok)throw Error(`HTTP ${response.status}`);render(await response.json())}catch(e){$('updated').textContent=`Dashboard data unavailable · ${e.message}`}}refresh();setInterval(refresh,5000);
</script></body></html>"""


# The scalper page reuses the momentum page's stylesheet so both views match.
SCALPER_PAGE = scalper_html(HTML.split("<style>", 1)[1].split("</style>", 1)[0])


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "PaperDashboard/1.0"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlsplit(self.path).path
        if path == "/":
            body = LAB_HTML.encode("utf-8")
            content_type = "text/html; charset=utf-8"
        elif path == "/api/lab":
            try:
                body = json.dumps(build_lab_snapshot(), allow_nan=False, default=str).encode("utf-8")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                logger.exception("Lab snapshot failed")
                body = json.dumps({"error": f"Could not read lab state: {exc}"}).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        elif path == "/trades":
            body = TRADES_HTML.encode("utf-8")
            content_type = "text/html; charset=utf-8"
        elif path == "/label":
            body = LABEL_HTML.encode("utf-8")
            content_type = "text/html; charset=utf-8"
        elif path == "/watch":
            body = WATCH_HTML.encode("utf-8")
            content_type = "text/html; charset=utf-8"
        elif path == "/api/assist/hot":                          # padre.gg extension: coins to highlight on the trenches page
            try:
                body = json.dumps(hot_json(parse_qs(urlsplit(self.path).query)), allow_nan=False, default=str).encode("utf-8")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                logger.exception("Hot list failed")
                body = json.dumps({"error": f"Could not build the hot list: {exc}", "coins": []}).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        elif path == "/api/assist/quick":                        # padre.gg extension: rug check in ~1 s
            try:
                body = json.dumps(quick_json(parse_qs(urlsplit(self.path).query)), allow_nan=False, default=str).encode("utf-8")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                logger.exception("Quick check failed")
                body = json.dumps({"error": f"Could not check the token: {exc}"}).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        elif path == "/api/assist":                              # padre.gg extension (tools/padre_assist), read-only
            try:
                body = json.dumps(assist_json(parse_qs(urlsplit(self.path).query)), allow_nan=False, default=str).encode("utf-8")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                logger.exception("Assist failed")
                body = json.dumps({"error": f"Could not analyze the token: {exc}"}).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        elif path in ("/api/watch", "/api/live"):
            try:
                data = watch_list() if path == "/api/watch" else live(parse_qs(urlsplit(self.path).query).get("m", [""])[0])
                body = json.dumps(data, allow_nan=False, default=str).encode("utf-8")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                logger.exception("Watch view failed")
                body = json.dumps({"error": f"Could not read the watchlist: {exc}"}).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        elif path == "/api/paper/next":
            try:
                body = json.dumps(paper_next(), allow_nan=False, default=str).encode("utf-8")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                logger.exception("Paper view failed")
                body = json.dumps({"error": f"Could not load a coin: {exc}"}).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        elif path == "/api/label/next":
            try:
                body = json.dumps(label_next(), allow_nan=False, default=str).encode("utf-8")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                logger.exception("Label view failed")
                body = json.dumps({"error": f"Could not load a coin: {exc}"}).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        elif path in ("/api/trades", "/api/chart"):
            try:
                if path == "/api/trades":
                    data = trade_list()
                else:
                    q = parse_qs(urlsplit(self.path).query)
                    data = chart(q.get("v", [""])[0], q.get("m", [""])[0])
                body = json.dumps(data, allow_nan=False, default=str).encode("utf-8")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                logger.exception("Trade view failed")
                body = json.dumps({"error": f"Could not build the trade view: {exc}"}).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        elif path == "/scalper":
            body = SCALPER_PAGE.encode("utf-8")
            content_type = "text/html; charset=utf-8"
        elif path == "/momentum":
            body = HTML.encode("utf-8")
            content_type = "text/html; charset=utf-8"
        elif path == "/api/scalper":
            try:
                body = json.dumps(build_scalper_snapshot(), allow_nan=False).encode("utf-8")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                logger.exception("Scalper snapshot failed")
                body = json.dumps({"error": f"Could not read scalper state: {exc}"}).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        elif path == "/api/state":
            try:
                body = json.dumps(build_snapshot(), allow_nan=False).encode("utf-8")
            except (OSError, ValueError, TypeError) as exc:
                logger.exception("Dashboard snapshot failed")
                body = json.dumps({"error": f"Could not read paper state: {exc}"}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            content_type = "application/json; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API; only the local blind-label form writes
        route = urlsplit(self.path).path
        if route not in ("/api/label", "/api/paper"):
            self.send_error(404)
            return
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 10000)
            data = (paper_save if route == "/api/paper" else label_save)(json.loads(self.rfile.read(n) or b"{}"))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            logger.exception("Label save failed")
            data = {"error": f"Could not save the label: {exc}"}
        body = json.dumps(data, allow_nan=False, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        logger.info("%s - %s", self.address_string(), format % args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    logger.info("Paper trading dashboard available at http://%s:%s", HOST, PORT)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        logger.info("Dashboard stopping")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
