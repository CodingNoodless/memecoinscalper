---
description: Round 2 long run - fix data supply first, then test cost-aware hypotheses. Use as /goal text or paste as a prompt.
---
Round 2: data first, then hypotheses built to clear costs. Follow CLAUDE.md throughout. `lab/gates.yaml` (174435d),
`tools/lab.py` and `tradeslave/cost_model.py` are locked. Paper only. No J (creator launch) work in this run.

Context: round 1 (vault `Reviews/Final-Report.md`, commit b3a85bb) killed LP1, MAX1, VD1, LP2, TOD1 on validation.
Gross edge was ~0 in every family; the losses were the costs (round trip 5.08% at 0.1 SOL, stops fill 16.2% below
the stop on average). Two root causes: (1) only 3.9 days of unbiased data, so graduated-coin ideas can't reach 300
OOS trades; (2) the hypotheses aimed at moves too small to clear costs. This run fixes both, in that order.

## Phase 0: housekeeping (keep it short)
- Fix the 4 failing tests in `tests/test_runners.py` (`runners.fee_for` no longer exists): update them to the current
  API without changing bot behavior. `uv run pytest -q` must be fully green; commit.
- Rohit's decision: the 6 h gap is accepted for the Oct 13–17 set (data v1). All new data uses 24 h gaps. Log it in
  `Project/Decisions.md`.

## Phase 1: data supply (the main job)
- The candle API is deprecated, but chain data isn't. Build candles + reserves from raw pump.fun / PumpSwap trades:
  `tradeslave/research/history_miner.py` (public RPC), `data/full_trades/`, `data/trades/history.jsonl` +
  `backfill.jsonl`, and the live feed recorder. Write one tested builder (raw trades → 1-min OHLCV + reserves). Check it
  against the v1 API candles on overlapping tokens and report the match rate. If the match rate is poor, fix the
  builder before going on.
- Sampling must be unbiased: a random sample of all launches by creation time (not only those a bot watched), rugs and
  dead tokens included. Report the rug/death share and compare it with v1's 97.9%.
- Start a continuous free launch recorder (all launches, trades, reserves) in the background. Log to `logs/`, add it to
  the `tools/start_all.sh` table (so `start_all.ps1` picks it up) and to `Data & Infra/Running programs.md`.
- Build `data/research/v2/` covering Oct 13 → now with 24 h gaps (60/20/20 by creation time, most recent = holdout).
  Write a new manifest. Keep v1 read-only; its holdout stays unused.
- Target ≥ 14 days. If free-RPC backfill is too slow, measure throughput, give the ETA, use what's built, and keep the
  recorder running. Report per split: tokens, graduated tokens, and whether 300 OOS trades are reachable for
  curve-only and graduated-only ideas.
- Document everything in `Data & Infra/Research datasets.md`.

## Phase 2: hypotheses designed to clear costs
- From `cost_model.py`, compute the break-even gross move per trade at 0.1 SOL and 0.25 SOL, curve vs PumpSwap,
  including the stop gap. Write it at the top of `Strategies/Hypotheses/Ranking-v2.md`.
- Every hypothesis must state its payoff shape (hit rate × avg win vs avg loss) and show why its expected gross edge is
  ≥ 2× the round-trip cost. It names who loses and why, and states what falsifies it. Exits that rely on curve stops
  must use the modeled stop gap; prefer time exits or wide stops where it fits.
- Candidates:
  - The 5 "never tested properly" items in `Strategies/Lessons-Learned.md` (M13 after fix #43, DL, NL, W, Y19), where
    v2 data supports them.
  - New ideas from the `Reviews/Final-Report.md` directions, only if the data is free and available in this run. For
    perps and prediction markets, only killtests on existing data.
  - Nothing listed as dead.
- Write 5–8 notes in `Strategies/Hypotheses/`. Get a `skeptic` review on each, rank them by economic reasoning, register
  them with `tools/lab.py reg add`, and **commit the ranking before any result is computed** (that commit is the
  pre-registration).

## Phase 3: testing
- For each ranked hypothesis in order: pre-register it (≤ 20 variants), run it on v2 train, select on v2 validation,
  costs from `cost_model.py`, and print the `uv run python tools/gate_check.py` output in the conversation.
- Only if every non-holdout gate passes: run `tools/gate_check.py --final <id>` once. A holdout fail is final.
- After each one: Experiments note, registry status, Lessons-Learned line if it's killed, `lab/state_notes.md`, today's
  Sessions note, `tools/lab.py brief --write D:/vault/tradeslave`, commit.

## Phase 4: keep going until something passes
A batch where every hypothesis dies is not the end. Write `Reviews/Batch-<n>-Report.md` (gross vs net per idea, why
each died, what that rules out), add the dead ideas to `Lessons-Learned.md`, then go back to Phase 2 with a new batch
of 5–8 hypotheses from **different families**. Use what the dead batch taught: where gross edge appeared, which
costs dominated, and which data was missing. When data is the blocker, go back to Phase 1 and build the missing data
(free sources only). Between batches, use the `researcher` agent for new sourced ideas (bots, MEV, launchpads,
prediction markets, perps, other chains, other venues) and check `Strategies/Lessons-Learned.md` first.
Trust protections, which get stricter as the run goes on (never looser):
- The trials count includes every variant of every batch, so deflated Sharpe and the luck hurdle rise automatically.
- The holdout is one shot per hypothesis. If more than 20 hypotheses have used the v2 holdout, build a fresh holdout
  from newly recorded data (the launch recorder) before the next `--final`.
- A backtest PASS is not the finish line: it must also pass a forward paper test under the `tools/lab.py` rules.

## Stop only when one of these holds
(a) **Goal reached:** a hypothesis has ALL PASS from `tools/gate_check.py --final <id>` (`lab/gates.yaml` unchanged
since 174435d) **and** a forward PASS in `uv run python tools/lab.py scoreboard`. While its forward test runs, keep
researching the next batch, and check the scoreboard each turn. Then write `Reviews/Final-Report-2.md`. No live
step without Rohit's go.

(b) **Turn cap:** 150 turns. Update `lab/state_notes.md`, STATE.md and `Reviews/Interim-Report-2.md` with batches
done, what's queued next, and the forward bot status, so the next run continues where this one stopped.

Each turn, finish one concrete step and print the tool output that proves it. Never relax a gate, cost or split to
get a pass: a pass obtained that way isn't the goal.
