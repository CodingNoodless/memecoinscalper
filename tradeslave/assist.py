"""Trading assistant (dashboard /api/assist, used by the padre.gg browser extension in tools/padre_assist).

For one token: the checks our bots use, risk flags, tracked wallets and hype, and an exit helper with M12r's rules.
Read-only: it never trades and never touches a wallet. Coin trades come from the live feed (the coin is added to the feed's
watch list on the first request, via data/m_assist/cands.jsonl); a coin the feed hasn't recorded is filled from Helius
(last 30 min, so holder figures are marked partial).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import defaultdict

import requests

from .paths import DATA_DIR
from .research.live_feed import feed_rows

FEE = 0.0125
ASSIST = DATA_DIR / "m_assist"
LAD_FIRST, LAD_STALL_S, LAD_DYING = 1.25, 120, 0.25
DECU = "4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9"
_lock = threading.Lock()
_cache: dict = {}            # mint -> (t, rows, partial)
_funding: dict = {}          # mint -> dict (filled in a thread)
_state = dict(live_t=0.0, live={}, creates_pos=0, creates=defaultdict(int), dev_of={}, wanted=set())


def _sol_usd():
    for v in ("m7", "m5", "m9"):
        try:
            return json.loads((DATA_DIR / v / "status.json").read_text(encoding="utf-8")).get("sol_usd") or 100.0
        except (OSError, ValueError):
            continue
    return 100.0


def _tracked():
    """wallet -> label: decu, the WL study wallets, the top pump.fun callers."""
    out = {DECU: "decu (streamer, followed)"}
    try:
        from .research.wl_fetch import WALLETS
        for w in WALLETS:
            if not w.startswith("ARu4"):
                out[w] = f"WL wallet {w[:6]}"
    except ImportError:
        pass
    for f in sorted((DATA_DIR / "callouts").glob("leaderboard_*.json"), reverse=True):
        try:
            lb = json.loads(f.read_text(encoding="utf-8")).get("callouts", [])
        except (OSError, ValueError):
            continue
        for u in lb:
            for w in u.get("wallets", []):
                if not w.startswith("0x"):
                    out[w] = f"top caller {w[:6]} ({u.get('pct2xOrMore', 0):.0%} of calls 2x)"
        break
    return out


def _creates():
    """creator -> coins created (from the launch recorder), read incrementally."""
    from .research.c1_curve import CREATES
    try:
        with CREATES.open(encoding="utf-8") as fh:
            fh.seek(_state["creates_pos"])
            for line in fh:
                if not line.endswith("\n"):
                    break
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                _state["creates"][j.get("dev")] += 1
                _state["dev_of"][j.get("m")] = j.get("dev")
            _state["creates_pos"] = fh.tell()
    except OSError:
        pass
    return _state["creates"], _state["dev_of"]


def _live_streams():
    if time.time() - _state["live_t"] > 60:
        _state["live_t"] = time.time()
        try:
            d = requests.get("https://frontend-api-v3.pump.fun/coins/currently-live", params={"limit": 100, "offset": 0, "includeNsfw": "false"},
                             headers={"User-Agent": "tradeslave-research/0.1 (personal research; low rate)"}, timeout=15).json()
            _state["live"] = {c["mint"]: c.get("num_participants") or 0 for c in d}
        except (requests.RequestException, ValueError, KeyError, TypeError):
            pass
    return _state["live"]


def _boosts(m):
    out = set()
    try:
        with (DATA_DIR / "hype" / "signals.jsonl").open(encoding="utf-8") as fh:
            for line in fh:
                if m in line:
                    j = json.loads(line)
                    if j["m"] == m and time.time() - j["t"] < 4 * 3600:
                        out.add(j["src"])
    except (OSError, ValueError):
        pass
    return sorted(out)


def _want(m):
    if m not in _state["wanted"]:
        _state["wanted"].add(m)
        ASSIST.mkdir(exist_ok=True)
        with (ASSIST / "cands.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(t=time.time(), m=m, stage="assist")) + "\n")
    else:                                          # keep it wanted (the feed forgets coins not logged for 30 min)
        last = _state.get("want_t", {}).get(m, 0)
        if time.time() - last > 600:
            with (ASSIST / "cands.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(dict(t=time.time(), m=m, stage="assist")) + "\n")
            _state.setdefault("want_t", {})[m] = time.time()


def _helius_rows(m):
    from .research.bw_test import gtfa
    from .research.fl_test import helius_url
    from .scalper.trade_events import parse_trade_events
    txs, _ = gtfa(helius_url(), m, time.time() - 1800, time.time(), max_pages=20)
    out, bal, h = [], {}, 0
    for tx in txs:
        for e in parse_trade_events((tx.get("meta") or {}).get("logMessages") or []):
            if e["mint"] != m or not e["vt"] or not e["vs"]:
                continue
            tok, w = e["tok"] / 1e6, e["user"]
            b0 = bal.get(w, 0.0)
            b1 = b0 + (tok if e["buy"] else -tok)
            bal[w] = b1
            h += (b0 <= 1 < b1) - (b1 <= 1 < b0)
            out.append([float(e["ts"]), e["vs"] / e["vt"] * 1e-3, e["buy"], e["sol"] / 1e9, w, tok, h])
    out.sort(key=lambda r: r[0])
    return out


def _rows(m):
    with _lock:
        c = _cache.get(m)
        if c and time.time() - c[0] < 2:
            return c[1], c[2]
    rows, complete = feed_rows(m)
    partial = False
    if not rows or len(rows) < 5:
        old = _cache.get(m)
        if old and old[2] and time.time() - old[0] < 20:
            return old[1], True
        rows, partial = _helius_rows(m), True
    elif not complete:
        partial = True
    with _lock:
        _cache[m] = (time.time(), rows, partial)
    return rows, partial


def _funding_checks(m, rows, creator):
    """Serial-funder and funding-cluster checks (Helius lookups), computed once per coin in a thread."""
    if m in _funding:
        return _funding[m]
    _funding[m] = dict(status="looking up")

    def work():
        try:
            from .research.fl_test import LiveFunding, NONSYSTEM
            lf = LiveFunding()
            fe = lf.cluster_share(rows, len(rows) - 1)
            f = (lf.cache.get(creator) or {}).get("funder") if creator else None
            try:
                seen = json.loads((DATA_DIR / "m7" / "serial_seen.json").read_text())
            except (OSError, ValueError):
                seen = {}
            serial = None if not f else 0 if f in NONSYSTEM else seen.get(f, 0)
            fe = fe if isinstance(fe, dict) else {}
            _funding[m] = dict(status="done", cluster=fe.get("cluster_share"), creator_linked=fe.get("creator_linked"),
                               fresh_share=fe.get("fresh_share"), funder_known=bool(f), serial=serial)
        except Exception as exc:  # noqa: BLE001 - shown in the panel, never fatal
            _funding[m] = dict(status=f"lookup failed: {type(exc).__name__}")
    threading.Thread(target=work, daemon=True).start()
    return _funding[m]


def analyze(m: str, entry_mc: float | None = None, t_entry: float | None = None) -> dict:
    from .research.br_test import state
    if not (32 <= len(m) <= 44):
        return dict(error="not a Solana token address")
    _want(m)
    rows, partial = _rows(m)
    if not rows:
        return dict(m=m, error="no curve trades found (graduated to PumpSwap, or not a pump.fun coin)")
    sol_usd = _sol_usd()
    now = time.time()
    p = rows[-1][1]
    k = len(rows) - 1
    f = state(rows, k) if k >= 3 else {}
    creates, dev_of = _creates()
    creator = dev_of.get(m) or (rows[0][4] if not partial else None)
    bal = defaultdict(float)
    for r in rows:
        bal[r[4]] += r[5] if r[2] else -r[5]
    holders = sorted(((w, v) for w, v in bal.items() if v > 1), key=lambda x: -x[1])
    supply = 1e9
    top10 = sum(v for _, v in holders[:10]) / supply
    top1 = holders[0][1] / supply if holders else 0.0
    t0 = rows[0][0]
    early = {r[4] for r in rows if r[2] and r[0] <= t0 + 2 and r[4] != creator}
    early_hold = sum(max(0.0, bal[w]) for w in early) / supply
    cr_hold = max(0.0, bal.get(creator, 0.0)) / supply if creator else None
    cr_sold = any((not r[2]) and r[4] == creator for r in rows) if creator else None
    tracked = _tracked()
    in_coin = {}
    for r in rows:
        if r[4] in tracked:
            x = in_coin.setdefault(r[4], dict(label=tracked[r[4]], buys=0, sells=0, first=r[0], hold=0.0))
            x["buys" if r[2] else "sells"] += 1
    for w, x in in_coin.items():
        x["hold"] = max(0.0, bal.get(w, 0.0)) / supply
        x["ago_s"] = now - x["first"]
    live = _live_streams().get(m)
    boosts = _boosts(m)
    fund = _funding_checks(m, rows, creator) if not partial else dict(status="needs the full history (coin not in the feed yet)")
    w60 = [r for r in rows if r[0] > now - 60]
    flow60 = sum(r[3] if r[2] else -r[3] for r in w60)
    acc = f.get("accel")
    checks = [
        dict(name="Not a hot burst (buyer acceleration <= 4)", ok=acc is not None and acc <= 4, value=acc,
             why="losing trades of every bot and wallet we studied were bought into the hottest acceleration"),
        dict(name="M9 flow rule (30 s net inflow >= 12.3 SOL)", ok=(f.get("flow_30") or 0) >= 12.3, value=f.get("flow_30"),
             why="M9's entry rule (the one PX pattern that held on holdout)"),
        dict(name="Creator's funder is new (serial-funder skip)", ok=None if fund.get("serial") is None else fund["serial"] == 0,
             value=fund.get("serial"), why="M7/M8 skip creators whose funder funded earlier signalled creators"),
        dict(name="No funding cluster (< 15% of top-20)", ok=None if fund.get("cluster") is None else fund["cluster"] < 0.15,
             value=fund.get("cluster"), why="M6+ skip when one funder controls >= 15% of the top-20 holders"),
        dict(name="Hype gate (M12): caller in it, or livestream / boost", ok=bool([1 for x in in_coin.values() if "caller" in x["label"]]) or bool(boosts) or (live or 0) >= 20,
             value=None, why="trades where a top caller had already bought did 6-10 points better (small samples)"),
    ]
    risks = []
    if cr_hold is not None and cr_hold > 0.05:
        risks.append(f"creator still holds {cr_hold:.1%} of supply")
    if cr_sold:
        risks.append("creator has sold")
    if creator and creates.get(creator, 0) >= 3:
        risks.append(f"creator launched {creates[creator]} coins we've seen (serial deployer)")
    if top10 > 0.35:
        risks.append(f"top-10 holders own {top10:.0%}")
    if top1 > 0.10:
        risks.append(f"one wallet owns {top1:.0%}")
    if early_hold > 0.15:
        risks.append(f"launch-burst buyers (first 2 s) still hold {early_hold:.0%}: likely snipers / bundle")
    if fund.get("creator_linked"):
        risks.append(f"top holders share a funder with the creator ({fund['creator_linked']})")
    if (fund.get("fresh_share") or 0) > 0.5:
        risks.append(f"{fund['fresh_share']:.0%} of top holders are fresh wallets")
    exit_help = None
    if entry_mc:
        mc = p * 1e9 * sol_usd
        r = mc / entry_mc
        since = [x for x in rows if t_entry is None or x[0] >= t_entry]
        pk = max([x[1] for x in since] + [p])
        peak_r = pk * 1e9 * sol_usd / entry_mc
        t_peak = max((x[0] for x in since if x[1] >= pk), default=now)
        per_min = defaultdict(int)
        for x in since:
            per_min[int(x[0] // 60)] += 1
        act, amax = len(w60), max(per_min.values(), default=0)
        if peak_r >= 2:
            n = int(peak_r)
            stop = LAD_FIRST if n < 3 else float(n - 1)
            mode = "runner (stop only moves up)"
        else:
            stop, mode = 0.90, "normal (-10% stop)"
        stalled = now - t_peak > LAD_STALL_S and flow60 <= 0
        dying = amax >= 5 and act < LAD_DYING * amax
        if r <= stop:
            action, why = "SELL", f"price {r:.2f}x is at or under the stop ({stop:.2f}x)"
        elif peak_r >= 2 and (stalled or dying):
            action, why = "TAKE PROFIT", "the run looks done: " + ("no new high for 2 min and net selling" if stalled else "trading activity fell below a quarter of its peak")
        elif r > 1.15 and (stalled or dying):
            action, why = "CONSIDER PROFIT", "momentum fading: " + ("stalled with net selling" if stalled else "activity dying")
        else:
            action, why = "HOLD", f"stop at {stop:.2f}x" + (" (moves up at the next whole multiple)" if peak_r >= 2 else "; moves to 1.25x at 2x")
        net = r * (1 - FEE) ** 2 - 1
        exit_help = dict(mc=mc, r=r, net=net, peak=peak_r, stop=stop, mode=mode, action=action, why=why,
                         stalled=stalled, dying=dying, act60=act, act_max=amax, flow60=flow60)
    return dict(m=m, t=now, partial=partial, n_trades=len(rows), age_s=now - t0, price=p, mc_usd=p * 1e9 * sol_usd, sol_usd=sol_usd,
                feats={k: f.get(k) for k in ("buyers_30", "accel", "flow_30", "buy_ratio_60", "from_lo60", "h_vs_max", "vs_launch", "trades_60", "avg_buy_60")},
                holders=len(holders), top10=top10, top1=top1, creator=creator, creator_hold=cr_hold, creator_sold=cr_sold,
                creator_coins=creates.get(creator, 0) if creator else None, early_hold=early_hold, flow60=flow60,
                checks=checks, risks=risks, funding=fund, tracked=sorted(in_coin.values(), key=lambda x: x["first"]),
                live_viewers=live, boosts=boosts, exit=exit_help)


def assist_json(qs: dict) -> dict:
    m = (qs.get("m") or [""])[0].strip()
    try:
        entry = float((qs.get("entry_mc") or [""])[0] or 0) or None
        t_entry = float((qs.get("t_entry") or [""])[0] or 0) or None
    except ValueError:
        entry = t_entry = None
    try:
        return analyze(m, entry, t_entry)
    except sqlite3.Error as exc:
        return dict(m=m, error=f"feed database busy: {exc}")
