"""Strategy N1: predict memecoin-perp charts from the raw candle sequence (dilated 1D CNN, GPU).

    uv run --no-sync python -m tradeslave.research.perp_nn design   # train on fit, choose rule on validation
    uv run --no-sync python -m tradeslave.research.perp_nn test     # judge the frozen rule once on test

Input: the last WIN minutes of 1-minute klines, per minute: log return, high-low range, body, log volume vs the
window's mean, BTC log return (all x100, clipped); plus hour of day. Targets: perp_chart.labels (long / short
x 15 / 60 / 240 min, entry next open, 0.07%/side). Split as T2 (strict): fit 2024-07..2025-09, validation
2025-10..2026-02, test 2026-03..2026-10-16, both universes (35 prominent + 18 newer memecoins).
Trades: perp_chart.trade (non-overlapping per coin) at 15-minute decision points.
Pre-registered in the vault note "2026-10-19 neural chart prediction (N)".
"""

from __future__ import annotations

import json
import statistics
import sys
import time

import numpy as np
import torch
from torch import nn

from ..paths import DATA_DIR
from .perp_chart import HS, LABELS, NEWER, btc_ref, day_t, labels, load, summary, trade
from .perp_pos import START, TEST_FROM, VAL_FROM
from .perps_data import SYMBOLS

WIN = 240
NF = 5
FROZEN = DATA_DIR / "perp_nn_rule.json"
WEIGHTS = DATA_DIR / "perp_nn.pt"
THRS = (0.0, 0.001, 0.002, 0.004, 0.008)
DEV = "cuda" if torch.cuda.is_available() else "cpu"


def build():
    """Concatenated per-minute features of every coin + sample indices (global row, symbol, time, labels)."""
    btc = btc_ref()
    feats, rows = [], []
    off = 0
    for s in SYMBOLS + NEWER:
        a = load(s)
        if a is None or len(a) < 5000:
            continue
        t, o, h, lo, c, qv = a.T
        r = np.r_[0.0, np.log(c[1:] / c[:-1])]
        lv = np.log1p(qv)
        mv = np.convolve(lv, np.ones(WIN) / WIN, mode="full")[: len(lv)]
        bi = np.clip(np.searchsorted(btc["t"], t), 1, len(btc["t"]) - 1)
        bc = np.log(np.maximum(btc[5][bi] + 1, 1e-9)) / 5          # ~1-min BTC log return from its 5-min move
        F = np.column_stack([r * 100, (h - lo) / c * 100, (c - o) / c * 100, lv - mv, bc * 100])
        F = np.clip(np.nan_to_num(F), -50, 50).astype(np.float32)
        i = np.arange(WIN, len(a) - max(HS) - 1)
        ok = ((t[i] - t[i - WIN + 1]) == 60 * (WIN - 1)) & ((t[i + max(HS)] - t[i]) == 60 * max(HS)) & (t[i] >= START)
        i = i[ok]
        Y = labels(a, i).astype(np.float32)
        feats.append(F)
        rows.append((off + i, np.full(len(i), s), t[i], Y))
        off += len(a)
        print(f"  {s}: {len(i)} samples", flush=True)
    X = np.concatenate(feats)
    g = np.concatenate([r[0] for r in rows])
    S = np.concatenate([r[1] for r in rows])
    T = np.concatenate([r[2] for r in rows])
    Y = np.concatenate([r[3] for r in rows])
    return X, g, S, T, Y


class Net(nn.Module):
    def __init__(self, ch=64):
        super().__init__()
        self.inp = nn.Conv1d(NF, ch, 1)
        self.blocks = nn.ModuleList([nn.Sequential(nn.Conv1d(ch, ch, 3, padding=d, dilation=d), nn.BatchNorm1d(ch),
                                                   nn.GELU()) for d in (1, 2, 4, 8, 16, 32)])
        self.head = nn.Sequential(nn.Linear(2 * ch + 2, 128), nn.GELU(), nn.Dropout(0.1), nn.Linear(128, len(LABELS)))

    def forward(self, x, hr):
        z = self.inp(x)
        for b in self.blocks:
            z = z + b(z)
        pooled = torch.cat([z.mean(-1), z[..., -1], torch.sin(hr), torch.cos(hr)], 1)
        return self.head(pooled)


