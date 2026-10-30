---
description: Long run that tests the ranked hypotheses one per turn against lab/gates.yaml. Use as the /goal text, or paste as a prompt if /goal isn't available.
---
Either (a) a hypothesis shows ALL PASS in the output of `uv run python tools/gate_check.py --final <id>` with
`lab/gates.yaml` unchanged since the commit that first added it (`git log --diff-filter=A --format=%h -- lab/gates.yaml`)
and vault `Reviews/Final-Report.md` is written; or (b) every hypothesis in vault `Strategies/Hypotheses/` has
registry status `killed` or `passed` with results logged, and `Reviews/Final-Report.md` is written with honest
findings and 3 new hypothesis directions (new families, not tweaks of killed ones); or stop after 60 turns with
`lab/state_notes.md`, STATE.md and `Reviews/Interim-Report.md` updated.

Each turn, following CLAUDE.md:
1. Take the highest-ranked untested hypothesis from `Strategies/Hypotheses/Ranking.md`. Pre-register it (≤ 20
   variants, written in its Experiments note before running) and set its registry status to `backtest`.
2. Run it on train, select on validation, with costs from `tradeslave/cost_model.py`. Print the
   `tools/gate_check.py` output in the conversation.
3. Only if every non-holdout gate passes: run `tools/gate_check.py --final <id>` once and print it. A holdout fail is
   final: status `killed`, no re-run, any fix is a new hypothesis.
4. Log the result in the Experiments note, set the registry status, add a line to `Strategies/Lessons-Learned.md`
   if killed, overwrite `lab/state_notes.md`, append to today's `Sessions/` note, run
   `uv run python tools/lab.py brief --write D:/vault/tradeslave`, and commit.
Never edit `lab/gates.yaml`, `tools/lab.py`, or the cost model during the run. Paper only: no live trades.
