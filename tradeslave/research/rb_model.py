"""RD decision point B (vault "2026-10-24 runner detection on 1,000 real runners (RD)", amendment 2): predict runners at the
moment a coin migrates, from its curve trades and metadata only.

    uv run python -X utf8 -m tradeslave.research.rb_model build     # features -> data/runners/rb/features.json
    uv run python -X utf8 -m tradeslave.research.rb_model design    # time-ordered CV inside design; picks model + threshold
    uv run python -X utf8 -m tradeslave.research.rb_model holdout   # scores holdout ONCE (writes rb/holdout_done.json)
"""

from __future__ import annotations

import json
import math
import re
import statistics
import sys
import time
from collections import defaultdict

import numpy as np

from ..paths import DATA_DIR
from .rb_collect import CURVE_MIN_S, OUT, START_MAX

DESIGN_FRAC, RUNNER_ATH = 0.60, 300_000
COST = 0.0225                                 # 1.25% fee + 1% slippage, each side
LAUNCHERS = ("j7tracker", "uxento", "rapidlaunch", "launchblitz", "usepaid", "fees to @", "clawpump", "agency", "higgspad")
WALLET_FEATS = ("callers_in", "elite_in", "wl_in", "decu_in")


def _wallets():
    from .m1_bot import ELITE_2X, ELITE_MED
    from .wl_fetch import WALLETS
    callers, elite = set(), set()
    for f in sorted((DATA_DIR / "callouts").glob("leaderboard_*.json"), reverse=True):
        try:
            lb = json.loads(f.read_text(encoding="utf-8")).get("callouts", [])
        except (OSError, ValueError):
            continue
        for u in lb:
            ws = {w for w in u.get("wallets", []) if not w.startswith("0x")}
            callers |= ws
            if u.get("pct2xOrMore", 0) >= ELITE_2X and u.get("medianMultiple", 0) >= ELITE_MED:
                elite |= ws
        break
    return callers, elite, set(WALLETS), "4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9"


def windows(x):
    """Amendment 3/3b: launch = last 200 trades with t <= created + 120 s; run-up = last 200 trades with t <= migration.
    Full-history files are cut to exactly these windows, so every coin carries the same information."""
    if "rows" in x:
        rows = x["rows"]
        return [r for r in rows if r[0] <= x["created"] + 120][-200:], [r for r in rows if r[0] <= x["t_mig"]][-200:]
    return x["launch"][-200:], x["runup"][-200:]


def features(x, d, wallets):
    callers, elite, wl, decu = wallets
    la, ru = windows(x)
    la = [r for r in la if r[6] == "pump"]
    ru = [r for r in ru if r[6] == "pump" and r[0] <= x["t_mig"]]
    if len(la) < 3 or len(ru) < 5:
        return None
    t0, tm = la[0][0], x["t_mig"]
    start_mc = la[0][5] * 1e9
    creator = (d or {}).get("creator")
    sup = 1e9

    def block(rows, pre):
        buys = [r for r in rows if r[2]]
        sells = [r for r in rows if not r[2]]
        bs = sum(r[3] for r in buys)
        per = defaultdict(float)
        for r in buys:
            per[r[1]] += r[3]
        cnt = defaultdict(int)
        for r in buys:
            cnt[r[1]] += 1
        peak, dd = 0.0, 0.0
        for r in rows:
            peak = max(peak, r[5])
            dd = max(dd, 1 - r[5] / peak if peak else 0)
        bsz = sorted(r[3] for r in buys)
        span = max(1.0, rows[-1][0] - rows[0][0])
        return {
            pre + "buyers": len(per), pre + "sellers": len({r[1] for r in sells}),
            pre + "flow": bs - sum(r[3] for r in sells), pre + "sell_ratio": sum(r[3] for r in sells) / (bs or 1),
            pre + "whale": sum(r[3] for r in buys if r[3] >= 2) / (bs or 1),
            pre + "top5": sum(sorted(per.values(), reverse=True)[:5]) / (bs or 1),
            pre + "repeat": sum(1 for v in cnt.values() if v > 1) / (len(cnt) or 1), pre + "dd": dd,
            pre + "chg": rows[-1][5] / rows[0][5] - 1 if rows[0][5] else 0.0, pre + "rate": math.log1p(len(rows) / span * 60),
            pre + "buy_med": statistics.median(bsz) if bsz else 0.0,
            pre + "dev_sold": float(any(r[1] == creator and not r[2] for r in rows)),
        }
    launch = [r for r in la if r[2] and r[0] <= t0 + 2 and r[1] != creator]
    desc = ((d or {}).get("description") or "")
    name, sym = (d or {}).get("name") or "", (d or {}).get("symbol") or ""
    tw = (d or {}).get("twitter") or ""
    users = {r[1] for r in la + ru if r[2]}
    hr = (tm % 86400) / 3600
    f = dict(
        curve_min=math.log1p((tm - t0) / 60), start_mc=start_mc,
        dev_buy=sum(r[3] for r in la if r[2] and r[1] == creator and r[0] <= t0 + 2),
        snipers=len({r[1] for r in launch}), snipe_share=sum(r[4] for r in launch) / sup,
        has_tw=float(bool(tw)), tw_status=float("/status/" in tw), has_web=float(bool((d or {}).get("website"))),
        has_tg=float(bool((d or {}).get("telegram"))), desc_len=math.log1p(len(desc)), name_len=len(name), sym_len=len(sym),
        sym_upper=float(sym.isupper()), vanity=float(not x["m"].endswith("pump")),
        launcher=float(any(k in desc.lower() for k in LAUNCHERS)),
        template=float(bool(re.search(r"meme energy|brings .* to solana|vibes", desc.lower()))),
        hour_sin=math.sin(2 * math.pi * hr / 24), hour_cos=math.cos(2 * math.pi * hr / 24),
        callers_in=len(users & callers), elite_in=len(users & elite), wl_in=len(users & wl), decu_in=float(decu in users),
    )
    f.update(block(la, "l_"))
    f.update(block(ru, "r_"))
    return f


