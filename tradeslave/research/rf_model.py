"""RF (vault "2026-10-25 runner forecast in the final stretch (RF)"): predict runners at the curve's 60-SOL point, with
chart features (A) and chart + meta / narrative features (B). Pre-registered 16:31 PDT; each test runs once.

    uv run python -X utf8 -m tradeslave.research.rf_model build     # features for D1, D2, F -> data/runners/rf/features.json
    uv run python -X utf8 -m tradeslave.research.rf_model meta      # test 1: does meta add? (RD holdout, stage R)
    uv run python -X utf8 -m tradeslave.research.rf_model forward   # test 2: G x R on the forward population F
    uv run python -X utf8 -m tradeslave.research.rf_model trade     # test 3 (report only): B's flags traded on 5-min candles
"""

from __future__ import annotations

import bisect
import gzip
import json
import math
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime

import numpy as np
import requests

from ..paths import DATA_DIR
from .rb_collect import OUT as RB, START_MAX
from .rb_model import COST, RUNNER_ATH, WALLET_FEATS, _auc, features, wilson
from .rf_collect import C1, D1_END, D2_HI, D2_LO, RF, crossing

WEIGHT_NON = 29514 / 4000                # curve graduations / uniform sample size (rb population.json)
TAU_TRAIN_END = datetime(2026, 10, 20).timestamp()
STOP = {"the", "of", "and", "for", "with", "coin", "token", "sol", "solana", "official", "pump", "inu", "on", "by"}
CATS = {
    "animal": "dog doge cat kitty kitten frog pepe penguin monkey ape bear bull fish bird duck hamster rat mouse wolf fox horse "
              "pig cow goat shiba tiger lion bunny rabbit panda otter capybara seal whale shark crab snail turtle owl chicken goose "
              "chihuahua puppy pup",
    "ai_tech": "ai agent gpt claude openai grok robot bot neural agi llm quantum tech protocol terminal code app",
    "pol_celeb": "trump elon musk biden maga president kanye drake mrbeast cz vitalik saylor melania barron vance obama putin pope "
                 "king queen prince",
    "crypto_meta": "pump sol solana coin token bonk wif moon 100x degen jeet rug ser wagmi bitcoin btc eth memecoin ticker cto",
}
CATS = {k: set(v.split()) for k, v in CATS.items()}
CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")
SOCIAL = ("x.com", "twitter.com", "tiktok.com", "youtube.com", "youtu.be", "instagram.com", "pump.fun")
META = ("g1_tok", "g6_tok", "r_tok", "r_sim", "cat_lift", "grad_rate6", "run_rate72", "sol_chg24", "tw_comm", "web_x",
        "web_other", "desc_at") + tuple("cat_" + k for k in (*CATS, "cn"))


# ---------------------------------------------------------------- text and meta

def name_tokens(d):
    name, sym = (d or {}).get("name") or "", (d or {}).get("symbol") or ""
    out = {w for w in re.findall(r"[a-z0-9]+", f"{name} {sym}".lower()) if len(w) >= 3 and w not in STOP}
    if CJK.search(name):
        out.add(name.strip().lower())
    return out


def all_tokens(d):
    desc = (d or {}).get("description") or ""
    return name_tokens(d) | {w for w in re.findall(r"[a-z0-9]+", desc.lower()) if len(w) >= 3 and w not in STOP}


def cats(d):
    name, sym, desc = ((d or {}).get(k) or "" for k in ("name", "symbol", "description"))
    words = set(re.findall(r"[a-z0-9]+", f"{name} {sym} {desc}".lower()))
    out = {k for k, v in CATS.items() if words & v}
    if CJK.search(name + sym):
        out.add("cn")
    return out


