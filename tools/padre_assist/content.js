// Tradeslave Assist: a compact verdict board on padre.gg for the token in the page URL (rug risk, narrative, runner, hype,
// dead coin, and an optional exit helper). Read-only: it reads only the page URL (to find the token / pair address) and
// never clicks, types into, or reads anything else on the page.
(() => {
  const POLL_MS = 3000;
  const B58 = /[1-9A-HJ-NP-Za-km-z]{32,44}/g;

  function mintFromUrl() {
    const hits = (decodeURIComponent(location.href).match(B58) || []).filter(x => x.length >= 32);
    if (!hits.length) return null;
    return hits.find(x => x.endsWith("pump")) || hits[hits.length - 1];
  }

  // ---------- panel (shadow DOM: padre's CSS can't touch it, and ours can't touch padre) ----------
  const host = document.createElement("div");
  host.style.cssText = "position:fixed;z-index:2147483647;top:90px;right:16px;";
  host.id = "ts-panel-host";
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `<style>
    .p{width:300px;background:#0f141bf2;color:#e6edf3;border:1px solid #2a3340;border-radius:12px;font:12.5px/1.4 system-ui,-apple-system,Segoe UI,sans-serif;box-shadow:0 10px 30px #0008}
    .h{display:flex;align-items:center;gap:8px;padding:7px 10px;border-bottom:1px solid #2a3340;cursor:move;user-select:none}
    .h b{flex:1;font-size:12.5px}.h button{background:none;border:0;color:#93a3b8;cursor:pointer;font-size:14px}
    .b{padding:6px 8px 8px}.mute{color:#93a3b8}.small{font-size:11px}
    .r{display:grid;grid-template-columns:92px 1fr;gap:8px;align-items:start;padding:6px 4px;border-bottom:1px solid #1b2330}
    .r:last-of-type{border-bottom:0}.k{color:#93a3b8;font-size:11px;text-transform:uppercase;letter-spacing:.05em;padding-top:2px}
    .v b{font-size:14px}.why{color:#93a3b8;font-size:11px;margin-top:1px}
    .g{color:#34d399}.y{color:#fbbf24}.red{color:#f87171}.n{color:#93a3b8}
    .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:1px}
    .dot.g{background:#34d399}.dot.y{background:#fbbf24}.dot.red{background:#f87171}.dot.n{background:#475569}
    .btns{display:flex;gap:6px;margin-top:6px}.btns button,.lnk{flex:1;background:#1c2533;border:1px solid #2a3340;color:#e6edf3;border-radius:6px;padding:4px;cursor:pointer;font:inherit;font-size:11.5px}
    .btns button:hover{border-color:#7c9cff}input{background:#161d27;border:1px solid #2a3340;color:#e6edf3;border-radius:6px;padding:4px 6px;font:inherit;width:100%;box-sizing:border-box;margin-top:6px}
    .det{margin-top:6px;border-top:1px solid #1b2330;padding-top:6px;font-size:11.5px}.det ul{margin:2px 0 6px 16px;padding:0}.lnk{display:block;width:100%;margin-top:6px;background:none;color:#93a3b8}
  </style>
  <div class="p"><div class="h"><b>Tradeslave Assist <span class="mute small">v0.3</span></b><span class="mute small" id="st">starting</span><button id="min" title="collapse">&#8211;</button></div>
  <div class="b" id="body"><div class="mute small">Open a token on padre.gg, or paste an address:</div><input id="paste" placeholder="token address"></div></div>`;
  document.documentElement.appendChild(host);
  const $ = id => root.getElementById(id);

  // ---------- state ----------
  let pasted = null, last = null, pos = {}, busy = false, showDet = false, Q = null, D = null;
  chrome.storage.local.get(["pos", "min", "det", "positions"], s => {
    if (s.pos) { host.style.top = s.pos.top; host.style.right = "auto"; host.style.left = s.pos.left; }
    if (s.min) $("body").style.display = "none";
    showDet = !!s.det;
    pos = s.positions || {};
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

  const savePos = () => chrome.storage.local.set({ positions: pos });
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const pct = (x, d = 0) => x == null ? "–" : `${(100 * x).toFixed(d)}%`;
  const row = (k, cls, val, why) => `<div class="r"><div class="k">${k}</div><div class="v"><span class="dot ${cls}"></span><b class="${cls}">${val}</b>${why ? `<div class="why">${why}</div>` : ""}</div></div>`;
  const wait = k => row(k, "n", '<span class="mute">checking&hellip;</span>', "");

  // ---------- the board ----------
  function board(m) {
    const q = Q, d = D, sc = d && d.scores;
    let html = "";
    // 1. rug risk (fast check, ~1 s)
    if (!q) html += wait("Rug risk");
    else if (q.error) html += row("Rug risk", "n", "?", esc(q.error));
    else {
      const cls = q.risk === "HIGH" ? "red" : q.risk === "MEDIUM" ? "y" : "g";
      const top = q.flags.slice().sort((a, b) => b.points - a.points)[0];
      html += row("Rug risk", cls, q.risk, top ? esc(top.text) + (q.flags.length > 1 ? ` (+${q.flags.length - 1} more)` : "") : "no red flags in holders, launch buys or creator");
    }
    // 2. narrative (M1's LLM)
    if (!sc || sc.status === "scoring") html += wait("Narrative");
    else if (sc.narrative == null) html += row("Narrative", "n", "no score", esc(sc.why || ""));
    else html += row("Narrative", sc.narrative >= 7 ? "g" : sc.narrative >= 4 ? "y" : "red", `${sc.narrative}/10`, esc(sc.why || ""));
    // 3. runner prediction (M5's model)
    if (!sc || sc.status !== "done") html += wait("Runner");
    else if (sc.runner == null) html += row("Runner", "n", "too early", esc(sc.runner_why || ""));
    else html += row("Runner", sc.runner_pass ? "g" : sc.runner >= 0.25 ? "y" : "red", sc.runner_pass ? "LIKELY" : sc.runner >= 0.25 ? "MAYBE" : "UNLIKELY",
      `${pct(sc.runner)} by M5's model (M5 buys at ${pct(sc.runner_cut)}+)`);
    // 4. hype
    if (!d || d.error) html += wait("Hype");
    else {
      const callers = (d.tracked || []).filter(t => /caller/.test(t.label));
      const others = (d.tracked || []).filter(t => !/caller/.test(t.label));
      const why = [callers.length ? `${callers.length} top caller${callers.length > 1 ? "s" : ""} bought` : null,
        d.live_viewers ? `livestream, ${d.live_viewers} viewers` : null,
        ...(d.boosts || []).map(b => b === "LIVE20" ? "big livestream" : "DexScreener boost"),
        others.length ? others.map(t => t.label.split(" (")[0]).slice(0, 2).join(", ") + " in it" : null].filter(Boolean);
      const yes = callers.length > 0 || (d.live_viewers || 0) >= 20 || (d.boosts || []).length > 0;
      html += row("Hype", yes ? "g" : why.length ? "y" : "n", yes ? "YES" : why.length ? "SOME" : "NO", why.length ? esc([...new Set(why)].join(" · ")) : "no callers, livestream or boost");
    }
    // 5. dead coin?
    if (!d) html += wait("Dead coin?");
    else if (d.error) html += row("Dead coin?", "n", "?", esc(d.error));
    else html += row("Dead coin?", d.dead === "DEAD" ? "red" : d.dead === "FADING" ? "y" : "g", d.dead, esc(d.dead_why || ""));
    // optional: exit helper for a position you record
    const P = pos[m], x = d && d.exit;
    if (P && x) {
      const cls = x.action === "SELL" ? "red" : x.action === "HOLD" ? "g" : "y";
      html += row("My position", cls, `${esc(x.action)} &middot; ${x.r.toFixed(2)}&times;`, `${esc(x.why)} &middot; ${x.net >= 0 ? "+" : ""}${pct(x.net)} after fees`);
      html += `<div class="btns"><button id="clr">I sold (clear)</button></div>`;
    } else if (d && !d.error) {
      html += `<div class="btns"><button id="now">I bought now</button></div>`;
    }
    // details (the reasons), hidden by default
    if (showDet && (q || d)) {
      const flags = q && !q.error ? q.flags.map(f => `<li>${esc(f.text)}</li>`).join("") : "";
      const risks = d && !d.error ? (d.risks || []).map(r => `<li>${esc(r)}</li>`).join("") : "";
      const tr = d && !d.error ? (d.tracked || []).map(t => `<li>${esc(t.label)}: ${t.buys} buys / ${t.sells} sells</li>`).join("") : "";
      html += `<div class="det">${flags ? `<div class="mute">Rug flags</div><ul>${flags}</ul>` : ""}${risks ? `<div class="mute">Other risks</div><ul>${risks}</ul>` : ""}
        ${tr ? `<div class="mute">Tracked wallets</div><ul>${tr}</ul>` : ""}${q && !q.error ? `<div class="mute small">top-10 ${pct(q.top10)} · top-1 ${pct(q.top1)} · launch buyers ${pct(q.launch_share)} · dev ${(+q.dev_buy_sol).toFixed(2)} SOL${q.dev_sold ? " (sold)" : ""}</div>` : ""}</div>`;
    }
    html += `<button class="lnk" id="det">${showDet ? "hide details" : "details"}</button>`;
    $("body").innerHTML = html;
    const now = $("now"), clr = $("clr"), det = $("det");
    if (now) now.onclick = () => { pos[m] = { entry_mc: D.mc_usd, t: Date.now() / 1000 }; savePos(); tick(true); };
    if (clr) clr.onclick = () => { delete pos[m]; savePos(); tick(true); };
    if (det) det.onclick = () => { showDet = !showDet; chrome.storage.local.set({ det: showDet }); board(m); };
  }

  function tick(force) {
    const m = mintFromUrl() || pasted;
    if (!m) {
      $("st").textContent = "no token";
      const inp = $("paste");
      if (inp && !inp.onkeydown) inp.onkeydown = e => { if (e.key === "Enter") { pasted = (inp.value.match(B58) || [null])[0]; tick(); } };
      return;
    }
    if (m !== last) {                                   // a new coin: rug check first (~1 s), then everything else
      last = m; Q = null; D = null; board(m);
      chrome.runtime.sendMessage({ type: "quick", m }, res => {
        if (last !== m || !res) return;
        Q = res.ok ? res.d : { error: res.error }; board(m);
      });
    }
    if (busy && !force) return;
    busy = true;
    const P = pos[m];
    chrome.runtime.sendMessage({ type: "assist", m, entry_mc: P && P.entry_mc, t_entry: P && P.t }, res => {
      busy = false;
      if (!res) { $("st").textContent = "extension reloading"; return; }
      if (!res.ok) { $("st").textContent = "retrying…"; return; }          // keep the last good board
      if (last !== m) return;
      D = res.d;
      $("st").textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
      board(m);
    });
  }
  tick();
  setInterval(() => tick(), POLL_MS);
})();

// ---------- trenches highlighter: green outline + badge on coins that are lighting up right now ----------
// Matches the coin links on the page against /api/assist/hot (token, bonding-curve and PumpSwap pool addresses).
(() => {
  const B58 = /[1-9A-HJ-NP-Za-km-z]{32,44}/g;
  const KIND = { callout: "CALLER", model: "MODEL", runner: "RUNNER", live: "LIVE", boost: "BOOST", decu: "DECU", smart: "SMART" };
  let HOT = new Map(), marked = new Map(), timer = null;
  const css = document.createElement("style");
  css.textContent = `.ts-hot{outline:2px solid #22c55e !important;outline-offset:-2px !important;box-shadow:0 0 14px #22c55e80,inset 0 0 0 9999px #22c55e12 !important;position:relative !important}
    .ts-badge{position:absolute;top:3px;right:3px;z-index:20;background:#16a34a;color:#fff;font:700 10px/1.35 system-ui,sans-serif;padding:1px 6px;border-radius:6px;
      pointer-events:none;max-width:75%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;letter-spacing:.02em;box-shadow:0 1px 4px #0006}`;
  document.documentElement.appendChild(css);

  function label(c) {
    return c.reasons.map(r => r.kind === "runner" ? r.text.replace("runner model ", "RUNNER ") : r.kind === "model" ? r.text.split(" ")[0]
      : r.kind === "live" ? "LIVE " + ((r.text.match(/(\d+)/) || [])[1] || "") : KIND[r.kind] || r.kind).join(" · ");
  }

  function refresh() {
    chrome.runtime.sendMessage({ type: "hot" }, res => {
      if (!res || !res.ok || !res.d || !res.d.coins) return;
      const map = new Map();
      for (const c of res.d.coins) for (const a of [c.m, c.curve, c.pool]) if (a) map.set(a, c);
      HOT = map;
      scan();
    });
  }

  function cardOf(el) {
    for (let i = 0, e = el; e && e !== document.body && i < 6; i++, e = e.parentElement) {
      if (e.offsetHeight >= 40 && e.offsetWidth >= 140) return e;
    }
    return el;
  }

  function scan() {
    if (!HOT.size) { for (const [el] of marked) unmark(el); marked.clear(); return; }
    const seen = new Map();
    for (const el of document.querySelectorAll("a[href], [data-address], [data-mint], [data-token], [data-pair]")) {
      const blob = (el.getAttribute("href") || "") + " " + (el.dataset.address || "") + " " + (el.dataset.mint || "") + " " + (el.dataset.token || "") + " " + (el.dataset.pair || "");
      const hits = blob.match(B58);
      if (!hits) continue;
      const c = hits.map(h => HOT.get(h)).find(Boolean);
      if (!c) continue;
      const card = cardOf(el);
      if (card && card.closest && !card.closest("#ts-panel-host")) seen.set(card, c);
    }
    for (const [el] of marked) if (!seen.has(el)) unmark(el);
    for (const [el, c] of seen) mark(el, c);
    marked = seen;
  }

  function mark(el, c) {
    el.classList.add("ts-hot");
    const txt = label(c), tip = c.reasons.map(r => r.text).join(" · ");
    let b = el.querySelector(":scope > .ts-badge");
    if (!b) { b = document.createElement("span"); b.className = "ts-badge"; el.appendChild(b); }
    if (b.textContent !== txt) b.textContent = txt;
    if (el.title !== tip) el.title = "Tradeslave: " + tip;
  }

  function unmark(el) {
    el.classList.remove("ts-hot");
    const b = el.querySelector(":scope > .ts-badge");
    if (b) b.remove();
    if (el.title && el.title.startsWith("Tradeslave: ")) el.removeAttribute("title");
  }

  new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(scan, 300); }).observe(document.body || document.documentElement, { childList: true, subtree: true });
  refresh();
  setInterval(refresh, 4000);
  setInterval(scan, 1500);
})();