def build() -> None:
    D = json.loads((OUT / "details.json").read_text())
    P = json.loads((OUT / "population.json").read_text())
    wallets = _wallets()
    out, skipped = [], defaultdict(int)
    seen = set()
    for p in sorted((OUT / "trades_lite").glob("*.json")) + sorted((OUT / "trades").glob("*.json")):   # windowed first
        x = json.loads(p.read_text())
        if x["m"] in seen:
            continue
        seen.add(x["m"])
        if x["status"] in ("error", "capped") or ("launch" in x and not x.get("runup_v2")):
            skipped["fetch error"] += 1
            continue
        if x["t_mig"] - x["created"] < CURVE_MIN_S:
            skipped["instant"] += 1
            continue
        f = features(x, D.get(x["m"]), wallets)
        if f is None:
            skipped["too few curve trades"] += 1
            continue
        if f["start_mc"] >= START_MAX:
            skipped["start >= $10k"] += 1
            continue
        out.append(dict(m=x["m"], t=x["t_mig"], sample=x["sample"], ath=x.get("ath") or 0, y=int((x.get("ath") or 0) >= RUNNER_ATH),
                        capped=x["status"] == "capped", f=f))
    (OUT / "features.json").write_text(json.dumps(out))
    s = [r for r in out if r["sample"]]
    print(f"features: {len(out)} coins ({len(s)} in the uniform sample, runners in sample {sum(r['y'] for r in s)}, "
          f"base rate {sum(r['y'] for r in s) / max(1, len(s)):.2%}); skipped {dict(skipped)}; "
          f"curve graduations in window {P['n_curve_graduations']}")


def _split(rows):
    ts = sorted(r["t"] for r in rows if r["sample"])
    cut = ts[int(DESIGN_FRAC * len(ts))]
    return [r for r in rows if r["t"] < cut], [r for r in rows if r["t"] >= cut and r["sample"]], cut


def _X(rows, names):
    return np.array([[r["f"][n] for n in names] for r in rows], dtype=float)


def _model(kind):
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    if kind == "gbm":
        return HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=30,
                                              l2_regularization=1.0, random_state=0)
    return make_pipeline(StandardScaler(), LogisticRegression(C=0.3, max_iter=2000, class_weight="balanced"))


def _names(rows, wallet):
    names = sorted(rows[0]["f"])
    return [n for n in names if wallet or n not in WALLET_FEATS]


def _cv(design, kind, names, folds=5):
    """Expanding-window CV by time: train on earlier folds (+ every earlier extra runner), predict the next fold's sample coins."""
    d = sorted(design, key=lambda r: r["t"])
    ts = sorted(r["t"] for r in d if r["sample"])
    edges = [ts[int(len(ts) * k / (folds + 1))] for k in range(1, folds + 1)] + [ts[-1] + 1]
    pred = []
    for a, b in zip(edges, edges[1:]):
        tr = [r for r in d if r["t"] < a]
        te = [r for r in d if a <= r["t"] < b and r["sample"]]
        if not te or sum(r["y"] for r in tr) < 10:
            continue
        m = _model(kind).fit(_X(tr, names), [r["y"] for r in tr])
        for r, p in zip(te, m.predict_proba(_X(te, names))[:, 1]):
            pred.append((p, r["y"], r["t"]))
    return pred


