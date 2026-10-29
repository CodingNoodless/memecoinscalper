# TradeSlaveBot — instructions for Codex

Autonomous Solana memecoin trading bot. **Paper only** until Rohit explicitly approves a live micro-test.
Vault (second brain): `D:\vault\tradeslave` (Obsidian). Code: this repo. Workflow: vault `Project/Workflow v2.md`.

## Mission and the numbers that matter
- Targets (Rohit): **$1k by 2026-11-15, $10k by 2026-12-31**. Capital when live: < $500.
- Profit/day = net edge per trade × size × trades/day. Nothing counts until a strategy **PASSES a forward test**
  (`uv run python tools/lab.py scoreboard`). Optimize for *time to a trustworthy yes/no*, not for more ideas.

## Start of every session (the SessionStart hook prints the brief)
1. Read the brief (`tools/lab.py brief`): forward scoreboard with KILL / CONTINUE / PASS, pipeline, action items.
2. Read vault `STATE.md`, `Project/Roadmap.md`, the newest note in `Sessions/`. Do **not** read the whole vault;
   search it (`grep -ril`) for the topic at hand. `Research/` holds 12k+ source notes: never bulk-read them.
3. If programs are down: `bash tools/start_all.sh --list`, then `bash tools/start_all.sh`.

## The pipeline (every piece of work is one registry entry: `tools/lab.py reg ...`)
`idea → killtest (≤ 2 h, existing data) → prereg → backtest (one-shot holdout) → forward (paper bot) → passed | killed`
- **WIP limit:** at most 3 forward-test bots beyond the baselines. A new one starts only when one is KILLED or PASSED.
- **Kill fast:** run the cheapest falsification first (existing data, ≤ 2 h). Most ideas should die here.
- **Pre-register before touching the holdout:** hypothesis, data split, exact parameter grid (≤ 20 variants),
  pass gate, number of variants tried in the family. Write it in the Experiments note *before* running.
- **One shot:** a holdout/test split is scored once. Re-running it after changes = new variant, new data.
- **Forward verdicts are mechanical** (tools/lab.py rules): KILL at n ≥ 20 if the 80% upper bound < 0, or n ≥ 60 with
  t < 1; PASS at n ≥ 100, t ≥ 2.5 (higher with many variants), both halves > 0, positive without the top 3 trades.
  No "one more tweak" on a KILLED bot: a fix is a new pre-registered variant.

## Hard rules
- **Costs:** 1.25% fee per side (worst case, Rohit 2026-10-16) + 0.0003 SOL network fee per tx + latency as in the
  existing simulators. Never assume better fills than the paper bots get.
- **Paper only.** Never sign transactions, never handle wallet keys, never read `.env` contents into chat or notes.
- **J (creator launches):** stays paper. Any live launch needs Rohit's explicit go and the guardrails in vault
  `Strategies/J - Creator launches.md` (honest metadata, legal/tax check).
- Label numbers *measured* / *sourced* / *assumption*. Report failures with the same care as wins.
- Long jobs: run in the background with a log in `logs/`, list them in vault `Data & Infra/Running programs.md`.
- Tests: `uv run pytest -q` before committing. Commit with a message that states the result, not just the change.

## End of every session (`/wrap`)
Session note in `Sessions/`, registry statuses updated, Experiments rows, Decisions rows, `tools/lab.py brief --write
D:/vault/tradeslave` to refresh `STATE.md`, Running programs updated, commit.

## Useful commands
- `/brief` `/idea` `/killtest` `/prereg` `/test-once` `/forward` `/wrap` `/weekly` (see `.Codex/commands/`)
- Subagents: `skeptic` (attack any claimed edge), `researcher` (sourced findings → vault), `experiment-runner`
  (executes one pre-registered test exactly), `scribe` (vault bookkeeping).
