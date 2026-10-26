"""NL (vault "2026-10-25 narrative leaders (NL)", pre-registered 21:20 PDT): is the first coin on a live, unclaimed narrative
to draw a crowd a runner? Decision at age 3 min on coins with >= 100 trades by then.

    uv run python -X utf8 -m tradeslave.research.nl_model build    # clusters + features -> data/narr_nl/features.json
    uv run python -X utf8 -m tradeslave.research.nl_model test     # T1 + T2 once (writes test_done.json)
    uv run python -X utf8 -m tradeslave.research.nl_model trade    # T3 report: fills + 5-min candles for holdout flags
"""

from __future__ import annotations

import bisect
import json
import math
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlsplit

import numpy as np
import requests

from ..paths import DATA_DIR
from .nl_collect import OUT, TAU
from .rb_model import COST, wilson
from .rf_collect import CANDLES, level

CUT = datetime(2026, 10, 24, 12).timestamp()
GENERIC_SYM = {"sol", "btc", "eth", "pump", "coin", "meme", "usd", "usdc"}
GENERIC_SEG = {"status", "communities", "github", "twitter", "search", "index"}
H24 = 86400
CLUSTER = ("n1", "n6", "n24", "age_h", "max_r", "n_r20", "unclaimed", "tw_n")
ATTN = ("n_trades", "capped", "buyers", "sellers", "buy_sol", "sell_sol", "r_tau", "max_r_tau", "dev_sol", "j7", "has_tw",
        "has_web", "mayhem")