def batch(X, g, T, idx):
    w = g[idx][:, None] + np.arange(-WIN + 1, 1)[None, :]
    x = torch.from_numpy(X[w]).permute(0, 2, 1)
    hr = torch.from_numpy(((T[idx] // 3600) % 24 * (2 * np.pi / 24)).astype(np.float32))[:, None]
    return x.to(DEV, non_blocking=True), hr.to(DEV)


def predict(net, X, g, T, idx, bs=4096):
    net.eval()
    out = []
    with torch.no_grad():
        for k in range(0, len(idx), bs):
            x, hr = batch(X, g, T, idx[k:k + bs])
            out.append(net(x, hr).float().cpu().numpy())
    return np.concatenate(out) / 100


def train(X, g, T, Y, fit_idx, val_idx, epochs=10, per_epoch=1_500_000, bs=2048):
    torch.manual_seed(0)
    rng = np.random.default_rng(0)
    net = Net().to(DEV)
    opt = torch.optim.AdamW(net.parameters(), lr=1e-3, weight_decay=1e-4)
    yt = torch.from_numpy(np.clip(Y, -0.2, 0.2) * 100)
    loss_fn = nn.HuberLoss(delta=2.0)
    best, best_state, bad = 1e9, None, 0
    vsub = rng.choice(val_idx, min(len(val_idx), 200_000), replace=False)
    for ep in range(epochs):
        net.train()
        t0 = time.time()
        order = rng.choice(fit_idx, min(per_epoch, len(fit_idx)), replace=False)
        tot = 0.0
        for k in range(0, len(order), bs):
            idx = order[k:k + bs]
            x, hr = batch(X, g, T, idx)
            loss = loss_fn(net(x, hr), yt[idx].to(DEV))
            opt.zero_grad()
            loss.backward()
            opt.step()
            tot += loss.item() * len(idx)
        P = predict(net, X, g, T, vsub) * 100
        vl = float(loss_fn(torch.from_numpy(P), yt[vsub]).item())
        print(f"  epoch {ep + 1}: train {tot / len(order):.4f} val {vl:.4f} ({time.time() - t0:.0f} s)", flush=True)
        if vl < best - 1e-4:
            best, best_state, bad = vl, {k: v.detach().clone() for k, v in net.state_dict().items()}, 0
        else:
            bad += 1
            if bad >= 2:
                break
    net.load_state_dict(best_state)
    return net


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "design"
    X, g, S, T, Y = build()
    newer = np.isin(S, NEWER)
    fm, vm, tm = T < VAL_FROM, (T >= VAL_FROM) & (T < TEST_FROM), T >= TEST_FROM
    step15 = (T % 900) == 0
    print(f"samples fit {fm.sum()}, val {vm.sum()}, test {tm.sum()} ({DEV})", flush=True)
    if cmd == "design":
        fit_idx = np.flatnonzero(fm & ((T % 300) == 0))
        val_idx = np.flatnonzero(vm & step15)
        net = train(X, g, T, Y, fit_idx, val_idx)
        torch.save(net.state_dict(), WEIGHTS)
        P = predict(net, X, g, T, val_idx)
        Yv, Sv, Tv = Y[val_idx], S[val_idx], T[val_idx]
        for k, (d, H) in enumerate(LABELS):
            c = np.corrcoef(P[:, k], Yv[:, k])[0, 1]
            print(f"  val {'long' if d > 0 else 'short'} {H}m: corr(pred, realized) {c:+.4f}")
        res = []
        for k in range(len(LABELS)):
            for thr in THRS:
                rows = trade(P, Yv, Sv, Tv, k, thr)
                if len(rows) >= 200:
                    res.append((day_t(rows), statistics.mean(r[2] for r in rows), (k, thr), rows))
        for tt, m, (k, thr), rows in sorted(res, key=lambda r: -r[0])[:8]:
            d, H = LABELS[k]
            print("  ", summary(rows, f"{'long' if d > 0 else 'short'} {H}m thr {thr}"))
        res = [r for r in res if r[1] >= 0.001]
        if res:
            res.sort(key=lambda r: -r[0])
            FROZEN.write_text(json.dumps({"k": res[0][2][0], "thr": res[0][2][1]}))
            print("frozen", LABELS[res[0][2][0]], res[0][2][1])
        else:
            print("nothing passes the validation floor (mean >= 0.1%, n >= 200)")
    else:
        fr = json.loads(FROZEN.read_text())
        net = Net().to(DEV)
        net.load_state_dict(torch.load(WEIGHTS, map_location=DEV))
        for lbl, sel in (("prominent", tm & ~newer & step15), ("newer", tm & newer & step15)):
            idx = np.flatnonzero(sel)
            P = predict(net, X, g, T, idx)
            rows = trade(P, Y[idx], S[idx], T[idx], fr["k"], fr["thr"])
            print(f"TEST {LABELS[fr['k']]} thr {fr['thr']} {lbl}:", summary(rows, ""))


if __name__ == "__main__":
    main()
