"""M13 paper bot (vault "2026-10-25 M13 - runner model at migration (M13)"): buys pump.fun graduations at migration when the
RD runner model says so, rides them with M12r's ratchet. Twin book M13b buys every graduation the same way (no model), so the
model's lift is measured on the same clock.

What it combines:
  - RD (holdout 44% / top-5% 41% precision, AUC 0.915): logistic model on the launch window (<= create + 120 s) and the
    run-up window (last 200 curve trades <= migration) + metadata; refit on all 1,981 coins (data/m1/m13_model.json).
  - RN1000: runners peak about 1 h after migration (median), so entry is at migration + 60 s; exits ratchet, 6 h cap.
  - RN1000 factory fingerprint: template description, or curve filled in < 3 min with <= 40 launch buyers -> skip.
  - M12r: stop 0.8x; once 2x, stop 1.25x; at each whole multiple N >= 3, stop N - 1.
  - Costs as in the RD check: 1.25% fee + 1% slippage, each side.

Paper only: it never signs or sends a transaction.

    uv run python -u -m tradeslave.research.m13_bot
"""

from __future__ import annotations

import json
import math
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from dotenv import dotenv_values
from urllib.parse import urlsplit

from ..paths import DATA_DIR, PROJECT_ROOT
from .backfill_trades import parse_trade
from .bw_test import MIG_AUTH, pool_and_mint
from .rb_collect import _pump_headers
from .rb_model import _wallets, features
from .runners import SOL_USD
from .ticks import curve_of

M13, M13B = DATA_DIR / "m13", DATA_DIR / "m13b"
RULES, RULES_B = "m13-v1", "m13b-v1"
COST, ENTRY_DELAY, STALE, HOLD, POLL = 0.0225, 60, 180, 6 * 3600, 2.0
CURVE_MIN_S, START_MAX, MIN_POOL_SOL = 60, 10_000, 40
HISTORY_PAGES, HISTORY_CANDIDATES, TX_WORKERS = 20, 200, 4
_RPC_PACE_LOCK = threading.Lock()
_RPC_LAST: dict[str, float] = {}
MODEL = json.loads((DATA_DIR / "m1" / "m13_model.json").read_text())


def rpc_urls():
    env = dotenv_values(PROJECT_ROOT / ".env")
    urls = [u.strip().strip('"').split("#")[0] for u in (env.get("HISTORY_RPC_URLS") or "").split(",")]
    good = [u for u in urls if u.startswith("http") and "helius" not in u]          # Helius credits stay for research
    order = ("alchemy", "rpcfast", "shyft")
    return sorted(good, key=lambda u: next((i for i, k in enumerate(order) if k in u), 9))


URLS = rpc_urls()


def rpc(method, params):
    last = None
    for u in URLS:
        try:
            r = requests.post(u, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15)
            j = r.json()
            if "result" in j:
                result = j["result"]
                # Some RPC plans return an empty history page instead of an error when they do not
                # retain the account's signatures. Try the next configured provider in that case.
                if result is None or (method == "getSignaturesForAddress" and not result):
                    last = "empty result"
                    continue
                return result
            last = j.get("error")
        except (requests.RequestException, ValueError) as exc:
            last = type(exc).__name__
    raise RuntimeError(f"{method}: {last}")


def _history_call(session, url, method, params):
    """A small retrying JSON-RPC call for the short M13 feature window."""
    last = "empty response"
    for attempt in range(3):
        with _RPC_PACE_LOCK:
            wait = 0.125 - (time.monotonic() - _RPC_LAST.get(url, 0.0))
            if wait > 0:
                time.sleep(wait)
            _RPC_LAST[url] = time.monotonic()
        try:
            r = session.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=12)
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                time.sleep(0.4 * (attempt + 1))
                continue
            r.raise_for_status()
            body = r.json()
            if body.get("error"):
                last = f"RPC {body['error'].get('code', 'error')}"
                break
            return body.get("result")
        except (requests.RequestException, ValueError) as exc:
            last = type(exc).__name__
            if attempt < 2:
                time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(last)


