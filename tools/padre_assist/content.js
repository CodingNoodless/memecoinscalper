// Tradeslave Assist: a floating panel on padre.gg for the token in the page URL. Read-only: it reads only the page URL
// (to find the token address) and never clicks, types into, or reads anything else on the page.
(() => {
  const POLL_MS = 3000;
  const B58 = /[1-9A-HJ-NP-Za-km-z]{32,44}/g;

  // ---------- token detection ----------
  function mintFromUrl() {
    const hits = (decodeURIComponent(location.href).match(B58) || []).filter(x => x.length >= 32);
    if (!hits.length) return null;
    return hits.find(x => x.endsWith("pump")) || hits[hits.length - 1];
  }

  // ---------- panel (shadow DOM so padre's CSS can't touch it, and ours can't touch padre) ----------
  const host = document.createElement("div");
  host.style.cssText = "position:fixed;z-index:2147483647;top:90px;right:16px;";
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `<style>
    .p{width:330px;max-height:78vh;overflow:auto;background:#0f141bee;color:#e6edf3;border:1px solid #2a3340;border-radius:12px;
       font:12.5px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;box-shadow:0 10px 30px #0008;backdrop-filter:blur(6px)}
    .h{display:flex;align-items:center;gap:8px;padding:8px 10px;border-bottom:1px solid #2a3340;cursor:move;user-select:none;position:sticky;top:0;background:#0f141b}
    .h b{flex:1;font-size:13px}.h button{background:none;border:0;color:#93a3b8;cursor:pointer;font-size:14px}
    .b{padding:8px 10px;display:grid;gap:10px}.mute{color:#93a3b8}.small{font-size:11px}
    .sec{border-top:1px solid #1f2733;padding-top:8px}.sec>div:first-child{font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;color:#93a3b8;margin-bottom:4px}
    .row{display:flex;justify-content:space-between;gap:8px}.ok{color:#34d399}.bad{color:#f87171}.warn{color:#fbbf24}
    .kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}.kpi{background:#161d27;border:1px solid #222c39;border-radius:8px;padding:5px 7px}
    .kpi span{display:block;font-size:10px;color:#93a3b8;text-transform:uppercase;letter-spacing:.05em}.kpi b{font-size:13px}
    .act{font-size:17px;font-weight:800;padding:6px 8px;border-radius:8px;text-align:center}
    .act.SELL{background:#f871712a;color:#f87171}.act.HOLD{background:#34d39922;color:#34d399}.act.TAKE{background:#fbbf2426;color:#fbbf24}.act.CONSIDER{background:#fbbf2418;color:#fbbf24}
    input{background:#161d27;border:1px solid #2a3340;color:#e6edf3;border-radius:6px;padding:4px 6px;font:inherit;width:100%;box-sizing:border-box}
    .btns{display:flex;gap:6px}.btns button{flex:1;background:#1c2533;border:1px solid #2a3340;color:#e6edf3;border-radius:6px;padding:5px;cursor:pointer;font:inherit}
    .btns button:hover{border-color:#7c9cff}ul{margin:2px 0 0 16px;padding:0}li{margin:1px 0}
  </style>
  <div class="p"><div class="h"><b>Tradeslave Assist</b><span class="mute small" id="st">starting</span><button id="min" title="collapse">&#8211;</button></div>
  <div class="b" id="body"><div class="mute">Open a token on padre.gg, or paste an address:</div><input id="paste" placeholder="token address"></div></div>`;
  document.documentElement.appendChild(host);
  const $ = id => root.getElementById(id);

  // drag + collapse (position remembered)
  chrome.storage.local.get(["pos", "min"], s => {
    if (s.pos) { host.style.top = s.pos.top; host.style.right = "auto"; host.style.left = s.pos.left; }
    if (s.min) $("body").style.display = "none";
  });
  let drag = null;
  root.querySelector(".h").addEventListener("mousedown", e => {
    if (e.target.tagName === "BUTTON") return;
    const r = host.getBoundingClientRect(); drag = { dx: e.clientX - r.left, dy: e.clientY - r.top }; e.preventDefault();
  });
  window.addEventListener("mousemove", e => {
    if (!drag) return;
    host.style.left = Math.max(0, e.clientX - drag.dx) + "px"; host.style.top = Math.max(0, e.clientY - drag.dy) + "px"; host.style.right = "auto";
  });
  window.addEventListener("mouseup", () => { if (drag) chrome.storage.local.set({ pos: { top: host.style.top, left: host.style.left } }); drag = null; });
  $("min").addEventListener("click", () => {
    const hide = $("body").style.display !== "none"; $("body").style.display = hide ? "none" : ""; chrome.storage.local.set({ min: hide });
  });

  // ---------- state ----------
  let pasted = null, last = null, pos = {}, busy = false;
  chrome.storage.local.get(["positions"], s => { pos = s.positions || {}; });
  const savePos = () => chrome.storage.local.set({ positions: pos });

  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const pct = (x, d = 0) => x == null ? "–" : `${(100 * x).toFixed(d)}%`;
  const usd = x => x == null ? "–" : "$" + (x >= 1e6 ? (x / 1e6).toFixed(2) + "M" : x >= 1e3 ? (x / 1e3).toFixed(1) + "k" : x.toFixed(0));
  const mark = ok => ok == null ? '<span class="mute">?</span>' : ok ? '<span class="ok">&#10003;</span>' : '<span class="bad">&#10007;</span>';
  const ago = s => s < 90 ? `${s | 0}s` : s < 5400 ? `${(s / 60) | 0}m` : `${(s / 3600).toFixed(1)}h`;

  function layout() {
    if (!$("full") || !root.getElementById("full").isConnected) {
      $("body").innerHTML = `<div id="rug"><div class="mute">checking rug risk&hellip;</div></div><div id="scores"></div><div id="full"><div class="mute small">loading full analysis&hellip;</div></div>`;
    }
  }

  function renderQuick(q) {
    layout();
    if (q.error) { $("rug").innerHTML = `<div class="warn">${esc(q.error)}</div>`; return; }
    const col = q.risk === "HIGH" ? "SELL" : q.risk === "MEDIUM" ? "TAKE" : "HOLD";
    const flags = q.flags.length ? `<ul>${q.flags.map(f => `<li class="${f.points >= 3 ? "bad" : "warn"}">${esc(f.text)}</li>`).join("")}</ul>` : '<div class="ok small">no rug flags in holders, launch buys or creator record</div>';
    $("rug").innerHTML = `<div class="act ${col}">RUG RISK: ${esc(q.risk)}</div>${flags}
      <div class="mute small">top-10 ${pct(q.top10)} &middot; top-1 ${pct(q.top1)} &middot; launch-second buyers ${pct(q.launch_share)} (${q.launch_wallets}) &middot; dev bought ${(+q.dev_buy_sol).toFixed(2)} SOL${q.dev_sold ? ", sold" : ""} &middot; checked in ${q.ms} ms</div>`;
  }

  function renderScores(sc) {
    if (!sc) return;
    const nar = sc.narrative == null ? `<span class="mute">${sc.status === "scoring" ? "scoring&hellip;" : esc(sc.why || "n/a")}</span>`
      : `<b class="${sc.narrative >= 7 ? "ok" : sc.narrative >= 4 ? "warn" : "bad"}">${sc.narrative}/10</b> <span class="mute small">${esc(sc.why || "")}</span>`;
    const run = sc.runner == null ? `<span class="mute">${sc.status !== "done" ? "scoring&hellip;" : esc(sc.runner_why || "n/a")}</span>`
      : `<b class="${sc.runner_pass ? "ok" : sc.runner >= 0.25 ? "warn" : "bad"}">${(+sc.runner).toFixed(2)}</b> <span class="mute small">${sc.runner_pass ? "M5 would buy" : "below M5's buy cut"} &middot; ${esc(sc.runner_why || "")}</span>`;
    $("scores").innerHTML = `<div class="sec"><div>Scores${sc.name ? " &middot; " + esc(sc.name) + (sc.sym ? " ($" + esc(sc.sym) + ")" : "") : ""}</div>
      <div class="row"><span>Narrative (M1's LLM)</span></div><div>${nar}</div>
      <div class="row" style="margin-top:4px"><span>Runner (M5 model)</span></div><div>${run}</div></div>`;
  }

  function render(m, d) {
    layout();
    renderScores(d.scores);
    if (d.error) { $("full").innerHTML = `<div class="warn">${esc(d.error)}</div><div class="mute small">${esc(m)}</div>`; return; }
    const f = d.feats || {}, x = d.exit, P = pos[m];
    const checks = d.checks.map(c => `<div class="row"><span>${mark(c.ok)} ${esc(c.name)}</span><span class="mute">${c.value == null ? "" : typeof c.value === "number" ? (+c.value).toFixed(2) : esc(c.value)}</span></div>`).join("");
    const risks = d.risks.length ? `<ul>${d.risks.map(r => `<li class="warn">${esc(r)}</li>`).join("")}</ul>` : '<span class="ok">none of our red flags</span>';
    const tracked = d.tracked.length ? d.tracked.map(t => `<div class="row"><span>${esc(t.label)}</span><span class="mute">${t.buys}b/${t.sells}s &middot; holds ${pct(t.hold, 1)} &middot; ${ago(t.ago_s)} ago</span></div>`).join("") : '<span class="mute">no tracked wallet in it</span>';
    const hype = [d.live_viewers != null ? `livestream, ${d.live_viewers} viewers` : null, ...(d.boosts || [])].filter(Boolean);
    let exitHtml;
    if (x) {
      const cls = x.action.split(" ")[0];
      exitHtml = `<div class="act ${cls}">${esc(x.action)}</div><div class="mute small" style="text-align:center">${esc(x.why)}</div>
        <div class="kpis"><div class="kpi"><span>Now</span><b>${x.r.toFixed(2)}&times;</b></div><div class="kpi"><span>After fees</span><b class="${x.net >= 0 ? "ok" : "bad"}">${x.net >= 0 ? "+" : ""}${pct(x.net, 0)}</b></div>
        <div class="kpi"><span>Peak</span><b>${x.peak.toFixed(2)}&times;</b></div><div class="kpi"><span>Stop</span><b>${x.stop.toFixed(2)}&times;</b></div>
        <div class="kpi"><span>Act / peak</span><b>${x.act60}/${x.act_max}</b></div><div class="kpi"><span>Flow 60 s</span><b class="${x.flow60 >= 0 ? "ok" : "bad"}">${x.flow60.toFixed(1)}</b></div></div>
        <div class="mute small">${esc(x.mode)} &middot; entry ${usd(P.entry_mc)} ${P.t ? "at " + new Date(P.t * 1000).toLocaleTimeString() : ""}</div>
        <div class="btns"><button id="clr">Clear position</button></div>`;
    } else {
      exitHtml = `<div class="mute small">Bought it? Record your entry and the helper tracks the stop and ratchet (M12r rules).</div>
        <div class="btns"><button id="now">I bought now (${usd(d.mc_usd)})</button></div>
        <input id="emc" placeholder="or type your entry market cap in $, then Enter">`;
    }
    $("full").innerHTML = `
      <div class="kpis"><div class="kpi"><span>Mcap</span><b>${usd(d.mc_usd)}</b></div><div class="kpi"><span>Age</span><b>${ago(d.age_s)}</b></div><div class="kpi"><span>Holders</span><b>${d.holders}</b></div>
      <div class="kpi"><span>Buyers 30 s</span><b>${f.buyers_30 ?? "–"}</b></div><div class="kpi"><span>Accel</span><b class="${(f.accel || 0) > 4 ? "bad" : ""}">${f.accel != null ? (+f.accel).toFixed(1) + "&times;" : "–"}</b></div><div class="kpi"><span>Flow 30 s</span><b class="${(f.flow_30 || 0) >= 0 ? "ok" : "bad"}">${f.flow_30 != null ? (+f.flow_30).toFixed(1) : "–"}</b></div></div>
      ${d.partial ? '<div class="warn small">Partial history (last 30 min from Helius): holder and creator figures may be incomplete. The feed is recording it now.</div>' : ""}
      <div class="sec"><div>Exit helper</div>${exitHtml}</div>
      <div class="sec"><div>Bot checks</div>${checks}<div class="mute small">funding lookup: ${esc(d.funding.status)}</div></div>
      <div class="sec"><div>Risk flags</div>${risks}<div class="mute small">top-10 ${pct(d.top10)} &middot; top-1 ${pct(d.top1)} &middot; creator holds ${pct(d.creator_hold, 1)}${d.creator_coins ? " &middot; creator coins seen " + d.creator_coins : ""}</div></div>
      <div class="sec"><div>Tracked wallets &amp; hype</div>${tracked}<div class="mute small" style="margin-top:4px">${hype.length ? esc(hype.join(" · ")) : "no livestream or boost"}</div></div>
      <div class="mute small">${esc(m.slice(0, 6))}&hellip;${esc(m.slice(-4))} &middot; not financial advice: our paper results are weak evidence</div>`;
    const now = $("now"), emc = $("emc"), clr = $("clr");
    if (now) now.onclick = () => { pos[m] = { entry_mc: d.mc_usd, t: Date.now() / 1000 }; savePos(); tick(); };
    if (emc) emc.onkeydown = e => { if (e.key === "Enter" && +emc.value > 0) { pos[m] = { entry_mc: +emc.value, t: Date.now() / 1000 }; savePos(); tick(); } };
    if (clr) clr.onclick = () => { delete pos[m]; savePos(); tick(); };
  }

  function tick() {
    const m = mintFromUrl() || pasted;
    if (!m) {
      $("st").textContent = "no token";
      const inp = $("paste");
      if (inp && !inp.onkeydown) inp.onkeydown = e => { if (e.key === "Enter") { pasted = (inp.value.match(B58) || [null])[0]; tick(); } };
      return;
    }
    if (root.activeElement && root.activeElement.id === "emc") return; // don't redraw while typing an entry
    const P = pos[m];
    if (m !== last) {                                   // a new coin: rug check first (~1 s), then the full analysis
      last = m; $("body").innerHTML = ""; layout();
      chrome.runtime.sendMessage({ type: "quick", m }, res => {
        if (res && res.ok && last === m) renderQuick(res.d);
        else if (res && !res.ok && last === m) $("rug").innerHTML = `<div class="warn">${esc(res.error)}</div>`;
      });
    }
    if (busy) return;
    busy = true;
    chrome.runtime.sendMessage({ type: "assist", m, entry_mc: P && P.entry_mc, t_entry: P && P.t }, res => {
      busy = false;
      if (!res) { $("st").textContent = "extension reloading"; return; }
      if (!res.ok) { $("st").textContent = "retrying: " + res.error.split(".")[0]; return; }   // keep the last good view
      if (last !== m) return;
      $("st").textContent = "updated " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
      render(m, res.d);
    });
  }
  tick();
  setInterval(tick, POLL_MS);
})();