class Meta:
    """Every migration (instants included) with its name tokens, categories and final ATH; features use only the past."""

    def __init__(self):
        D = json.loads((RB / "details.json").read_text())
        rows = []
        for line in (DATA_DIR / "runners" / "migrations.jsonl").open():
            r = json.loads(line)
            d = D.get(r["m"])
            rows.append((r["t"], name_tokens(d), all_tokens(d), cats(d), (r.get("ath") or 0) >= RUNNER_ATH))
        rows.sort(key=lambda z: z[0])
        self.rows, self.ts = rows, [z[0] for z in rows]
        sol = json.loads((DATA_DIR / "runners" / "sol_usd.json").read_text())
        self.sol = sorted((float(k), v) for k, v in sol.items())
        self.sol_t = [k for k, _ in self.sol]

    def window(self, a, b):
        return self.rows[bisect.bisect_left(self.ts, a):bisect.bisect_left(self.ts, b)]

    def usd(self, t):
        i = max(0, bisect.bisect_right(self.sol_t, t) - 1)
        return self.sol[i][1]

    def features(self, d, t):
        nt, at, cs = name_tokens(d), all_tokens(d), cats(d)
        g1 = sum(1 for z in self.window(t - 3600, t) if z[1] & nt)
        g6w = self.window(t - 6 * 3600, t)
        old = self.window(t - 72 * 3600, t - 6 * 3600)
        run = [z for z in old if z[4]]
        lift = 0.0
        for c in cs:
            rc, gc = sum(c in z[3] for z in run), sum(c in z[3] for z in old)
            lift = max(lift, math.log((rc + 1) / (len(run) + 5)) - math.log((gc + 1) / (len(old) + 5)))
        sims = [len(at & z[2]) / len(at | z[2]) for z in run if at | z[2]]
        tw, web, desc = ((d or {}).get(k) or "" for k in ("twitter", "website", "description"))
        f = dict(g1_tok=math.log1p(g1), g6_tok=math.log1p(sum(1 for z in g6w if z[1] & nt)),
                 r_tok=float(sum(1 for z in run if z[1] & nt)), r_sim=max(sims, default=0.0), cat_lift=lift,
                 grad_rate6=math.log1p(len(g6w) / 6), run_rate72=(len(run) + 1) / (len(old) + 20),
                 sol_chg24=self.usd(t) / self.usd(t - 86400) - 1, tw_comm=float("communit" in tw.lower()),
                 web_x=float(any(s in web.lower() for s in ("x.com", "twitter.com"))),
                 web_other=float(bool(web) and not any(s in web.lower() for s in SOCIAL)), desc_at=float("@" in desc))
        for c in (*CATS, "cn"):
            f["cat_" + c] = float(c in cs)
        return f


# ---------------------------------------------------------------- building the three sets

def _at60(m, created, launch, rows, d, meta):
    """Features at the 60-SOL crossing, or (None, reason)."""
    i = crossing(rows)
    if i is None:
        return None, "no crossing"
    t60 = rows[i][0]
    if t60 - created < 60:
        return None, "instant (< 60 s to R 60)"
    x = dict(m=m, created=created, t_mig=t60, launch=launch, runup=rows[:i + 1][-200:])
    f = features(x, d, (set(), set(), set(), ""))
    if f is None:
        return None, "too few curve trades"
    if f["start_mc"] >= START_MAX:
        return None, "start >= $10k"
    for k in WALLET_FEATS:
        f.pop(k)
    f.update(meta.features(d, t60))
    return dict(t=t60, f=f, price=rows[i][3] / rows[i][4]), None


def build() -> None:
    meta = Meta()
    Drb = json.loads((RB / "details.json").read_text())
    Drf = json.loads((RF / "details.json").read_text())
    P = set(json.loads((RB / "population.json").read_text())["sample"])
    out, skip = [], defaultdict(lambda: defaultdict(int))
    # D1: RD graduations migrating before Oct 23
    seen = set()
    for p in sorted((RB / "trades_lite").glob("*.json")) + sorted((RB / "trades").glob("*.json")):
        x = json.loads(p.read_text())
        if x["m"] in seen or x["t_mig"] >= D1_END:
            continue
        seen.add(x["m"])
        if x["status"] != "ok" or ("launch" in x and not x.get("runup_v2")):
            skip["D1"]["fetch error"] += 1
            continue
        if "rows" in x:
            launch, rows = [r for r in x["rows"] if r[0] <= x["created"] + 120][-200:], [r for r in x["rows"] if r[0] <= x["t_mig"]]
        else:
            launch, rows = x["launch"], x["runup"]
            sp = RF / "stretch" / p.name
            if crossing(rows) is None and sp.exists():
                rows = json.loads(sp.read_text())["rows"]
        r, why = _at60(x["m"], x["created"], launch, rows, Drb.get(x["m"]), meta)
        if r is None:
            skip["D1"][why] += 1
            continue
        y = int((x.get("ath") or 0) >= RUNNER_ATH)
        out.append(dict(set="D1", m=x["m"], sample=x["m"] in P, grad=1, y=y, w=1.0 if y else WEIGHT_NON, **r))
    # D2: C1 full trade files (every create Oct 20 23:19 - Oct 22 09:00 reaching R >= 38)
    for p in sorted((C1 / "trades").glob("*.json.gz")):
        x = json.loads(gzip.open(p, "rt", encoding="utf-8").read())
        c = x["c"]
        if not D2_LO <= c["t"] < D2_HI:
            continue
        tr = x["trades"]
        rows = [[t[2], t[3], t[4], t[5], t[6], t[8] * meta.usd(t[2]), "pump-amm" if t[7] else "pump"] for t in tr]
        d = Drf.get(c["m"])
        r, why = _at60(c["m"], c["t"], [z for z in rows if z[0] <= c["t"] + 120][-200:], [z for z in rows if z[6] == "pump"],
                       d, meta)
        if r is None:
            skip["D2"][why] += 1
            continue
        grad = int(any(t[7] for t in tr) or bool((d or {}).get("complete")))
        out.append(dict(set="D2", m=c["m"], sample=True, grad=grad, y=int(grad and ((d or {}).get("ath") or 0) >= RUNNER_ATH),
                        w=1.0, **r))
    # F: the forward population
    for p in sorted((RF / "forward").glob("*.json")):
        x = json.loads(p.read_text())
        if x["status"] != "ok":
            skip["F"]["fetch: " + x["status"]] += 1
            continue
        d = Drf.get(x["m"])
        if d is None or d.get("ath") is None:
            skip["F"]["no pump.fun record / ATH"] += 1
            continue
        r, why = _at60(x["m"], x["created"], x["launch"], x["stretch"], d, meta)
        if r is None:
            skip["F"][why] += 1
            continue
        grad = int(bool(d.get("complete")))
        out.append(dict(set="F", m=x["m"], sample=True, grad=grad, y=int(grad and d["ath"] >= RUNNER_ATH), w=1.0, **r))
    (RF / "features.json").write_text(json.dumps(out))
    for s in ("D1", "D2", "F"):
        z = [r for r in out if r["set"] == s]
        print(f"{s}: {len(z)} coins, graduated {sum(r['grad'] for r in z)}, runners {sum(r['y'] for r in z)}; "
              f"skipped {dict(skip[s])}")