def migration_rows(mint, created, migrated):
    """Build M13's two pump-curve feature windows from RPC transaction history.

    The retired Pump swap-history endpoint is not used. Signatures are read from every configured
    RPC so an endpoint with no archival history cannot mask one that has it. Only transactions in
    the two model windows are fetched.
    """
    curve = curve_of(mint)
    by_signature, provider_counts, errors = {}, {}, []
    lower = created - 5
    for url in URLS:
        session = requests.Session()
        before, count = None, 0
        try:
            for _ in range(HISTORY_PAGES):
                options = {"limit": 1000, "commitment": "confirmed"}
                if before:
                    options["before"] = before
                page = _history_call(session, url, "getSignaturesForAddress", [curve, options])
                if not page:
                    break
                count += len(page)
                for item in page:
                    stamp = item.get("blockTime")
                    sig = item.get("signature")
                    if sig and stamp and lower <= stamp <= migrated:
                        by_signature.setdefault(sig, dict(item, _provider=url))
                oldest = page[-1]
                if (oldest.get("blockTime") or 0) < lower or len(page) < 1000:
                    break
                before = oldest.get("signature")
                if not before:
                    break
        except Exception as exc:  # noqa: BLE001 - try other configured RPCs
            errors.append(type(exc).__name__)
        if count:
            provider_counts[url] = count
        session.close()

    if not by_signature:
        why = errors[-1] if errors else "no signatures"
        raise RuntimeError(f"no curve history ({why})")

    signatures = sorted(by_signature.values(), key=lambda x: (x.get("blockTime") or 0, x.get("slot") or 0), reverse=True)
    for index, item in enumerate(signatures):
        item["_rank"] = index
    launch_end = created + 120
    launch = [x for x in signatures if x["blockTime"] <= launch_end][:HISTORY_CANDIDATES]
    runup = signatures[:HISTORY_CANDIDATES]
    selected = {x["signature"]: x for x in launch + runup}
    providers = sorted(provider_counts, key=provider_counts.get, reverse=True)
    # Prefer the RPC that actually returned curve history; some endpoints answer recent calls but
    # time out on old transactions. Fall back only when that provider lacks the transaction.
    tx_providers = providers + [url for url in URLS if url not in providers]

    def fetch(item):
        params = [item["signature"], {"encoding": "json", "maxSupportedTransactionVersion": 1,
                                      "commitment": "confirmed"}]
        for url in tx_providers or URLS:
            session = requests.Session()
            try:
                tx = _history_call(session, url, "getTransaction", params)
                if tx:
                    return tx
            except Exception:  # noqa: BLE001 - another provider may retain this transaction
                continue
            finally:
                session.close()
        return None

    with ThreadPoolExecutor(max_workers=TX_WORKERS) as pool:
        transactions = list(pool.map(fetch, selected.values()))

    rows = []
    for item, tx in zip(selected.values(), transactions):
        if not tx:
            continue
        trade = parse_trade(tx, mint, curve)
        if not trade or not trade.get("sol") or not trade.get("tok"):
            continue
        rows.append((trade["t"], item.get("slot") or 0, -item["_rank"],
                     [trade["t"], trade["w"], bool(trade["b"]), trade["sol"], trade["tok"],
                      trade["sol"] / trade["tok"] * SOL_USD, "pump"]))
    # getSignaturesForAddress is newest-first. Keep chain order for same-second transactions.
    rows.sort(key=lambda x: (x[0], x[1], x[2]))
    return [x[3] for x in rows]


def score(f):
    z = MODEL["intercept"] + sum(c * (f[n] - mu) / (sd or 1) for n, c, mu, sd in
                                 zip(MODEL["names"], MODEL["coef"], MODEL["mean"], MODEL["scale"]))
    return 1 / (1 + math.exp(-z))


def factory(f):
    return bool(f["template"]) or (math.expm1(f["curve_min"]) < 3 and f["l_buyers"] <= 40)


POOL_SOL = {}                                           # mint -> SOL in the pool at the last read


