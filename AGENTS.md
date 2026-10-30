# TradeSlaveBot — instructions for Codex (copy of CLAUDE.md; CLAUDE.md is the source, keep in sync)

Autonomous Solana memecoin trading bot. **Paper only** until Rohit explicitly approves a live micro-test.
Vault (second brain): `D:\vault\tradeslave` (Obsidian). Code: this repo. Workflow: vault `Project/Workflow v2.md`.
Shell: Windows + PowerShell (no WSL). Use `uv run python ...`; `bash` commands only work in Git Bash.

## Mission and the numbers that matter
- Targets (Rohit): **$1k by 2026-11-15, $10k by 2026-12-31**. Capital when live: < $500.
- Profit/day = net edge per trade × size × trades/day. Nothing counts until a strategy **PASSES a forward test**
  (`uv run python tools/lab.py scoreboard`). Optimize for *time to a trustworthy yes/no*, not for more ideas.
- **Targets never override rules.** If hitting a target would need a relaxed gate, a softened cost, or a reversed
  KILL, the target loses. Do the math in the open: at 1.25% fee per side, a 5% net trade needs ~7.5%+ gross.
  If the scoreboard shows a target is out of reach by its date, say so in the brief with the numbers.
  That is information Rohit needs, not a failure to hide.
- **"No edge in this family" is a valid result.** A fake edge is the worst possible result: it loses real money later.

## Start of every session (the SessionStart hook prints the brief)
1. Read the brief (`tools/lab.py brief`): forward scoreboard with KILL / CONTINUE / PASS, pipeline, action items.
2. Read vault `STATE.md`, `Project/Roadmap.md`, the newest note in `Sessions/`. Do **not** read the whole vault;
   search it (`grep -ril`) for the topic at hand. `Research/` holds 12k+ source notes: never bulk-read them.
   Navigation starts at `Map/Memecoin bot knowledge map.md` and the `Map/Map - <topic>.md` notes.
3. If programs are down: `powershell -ExecutionPolicy Bypass -File tools\start_all.ps1 -List`, then the same without
   `-List` (starts only what's missing; `-Stop` stops all). `tools/start_all.sh` is the program table it reads.

## The pipeline (every piece of work is one registry entry: `tools/lab.py reg ...`)
`idea → killtest (≤ 2 h, existing data) → prereg → backtest (one-shot holdout) → forward (paper bot) → passed | killed`
- **WIP limit:** at most 3 forward-test bots beyond the baselines. A new one starts only when one is KILLED or PASSED.
- **Kill fast:** run the cheapest falsification first (existing data, ≤ 2 h). Most ideas should die here.
- **Pre-register before touching the holdout:** hypothesis, data split, exact parameter grid (≤ 20 variants),
  pass gate, number of variants tried in the family. Write it in the Experiments note *before* running.
- **Economic rationale required:** every prereg names who is on the losing side of the trade and why they keep
  losing. No answer = no test.
- **One shot:** a holdout/test split is scored once. Re-running it after changes = new variant, new data.
- **Backtest gates live in `lab/gates.yaml`** and are checked by `uv run python tools/gate_check.py <trades file>`,
  which prints PASS/FAIL per gate plus the git hash of `lab/gates.yaml`. They include: ≥ 300 out-of-sample trades,
  profit factor after costs > 1.3, bootstrap 95% CI of expectancy > 0, max drawdown < 35% (at 10%-of-equity
  sizing), still PF > 1.1 with every parameter shifted ±20%, still profitable without the top 5% of trades, deflated
  Sharpe probability > 0.95, holdout PF > 1.2, and a **capacity estimate** (the position size where the edge
  disappears). Report the capacity number with every backtest PASS.
- **Trials count** (for deflated Sharpe and the luck hurdle) = all variants ever tried in the family: registry
  `variants` fields in `lab/registry.jsonl` plus the historical tally in vault `Project/Hypothesis count.md`.
- **Forward verdicts are mechanical** (tools/lab.py rules): KILL at n ≥ 20 if the 80% upper bound < 0, or n ≥ 60 with
  t < 1; PASS at n ≥ 100, t ≥ 2.5 (higher with many variants), both halves > 0, positive without the top 3 trades.
  These forward rules (in `tools/lab.py`) are separate from the backtest gates (in `lab/gates.yaml`).
  No "one more tweak" on a KILLED bot: a fix is a new pre-registered variant.

## Hard rules
- **Costs:** 1.25% fee per side (worst case, Rohit 2026-10-16) + 0.0003 SOL network fee per tx + latency as in the
  existing simulators. The single cost model is `tradeslave/cost_model.py` (documented in vault
  `Data & Infra/Cost model.md`); backtests import it rather than hard-coding costs. Stop exits fill below the stop
  (measured on M13b: median 6.8% below, worst 10% at −64% on rug gaps): model the gap, never fill at the stop. Never assume better fills than the paper bots get. Slippage must scale with position size
  vs. pool liquidity; if a simulator doesn't model that, add it as a cost layer rather than assuming zero.
- **Locked thresholds:** never edit the verdict rules in `tools/lab.py`, `lab/gates.yaml`, a prereg's pass gate, or
  the cost model to make a result pass. Any change needs Rohit's explicit OK and a Decisions row. Every reported
  verdict includes the git hash of the rule file that produced it (`tools/lab.py` for forward, `lab/gates.yaml` for
  backtest); `tools/lab.py brief` prints both.
- **Data integrity:** no lookahead (signals use only data available at decision time; no fills at the same bar's
  close; no indicators on unfinished candles). Datasets must include rugged, dead, and delisted tokens; testing
  only on survivors is survivorship bias and invalidates the result. Note the token universe in every prereg.
  Research datasets live in `data/research/` (train 60% / validation 20% / holdout 20% = most recent, split by token
  creation time with a gap between splits, plus a `manifest.json` of sources and date ranges). The holdout is read
  only by a pre-registered one-shot test; nothing else (exploration, plots, feature work) may open it.
- **Paper only.** Never sign transactions, never handle wallet keys, never read `.env` contents into chat or notes.
- **J (creator launches):** stays paper. Any live launch needs Rohit's explicit go and the guardrails in vault
  `Strategies/J - Creator launches.md` (honest metadata, legal/tax check).
- Label numbers *measured* / *sourced* / *assumption*. Report failures with the same care as wins.
- Long jobs: run in the background with a log in `logs/`, list them in vault `Data & Infra/Running programs.md`.
- Tests: `uv run pytest -q` before committing. Commit with a message that states the result, not just the change.

## Token management
- Any vault search touching more than a handful of notes goes through a subagent (`researcher` or `scribe`), which
  returns a short summary with links. Raw notes stay out of the main context.
- Tools print compact summaries (tables, < 50 lines). Never dump raw data, full logs, or whole notes into chat;
  use `tail`, `grep`, or a summary script.
- After each pipeline step (one killtest, one prereg, one test): update the registry, overwrite
  `lab/state_notes.md` (what's in progress, what's next), append a line to today's `Sessions/` note, and run
  `uv run python tools/lab.py brief --write D:/vault/tradeslave`. `STATE.md` is generated: never edit it by hand.
  This way compaction (automatic or `/compact`) or a resumed session loses nothing.
- Prefer one finished pipeline step over three half-started ones.

## Autonomous runs (`/goal` and `/loop`)
- `/goal` is *unverified*: if your Claude Code version doesn't have it, paste the goal text as a normal prompt; the
  rules below still apply.
- The `/goal` evaluator only reads the conversation; it doesn't run commands or open files. Goal conditions must
  therefore point at tool output printed in the transcript (e.g. `tools/lab.py scoreboard`), never at a claimed number.
- Never set or accept a goal stated as a return target ("make 5%/trade", "reach $1k"). Goals are pipeline states:
  "N killtests completed and logged", "scoreboard shows PASS for a pre-registered variant", "idea queue resolved".
- Every goal has a turn cap (`or stop after N turns`) and requires printing the relevant lab.py output each turn.
- Template:
  `/goal Either uv run python tools/lab.py scoreboard shows PASS for a pre-registered variant with tools/lab.py
  unchanged since commit <hash>, or every idea in the registry queue is resolved (killed / forward / passed) with
  results logged in the vault; or stop after 40 turns. Each turn: complete one pipeline step, print the lab.py
  output for it, update the registry, vault, and lab/state_notes.md, then run lab.py brief --write.`
- `/loop` is for watching forward bots, e.g. `/loop 1h` read paper logs since the last check, append a summary to
  the day's Sessions note, and flag any bot that hits a KILL condition at the top of `lab/state_notes.md` (then refresh
  `STATE.md` with `lab.py brief --write`). It only runs while
  the session is open and expires after 7 days; re-arm it weekly.
