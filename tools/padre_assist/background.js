// Fetches the analysis from the local dashboard. Runs in the extension's own context, so the padre.gg page's CORS and
// content-security rules don't apply, and the page never talks to localhost itself.
const API = "http://127.0.0.1:8765/api/assist";

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg && msg.type === "assist") {
    const q = new URLSearchParams({ m: msg.m });
    if (msg.entry_mc) q.set("entry_mc", String(msg.entry_mc));
    if (msg.t_entry) q.set("t_entry", String(msg.t_entry));
    fetch(`${API}?${q}`, { cache: "no-store" })
      .then(r => r.json())
      .then(d => reply({ ok: true, d }))
      .catch(e => reply({ ok: false, error: `dashboard not reachable (${e.message}). Is "python dashboard.py" running?` }));
    return true; // keep the channel open for the async reply
  }
});