def _alnum(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def keys(name, sym, d):
    out = set()
    nm = _alnum(name)
    if len(nm) >= 5:
        out.add("nm:" + nm)
    sy = _alnum(sym)
    if len(sy) >= 3 and sy not in GENERIC_SYM:
        out.add("sym:" + sy)
    tw, web = ((d or {}).get(k) or "" for k in ("twitter", "website"))
    st = re.search(r"/status/(\d+)", tw)
    if st:
        out.add("tw:" + st.group(1))
    for url in (tw, web):
        if not url:
            continue
        try:
            u = urlsplit(url if "://" in url else "https://" + url)
        except ValueError:
            continue
        segs = [s for s in u.path.split("/") if s]
        if "x.com" in u.netloc or "twitter.com" in u.netloc:
            if segs and segs[0] not in ("i", "search", "home"):
                out.add("xh:" + segs[0].lower())
        for seg in segs:
            for piece in re.split(r"[^A-Za-z0-9]+", seg):
                p = piece.lower()
                if len(p) >= 6 and p not in GENERIC_SEG:
                    out.add("nm:" + p)
    return out


class Clusters:
    def __init__(self):
        D = json.loads((OUT / "details.json").read_text())
        self.idx = defaultdict(list)                      # key -> sorted [(t, m)]
        self.keys, self.t = {}, {}
        for line in (DATA_DIR / "c1" / "creates.jsonl").open():
            x = json.loads(line)
            if not x.get("bc"):
                continue
            d = D.get(x["m"])
            k = keys((d or {}).get("name") or x.get("name"), (d or {}).get("symbol") or x.get("sym"), d)
            self.keys[x["m"]], self.t[x["m"]] = k, x["t"]
            for kk in k:
                self.idx[kk].append((x["t"], x["m"]))
        for v in self.idx.values():
            v.sort()
        self.checks = defaultdict(list)                   # m -> [(t, maxR)]
        for line in (DATA_DIR / "c1" / "checks.jsonl").open():
            c = json.loads(line)
            self.checks[c["m"]].append((c["t"], c.get("maxR") or 0))
        self.D = D
        sp = OUT / "search.json"
        self.S = json.loads(sp.read_text()) if sp.exists() else {}
        sol = json.loads((DATA_DIR / "runners" / "sol_usd.json").read_text())
        self.sol = sorted((float(k), v) for k, v in sol.items())
        self.sol_t = [k for k, _ in self.sol]

    def usd(self, t):
        return self.sol[max(0, bisect.bisect_right(self.sol_t, t) - 1)][1]

    def r_known(self, m, by, res=None):
        """Curve level a member was known to have reached before `by`: C1 candle checks, else (amendment 1) its search ATH
        if that ATH happened before `by`."""
        if m in self.checks:
            return max((r for t, r in self.checks[m] if t < by), default=0.0)
        if res and res.get("ath") and res.get("ath_t") and res["ath_t"] < by:
            price = res["ath"] / self.usd(by) / 1e9
            return max(0.0, math.sqrt(price * 30 * 1.073e9) - 30)
        return 0.0

    def features(self, m):
        tc, tau = self.t[m], self.t[m] + TAU
        prior = {}
        for k in self.keys.get(m, ()):
            v = self.idx[k]
            for t, mm in v[bisect.bisect_left(v, (tc - H24, "")):bisect.bisect_left(v, (tc, ""))]:
                if mm != m:
                    prior[mm] = t
        found = {}
        for mm, r in (self.S.get(m) or {}).get("results", {}).items():          # amendment 1: search-completed members
            q = _alnum(r["q"])
            if mm != m and tc - H24 <= r["t"] < tc and len(q) >= 3 and (q in _alnum(r["name"]) or q in _alnum(r["symbol"])):
                prior.setdefault(mm, r["t"])
                found[mm] = r
        rs = [self.r_known(mm, tau, found.get(mm)) for mm in prior]
        twk = [k for k in self.keys.get(m, ()) if k.startswith("tw:")]
        return dict(n1=math.log1p(sum(t >= tc - 3600 for t in prior.values())),
                    n6=math.log1p(sum(t >= tc - 6 * 3600 for t in prior.values())), n24=math.log1p(len(prior)),
                    n24_raw=len(prior), age_h=(tc - min(prior.values())) / 3600 if prior else 0.0, max_r=max(rs, default=0.0),
                    n_r20=float(sum(r >= 20 for r in rs)), unclaimed=float(all(r < 60 for r in rs)),
                    tw_n=float(sum(1 for mm in prior if set(twk) & self.keys[mm])) if twk else 0.0)


def attention(x, d):
    t0 = x["created"]
    rows = [r for r in x["rows"] if r[0] <= t0 + TAU and r[6] == "pump"]
    buys = [r for r in rows if r[2]]
    lv = [v for v in (level(r) for r in rows) if v is not None]
    return dict(n_trades=float(len(rows)), capped=float(len(x["rows"]) >= 200), buyers=float(len({r[1] for r in buys})),
                sellers=float(len({r[1] for r in rows if not r[2]})), buy_sol=sum(r[3] for r in buys),
                sell_sol=sum(r[3] for r in rows if not r[2]), r_tau=lv[-1] if lv else 0.0, max_r_tau=max(lv, default=0.0),
                dev_sol=float(x.get("dev_sol") or 0), j7=float("j7tracker" in (x.get("uri") or "")),
                has_tw=float(bool((d or {}).get("twitter"))), has_web=float(bool((d or {}).get("website"))),
                mayhem=float(bool(x.get("mayhem"))))


def build() -> None:
    C = Clusters()
    U = json.loads((OUT / "universe.json").read_text())
    L = json.loads((OUT / "labels.json").read_text())["labels"]
    w_of = {b: (U["sizes"][b] / len(U["pick"][b]) if U["pick"][b] else 0) for b in U["pick"]}
    out, skip = [], defaultdict(int)
    for p in sorted((OUT / "trades").glob("*.json")):
        x = json.loads(p.read_text())
        if x["status"] != "ok":
            skip["fetch error"] += 1
            continue
        if not x["hot"]:
            skip["no attention event"] += 1
            continue
        lab = L.get(x["m"]) or {}
        if lab.get("ath") is None:
            skip["no ATH"] += 1
            continue
        f = attention(x, C.D.get(x["m"]))
        f.update(C.features(x["m"]))
        out.append(dict(m=x["m"], t=x["created"], bucket=x["bucket"], w=1.0 if x["bucket"] == "top" else w_of[x["bucket"]],
                        y=int(bool(lab.get("complete")) and lab["ath"] >= 300_000), big=int(lab["ath"] >= 100_000),
                        grad=int(bool(lab.get("complete"))), f=f,
                        leader=int(f["n24_raw"] >= 3 and f["unclaimed"] == 1.0)))
    (OUT / "features.json").write_text(json.dumps(out))
    for part, lo, hi in (("design", 0, CUT), ("holdout", CUT, 1e12)):
        z = [r for r in out if lo <= r["t"] < hi]
        print(f"{part}: {len(z)} attention coins ({sum(r['bucket'] == 'top' for r in z)} pre-screened), runners {sum(r['y'] for r in z)}, "
              f"big {sum(r['big'] for r in z)}, leaders {sum(r['leader'] for r in z)}")
    print(f"skipped {dict(skip)}")


def _rate(rows, k):
    w = sum(r["w"] for r in rows)
    return (sum(r["w"] * r[k] for r in rows) / w) if w else float("nan"), sum(r[k] for r in rows), len(rows), w


def _X(rows, names):
    return np.array([[math.log1p(r["f"][n]) if n in ("n_trades", "buyers", "sellers", "buy_sol", "sell_sol", "dev_sol", "age_h",
                                                     "n_r20", "tw_n") else r["f"][n] for n in names] for r in rows], dtype=float)


def _fit(rows, names, k):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    m = make_pipeline(StandardScaler(), LogisticRegression(C=0.3, max_iter=3000, class_weight="balanced"))
    return m.fit(_X(rows, names), [r[k] for r in rows], logisticregression__sample_weight=[r["w"] for r in rows])


def _auc(rows, p, k):
    from sklearn.metrics import roc_auc_score
    y = [r[k] for r in rows]
    return roc_auc_score(y, p, sample_weight=[r["w"] for r in rows]) if 0 < sum(y) < len(y) else float("nan")


def _cv_thr(design, names, k, folds=5):
    d = sorted(design, key=lambda r: r["t"])
    edges = [d[int(len(d) * j / (folds + 1))]["t"] for j in range(1, folds + 1)] + [d[-1]["t"] + 1]
    ps = []
    for a, b in zip(edges, edges[1:]):
        tr = [r for r in d if r["t"] < a]
        te = [r for r in d if a <= r["t"] < b]
        if te and sum(r[k] for r in tr) >= 3:
            ps += list(_fit(tr, names, k).predict_proba(_X(te, names))[:, 1])
    return float(np.percentile(ps, 95)) if ps else 1.0


def test() -> None:
    from scipy.stats import fisher_exact
    done = OUT / "test_done.json"
    if done.exists():
        print(done.read_text())
        print("NL test was already run once; not rerunning")
        return
    rows = json.loads((OUT / "features.json").read_text())
    res = {}
    for part, lo, hi in (("design", 0, CUT), ("holdout", CUT, 1e12)):
        z = [r for r in rows if lo <= r["t"] < hi]
        L, Oth = [r for r in z if r["leader"]], [r for r in z if not r["leader"]]
        blk = {}
        for k in ("y", "big"):
            rl, rl_k, rl_n, rl_w = _rate(L, k)
            ro, ro_k, ro_n, ro_w = _rate(Oth, k)
            tab = [[rl_k, round(rl_w) - rl_k], [ro_k, max(0, round(ro_w) - ro_k)]]
            p = fisher_exact(tab, alternative="greater")[1] if rl_n and ro_n else float("nan")
            blk[k] = dict(leaders=[rl_k, rl_n, rl], others=[ro_k, ro_n, ro], ratio=(rl / ro) if ro else float("inf"), fisher_p=p,
                          leader_wilson=wilson(rl_k, rl_n))
        res["T1_" + part] = blk
    res["T1_pass"] = bool(res["T1_holdout"]["y"]["fisher_p"] < 0.05 and res["T1_holdout"]["y"]["ratio"] >= 2)
    design = [r for r in rows if r["t"] < CUT]
    hold = [r for r in rows if r["t"] >= CUT]
    for k in ("y", "big"):
        out = {}
        preds = {}
        for nm, names in (("full", list(CLUSTER) + list(ATTN)), ("attention_only", list(ATTN))):
            if sum(r[k] for r in design) < 3:
                out[nm] = "too few positives in design"
                continue
            m = _fit(design, names, k)
            p = m.predict_proba(_X(hold, names))[:, 1]
            preds[nm] = p
            thr = _cv_thr(design, names, k)
            top = np.percentile(p, 95)
            fl = [r for r, pi in zip(hold, p) if pi >= thr]
            tp = [r for r, pi in zip(hold, p) if pi >= top]
            out[nm] = dict(auc=_auc(hold, p, k), thr=thr, flags=len(fl), hits=sum(r[k] for r in fl), wilson=wilson(sum(r[k] for r in fl), len(fl)),
                           top5=[sum(r[k] for r in tp), len(tp)],
                           coef={n: float(c) for n, c in zip(names, m.named_steps["logisticregression"].coef_[0])},
                           flagged=[r["m"] for r in fl])
        if len(preds) == 2:
            rng = np.random.default_rng(0)
            y = np.array([r[k] for r in hold])
            wts = np.array([r["w"] for r in hold])
            from sklearn.metrics import roc_auc_score
            diffs = []
            for _ in range(2000):
                i = rng.integers(0, len(hold), len(hold))
                if 0 < y[i].sum() < len(i):
                    diffs.append(roc_auc_score(y[i], preds["full"][i], sample_weight=wts[i])
                                 - roc_auc_score(y[i], preds["attention_only"][i], sample_weight=wts[i]))
            out["delta_auc_ci"] = [float(v) for v in np.percentile(diffs, [2.5, 97.5])]
            out["clusters_add"] = bool(out["delta_auc_ci"][0] > 0)
        res["T2_" + k] = out
    res["holdout_n"], res["holdout_runners"], res["holdout_big"] = len(hold), sum(r["y"] for r in hold), sum(r["big"] for r in hold)
    res["holdout_base_runner"] = _rate(hold, "y")[0]
    res["holdout_base_big"] = _rate(hold, "big")[0]
    done.write_text(json.dumps(res, indent=1, default=float))
    for k, v in res.items():
        if isinstance(v, dict):
            print(k, json.dumps({a: ({c: d for c, d in b.items() if c not in ("flagged", "coef")} if isinstance(b, dict) else b)
                                 for a, b in v.items()}, default=float))
        else:
            print(k, v)


def trade() -> None:
    """T3 (report only): holdout leaders and the full model's top 5% (primary label), +1 s fill, M13 ratchet on 5-min candles."""
    from .rb_collect import _page_back, _rows
    from .rn_collect import _pump_headers
    res = json.loads((OUT / "test_done.json").read_text())
    rows = {r["m"]: r for r in json.loads((OUT / "features.json").read_text())}
    hold = [r for r in rows.values() if r["t"] >= CUT]
    full = res["T2_y"].get("full")
    top_ms = set()
    if isinstance(full, dict):
        names = list(CLUSTER) + list(ATTN)
        design = [r for r in rows.values() if r["t"] < CUT]
        p = _fit(design, names, "y").predict_proba(_X(hold, names))[:, 1]
        cut = np.percentile(p, 95)
        top_ms = {r["m"] for r, pi in zip(hold, p) if pi >= cut}
    books = {"leaders": [r["m"] for r in hold if r["leader"]], "model_top5": sorted(top_ms)}
    H, s, out = _pump_headers(), requests.Session(), {}
    for book, ms in books.items():
        trades = []
        for m in ms:
            r = rows[m]
            tau = r["t"] + TAU
            a, st = _page_back(s, H, m, tau + 31, 1)
            fills = [z for z in _rows(a) if tau + 1 <= z[0] <= tau + 31 and z[6] == "pump" and z[3] >= 0.01 and z[4]]
            if not fills:
                trades.append(dict(m=m, ret=None, why="no fill within 30 s"))
                continue
            e = fills[0][3] / fills[0][4] * (1 + COST)
            js = s.get(CANDLES.format(m), params={"interval": "5m", "limit": 1000, "currency": "SOL", "createdTs": int(r["t"] * 1000)},
                       headers={"Origin": "https://pump.fun", "Accept": "application/json"}, timeout=30).json()
            c = sorted([[q["timestamp"] / 1000, float(q["open"]), float(q["high"]), float(q["low"]), float(q["close"])]
                        for q in js if q.get("high") is not None])
            p0 = e / (1 + COST)
            stop, armed, last, ret, why = 0.8 * p0, False, p0, None, "6 h"
            for t, o, h, lo, cl in c:
                if t < tau - tau % 300 + 300:
                    continue
                if t > tau + 6 * 3600:
                    break
                if lo <= stop:
                    ret, why = min(o, stop), "stop"
                    break
                if h >= 2 * p0:
                    armed = True
                if armed:
                    stop = max(stop, 1.25 * p0, (math.floor(h / p0) - 1) * p0 if h >= 3 * p0 else 0)
                last = cl
            x = (ret if ret is not None else last) * (1 - COST) / e - 1
            trades.append(dict(m=m, t=r["t"], y=r["y"], big=r["big"], ret=x, why=why))
        rs = [q["ret"] for q in sorted((q for q in trades if q["ret"] is not None), key=lambda q: q["t"])]
        h = len(rs) // 2
        out[book] = dict(n=len(rs), mean=statistics.mean(rs) if rs else None, median=statistics.median(rs) if rs else None,
                         halves=[statistics.mean(rs[:h]) if h else None, statistics.mean(rs[h:]) if rs[h:] else None],
                         win=sum(v > 0 for v in rs) / max(1, len(rs)), trades=trades)
        print(book, {k: v for k, v in out[book].items() if k != "trades"})
    (OUT / "trade_report.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    {"build": build, "test": test, "trade": trade}[sys.argv[1]]()