- When the queue is exhausted: stop, write a report in `Sessions/` covering what was tested and learned, and
  propose new idea *families* (not tweaks of killed ideas).

## Vault conventions
- Frontmatter on every new note: `type`, `status`, `strategy_family`, `date`, `result`, `links`.
- Link chain: every Experiments row links to its idea/prereg note; every idea links to a strategy-family Map of
  Content in `Map/` (create one if missing, named `Map - <topic>.md`); every MOC is linked from
  `Map/Memecoin bot knowledge map.md`.
- Blocks between `<!-- map:start -->` and `<!-- map:end -->` are generated by `uv run python -m tradeslave.vault_map`:
  never hand-edit them (they get overwritten). Write your own links outside those blocks.
- Folders: strategy families `Strategies/<letter> - <name>.md`; hypotheses `Strategies/Hypotheses/`; tests
  `Experiments/` (index `Experiments/Experiments.md`); paper trades `Trades/`; data and infra `Data & Infra/`;
  decisions `Project/Decisions.md`; daily logs `Sessions/YYYY-MM-DD.md`.
- Every KILLED idea gets a short "why it died" paragraph and a line in `Strategies/Lessons-Learned.md` (the one and
  only lessons file), so dead ideas aren't re-tested under a new name. Check it before proposing any idea.
- Never delete notes. Mark superseded ones `status: archived` and link the replacement.
- New research enters via the `researcher` subagent, sourced, and linked to the idea it informs.

## End of every session (`/wrap`)
Session note in `Sessions/`, registry statuses updated, Experiments rows, Decisions rows, `lab/state_notes.md`
overwritten, `tools/lab.py brief --write D:/vault/tradeslave` to refresh `STATE.md`, Running programs updated,
Lessons-Learned updated for anything killed, commit. (`AGENTS.md` is the Codex copy of this file: keep it in sync.)

## Useful commands
- `/brief` `/idea` `/killtest` `/prereg` `/test-once` `/forward` `/wrap` `/weekly` `/setup-research`
  (see `.claude/commands/`)
- `uv run python tools/lab.py scoreboard | brief [--write VAULT] | reg add/set/list` ·
  `uv run python tools/gate_check.py <trades>` · `uv run python -m tradeslave.vault_map` · `uv run pytest -q`
- `/goal` and `/loop`: see "Autonomous runs" above.
- Subagents: `skeptic` (attack any claimed edge), `researcher` (sourced findings → vault), `experiment-runner`
  (executes one pre-registered test exactly), `scribe` (vault bookkeeping).