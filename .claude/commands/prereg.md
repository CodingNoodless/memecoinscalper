---
description: Write the pre-registration for a backtest or forward test before any results are seen
argument-hint: <registry id>
---
Pre-register $ARGUMENTS in a new vault note `Experiments/<date> <name> (<ID>).md` (template `Templates/Experiment.md`):
- Hypothesis in one sentence; why it could be real (mechanism) and what earlier tests it builds on.
- Data: design / validation / holdout splits by time, with exact timestamps. Holdout is scored ONCE.
- Exact variant grid (≤ 20). Count of variants already tried in this family (for the luck hurdle).
- Costs and fill model. Pass gate (numbers). What happens on pass (forward bot spec) and on fail.
Then `uv run python tools/lab.py reg set <ID> prereg --note "<note name>"`. Commit the note before running.
