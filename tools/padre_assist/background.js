// Fetches from the local dashboard. Runs in the extension's own context, so the padre.gg page's CORS and content-security
// rules don't apply, and the page never talks to localhost itself.
const API = "http://127.0.0.1:8765/api/assist";

function get(url, ms) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), ms);
  return fetch(url, { cache: "no-store", signal: ctl.signal }).then(r => r.json()).finally(() => clearTimeout(timer));
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg && msg.type === "hot") {
    get(`${API}/hot`, 8000).then(d => reply({ ok: true, d })).catch(e => reply({ ok: false, error: e.message }));
    return true;
  }
  if (!msg || (msg.type !== "assist" && msg.type !== "quick")) return;
  const q = new URLSearchParams({ m: msg.m });
  if (msg.entry_mc) q.set("entry_mc", String(msg.entry_mc));
  if (msg.t_entry) q.set("t_entry", String(msg.t_entry));
  const url = msg.type === "quick" ? `${API}/quick?${q}` : `${API}?${q}`;
  get(url, msg.type === "quick" ? 10000 : 25000)
    .then(d => reply({ ok: true, d }))
    .catch(e => reply({ ok: false, error: e.name === "AbortError" ? "the dashboard is slow to answer (still working)" : `dashboard not reachable (${e.message}). Is "python dashboard.py" running?` }));
  return true; // keep the channel open for the async reply
});
