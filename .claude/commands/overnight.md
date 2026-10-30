---
description: Unattended overnight run - Rohit's Oct 30 answers, data speed-up first, then round2.md loop. No questions; log decisions and keep going.
---
Rohit is asleep. Work autonomously: never stop to ask. When a decision is needed, make the reasonable choice, log it
in `Project/Decisions.md` as "Claude, overnight, Rohit to confirm", and continue. Follow CLAUDE.md and
`.claude/commands/round2.md` (gates, costs, splits locked; paper only; no J work). Speed matters: every week lost delays
the result. So does trust: never trade rigor for speed.

## Rohit's answers (2026-10-30)
1. One more Helius endpoint is in `.env`.
2. Helius credits reset monthly. When a key runs out, mark it exhausted with its reset date and skip it until then;
   one dead key must never block work. Keep `lab/endpoints.json` (labels only, never keys or URLs with keys): provider,
   label, status working/exhausted/erroring, credits used if known, last error, reset date. Every status update says
   how many endpoints are working per provider. Never print or write `.env` contents.
3. The recorder may use 100–300 MB/day of disk.
4. Find a way to speed things up. Train-only killtests on v1 are approved now (no gates, no holdout).

## Order of work (start long jobs first, then work in the foreground while they run)
1. **Recorder, first hour.** Start the all-launches recorder (every pump.fun + PumpSwap create, trade and migration,
   with reserves). Verify it is writing, then add it to the `tools/start_all.sh` table and `Data & Infra/Running
   programs.md`. Every hour without it is data lost for good.
2. **Faster history: measure, then pick.** Test each method on a small sample (≤ 30 min each), report throughput
   (token-days or chain-days per hour), and run the fastest in the background with checkpoint/resume:
   a. Program-wide fetching instead of one token at a time: `getBlock`, or `getSignaturesForAddress` on the pump.fun
      and PumpSwap program IDs. Use batched JSON-RPC and concurrency across all working endpoints, with a per-endpoint
      rate limiter and failover.
   b. Helius bulk/enhanced history methods within the credit budget, rotating keys.
   c. Free bulk datasets: check `tradeslave/research/bq_test.py` and any earlier BigQuery work (Google's public Solana
      dataset, 1 TB/month of free queries), the Dune free tier, and other free indexers. Before relying on a source,
      check its completeness against `data/c1/creates.jsonl` for one full day.
   d. GeckoTerminal one-minute candles for graduated pools, within its rate limit.
   Write the new ETA for a usable v2 into `lab/state_notes.md`.
3. **Candle builder:** raw trades → 1-minute OHLCV + reserves, with tests. Check it against the v1 API candles on the
   `data/full_trades` overlap and report the match rate. Don't use the builder until the match is good.
4. **Don't wait for a calendar date.** Build each v2 split (24 h gaps, manifest) as soon as its validation set can
   support ≥ 300 out-of-sample trades for the hypothesis being tested. Graduated-coin data (Oct 21+) will likely be
   first. Never run a full gate test underpowered.
5. **Hypotheses (round2.md Phase 2):** start with the break-even table. Every idea must show expected gross edge ≥ 2× the
   round-trip cost, get a skeptic review, and have its ranking committed before any results. Graduated ideas come
   first. Cull fast with train-only killtests on v1 or v2-train; only survivors get the full validation gates and
   `--final`.
6. **Then the round2.md Phase 3/4 loop**, batch after batch, until the goal is met.

## Efficiency
- Anything over 5 minutes runs in the background with a log in `logs/` and a checkpoint. Never sit idle waiting: do
  the next foreground step. Each turn, check the background jobs (one compact line each) and restart dead ones from
  their checkpoint.
- Use subagents for vault and research scans. Keep outputs compact.
- Don't polish, and don't start side projects that don't move the data or the hypotheses forward.

## Morning report
Keep `Reviews/Overnight-2026-10-31.md` updated at least every 10 turns, so Rohit can read it in 2 minutes:
- what is running and its health;
- data built per source (days, tokens, rug share) and throughput;
- the new ETA;
- endpoint counts per provider (working / exhausted / erroring);
- each hypothesis's status with numbers;
- decisions waiting for Rohit.
