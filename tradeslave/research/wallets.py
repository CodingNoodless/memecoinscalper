"""Do some wallets consistently buy winners, and can we profit by following them?

    uv run python -m tradeslave.research.wallets

Needs wallet-level trades (SCALP_TRADE_FEED=true with a PumpPortal API key).
1. Profiles every wallet on the FIRST half of the recording: realized SOL PnL
   per token (sells - buys, open bags marked at the last price), tokens traded,
   win rate.
2. "Smart" = traded >= min_tokens tokens, net profitable, wins >= min_win_rate.
3. On the SECOND half, every first buy of a token by a smart wallet becomes our
   signal: buy after the fill delay, exit per policy, real fees. Compared with
   following ANY wallet's buy of the same size (the baseline).
"""

from __future__ import annotations

import argparse
import collections
import statistics
from pathlib import Path

from ..scalper.recorder import TICK_DIR, read_ticks
from .dataset import load_tokens
from .sim import simulate, summary


def load_trades(paths=None) -> dict[str, list[tuple]]:
    """mint -> [(t, wallet, is_buy, sol, tokens)] in time order."""
    out: dict[str, list[tuple]] = collections.defaultdict(list)
    for r in read_ticks(paths or sorted(TICK_DIR.glob("*.jsonl.gz"))):
        if r.get("k") == "trade" and r.get("w"):
            out[r["m"]].append((float(r["t"]), r["w"], bool(r.get("b")), float(r.get("sol") or 0.0),
                                float(r.get("tok") or 0.0)))
    for v in out.values():
        v.sort()
    return out


def wallet_profiles(trades: dict[str, list[tuple]], toks, until: float) -> dict[str, dict]:
    """Realized + marked PnL per wallet using only trades before `until`."""
    per = collections.defaultdict(lambda: collections.defaultdict(lambda: [0.0, 0.0, 0.0]))  # w -> m -> [sol_in, sol_out, tokens]
    for mint, rows in trades.items():
        for t, w, buy, sol, tok in rows:
            if t >= until:
                break
            book = per[w][mint]
            if buy:
                book[0] += sol; book[2] += tok
            else:
                book[1] += sol; book[2] -= tok
    profiles = {}
    for w, mints in per.items():
        pnls = []
        for mint, (sol_in, sol_out, bag) in mints.items():
            if sol_in <= 0:
                continue
            tok = toks.get(mint)
            mark = 0.0
            if tok is not None and bag > 0 and tok.p:
                i = tok.idx_at(until)
                mark = bag * tok.p[i] if i >= 0 else 0.0
            pnls.append(sol_out + mark - sol_in)
        if pnls:
            profiles[w] = {"tokens": len(pnls), "pnl_sol": sum(pnls), "win_rate": sum(p > 0 for p in pnls) / len(pnls)}
    return profiles


def follow_signals(trades, smart: set[str] | None, start: float, end: float, min_sol: float) -> list[tuple[float, str]]:
    """First qualifying buy per token in [start, end): by a smart wallet, or by anyone if smart is None."""
    out = []
    for mint, rows in trades.items():
        for t, w, buy, sol, _ in rows:
            if start <= t < end and buy and sol >= min_sol and (smart is None or w in smart):
                out.append((t, mint))
                break
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--min-tokens", type=int, default=3)
    parser.add_argument("--min-win-rate", type=float, default=0.5)
    parser.add_argument("--min-sol", type=float, default=0.2, help="ignore follow signals from buys smaller than this")
    args = parser.parse_args()
    trades = load_trades()
    n = sum(len(v) for v in trades.values())
    if n == 0:
        raise SystemExit("No wallet-level trades recorded yet. Set SCALP_TRADE_FEED=true and PUMPPORTAL_API_KEY, then run scalp.py.")
    toks = load_tokens()
    times = sorted(t for v in trades.values() for t, *_ in v)
    t_start, t_mid, t_end = times[0], times[len(times) // 2], times[-1] + 1
    profiles = wallet_profiles(trades, toks, t_mid)
    smart = {w for w, p in profiles.items()
             if p["tokens"] >= args.min_tokens and p["pnl_sol"] > 0 and p["win_rate"] >= args.min_win_rate}
    print(f"{n} trades by {len({w for v in trades.values() for _, w, *_ in v})} wallets on {len(trades)} tokens; "
          f"profiled on the first half: {len(profiles)} wallets, {len(smart)} 'smart'")
    policies = {"tp10/sl10/60s": dict(tp=0.10, sl=0.10, hold=60), "tp30/sl15/180s": dict(tp=0.30, sl=0.15, hold=180),
                "trail 20/10, sl15, 600s": dict(tp=9, sl=0.15, hold=600, trail=(0.20, 0.10))}
    for label, who in (("follow smart wallets", smart), ("follow any wallet (baseline)", None)):
        signals = follow_signals(trades, who, t_mid, t_end, args.min_sol)
        print(f"\n{label}: {len(signals)} signals in the second half")
        for name, pol in policies.items():
            res = [simulate(toks[m], t, **pol) for t, m in signals if m in toks]
            print(f"  {name:24s} {summary(res)}")
    top = sorted(((p["pnl_sol"], w, p) for w, p in profiles.items() if w in smart), reverse=True)[:10]
    if top:
        print("\ntop smart wallets (first half):")
        for pnl, w, p in top:
            print(f"  {w[:6]}…{w[-4:]}  pnl {pnl:+.3f} SOL  tokens {p['tokens']}  win {p['win_rate']:.0%}")


if __name__ == "__main__":
    main()
