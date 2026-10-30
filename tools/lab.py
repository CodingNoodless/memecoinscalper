"""Lab: one place for the experiment registry, forward-test verdicts and the STATE brief.

    uv run python tools/lab.py scoreboard            # every paper bot, with KILL / CONTINUE / PASS
    uv run python tools/lab.py brief                 # the session-start brief (also used by the SessionStart hook)
    uv run python tools/lab.py brief --write D:/vault/tradeslave   # also writes STATE.md in the vault
    uv run python tools/lab.py reg add ID FAMILY "hypothesis" [--variants N]
    uv run python tools/lab.py reg set ID STATUS [--note "..."]    # idea|prereg|killtest|backtest|forward|live|passed|killed
    uv run python tools/lab.py reg list

Forward-test rules (fixed, same for every bot; returns are net per trade after costs):
  KILL      n >= 20 and the 80% upper bound of the mean is below 0  (clearly losing: stop wasting days)
            or n >= 60 and t < 1                                       (no sign of an edge after a fair sample)
  PASS      n >= 100, t >= max(2.5, sqrt(2 ln N)) for N variants tried in the family,
            both halves > 0, and the mean is still > 0 without the 3 best trades
  CONTINUE  otherwise
PASS only makes a bot eligible for a live micro-test; going live is always Rohit's decision.
size      guarded half-Kelly share of bankroll per trade (tradeslave/sizing.py); 0 until the edge's 80% lower bound > 0
"""

from __future__ import annotations

import json
import math
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tradeslave.sizing import kelly_fraction  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REGISTRY = ROOT / "lab" / "registry.jsonl"   # tracked in git
STATUSES = ("idea", "prereg", "killtest", "backtest", "forward", "live", "passed", "killed")


# ---------------------------------------------------------------- stats

def stats(rets: list[float]) -> dict:
    n = len(rets)
    if n == 0:
        return {"n": 0}
    mean = statistics.mean(rets)
    sd = statistics.stdev(rets) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else float("inf")
    half = n // 2
    trimmed = sorted(rets)[:-3] if n > 3 else []
    return {
        "n": n, "mean": mean, "median": statistics.median(rets), "win": sum(r > 0 for r in rets) / n,
        "t": mean / se if se and se != float("inf") else 0.0, "se": se,
        "h1": statistics.mean(rets[:half]) if half else 0.0, "h2": statistics.mean(rets[half:]) if n - half else 0.0,
        "mean_ex_top3": statistics.mean(trimmed) if trimmed else float("nan"),
    }


def hurdle(variants: int) -> float:
    return max(2.5, math.sqrt(2 * math.log(max(variants, 1)))) if variants > 1 else 2.5


def verdict(s: dict, variants: int = 1) -> str:
    n = s.get("n", 0)
    if n >= 20 and s["mean"] + 1.2816 * s["se"] < 0:
        return "KILL"
    if n >= 60 and s["t"] < 1:
        return "KILL"
    if (n >= 100 and s["t"] >= hurdle(variants) and s["h1"] > 0 and s["h2"] > 0
            and s["mean_ex_top3"] == s["mean_ex_top3"] and s["mean_ex_top3"] > 0):
        return "PASS"
    return "CONTINUE"


# ---------------------------------------------------------------- data

def bot_returns(path: Path) -> list[float]:
    rets = []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return rets
    rows = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        v = r.get("ret", r.get("pnl_pct"))
        if isinstance(v, (int, float)) and math.isfinite(v):
            rows.append((r.get("t_exit") or r.get("t") or 0, float(v)))
    rows.sort()
    return [v for _, v in rows]


def registry() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if REGISTRY.exists():
        for line in REGISTRY.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            cur = out.setdefault(e["id"], {"id": e["id"], "history": []})
            cur.update({k: v for k, v in e.items() if k not in ("history",)})
            cur["history"].append({"t": e.get("t"), "status": e.get("status"), "note": e.get("note")})
    return out


def _append(event: dict) -> None:
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    event["t"] = time.strftime("%Y-%m-%d %H:%M")
    with REGISTRY.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def family_variants(reg: dict, family: str) -> int:
    return sum(int(e.get("variants", 1)) for e in reg.values() if e.get("family") == family) or 1


# ---------------------------------------------------------------- commands

