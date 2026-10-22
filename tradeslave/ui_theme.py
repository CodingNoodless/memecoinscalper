"""Shared look for the dashboard pages (lab, trade replay, blind labels): one dark palette and one tab bar."""

THEME = r"""
:root{--bg:#0b0e13;--card:#141922;--card2:#1a2029;--line:#252d38;--ink:#e7ebf1;--mute:#8b95a5;--good:#34d399;
--bad:#f87171;--warn:#fbbf24;--acc:#7c9cff;--acc2:#a78bfa;--buy:#60a5fa;--sell:#f59e0b;--half:#c084fc;
--r:12px;--shadow:0 1px 0 #ffffff08 inset,0 8px 24px #00000040}
*{box-sizing:border-box}
html,body{background:var(--bg)}
body{margin:0;color:var(--ink);font:14px/1.5 "Segoe UI Variable","Segoe UI",system-ui,-apple-system,sans-serif;
-webkit-font-smoothing:antialiased}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
.nav{position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:6px;padding:10px 18px;
background:#0b0e13e6;backdrop-filter:blur(8px);border-bottom:1px solid var(--line);flex-wrap:wrap}
.brand{font-weight:700;letter-spacing:.2px;margin-right:14px;display:flex;align-items:center;gap:8px}
.brand i{width:10px;height:10px;border-radius:50%;background:linear-gradient(135deg,var(--acc),var(--acc2));display:inline-block}
.tab{color:var(--mute);padding:6px 12px;border-radius:999px;font-size:13px;font-weight:600}
.tab:hover{color:var(--ink);background:var(--card);text-decoration:none}
.tab.on{color:var(--ink);background:var(--card2);box-shadow:0 0 0 1px var(--line) inset}
.nav .sp{flex:1}.nav .note{color:var(--mute);font-size:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:14px 16px;box-shadow:var(--shadow);min-width:0}
.card h2,.card h3{margin:0 0 10px;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--mute);font-weight:650}
.mute{color:var(--mute)}.pos{color:var(--good)}.neg{color:var(--bad)}.small{font-size:12px}
button,select,input{background:var(--card2);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:6px 10px;
font:inherit;font-size:13px}
button{cursor:pointer;font-weight:600}button:hover{border-color:#3a4554}button.on{border-color:var(--acc);background:#1c2440}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mute);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;position:sticky;top:0;background:var(--card)}
tbody tr:hover,table tr:hover td{background:#ffffff05}
.pill{display:inline-block;padding:1px 9px;border-radius:999px;font-size:11.5px;font-weight:650;border:1px solid}
.kpi{background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:10px 12px;min-width:0}
.kpi span{display:block;color:var(--mute);font-size:11.5px;text-transform:uppercase;letter-spacing:.05em}
.kpi b{display:block;font-size:20px;font-weight:700;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kpi b span{display:inline;font-size:inherit;text-transform:none;letter-spacing:0;color:inherit}
.kpi b span.pos{color:var(--good)}.kpi b span.neg{color:var(--bad)}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:#2a3340;border-radius:8px}
::-webkit-scrollbar-track{background:transparent}
"""


def nav(active: str, note: str = "") -> str:
    tabs = (("lab", "/", "Lab"), ("trades", "/trades", "Trade replay"), ("label", "/label", "Blind labels"))
    links = "".join(f'<a class="tab{" on" if k == active else ""}" href="{h}">{t}</a>' for k, h, t in tabs)
    return (f'<nav class="nav"><span class="brand"><i></i>Tradeslave</span>{links}<span class="sp"></span>'
            f'<span class="note" id="navnote">{note}</span></nav>')