# ---------------------------------------------------------------- models

def names(rows, kind):
    a = sorted(n for n in rows[0]["f"] if n not in META)
    return a if kind == "A" else a + list(META)


def _X(rows, nm):
    return np.array([[r["f"][n] for n in nm] for r in rows], dtype=float)


def fit(rows, nm, target="y", weighted=True):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    m = make_pipeline(StandardScaler(), LogisticRegression(C=0.3, max_iter=3000))
    m.fit(_X(rows, nm), [r[target] for r in rows],
          logisticregression__sample_weight=[r["w"] for r in rows] if weighted else None)
    return m


def _rows():
    return json.loads((RF / "features.json").read_text())


def _once(name):
    p = RF / f"{name}_done.json"
    if p.exists():
        print(p.read_text())
        print(f"{name} was already run once; not rerunning")
        return None
    return p


def meta_test() -> None:
    from .rb_model import _split
    done = _once("meta")
    if done is None:
        return
    _, _, cut = _split(json.loads((RB / "features.json").read_text()))
    rows = _rows()
    d1 = [r for r in rows if r["set"] == "D1"]
    tr = [r for r in d1 if r["t"] < cut]
    ho = [r for r in d1 if r["t"] >= cut and r["sample"]]
    res, preds = dict(cut=cut, n_train=len(tr), n_holdout=len(ho), runners_holdout=sum(r["y"] for r in ho)), {}
    for k in ("A", "B"):
        nm = names(rows, k)
        p = fit(tr, nm).predict_proba(_X(ho, nm))[:, 1]
        preds[k] = p
        res[k] = dict(auc=_auc([(pi, r["y"], r["t"]) for pi, r in zip(p, ho)]))
    from sklearn.metrics import roc_auc_score
    y = np.array([r["y"] for r in ho])
    rng = np.random.default_rng(0)
    diffs = []
    for _ in range(2000):
        i = rng.integers(0, len(ho), len(ho))
        if 0 < y[i].sum() < len(i):
            diffs.append(roc_auc_score(y[i], preds["B"][i]) - roc_auc_score(y[i], preds["A"][i]))
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    res.update(delta_auc=res["B"]["auc"] - res["A"]["auc"], ci=[float(lo), float(hi)], meta_adds=bool(lo > 0))
    nm = names(rows, "B")
    m = fit(tr, nm)
    res["B_coef_meta"] = {n: float(c) for n, c in zip(nm, m.named_steps["logisticregression"].coef_[0]) if n in META}
    done.write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


def scores(rows, kind, r_train):
    """Score = P(graduate | t60) x P(runner | graduate); G on D2 (A features), R on r_train."""
    nmA, nm = names(rows, "A"), names(rows, kind)
    d2 = [r for r in rows if r["set"] == "D2"]
    G = fit(d2, nmA, "grad", weighted=False)
    R = fit(r_train, nm)
    return lambda zs: G.predict_proba(_X(zs, nmA))[:, 1] * R.predict_proba(_X(zs, nm))[:, 1]