def scoreboard(print_out=True) -> list[dict]:
    reg = registry()
    rows = []
    for f in sorted(DATA.glob("*/trades.jsonl")):
        bot = f.parent.name
        rets = bot_returns(f)
        if not rets:
            continue
        s = stats(rets)
        fam = reg.get(bot, {}).get("family", bot)
        s.update(bot=bot, size=kelly_fraction(rets), verdict=verdict(s, family_variants(reg, fam) if bot in reg else 1),
                 last=time.strftime("%m-%d %H:%M", time.localtime(f.stat().st_mtime)))
        rows.append(s)
    rows.sort(key=lambda r: ({"PASS": 0, "CONTINUE": 1, "KILL": 2}[r["verdict"]], -r["t"]))
    if print_out:
        print(f"{'bot':10s} {'n':>5s} {'mean':>8s} {'median':>8s} {'win':>5s} {'t':>6s} {'halves':>15s} {'ex-top3':>8s} {'size':>6s}  verdict   last trade")
        for r in rows:
            print(f"{r['bot']:10s} {r['n']:5d} {r['mean']*100:+7.2f}% {r['median']*100:+7.2f}% {r['win']*100:4.0f}% "
                  f"{r['t']:+6.2f} {r['h1']*100:+6.1f}/{r['h2']*100:+6.1f}% {r['mean_ex_top3']*100:+7.2f}% {r['size']*100:5.1f}%  "
                  f"{r['verdict']:9s} {r['last']}")
    return rows


def brief(write: str | None = None) -> str:
    rows = scoreboard(print_out=False)
    reg = registry()
    by_status: dict[str, list[str]] = {}
    for e in reg.values():
        by_status.setdefault(e.get("status", "idea"), []).append(e["id"])
    lines = [
        "---", "tags: [state]", f"updated: {time.strftime('%Y-%m-%d %H:%M')}", "---",
        "# STATE (generated by `tools/lab.py brief`; do not edit by hand)", "",
        "## Mission math",
        "- Targets: $1k by 2026-11-15, $10k by 2026-12-31 (Rohit).",
        "- Profit/day = net edge per trade x size x trades/day. Example: +5% x $27 (0.15 SOL) x 40/day = $54/day.",
        "- Nothing reaches the targets until one strategy PASSES its forward test. Speed to a trustworthy yes/no is the job.",
        "",
        "## Paper bots (forward tests, net per trade)",
        "| bot | n | mean | median | win | t | halves | ex-top3 | size | verdict |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        lines.append(f"| {r['bot']} | {r['n']} | {r['mean']*100:+.2f}% | {r['median']*100:+.2f}% | {r['win']*100:.0f}% | "
                     f"{r['t']:+.2f} | {r['h1']*100:+.1f} / {r['h2']*100:+.1f}% | {r['mean_ex_top3']*100:+.2f}% | {r['size']*100:.1f}% | **{r['verdict']}** |")
    lines += ["", "Rules: KILL at n>=20 if the 80% upper bound < 0, or n>=60 with t<1. PASS at n>=100, t>=2.5 "
              "(higher if many variants), both halves > 0, positive without the top 3. PASS = eligible for a live "
              "micro-test, Rohit decides. Size = guarded half-Kelly share of bankroll per trade "
              "(tradeslave/sizing.py): 0 until the 80% lower bound of the edge is > 0.", "", "## Pipeline (registry: lab/registry.jsonl)"]
    for st in STATUSES:
        if by_status.get(st):
            lines.append(f"- **{st}**: {', '.join(sorted(by_status[st]))}")
    if not reg:
        lines.append("- (registry empty: add work with `tools/lab.py reg add`)")
    kills = [r["bot"] for r in rows if r["verdict"] == "KILL"]
    passes = [r["bot"] for r in rows if r["verdict"] == "PASS"]
    lines += ["", "## Action items from the rules"]
    if passes:
        lines.append(f"- PASS: {', '.join(passes)} → ask Rohit about a live micro-test.")
    if kills:
        lines.append(f"- KILL: {', '.join(kills)} → stop these bots, record the verdict, free the slot.")
    if not passes and not kills:
        lines.append("- Nothing decided yet: keep the forward tests running.")
    text = "\n".join(lines) + "\n"
    if write:
        Path(write, "STATE.md").write_text(text, encoding="utf-8")
    return text


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "scoreboard":
        scoreboard()
    elif cmd == "brief":
        write = argv[argv.index("--write") + 1] if "--write" in argv else os.environ.get("TRADESLAVE_VAULT")
        if write and not Path(write).exists():
            write = None
        print(brief(write))
    elif cmd == "reg":
        sub = argv[1] if len(argv) > 1 else "list"
        if sub == "add":
            _, _, rid, family, hyp, *rest = argv
            variants = int(rest[rest.index("--variants") + 1]) if "--variants" in rest else 1
            _append({"id": rid, "family": family, "hypothesis": hyp, "status": "idea", "variants": variants})
        elif sub == "set":
            _, _, rid, status, *rest = argv
            if status not in STATUSES:
                raise SystemExit(f"status must be one of {STATUSES}")
            note = rest[rest.index("--note") + 1] if "--note" in rest else None
            _append({"id": rid, "status": status, "note": note})
        else:
            for e in sorted(registry().values(), key=lambda e: e["id"]):
                print(f"{e['id']:12s} {e.get('family', ''):10s} {e.get('status', ''):9s} {e.get('hypothesis', '')[:80]}")
    else:
        raise SystemExit(f"unknown command {cmd}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