def _auc(pred):
    from sklearn.metrics import roc_auc_score
    ys = [y for _, y, _ in pred]
    return roc_auc_score(ys, [p for p, _, _ in pred]) if 0 < sum(ys) < len(ys) else float("nan")


def _at(pred, thr):
    k = [y for p, y, _ in pred if p >= thr]
    return len(k), sum(k)


def wilson(k, n, z=1.96):
    if not n:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)


def _scale():
    P = json.loads((OUT / "population.json").read_text())
    return P["n_curve_graduations"] / len(P["sample"])


def design() -> None:
    rows = json.loads((OUT / "features.json").read_text())
    dz, _, cut = _split(rows)
    days = (cut - min(r["t"] for r in dz if r["sample"])) / 86400
    scale = _scale()
    print(f"design: {len(dz)} coins, {sum(r['sample'] for r in dz)} sample ({sum(r['y'] for r in dz if r['sample'])} runners), "
          f"{days:.1f} days; population scale x{scale:.1f}")
    best = None
    for kind in ("gbm", "logit"):
        for wallet in (False, True):
            names = _names(rows, wallet)
            pred = _cv(dz, kind, names)
            base = sum(y for _, y, _ in pred) / len(pred)
            cv_days = (max(t for _, _, t in pred) - min(t for _, _, t in pred)) / 86400
            ps = sorted((p for p, _, _ in pred), reverse=True)
            line = [f"{kind:5s} wallets={str(wallet):5s} AUC {_auc(pred):.3f} base {base:.2%}"]
            for q in (0.01, 0.05, 0.10):
                n, k = _at(pred, ps[max(0, int(q * len(ps)) - 1)])
                line.append(f"top{int(q * 100)}% {k}/{n}={k / max(1, n):.0%}")
            print("  " + " | ".join(line))
            if wallet:                                  # PASS model is the one without wallet features
                continue
            for thr in sorted(set(ps), reverse=True):  # highest-precision threshold that still flags >= 1 coin/day
                n, k = _at(pred, thr)
                per_day = n * scale / cv_days
                if per_day >= 1 and n >= 10:
                    cand = (k / n, kind, thr, n, k, per_day)
                    if best is None or cand[0] > best[0]:
                        best = cand
                    break
    # amendment 4: logistic without wallet features, threshold = 95th percentile of its design-CV scores
    pred = _cv(dz, "logit", _names(rows, False))
    ps = sorted((p for p, _, _ in pred), reverse=True)
    thr = ps[max(0, int(0.05 * len(ps)) - 1)]
    n, k = _at(pred, thr)
    cv_days = (max(t for _, _, t in pred) - min(t for _, _, t in pred)) / 86400
    best = (k / max(1, n), "logit", thr, n, k, n * scale / cv_days)
    prec, kind, thr, n, k, per_day = best
    lo, hi = wilson(k, n)
    print(f"chosen: {kind} without wallet features, threshold {thr:.3f}: design-CV precision {k}/{n} = {prec:.0%} "
          f"(95% {lo:.0%}-{hi:.0%}), {per_day:.1f} flags/day")
    (OUT / "design_choice.json").write_text(json.dumps(dict(kind=kind, thr=thr, cv_precision=prec, n=n, k=k, per_day=per_day)))


def simulate(c, entry_i, ladder=True):
    """Exit on 1-minute candles [t, o, h, l, c, v]; returns net multiple after costs."""
    e = c[entry_i][4]
    if not e:
        return None
    stop, armed, end_t, last = 0.8 * e, False, c[entry_i][0] + 6 * 3600, e
    for t, o, h, lo, cl, _ in c[entry_i + 1:]:
        if t > end_t:
            break
        if ladder:
            if lo <= stop:
                return min(o, stop) / e * (1 - COST) / (1 + COST)
            if h >= 2 * e:
                armed = True
            if armed:
                stop = max(stop, 1.25 * e, (math.floor(h / e) - 1) * e if h >= 3 * e else 0)
        else:
            if lo <= 0.7 * e:
                return min(o, 0.7 * e) / e * (1 - COST) / (1 + COST)
            if h >= 2 * e:
                return 2.0 * (1 - COST) / (1 + COST)
        last = cl
    return last / e * (1 - COST) / (1 + COST)


