---
description: End-of-session logging so the next session starts with full context
---
Use the `scribe` agent to:
1. Write today's `Sessions/` note: what was done, what was learned (measured), decisions, next 3 actions.
2. Update registry statuses, Experiments rows, Decisions rows, Known issues, `Data & Infra/Running programs.md`.
3. `uv run python tools/lab.py brief --write D:/vault/tradeslave`.
Then run `uv run pytest -q` and commit code + any registry changes with a result-stating message.
Remind Rohit which background programs stop when this Claude Code session closes.
