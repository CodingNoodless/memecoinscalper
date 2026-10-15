"""python -m tradeslave.autopilot [cycle|status]

cycle  : backfill exact outcomes -> build samples -> train -> test -> promote
status : what the autopilot has learned so far
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from ..paths import PROJECT_ROOT
from ..research.chain import env_rpc
from ..scalper.config import load_config
from .model import AUTO_DIR, HISTORY, compete, record_history, save_champion, train_cycle
from .samples import load_samples


def _say(*parts):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *parts, flush=True)


def _lock():
    path = PROJECT_ROOT / ".autopilot.lock"
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def cycle(args) -> int:
    lock = _lock()
    if lock is None:
        _say("another autopilot cycle is running; skipping")
        return 0
    try:
        import numpy  # noqa: F401
        import sklearn  # noqa: F401
    except ImportError:
        _say("the autopilot needs numpy + scikit-learn: run `uv sync`")
        return 2
    cfg = load_config()
    AUTO_DIR.mkdir(parents=True, exist_ok=True)
    started = time.time()
    _say(f"cycle start: last {cfg.autopilot_days} days")
    if env_rpc() and not args.no_backfill:
        from ..research.backfill_trades import run as backfill
        budget_file = AUTO_DIR / "credits.json"
        day = time.strftime("%Y-%m-%d", time.gmtime())
        try:
            used = json.loads(budget_file.read_text()).get(day, 0)
        except (OSError, ValueError):
            used = 0
        left = cfg.autopilot_daily_credits - used
        _say(f"Helius credits used today by backfills: ~{used} of {cfg.autopilot_daily_credits}")
        try:
            if left > 0:
                backfill(since=time.time() - cfg.autopilot_days * 86400, until=time.time() - 900, window=900.0,
                         limit=cfg.autopilot_backfill_limit, max_credits=int(left * 0.8), log=lambda *a: _say(*a))
                used += getattr(backfill, "last_credits", 0)
                left = cfg.autopilot_daily_credits - used
            if cfg.autopilot_launch_sample and left > 0:
                _say("unbiased launch sample (all launches, for launch research):")
                backfill(since=time.time() - 2 * 86400, until=time.time() - 900, window=900.0, min_updates=0,
                         limit=cfg.autopilot_launch_sample, sample="all", max_credits=left,
                         log=lambda *a: _say(*a))
                used += getattr(backfill, "last_credits", 0)
            if left <= 0:
                _say("daily credit budget used up: skipping backfill until tomorrow (UTC)")
        except Exception as exc:  # noqa: BLE001 - learning continues on recorded data
            _say(f"backfill failed ({type(exc).__name__}: {exc}); continuing with what we have")
        finally:
            try:
                budget_file.write_text(json.dumps({day: used}))
            except OSError:
                pass
    elif not args.no_backfill:
        _say("no RESEARCH_RPC_URL in .env: labels come from recorded ticks only (exact chain outcomes are better)")
    samples = load_samples(cfg, cfg.autopilot_days, log=lambda *a: _say(*a))
    _say(f"{len(samples)} samples from {len({s.mint for s in samples})} tokens")
    card, model, info = train_cycle(samples, log=lambda *a: _say(*a))
    promoted = False
    if card is not None:
        h = card.holdout
        _say(f"challenger {card.version}: policy {card.policy}, threshold {card.threshold:+.4f} | holdout "
             f"{h.trades} trades, mean {h.mean * 100:+.2f}%, median {h.median * 100:+.2f}%, win {h.win:.0%}, "
             f"biggest token share {h.top_token_share:.0%} | paper_ok={card.paper_ok} proven={card.proven}")
        promoted = compete(card, model, info, log=lambda *a: _say(*a))
        if promoted:
            save_champion(card, model)
            _say(f"PROMOTED {card.version} -> the scalper picks it up within a minute")
        elif not card.paper_ok:
            _say("not promoted: it lost money on the unseen holdout (or had too few trades). Nothing changes.")
        else:
            _say("not promoted: the current champion did better on the same unseen data")
    else:
        _say("no candidate: nothing made money even on validation. Nothing changes.")
    record_history(card, info, promoted)
    _say(f"cycle done in {(time.time() - started) / 60:.1f} min")
    return 0


def status(_args) -> int:
    if (AUTO_DIR / "champion.json").exists():
        c = json.loads((AUTO_DIR / "champion.json").read_text())
        h = c["holdout"]
        print(f"champion {c['version']}: {c['policy']} | holdout {h['trades']} trades mean {h['mean'] * 100:+.2f}% "
              f"| paper_ok={c['paper_ok']} proven={c['proven']}")
    else:
        print("no champion yet: still learning")
    if HISTORY.exists():
        print("\nlearning history (newest last):")
        for line in HISTORY.read_text(encoding="utf-8").splitlines()[-15:]:
            r = json.loads(line)
            h = r.get("holdout") or {}
            print(f"  {time.strftime('%m-%d %H:%M', time.localtime(r['t']))} samples {r.get('n_samples', 0):7d} "
                  f"policy {r.get('policy', '-'):22s} holdout {h.get('trades', 0):4d} trades "
                  f"mean {h.get('mean', 0) * 100:+6.2f}%  {'PROMOTED' if r['promoted'] else ''}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cycle")
    c.add_argument("--no-backfill", action="store_true")
    sub.add_parser("status")
    args = parser.parse_args()
    sys.exit(cycle(args) if args.cmd == "cycle" else status(args))


if __name__ == "__main__":
    main()
