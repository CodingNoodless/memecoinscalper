"""What do skilled late traders see when they buy? (strategy N2: imitate skill, not copy trades)

    uv run python -m tradeslave.research.skilled

Skilled = wallets whose ROI on entries >= 30 s after launch was in the top 20% of the FIRST half
(>= 8 such entries). For every first buy at age >= 30 s in the SECOND half we compute the token's state
at that moment (only trades strictly before it) and compare skilled vs everyone else.
"""

from __future__ import annotations

import statistics

from .traders import VS0, load, positions

FEATURES = ("age", "curve_sol", "ret_10s", "ret_30s", "ret_120s", "from_peak", "buy_sol_30s", "sell_sol_30s",
            "net_30s", "buyers_30s", "buyers_total", "creator_sold", "trades_30s", "top_buyer_share")


def state(ts: list[dict], i: int, grp: set) -> dict:
    tc, t = ts[0]["t"], ts[i]["t"]
    vs, path, peak = VS0, [], 0.0
    buyers, b30, s30, n30, by30, creator_sold, per_w = set(), 0.0, 0.0, 0, set(), False, {}
    for r in ts[:i]:
        vs += r["dv"]
        path.append((r["t"], vs))
        peak = max(peak, vs)
        if r["b"]:
            buyers.add(r["w"])
            per_w[r["w"]] = per_w.get(r["w"], 0) + r["sol"]
        if r["w"] in grp and not r["b"]:
            creator_sold = True
        if t - r["t"] <= 30:
            n30 += 1
            if r["b"]:
                b30 += r["sol"]
                by30.add(r["w"])
            else:
                s30 += r["sol"]

    def back(sec):
        old = [v for tt, v in path if tt <= t - sec]
        base = old[-1] if old else VS0
        return (vs / base) ** 2 - 1                     # price ~ vs^2 on this curve

    tot = sum(per_w.values()) or 1
    return {"age": t - tc, "curve_sol": vs / 1e9, "ret_10s": back(10), "ret_30s": back(30), "ret_120s": back(120),
            "from_peak": (vs / peak) ** 2 - 1 if peak else 0, "buy_sol_30s": b30, "sell_sol_30s": s30,
            "net_30s": b30 - s30, "buyers_30s": len(by30), "buyers_total": len(buyers), "creator_sold": float(creator_sold),
            "trades_30s": n30, "top_buyer_share": max(per_w.values()) / tot if per_w else 0}


def main() -> None:
    data = load()
    t0s = {m: ts[0]["t"] for m, ts in data.items()}
    order = sorted(data, key=lambda m: t0s[m])
    cut = t0s[order[len(order) // 2]]
    pos = {m: positions(data[m]) for m in order}
    hist = {}
    for m in order:
        if t0s[m] < cut:
            for w, p in pos[m].items():
                if p["in"] > 0 and p["t_first"] >= 30:
                    h = hist.setdefault(w, [0, 0.0, 0.0])
                    h[0] += 1
                    h[1] += p["pnl"]
                    h[2] += p["in"]
    ws = sorted((w for w, h in hist.items() if h[0] >= 8), key=lambda w: -hist[w][1] / hist[w][2])
    skilled = set(ws[: len(ws) // 5])
    print(f"skilled late traders (top 20% of {len(ws)} on half 1): {len(skilled)}")
    rows = {"skilled": [], "others": []}
    for m in order:
        if t0s[m] < cut:
            continue
        ts = data[m]
        s0 = ts[0]["slot"]
        grp = {r["w"] for r in ts if r["slot"] == s0}
        seen = set()
        for i, r in enumerate(ts):
            if not r["b"] or r["w"] in seen or r["w"] in grp:
                continue
            seen.add(r["w"])
            if r["t"] - ts[0]["t"] < 30:
                continue
            p = pos[m].get(r["w"])
            if not p or p["in"] <= 0:
                continue
            st = state(ts, i, grp)
            st["roi"] = p["pnl"] / p["in"]
            rows["skilled" if r["w"] in skilled else "others"].append(st)
    for k, v in rows.items():
        print(f"{k}: {len(v)} entries, ROI {statistics.mean(x['roi'] for x in v):+.1%} (mean per entry), "
              f"median {statistics.median(x['roi'] for x in v):+.1%}")
    print(f"\n{'feature':16s} {'skilled median':>15s} {'others median':>14s}")
    for f in FEATURES:
        a = statistics.median(x[f] for x in rows["skilled"])
        b = statistics.median(x[f] for x in rows["others"])
        print(f"{f:16s} {a:15.3f} {b:14.3f}")
    import json
    from .backfill_trades import OUT
    (OUT.parent / "skilled_states.jsonl").write_text(
        "\n".join(json.dumps({"skilled": k == "skilled", **x}) for k, v in rows.items() for x in v))


if __name__ == "__main__":
    main()