def forward() -> None:
    done = _once("forward")
    if done is None:
        return
    rows = _rows()
    d1 = [r for r in rows if r["set"] == "D1"]
    d2 = [r for r in rows if r["set"] == "D2"]
    F = sorted((r for r in rows if r["set"] == "F"), key=lambda r: r["t"])
    days = (F[-1]["t"] - F[0]["t"]) / 86400
    base = sum(r["y"] for r in F) / len(F)
    res = dict(n=len(F), runners=sum(r["y"] for r in F), graduated=sum(r["grad"] for r in F), base=base, days=days)
    for k in ("A", "B"):
        tau = float(np.percentile(scores(rows, k, [r for r in d1 if r["t"] < TAU_TRAIN_END])(d2), 95))
        p = scores(rows, k, d1)(F)
        fl = [r for r, pi in zip(F, p) if pi >= tau]
        hits = sum(r["y"] for r in fl)
        lo, hi = wilson(hits, len(fl))
        ps = sorted(p, reverse=True)
        top = ps[max(0, int(0.05 * len(ps)) - 1)]
        res[k] = dict(tau=tau, auc=_auc([(pi, r["y"], r["t"]) for pi, r in zip(p, F)]),
                      auc_grad=_auc([(pi, r["grad"], r["t"]) for pi, r in zip(p, F)]), flags=len(fl), hits=hits,
                      precision=hits / max(1, len(fl)), wilson=[lo, hi], flags_per_day=len(fl) / days,
                      flags_not_graduated=sum(1 for r in fl if not r["grad"]),
                      top5pct=[sum(r["y"] for r, pi in zip(F, p) if pi >= top), sum(1 for pi in p if pi >= top)],
                      flagged=[dict(m=r["m"], t=r["t"], price=r["price"], p=float(pi), y=r["y"], grad=r["grad"])
                               for r, pi in zip(F, p) if pi >= tau])
    b = res["B"]
    res["meets_40"] = bool(b["precision"] >= 0.40 and b["wilson"][0] > base)
    done.write_text(json.dumps(res, indent=1))
    for k in ("A", "B"):
        print(k, {a: v for a, v in res[k].items() if a != "flagged"})
    print({a: v for a, v in res.items() if a not in ("A", "B")})


def trade() -> None:
    """Report only: B's F flags, buy at the crossing price + cost, M13 ratchet on 5-min candles after the t60 bucket."""
    fw = json.loads((RF / "forward_done.json").read_text())
    s, out = requests.Session(), []
    for z in fw["B"]["flagged"]:
        x = json.loads((RF / "forward" / f"{z['m']}.json").read_text())
        full = s.get(f"https://swap-api.pump.fun/v2/coins/{z['m']}/candles",
                     params={"interval": "5m", "limit": 1000, "currency": "SOL", "createdTs": int(x["created"] * 1000)},
                     headers={"Origin": "https://pump.fun", "Accept": "application/json"}, timeout=30).json()
        c = sorted([[q["timestamp"] / 1000, float(q["open"]), float(q["high"]), float(q["low"]), float(q["close"])] for q in full])
        e = z["price"]
        stop, armed, end, last, ret, why = 0.8 * e, False, z["t"] + 6 * 3600, e, None, "6 h"
        for t, o, h, lo, cl in c:
            if t < z["t"] - z["t"] % 300 + 300:
                continue
            if t > end:
                break
            if lo <= stop:
                ret, why = min(o, stop) / e, "stop"
                break
            if h >= 2 * e:
                armed = True
            if armed:
                stop = max(stop, 1.25 * e, (math.floor(h / e) - 1) * e if h >= 3 * e else 0)
            last = cl
        ret = (ret if ret is not None else last / e) * (1 - COST) / (1 + COST) - 1
        out.append(dict(m=z["m"], t=z["t"], y=z["y"], grad=z["grad"], ret=ret, exit=why))
    out.sort(key=lambda q: q["t"])
    h = len(out) // 2
    rs = [q["ret"] for q in out]
    res = dict(n=len(out), mean=statistics.mean(rs) if rs else None, median=statistics.median(rs) if rs else None,
               halves=[statistics.mean(rs[:h]) if h else None, statistics.mean(rs[h:]) if rs[h:] else None],
               win=sum(r > 0 for r in rs) / max(1, len(rs)), trades=out)
    (RF / "trade_report.json").write_text(json.dumps(res, indent=1))
    print({a: v for a, v in res.items() if a != "trades"})
    for q in out:
        print(f"  {datetime.fromtimestamp(q['t']):%m-%d %H:%M} runner={q['y']} grad={q['grad']} {q['ret']:+.1%} {q['exit']}")


if __name__ == "__main__":
    {"build": build, "meta": meta_test, "forward": forward, "trade": trade}[sys.argv[1]]()
