"""Strategy N2: predict pump.fun 1-minute charts from the raw candle sequence (dilated 1D CNN, GPU).

    uv run --no-sync python -m tradeslave.research.pump_nn design   # train on fit launches, choose rule on validation
    uv run --no-sync python -m tradeslave.research.pump_nn test     # judge the frozen rule once on test launches

Universe: research.long_paths (1-minute candles of every logged launch, curve + PumpSwap), on a regular 1-minute
grid over the first 24 h (minutes without trades = unchanged price, zero volume). Decision at a minute close when
the coin's market cap so far has reached MIN_MC SOL, its age is >= 5 min and >= MIN_V15 SOL traded in
the last 15 min. Input: last WIN minutes (log return,
range, body, log SOL volume) + age, log market cap, graduated. Targets (exact costs, research.playbook buy/sell with
our bag, 0.5 SOL, 0.002 SOL/tx): flat 5 / 15 / 60 min (entry next open, exit close) and the X3 structure exit.
Split: design = launch before playbook.CUT_T0 (fit on the first 70% by launch time, choose on the rest), test =
after, judged once. Trades: one open position per coin, next entry after the previous exit.
Pre-registered in the vault note "2026-10-19 neural chart prediction (N)", section N2.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
import time

import numpy as np
import torch
from torch import nn

from ..paths import DATA_DIR
from . import discretion as D
from . import playbook as H

WIN, NF, NS = 60, 4, 3
MIN_MC = 100.0
MIN_V15 = 2.0                   # SOL traded in the last 15 min: traders look at active charts only
HORIZON = 24 * 3600
TARGETS = [("flat5", 5), ("flat15", 15), ("flat60", 60), ("X3", None)]
CACHE = DATA_DIR / "pump_nn.npz"
FROZEN = DATA_DIR / "pump_nn_rule.json"
WEIGHTS = DATA_DIR / "pump_nn.pt"
THRS = (0.0, 0.05, 0.10, 0.20, 0.40)
DEV = "cuda" if torch.cuda.is_available() else "cpu"


def grid(t0, c):
    """Regular 1-minute candles from the first candle to min(last, t0 + HORIZON)."""
    t = c[:, 0]
    g = np.arange(t[0], min(t[-1], t0 + HORIZON) + 1, 60.0)
    k = np.searchsorted(t, g, side="right") - 1
    exact = t[k] == g
    cl = c[k, 4]
    o = np.where(exact, c[k, 1], cl)
    h = np.where(exact, c[k, 2], cl)
    lo = np.where(exact, c[k, 3], cl)
    v = np.where(exact, c[k, 5], 0.0)
    return np.column_stack([g, o, h, lo, cl, v])


def net_return(p_in, g_in, p_out, g_out):
    tokens, net = H.buy(p_in, g_in)
    return (H.sell(tokens, p_out, net, g_out) - H.SIZE - 2 * H.PRIO) / H.SIZE


def build():
    X, idx, meta, Y, M, T0, TT = [], [], [], [], [], [], []
    off = 0
    files = sorted(H.LP_OUT.glob("*.json"))
    for j, p in enumerate(files):
        x = H.load(p)
        if x is None:
            continue
        m, t0, c = x
        if c[:, 2].max() * 1e9 < MIN_MC:
            continue
        a = grid(t0, c)
        t, o, h, lo, cl, v = a.T
        n = len(a)
        grad = np.maximum.accumulate(h >= D.P_GRAD * 0.999)
        mcmax = np.maximum.accumulate(h) * 1e9
        v15 = np.convolve(v, np.ones(15), mode="full")[:n]
        r = np.r_[0.0, np.log(cl[1:] / cl[:-1])]
        F = np.column_stack([r * 10, (h - lo) / cl * 10, (cl - o) / cl * 10, np.log1p(v)])
        F = np.clip(np.nan_to_num(F), -50, 50).astype(np.float32)
        X.append(np.vstack([np.zeros((WIN, NF), np.float32), F]))     # left padding for young coins
        for i in range(n - 2):
            age = t[i] + 60 - t0
            if mcmax[i] < MIN_MC or age < 300 or v15[i] < MIN_V15:
                continue
            p_in, g_in = o[i + 1], bool(grad[i])
            ys = []
            for _, hm in TARGETS:
                if hm is None:
                    ys.append(H.trade(a, i, H.EXITS["X3"]))
                    continue
                k = min(i + hm, n - 1)
                ys.append(net_return(p_in, g_in, cl[k], bool(grad[k])))
            idx.append(off + WIN + i)
            meta.append([age / 3600, math.log(cl[i] * 1e9), float(grad[i])])
            Y.append(ys)
            M.append(m)
            T0.append(t0)
            TT.append(t[i])
        off += n + WIN
        if j % 2000 == 0:
            print(f"  {j}/{len(files)} files, {len(Y)} decision points", flush=True)
    np.savez_compressed(CACHE, X=np.concatenate(X), idx=np.array(idx), meta=np.array(meta, np.float32),
                        Y=np.array(Y, np.float32), M=np.array(M), T0=np.array(T0), TT=np.array(TT))


class Net(nn.Module):
    def __init__(self, ch=64):
        super().__init__()
        self.inp = nn.Conv1d(NF, ch, 1)
        self.blocks = nn.ModuleList([nn.Sequential(nn.Conv1d(ch, ch, 3, padding=d, dilation=d), nn.BatchNorm1d(ch),
                                                   nn.GELU()) for d in (1, 2, 4, 8, 16)])
        self.head = nn.Sequential(nn.Linear(2 * ch + NS, 128), nn.GELU(), nn.Dropout(0.1), nn.Linear(128, len(TARGETS)))

    def forward(self, x, s):
        z = self.inp(x)
        for b in self.blocks:
            z = z + b(z)
        return self.head(torch.cat([z.mean(-1), z[..., -1], s], 1))


def batch(X, gi, meta, rows):
    w = gi[rows][:, None] + np.arange(-WIN + 1, 1)[None, :]
    return (torch.from_numpy(X[w]).permute(0, 2, 1).to(DEV), torch.from_numpy(meta[rows]).to(DEV))


def predict(net, X, gi, meta, rows, bs=4096):
    net.eval()
    out = []
    with torch.no_grad():
        for k in range(0, len(rows), bs):
            out.append(net(*batch(X, gi, meta, rows[k:k + bs])).cpu().numpy())
    return np.concatenate(out) / 10


def train(X, gi, meta, Y, fit, val, epochs=12, bs=1024):
    torch.manual_seed(0)
    rng = np.random.default_rng(0)
    net = Net().to(DEV)
    opt = torch.optim.AdamW(net.parameters(), lr=1e-3, weight_decay=1e-4)
    yt = torch.from_numpy(np.clip(Y, -1, 3) * 10)
    loss_fn = nn.HuberLoss(delta=2.0)
    best, state, bad = 1e9, None, 0
    for ep in range(epochs):
        net.train()
        t0 = time.time()
        order = rng.permutation(fit)
        for k in range(0, len(order), bs):
            rows = order[k:k + bs]
            loss = loss_fn(net(*batch(X, gi, meta, rows)), yt[rows].to(DEV))
            opt.zero_grad()
            loss.backward()
            opt.step()
        vl = float(loss_fn(torch.from_numpy(predict(net, X, gi, meta, val) * 10), yt[val]).item())
        print(f"  epoch {ep + 1}: val {vl:.4f} ({time.time() - t0:.0f} s)", flush=True)
        if vl < best - 1e-4:
            best, state, bad = vl, {k: v.detach().clone() for k, v in net.state_dict().items()}, 0
        else:
            bad += 1
            if bad >= 2:
                break
    net.load_state_dict(state)
    return net


def trades(P, Y, M, TT, k, thr):
    """One open position per coin: enter when predicted net return of target k >= thr; busy until the exit."""
    hold = TARGETS[k][1]
    busy, out = {}, []
    for j in np.lexsort((TT, M)):
        if P[j, k] < thr or TT[j] < busy.get(M[j], -1):
            continue
        busy[M[j]] = TT[j] + 60 * (hold if hold else 360)
        out.append((M[j], TT[j], float(Y[j, k])))
    return out


def summary(rows, label):
    xs = [r[2] for r in sorted(rows, key=lambda r: r[1])]
    if len(xs) < 5:
        return f"{label}: n={len(xs)}"
    by = {}
    for m, _, x in rows:
        by[m] = by.get(m, 0.0) + x
    v = list(by.values())
    sd = statistics.pstdev(v)
    tcl = statistics.mean(v) / (sd / math.sqrt(len(v))) if sd else 0.0
    trim = sorted(xs)[:-max(1, len(xs) // 50)]
    h = len(xs) // 2
    return (f"{label}: n={len(xs)} coins {len(v)} mean {statistics.mean(xs):+.2%} med {statistics.median(xs):+.2%} "
            f"win {np.mean(np.array(xs) > 0):.0%} coin-t {tcl:+.2f} | w/o top 2% {statistics.mean(trim):+.2%} | "
            f"halves {statistics.mean(xs[:h]):+.2%} / {statistics.mean(xs[h:]):+.2%}")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "design"
    if cmd == "build":
        build()
        return
    z = np.load(CACHE)
    X, gi, meta, Y, M, T0, TT = (z[k] for k in ("X", "idx", "meta", "Y", "M", "T0", "TT"))
    design = T0 < H.CUT_T0
    ut = np.sort(np.unique(T0[design]))
    cut_f = ut[int(len(ut) * 0.7)]
    fm, vm, tm = design & (T0 < cut_f), design & (T0 >= cut_f), ~design
    print(f"points fit {fm.sum()} ({len(set(M[fm]))} coins), val {vm.sum()} ({len(set(M[vm]))}), "
          f"test {tm.sum()} ({len(set(M[tm]))}) [{DEV}]", flush=True)
    for k, (name, _) in enumerate(TARGETS):
        print(f"  every point {name}: fit {Y[fm, k].mean():+.2%} val {Y[vm, k].mean():+.2%}")
    if cmd == "design":
        fit, val = np.flatnonzero(fm), np.flatnonzero(vm)
        net = train(X, gi, meta, Y, fit, val)
        torch.save(net.state_dict(), WEIGHTS)
        P = predict(net, X, gi, meta, val)
        res = []
        for k, (name, _) in enumerate(TARGETS):
            print(f"  val {name}: corr(pred, realized) {np.corrcoef(P[:, k], Y[val, k])[0, 1]:+.4f}")
            for thr in THRS:
                rows = trades(P, Y[val], M[val], TT[val], k, thr)
                print("    ", summary(rows, f"{name} thr {thr}"))
                if len(rows) >= 50:
                    res.append((statistics.mean(r[2] for r in rows), (k, thr)))
        if res:
            res.sort(reverse=True)
            FROZEN.write_text(json.dumps({"k": res[0][1][0], "thr": res[0][1][1]}))
            print("frozen", TARGETS[res[0][1][0]][0], res[0][1][1])
    else:
        fr = json.loads(FROZEN.read_text())
        net = Net().to(DEV)
        net.load_state_dict(torch.load(WEIGHTS, map_location=DEV))
        test = np.flatnonzero(tm)
        P = predict(net, X, gi, meta, test)
        print("TEST", summary(trades(P, Y[test], M[test], TT[test], fr["k"], fr["thr"]),
                              f"{TARGETS[fr['k']][0]} thr {fr['thr']}"))


if __name__ == "__main__":
    main()