def holdout() -> None:
    done = OUT / "holdout_done.json"
    if done.exists():
        print(json.loads(done.read_text()))
        print("holdout was already scored once; not rescoring")
        return
    rows = json.loads((OUT / "features.json").read_text())
    ch = json.loads((OUT / "design_choice.json").read_text())
    dz, ho, cut = _split(rows)
    res = {}
    for wallet in (False, True):
        names = _names(rows, wallet)
        m = _model(ch["kind"]).fit(_X(dz, names), [r["y"] for r in dz])
        p = m.predict_proba(_X(ho, names))[:, 1]
        pred = [(pi, r["y"], r["t"]) for pi, r in zip(p, ho)]
        days = (max(r["t"] for r in ho) - cut) / 86400
        n, k = _at(pred, ch["thr"])
        ps = sorted(p, reverse=True)
        tops = {"holdout top5% (secondary)": list(_at(pred, ps[max(0, int(0.05 * len(ps)) - 1)])[::-1])}
        for q in (0.01, 0.05, 0.10):
            nn, kk = _at(pred, ps[max(0, int(q * len(ps)) - 1)])
            tops[f"top{int(q * 100)}%"] = [kk, nn]
        res["wallets" if wallet else "no_wallets"] = dict(
            auc=_auc(pred), base=sum(r["y"] for r in ho) / len(ho), n_holdout=len(ho), days=days, flagged=n, hits=k,
            precision=k / max(1, n), wilson=wilson(k, n), per_day=n * _scale() / days, tops=tops,
            flagged_mints=[r["m"] for pi, r in zip(p, ho) if pi >= ch["thr"]])
    # trading check (no-wallet model, the PASS model)
    trades = []
    for mnt in res["no_wallets"]["flagged_mints"]:
        p = OUT / "candles1m" / f"{mnt}.json"
        if not p.exists():
            continue
        x = json.loads(p.read_text())
        c = x["c"]
        i = next((j for j, cc in enumerate(c) if cc[0] >= x["t_mig"]), None)
        if i is None:
            continue
        a, b = simulate(c, i, True), simulate(c, i, False)
        if a is not None:
            trades.append(dict(m=mnt, t=x["t_mig"], ladder=a - 1, plain=b - 1))
    trades.sort(key=lambda z: z["t"])
    h = len(trades) // 2
    res["trading"] = dict(n=len(trades), ladder_mean=statistics.mean(z["ladder"] for z in trades) if trades else None,
                          plain_mean=statistics.mean(z["plain"] for z in trades) if trades else None,
                          ladder_halves=[statistics.mean(z["ladder"] for z in trades[:h]) if h else None,
                                         statistics.mean(z["ladder"] for z in trades[h:]) if trades[h:] else None],
                          trades=trades)
    done.write_text(json.dumps(res, indent=1))
    for k_, v in res.items():
        if k_ != "trading":
            print(k_, {a: b for a, b in v.items() if a != "flagged_mints"})
    t = res["trading"]
    print("trading", {a: b for a, b in t.items() if a != "trades"})


def final() -> None:
    """M13's model: the holdout-validated choice (logistic, no wallet features) refit on all coins; threshold = 95th percentile
    of time-ordered CV scores over all data (same rule as amendment 4). Writes data/m1/m13_model.json."""
    rows = json.loads((OUT / "features.json").read_text())
    names = _names(rows, False)
    pred = _cv(rows, "logit", names)
    ps = sorted((p for p, _, _ in pred), reverse=True)
    thr = ps[max(0, int(0.05 * len(ps)) - 1)]
    m = _model("logit").fit(_X(rows, names), [r["y"] for r in rows])
    sc, lr = m.named_steps["standardscaler"], m.named_steps["logisticregression"]
    n, k = _at(pred, thr)
    out = dict(names=names, mean=sc.mean_.tolist(), scale=sc.scale_.tolist(), coef=lr.coef_[0].tolist(),
               intercept=float(lr.intercept_[0]), thr=thr, cv_precision=k / max(1, n), cv_auc=_auc(pred), n_train=len(rows),
               trained=time.strftime("%Y-%m-%d %H:%M"))
    (DATA_DIR / "m1" / "m13_model.json").write_text(json.dumps(out, indent=1))
    print(f"M13 model: {len(names)} features, {len(rows)} coins, CV AUC {out['cv_auc']:.3f}, top-5% threshold {thr:.3f} "
          f"(CV precision {k}/{n})")
    for nm, c in sorted(zip(names, out["coef"]), key=lambda z: -abs(z[1]))[:10]:
        print(f"  {nm:16s} {c:+.2f}")


if __name__ == "__main__":
    {"build": build, "design": design, "holdout": holdout, "final": final}[sys.argv[1]]()
