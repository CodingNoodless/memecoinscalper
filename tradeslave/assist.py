"""Trading assistant (dashboard /api/assist, used by the padre.gg browser extension in tools/padre_assist).

For one token: the checks our bots use, risk flags, tracked wallets and hype, and an exit helper with M12r's rules.
Read-only: it never trades and never touches a wallet. Coin trades come from the live feed (the coin is added to the feed's
watch list on the first request, via data/m_assist/cands.jsonl); a coin the feed hasn't recorded is filled from Helius
(last 30 min, so holder figures are marked partial).
"""

from __future__ import annotations

import json
import math
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
                    elite = u.get("pct2xOrMore", 0) >= 0.31 and u.get("medianMultiple", 0) >= 1.40   # Rohit's elite list (M12 v2)
                    out[w] = f"{'elite' if elite else 'top'} caller {w[:6]} ({u.get('pct2xOrMore', 0):.0%} of calls 2x)"
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
    per_min = defaultdict(int)
    for r in rows:
        per_min[int(r[0] // 60)] += 1
    peak_min = max(per_min.values(), default=0)
    last_age = now - rows[-1][0]
    off_peak = p / max(r[1] for r in rows)
    if last_age > 180 or (peak_min >= 20 and len(w60) < 0.1 * peak_min and off_peak < 0.5):
        dead, dead_why = "DEAD", (f"no trades for {last_age / 60:.0f} min" if last_age > 180 else
                                  f"{len(w60)} trades in the last minute vs {peak_min} at its busiest, {off_peak:.0%} of its peak price")
    elif peak_min >= 10 and len(w60) < LAD_DYING * peak_min:
        dead, dead_why = "FADING", f"{len(w60)} trades in the last minute vs {peak_min} at its busiest, {off_peak:.0%} of peak price"
    else:
        dead, dead_why = "ALIVE", f"{len(w60)} trades in the last minute ({peak_min} at its busiest), net flow {flow60:+.1f} SOL"
    return dict(m=m, t=now, partial=partial, scores=scores(m), dead=dead, dead_why=dead_why, n_trades=len(rows), age_s=now - t0, price=p, mc_usd=p * 1e9 * sol_usd, sol_usd=sol_usd,
                feats={k: f.get(k) for k in ("buyers_30", "accel", "flow_30", "buy_ratio_60", "from_lo60", "h_vs_max", "vs_launch", "trades_60", "avg_buy_60")},
                holders=len(holders), top10=top10, top1=top1, creator=creator, creator_hold=cr_hold, creator_sold=cr_sold,
                creator_coins=creates.get(creator, 0) if creator else None, early_hold=early_hold, flow60=flow60,
                checks=checks, risks=risks, funding=fund, tracked=sorted(in_coin.values(), key=lambda x: x["first"]),
                live_viewers=live, boosts=boosts, exit=exit_help)


_pair_map: dict = {}         # pair / bonding-curve address -> token mint (padre.gg URLs carry the pair, not the mint)


def resolve(addr: str) -> tuple[str, str | None]:
    """(token mint, pair address or None). An address the feed doesn't know as a coin is looked up as a pair on DexScreener."""
    if addr in _pair_map:
        return _pair_map[addr], addr
    try:
        db = sqlite3.connect(DATA_DIR / "feed" / "trades.sqlite", timeout=5)
        known = db.execute("SELECT 1 FROM tr WHERE m=? LIMIT 1", (addr,)).fetchone()
        db.close()
    except sqlite3.Error:
        known = None
    if known or addr.endswith("pump"):
        return addr, None
    try:
        pairs = requests.get(f"https://api.dexscreener.com/latest/dex/pairs/solana/{addr}", timeout=10,
                             headers={"User-Agent": "tradeslave-research/0.1 (personal research; low rate)"}).json().get("pairs") or []
    except (requests.RequestException, ValueError):
        pairs = []
    mint = next((p["baseToken"]["address"] for p in pairs if (p.get("baseToken") or {}).get("address")), None)
    if mint:
        _pair_map[addr] = mint
        return mint, addr
    return addr, None


def assist_json(qs: dict) -> dict:
    addr = (qs.get("m") or [""])[0].strip()
    m, pair = resolve(addr) if 32 <= len(addr) <= 44 else (addr, None)
    try:
        entry = float((qs.get("entry_mc") or [""])[0] or 0) or None
        t_entry = float((qs.get("t_entry") or [""])[0] or 0) or None
    except ValueError:
        entry = t_entry = None
    try:
        out = analyze(m, entry, t_entry)
    except sqlite3.Error as exc:
        out = dict(m=m, error=f"feed database busy: {exc}")
    out["pair"] = pair
    return out


# ---------------- fast rug check (one RPC call + the first 100 transactions, ~1 s) ----------------
TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
MAYHEM_AGENT = "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"   # holds ~1B of every 2B-supply Mayhem-mode coin (seen 4 of 4)
_quick: dict = {}


def _rpc(method, params):
    from .research.fl_test import helius_url
    r = requests.post(helius_url(), json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=8)
    return r.json().get("result")


def _curve_accounts(mint, pair):
    """The bonding curve, the PumpSwap pool and their token accounts (excluded from holder figures: they hold the unsold
    supply / the pool's liquidity, not a wallet's bag)."""
    from .research.pda import PUMP_PROGRAM, b58decode, find_pda, pumpswap_pool_address
    owners = {pair, find_pda([b"bonding-curve", b58decode(mint)], PUMP_PROGRAM), MAYHEM_AGENT}
    try:
        owners.add(pumpswap_pool_address(mint))
    except (ValueError, KeyError):
        pass
    out = {o for o in owners if o}
    for o in list(out):
        for tp in TOKEN_PROGRAMS:
            out.add(find_pda([b58decode(o), b58decode(tp), b58decode(mint)], ATA_PROGRAM))
    return out


def quick(addr: str, bg: bool = False) -> dict:
    """Rug risk in about a second: current top-20 holders (exact, from the chain) + launch transactions + creator record.
    bg: a background screen for the trenches highlighter (no feed watch, no narrative/runner scoring)."""
    from concurrent.futures import ThreadPoolExecutor
    from .research.bw_test import gtfa
    from .research.fl_test import helius_url
    from .scalper.trade_events import parse_trade_events
    t0 = time.time()
    m, pair = resolve(addr) if 32 <= len(addr) <= 44 else (addr, None)
    c = _quick.get(m)
    if c and time.time() - c["t"] < 20:
        return c
    if not bg:
        _want(m)
    with ThreadPoolExecutor(4) as ex:
        f_hold = ex.submit(_rpc, "getTokenLargestAccounts", [m, {"commitment": "confirmed"}])
        f_sup = ex.submit(_rpc, "getTokenSupply", [m])
        f_first = ex.submit(gtfa, helius_url(), m, 0, time.time(), "full", 100, 1)
        f_cr = ex.submit(_creates)
        try:
            hold = (f_hold.result(timeout=8) or {}).get("value") or []
        except Exception:  # noqa: BLE001 - a partial answer beats none
            hold = []
        try:
            txs = f_first.result(timeout=8)[0]
        except Exception:  # noqa: BLE001
            txs = []
        creates, dev_of = f_cr.result(timeout=8)
    ev = [e for tx in txs for e in parse_trade_events((tx.get("meta") or {}).get("logMessages") or []) if e["mint"] == m]
    ev.sort(key=lambda e: e["ts"])
    creator = dev_of.get(m)
    if not creator and txs:
        k0 = txs[0]["transaction"]["message"]["accountKeys"][0]
        creator = k0 if isinstance(k0, str) else k0.get("pubkey")
    curve = _curve_accounts(m, pair)
    accts = [(a["address"], float(a.get("uiAmount") or 0)) for a in hold if a["address"] not in curve]
    try:
        supply = float((f_sup.result(timeout=8) or {})["value"]["uiAmount"]) or 1e9
    except Exception:  # noqa: BLE001
        supply = 1e9
    supply -= 1e9 if supply > 1.5e9 else 0      # Mayhem mode: the agent's extra 1B is not tradable supply
    top10 = sum(v for _, v in accts[:10]) / supply
    top1 = accts[0][1] / supply if accts else 0.0
    first_ts = ev[0]["ts"] if ev else None
    dev_buy = sum(e["sol"] for e in ev if e["buy"] and e["user"] == creator) / 1e9
    dev_sold = any((not e["buy"]) and e["user"] == creator for e in ev)
    launch = [e for e in ev if e["buy"] and e["user"] != creator and first_ts is not None and e["ts"] <= first_ts + 1]
    launch_tok = sum(e["tok"] for e in launch) / 1e6 / supply
    launch_w = len({e["user"] for e in launch})
    n_created = creates.get(creator, 0) if creator else 0
    flags = []
    if top1 > 0.15:
        flags.append((3, f"one wallet holds {top1:.0%} of supply"))
    elif top1 > 0.08:
        flags.append((1, f"largest holder has {top1:.0%}"))
    if top10 > 0.40:
        flags.append((3, f"top-10 holders own {top10:.0%}"))
    elif top10 > 0.25:
        flags.append((1, f"top-10 holders own {top10:.0%}"))
    if launch_tok > 0.20:
        flags.append((3, f"{launch_w} wallets bought {launch_tok:.0%} of supply in the launch second (bundle / snipers)"))
    elif launch_tok > 0.08:
        flags.append((1, f"launch-second buyers took {launch_tok:.0%} ({launch_w} wallets)"))
    if dev_sold:
        flags.append((2, "creator already sold in the first transactions"))
    if n_created >= 10:
        flags.append((3, f"creator launched {n_created} coins we've recorded (serial deployer)"))
    elif n_created >= 3:
        flags.append((1, f"creator launched {n_created} coins we've recorded"))
    if dev_buy >= 4 and not dev_sold and first_ts is not None and time.time() - first_ts < 180:
        # SB (vault "2026-10-25 fast-dumping devs (SB)"): our bots' 17 entries with a >= 4 SOL dev bag still held all saw the
        # dev sell within 60 s (mean -33%); the first 100 transactions only cover a young coin, hence the 3 min limit
        flags.append((4, f"creator bought {dev_buy:.1f} SOL at launch and hasn't sold: devs like this dumped within a minute "
                         f"in all 17 of our bots' entries (avg -33%)"))
    elif dev_buy >= 5:
        flags.append((1, f"creator bought {dev_buy:.1f} SOL at launch (big dev bag)"))
    fund = _funding.get(m) or {}
    if fund.get("serial"):
        flags.append((2, f"creator's funder already funded {fund['serial']} signalled creators"))
    if (fund.get("cluster") or 0) >= 0.15:
        flags.append((2, f"one funder controls {fund['cluster']:.0%} of the top-20 holders"))
    score = sum(pts for pts, _ in flags)
    risk = "HIGH" if score >= 4 else "MEDIUM" if score >= 2 else "LOW"
    out = dict(m=m, pair=pair, t=time.time(), ms=int(1000 * (time.time() - t0)), risk=risk, score=score,
               flags=[dict(points=pts, text=txt) for pts, txt in flags], top10=top10, top1=top1, launch_share=launch_tok,
               launch_wallets=launch_w, dev_buy_sol=dev_buy, dev_sold=dev_sold, creator_coins=n_created,
               holders_seen=len(accts), launch_seen=len(ev))
    _quick[m] = out
    if not bg:
        scores(m)                                  # starts the narrative + runner scoring in the background
    return out


# ---------------- narrative and runner scores (background, cached per coin) ----------------
_scores: dict = {}
_llm: dict = {}


def _scores_job(m):
    res = dict(status="done")
    try:
        from .research import m1_bot as M1
        asset = _rpc("getAsset", {"id": m}) or {}
        content = asset.get("content") or {}
        md = content.get("metadata") or {}
        uri = content.get("json_uri")
        meta = {}
        if uri:
            try:
                meta = requests.get(uri, timeout=8).json()
            except (requests.RequestException, ValueError):
                meta = {}
        name, sym = md.get("name") or meta.get("name"), md.get("symbol") or meta.get("symbol")
        desc = str(meta.get("description", ""))[:400]
        link = next((str(meta[k]) for k in ("twitter", "website", "telegram") if M1.LINK.match(str(meta.get(k) or ""))), None)
        res.update(name=name, sym=sym, link=link)
        if link:
            if "llm" not in _llm:
                _llm["llm"] = M1.Llm()
            info, _ = M1.tweet_info(M1.SOC, link)
            sc, crit, why, _b = _llm["llm"].score(M1.PROMPT.format(name=name, sym=sym, desc=desc or "(none)", link=link,
                                                                   tweet=info, earlier="", improves=""))
            res.update(narrative=sc, criterion=crit, why=why, model=_llm["llm"].used)
        else:
            res.update(narrative=None, why="no X / website link in the coin's metadata, so M1 wouldn't score it")
    except Exception as exc:  # noqa: BLE001 - shown in the panel
        res.update(narrative=None, why=f"narrative scoring failed: {type(exc).__name__}")
    _scores[m] = dict(res, status="runner pending")
    try:
        res.update(_runner(m))
    except Exception as exc:  # noqa: BLE001
        res.update(runner=None, runner_why=f"runner score failed: {type(exc).__name__}")
    _scores[m] = res


def _runner(m):
    """M5's runner model probability at the coin's band moment (needs the history from launch)."""
    from .research.m5_bt import Coin, identical_top5
    from .research.runners import decision
    rows, complete = feed_rows(m)
    if not rows or not complete:
        from .research.bw_test import gtfa
        from .research.fl_test import helius_url
        from .scalper.trade_events import parse_trade_events
        txs, _ = gtfa(helius_url(), m, 0, time.time(), max_pages=30)
        rows, bal, h = [], {}, 0
        for tx in txs:
            for e in parse_trade_events((tx.get("meta") or {}).get("logMessages") or []):
                if e["mint"] != m or not e["vt"] or not e["vs"]:
                    continue
                tok, w = e["tok"] / 1e6, e["user"]
                b0 = bal.get(w, 0.0)
                b1 = b0 + (tok if e["buy"] else -tok)
                bal[w] = b1
                h += (b0 <= 1 < b1) - (b1 <= 1 < b0)
                rows.append([float(e["ts"]), e["vs"] / e["vt"] * 1e-3, e["buy"], e["sol"] / 1e9, w, tok, h])
        rows.sort(key=lambda r: r[0])
    if len(rows) < 10:
        return dict(runner=None, runner_why="too few trades yet")
    model = json.loads((DATA_DIR / "m1" / "m5_model.json").read_text())
    t0 = rows[0][0]
    i = decision(rows, t0)
    if i is None:
        return dict(runner=None, runner_why="hasn't reached M5's decision point (band) yet")
    f, k = Coin({"rows": rows, "t0": t0}).feats(min(time.time(), rows[i][0] + 60), i)
    x = [(math.log1p(max(0.0, f[n])) if n not in ("vs_band", "vs_high") else f[n]) for n in model["feats"]]
    z = model["intercept"] + sum(w * (v - mu) / sd for w, v, mu, sd in zip(model["coef"], x, model["mu"], model["sd"]))
    p = 1 / (1 + math.exp(-z))
    return dict(runner=p, runner_cut=model["P"], runner_pass=bool(p >= model["P"] and f["mc_k"] <= 20 and not identical_top5(rows, k)),
                runner_why=f"at M5's decision point ({time.strftime('%H:%M:%S', time.localtime(rows[i][0]))}); M5 buys at >= {model['P']:.2f}")


def scores(m):
    if m not in _scores:
        _scores[m] = dict(status="scoring")
        threading.Thread(target=_scores_job, args=(m,), daemon=True).start()
    return _scores[m]


def quick_json(qs: dict) -> dict:
    return quick((qs.get("m") or [""])[0].strip())


threading.Thread(target=_creates, daemon=True).start()     # preload the creator record so the first lookup is fast


# ---------------- hot coins for the trenches page (callouts, our models' picks, runner signals, hype) ----------------
_hot = dict(t=0.0, out=None)
_addr: dict = {}             # mint -> (bonding curve, PumpSwap pool), the addresses padre links use


def _tail(path, max_bytes=3_000_000):
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            n = fh.tell()
            fh.seek(max(0, n - max_bytes))
            data = fh.read().decode("utf-8", errors="ignore").splitlines()
        return data[1:] if n > max_bytes else data
    except OSError:
        return []


def _addresses(m):
    if m not in _addr:
        from .research.pda import PUMP_PROGRAM, b58decode, find_pda, pumpswap_pool_address
        try:
            _addr[m] = (find_pda([b"bonding-curve", b58decode(m)], PUMP_PROGRAM), pumpswap_pool_address(m))
        except (ValueError, KeyError):
            _addr[m] = (None, None)
    return _addr[m]


# Market scan: pump.fun's coin lists (most recently traded + newest), every 8 s while a padre tab polls /hot, with 10 min of
# history per coin, so the highlighter sees momentum on every active coin, not only the ones our bots watch.
PUMP_LIST = "https://frontend-api-v3.pump.fun/coins"
CURVE_TOKENS = 793_100_000                   # tokens sold on the bonding curve before it migrates
_scan = dict(want_t=0.0, t=0.0, hist=defaultdict(list), info={}, rug={}, rug_q=[], logged={})


def _scan_once(H):
    now = time.time()
    got = []
    for q in [f"offset={o}&limit=50&sort=last_trade_timestamp&order=DESC" for o in (0, 50, 100, 150)] + \
             ["offset=0&limit=50&sort=created_timestamp&order=DESC"]:
        try:
            r = requests.get(f"{PUMP_LIST}?{q}&includeNsfw=true", headers=H, timeout=10)
            got += r.json() if r.ok else []
        except (requests.RequestException, ValueError):
            pass
    for c in got:
        m, mc = c.get("mint"), c.get("usd_market_cap")
        if not m or not mc:
            continue
        h = _scan["hist"][m]
        if not h or now - h[-1][0] > 4:
            h.append((now, mc, c.get("reply_count") or 0))
        _scan["info"][m] = dict(sym=c.get("symbol"), created=(c.get("created_timestamp") or 0) / 1000, complete=c.get("complete"),
                                ath=c.get("ath_market_cap") or 0, live=c.get("is_currently_live"),
                                prog=None if c.get("complete") else 1 - (c.get("real_token_reserves") or 0) / 1e6 / CURVE_TOKENS,
                                curve=c.get("bonding_curve"), pool=c.get("pump_swap_pool") or c.get("pool_address"))
    for m in list(_scan["hist"]):
        h = _scan["hist"][m] = [x for x in _scan["hist"][m] if now - x[0] < 600]
        if not h:
            del _scan["hist"][m]
            _scan["info"].pop(m, None)
    _scan["t"] = now


def _scanner():
    from .research.rn_collect import _pump_headers
    H = _pump_headers()
    while True:
        if time.time() - _scan["want_t"] < 90:
            _scan_once(H)
            time.sleep(8)
        else:
            time.sleep(2)


def _rug_worker():
    """Background rug check (quick, ~1 s) for coins the scan flags, newest first; one at a time, cached 10 min."""
    while True:
        q = _scan["rug_q"]
        m = q.pop() if q else None
        if not m or time.time() - _scan["rug"].get(m, {}).get("t", 0) < 600:
            time.sleep(0.5)
            continue
        try:
            _scan["rug"][m] = dict(t=time.time(), risk=quick(m, bg=True).get("risk"))
        except Exception:  # noqa: BLE001 - an unchecked coin just stays unchecked
            _scan["rug"][m] = dict(t=time.time(), risk=None)
        time.sleep(15)                           # 4 checks a minute at most: each one reads 100 transactions from Helius


def _potential(m, now):
    """Momentum signals from the scan history: (kind, text, weight) list. Heuristics, not yet validated (logged for that)."""
    h, inf = _scan["hist"].get(m) or [], _scan["info"].get(m) or {}
    if len(h) < 2 or not inf:
        return []
    t1, mc, rep = h[-1]
    if mc < 8_000 or mc > 5_000_000 or now - t1 > 60:
        return []
    out = []
    lo3 = min(x[1] for x in h if t1 - x[0] <= 180)
    lo10 = min(x[1] for x in h)
    age = now - (inf["created"] or now)
    if mc >= 2.5 * lo3:
        out.append(("pump", f"up {mc / lo3 - 1:.0%} in 3 min", 3))
    elif mc >= 1.5 * lo3:
        out.append(("pump", f"up {mc / lo3 - 1:.0%} in 3 min", 2))
    elif mc >= 2 * lo10 and mc >= max(x[1] for x in h) * 0.9:
        out.append(("pump", f"up {mc / lo10 - 1:.0%} in 10 min, at its high", 2))
    if not inf["complete"] and age < 1200 and mc >= 25_000 and mc >= lo3:
        out.append(("fast", f"${mc / 1e3:.0f}k only {age / 60:.0f} min after launch", 2))
    if not inf["complete"] and (inf["prog"] or 0) >= 0.8 and mc > h[0][1]:
        out.append(("grad", f"{inf['prog']:.0%} of the curve filled, climbing", 1))
    if inf["complete"] and inf["ath"] >= 60_000 and mc >= 0.95 * inf["ath"] and mc > lo3 * 1.1:
        out.append(("ath", f"breaking its high (${inf['ath'] / 1e3:.0f}k) after migrating", 2))
    old = [x[2] for x in h if t1 - x[0] >= 120]
    if old and rep - old[-1] >= 8:
        out.append(("chat", f"+{rep - old[-1]} replies in the last few minutes", 1))
    if inf["live"] and mc >= lo3:
        out.append(("live", "livestreaming now", 1))
    return out


threading.Thread(target=_scanner, daemon=True).start()
threading.Thread(target=_rug_worker, daemon=True).start()


def hot() -> dict:
    """Coins lighting up right now, with reasons and a tier: hot (a strong signal: callers, our models, the runner model,
    decu), watch (momentum / potential from the market scan) or risky (pumping, but the rug check says HIGH).
    Cached 4 s (every open padre tab polls it)."""
    now = time.time()
    _scan["want_t"] = now
    if _hot["out"] is not None and now - _hot["t"] < 4:
        return _hot["out"]
    found: dict = {}

    def add(m, kind, text, weight, t):
        x = found.setdefault(m, dict(m=m, reasons=[], score=0, t=t))
        if kind not in {r["kind"] for r in x["reasons"]}:
            x["reasons"].append(dict(kind=kind, text=text))
            x["score"] += weight
        x["t"] = max(x["t"], t)

    tracked = _tracked()
    # 1. callouts: a top pump.fun caller's wallet bought (feed's leader capture), 15 min
    for line in _tail(DATA_DIR / "feed" / "leader_trades.jsonl", 2_000_000):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not e.get("buy") or now - e.get("recv", 0) > 900 or (e.get("sol") or 0) < 0.05:
            continue
        lab = tracked.get(e["w"], "")
        if lab.startswith("elite caller"):
            add(e["m"], "elite", lab.split(" (")[0] + " bought (" + lab.split(" (")[1], 4, e["recv"])
        elif "caller" in lab:
            add(e["m"], "callout", "top caller bought " + lab.split(" (")[0].replace("top caller ", ""), 3, e["recv"])
        elif e["w"] == DECU and now - e["recv"] < 600:
            add(e["m"], "decu", "decu bought", 2, e["recv"])
        elif lab and now - e["recv"] < 300:
            add(e["m"], "smart", f"{lab} bought", 1, e["recv"])
    # 2. our models bought it (open now or bought in the last 15 min)
    for v, name in (("m12", "M12"), ("m8", "M8"), ("m9", "M9"), ("m11", "M11")):
        try:
            st = json.loads((DATA_DIR / v / "status.json").read_text(encoding="utf-8"))
            for o in st.get("open", []):
                add(o["m"], "model", f"{name} is in it", 3, o.get("t") or now)
        except (OSError, ValueError, KeyError):
            pass
        for line in _tail(DATA_DIR / v / "trades.jsonl", 200_000):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if now - r.get("t", 0) < 900:
                add(r["m"], "model", f"{name} bought", 3, r["t"])
    # 3. runner model: M5's probability at or above its buy cut, last 10 min
    try:
        cut = json.loads((DATA_DIR / "m1" / "m5_model.json").read_text())["P"]
    except (OSError, ValueError, KeyError):
        cut = 0.37
    for line in _tail(DATA_DIR / "m5" / "cands.jsonl", 3_000_000):
        if '"p"' not in line:
            continue
        try:
            c = json.loads(line)
        except ValueError:
            continue
        if now - c.get("t", 0) < 600 and (c.get("p") or 0) >= cut:
            add(c["m"], "runner", f"runner model {c['p']:.0%}", 3, c["t"])
    # 4. hype: livestream with 20+ viewers or DexScreener boost, last 30 min (HY recorder)
    for line in _tail(DATA_DIR / "hype" / "signals.jsonl", 500_000):
        try:
            j = json.loads(line)
        except ValueError:
            continue
        if now - j.get("t", 0) < 1800:
            if j["src"] == "LIVE20":
                add(j["m"], "live", f"livestream, {j.get('viewers') or '20+'} viewers", 2, j["t"])
            else:
                add(j["m"], "boost", "DexScreener boost", 1, j["t"])
    # 5. potential: momentum from the market scan
    for m in list(_scan["hist"]):
        for kind, text, w in _potential(m, now):
            add(m, kind, text, w, now)
    STRONG = {"elite", "callout", "model", "runner", "decu"}
    for x in found.values():
        kinds = {r["kind"] for r in x["reasons"]}
        rug = _scan["rug"].get(x["m"]) or {}
        x["rug"] = rug.get("risk")
        if kinds & STRONG or x["score"] >= 4:
            x["tier"] = "hot"
        else:
            x["tier"] = "watch"
        if x["rug"] == "HIGH":
            x["tier"] = "risky"
        if not rug and x["score"] >= 2 and x["m"] not in _scan["rug_q"]:
            _scan["rug_q"].append(x["m"])
    del _scan["rug_q"][:-30]
    keep = [x for x in found.values() if x["score"] >= 2 or {r["kind"] for r in x["reasons"]} & (STRONG | {"smart", "live"})]
    rows = sorted(keep, key=lambda x: (-x["score"], -x["t"]))[:200]
    for x in rows:
        inf = _scan["info"].get(x["m"]) or {}
        x["sym"] = inf.get("sym")
        x["curve"], x["pool"] = (inf["curve"], inf["pool"]) if inf.get("curve") else _addresses(x["m"])
        if x["tier"] != "risky" and x["score"] >= 2 and now - _scan["logged"].get(x["m"], 0) > 1800:
            _scan["logged"][x["m"]] = now      # flag log, to measure later how often a flag led to a 2x / a runner
            h = _scan["hist"].get(x["m"]) or [(0, None, 0)]
            with (ASSIST / "hot_flags.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(dict(t=round(now), m=x["m"], tier=x["tier"], score=x["score"], mc=h[-1][1], rug=x["rug"],
                                         kinds=sorted({r["kind"] for r in x["reasons"]}))) + "\n")
    out = dict(t=now, coins=rows, scanned=len(_scan["hist"]))
    _hot.update(t=now, out=out)
    return out


def hot_json(_qs: dict) -> dict:
    return hot()