def vault_price(vaults):
    """{mint: SOL per token} from the PumpSwap pool vaults (base token, WSOL)."""
    keys = [k for b, q in vaults.values() for k in (b, q)]
    out = {}
    for i in range(0, len(keys), 100):
        res = rpc("getMultipleAccounts", [keys[i:i + 100], {"encoding": "jsonParsed", "commitment": "confirmed"}])["value"]
        amt = {}
        for k, acc in zip(keys[i:i + 100], res):
            try:
                amt[k] = float(acc["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
            except (TypeError, KeyError):
                amt[k] = 0.0
        for m, (b, q) in vaults.items():
            if b in amt and amt.get(b):
                out[m] = amt.get(q, 0.0) / amt[b]
                POOL_SOL[m] = amt.get(q, 0.0)
    return out


class Book:
    def __init__(self, d, rules, label):
        self.d, self.rules, self.label = d, rules, label
        d.mkdir(parents=True, exist_ok=True)
        self.open, self.closed = {}, sum(1 for line in (d / "trades.jsonl").open() if line.strip()) if (d / "trades.jsonl").exists() else 0

    def enter(self, c, p0, now):
        self.open[c["m"]] = dict(m=c["m"], name=c["name"], sym=c["sym"], t=now, t_mig=c["t_mig"], p0=p0, last=p0, peak=p0,
                                 stop=0.8 * p0, armed=False, score=c["p"], rules=self.rules, pool=c["pool"],
                                 mc=p0 * 1e9 * SOL_USD)
        print(time.strftime("%H:%M:%S"), f"{self.label} BUY {c['sym']} p={c['p']:.2f} at {p0 * 1e9:.1f} SOL mcap", flush=True)

    def tick(self, prices, now):
        for m, o in list(self.open.items()):
            p = prices.get(m)
            if not p:
                continue
            o["last"], o["peak"] = p, max(o["peak"], p)
            if o["peak"] >= 2 * o["p0"]:
                o["armed"] = True
            if o["armed"]:
                n = math.floor(o["peak"] / o["p0"])
                o["stop"] = max(o["stop"], 1.25 * o["p0"], (n - 1) * o["p0"] if n >= 3 else 0)
            why = "stop" if p <= o["stop"] else "time" if now - o["t"] >= HOLD else None
            if why:
                self.close(m, p, now, why)

    def close(self, m, p, now, why):
        o = self.open.pop(m)
        ret = p / o["p0"] * (1 - COST) / (1 + COST) - 1
        o.update(t_exit=now, p_exit=p, exit=why, ret=ret, peak_x=o["peak"] / o["p0"])
        with (self.d / "trades.jsonl").open("a") as fh:
            fh.write(json.dumps(o) + "\n")
        self.closed += 1
        print(time.strftime("%H:%M:%S"), f"{self.label} SELL {o['sym']} {why} {ret:+.1%} (peak {o['peak_x']:.2f}x)", flush=True)

    def status(self, now, extra):
        st = dict(t=now, rules=self.rules, closed=self.closed, open=[
            dict(m=o["m"], name=o["name"], t=o["t"], p0=o["p0"], last=o["last"], peak=o["peak"], stop=o["stop"], score=o["score"],
                 mc=o["last"] * 1e9 * SOL_USD) for o in self.open.values()], **extra)
        tmp = self.d / "status.tmp"
        tmp.write_text(json.dumps(st))
        for i in range(10):                              # Windows: the dashboard may have status.json open
            try:
                tmp.replace(self.d / "status.json")
                return
            except PermissionError:
                time.sleep(0.05 * (i + 1))


def watch(q):
    """New migrations from the migration authority's signatures (polled every 3 s; no backfill)."""
    seen = {s["signature"] for s in rpc("getSignaturesForAddress", [MIG_AUTH, {"limit": 50}])}
    mints = set()                                       # one migration can span two signed transactions
    while True:
        try:
            sigs = rpc("getSignaturesForAddress", [MIG_AUTH, {"limit": 50}])
            for s in reversed(sigs):
                if s["signature"] in seen or s.get("err"):
                    continue
                seen.add(s["signature"])
                tx = rpc("getTransaction", [s["signature"], {"encoding": "json", "maxSupportedTransactionVersion": 1,
                                                            "commitment": "confirmed"}])
                if not tx:
                    continue
                pool, mint = pool_and_mint(tx)
                if not mint:
                    continue
                keys = tx["transaction"]["message"]["accountKeys"]
                la = (tx["meta"].get("loadedAddresses") or {})
                keys = keys + la.get("writable", []) + la.get("readonly", [])
                v = {b["mint"]: keys[b["accountIndex"]] for b in tx["meta"].get("postTokenBalances") or [] if b.get("owner") == pool}
                wsol = "So11111111111111111111111111111111111111112"
                if mint in v and wsol in v and mint not in mints:
                    mints.add(mint)
                    q.put(dict(m=mint, pool=pool, t_mig=float(s.get("blockTime") or tx.get("blockTime") or time.time()),
                               vaults=(v[mint], v[wsol])))
        except Exception as exc:  # noqa: BLE001 - keep watching
            print(time.strftime("%H:%M:%S"), f"watch error: {type(exc).__name__} {str(exc)[:80]}", flush=True)
        time.sleep(3)


def evaluate(q, ready, wallets):
    """Score each migration: details, two trade windows, features, model, factory filter."""
    H = _pump_headers()
    s = requests.Session()
    while True:
        c = q.get()
        rec = dict(t=time.time(), m=c["m"], t_mig=c["t_mig"])
        try:
            r = s.post("https://frontend-api-v3.pump.fun/coins-v2/mints", headers=H, json={"mints": [c["m"]]}, timeout=20)
            d = (r.json() or [None])[0] if r.ok else None
            d = {k: d.get(k) for k in ("name", "symbol", "description", "twitter", "website", "telegram", "created_timestamp",
                                       "creator")} if d else None
            tc = (d or {}).get("created_timestamp")
            if not tc:
                rec.update(skip="no details")
            elif c["t_mig"] - tc / 1000 < CURVE_MIN_S:
                rec.update(skip="instant graduation")
            else:
                tc /= 1000
                rows = migration_rows(c["m"], tc, c["t_mig"])
                rec.update(history_source="solana-rpc", history_trades=len(rows))
                f = features(dict(m=c["m"], created=tc, t_mig=c["t_mig"], rows=rows), d, wallets)
                if f is None:
                    rec.update(skip="too few curve trades")
                elif f["start_mc"] >= START_MAX:
                    rec.update(skip="start >= $10k")
                else:
                    p, fac = score(f), factory(f)
                    c.update(p=p, factory=fac, name=d.get("name"), sym=d.get("symbol"), f=f)
                    rec.update(p=round(p, 4), factory=fac, sym=d.get("symbol"), buy=bool(p >= MODEL["thr"] and not fac),
                               lag=round(time.time() - c["t_mig"], 1))
                    ready.put(c)
        except Exception as exc:  # noqa: BLE001
            rec.update(skip=f"error {type(exc).__name__}")
        with (M13 / "scored.jsonl").open("a") as fh:
            fh.write(json.dumps(rec) + "\n")


def main() -> None:
    M13.mkdir(parents=True, exist_ok=True)
    m13, m13b = Book(M13, RULES, "M13 "), Book(M13B, RULES_B, "M13b")
    q, ready, pending, vaults = queue.Queue(), queue.Queue(), [], {}
    wallets = _wallets()
    threading.Thread(target=watch, args=(q,), daemon=True).start()
    threading.Thread(target=evaluate, args=(q, ready, wallets), daemon=True).start()
    print(time.strftime("%H:%M:%S"), f"M13 up: threshold {MODEL['thr']:.3f}, RPC {[urlsplit(u).hostname for u in URLS]}", flush=True)
    last_status = 0.0
    while True:
        now = time.time()
        while not ready.empty():
            pending.append(ready.get())
        for c in [c for c in pending if now >= c["t_mig"] + ENTRY_DELAY]:
            pending.remove(c)
            if now - c["t_mig"] > STALE:
                continue                                        # too late for a migration-time entry; logged in scored.jsonl
            vaults[c["m"]] = c["vaults"]
            p0 = vault_price({c["m"]: c["vaults"]}).get(c["m"])
            if not p0 or POOL_SOL.get(c["m"], 0) < MIN_POOL_SOL:
                continue                                        # not a real graduation (those deposit ~60-85 SOL)
            m13b.enter(c, p0, now)
            if c["p"] >= MODEL["thr"] and not c["factory"]:
                m13.enter(c, p0, now)
        live = {m: v for m, v in vaults.items() if m in m13.open or m in m13b.open}
        if live:
            try:
                prices = vault_price(live)
                m13.tick(prices, now)
                m13b.tick(prices, now)
            except Exception as exc:  # noqa: BLE001
                print(time.strftime("%H:%M:%S"), f"price error: {type(exc).__name__}", flush=True)
        vaults = {m: v for m, v in vaults.items() if m in m13.open or m in m13b.open}
        if now - last_status > 10:
            last_status = now
            extra = dict(threshold=MODEL["thr"], waiting=len(pending))
            m13.status(now, extra)
            m13b.status(now, extra)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
