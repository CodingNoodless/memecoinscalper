---
description: Capture an idea, check the vault for prior tests, score it, register it
argument-hint: <short idea description>
---
Idea: $ARGUMENTS
1. Grep the vault (`Experiments/`, `Strategies/`, `Idea bank*`) for the same or a similar idea. Quote any earlier
   verdict. If it was already rejected, say so and only continue if the idea differs in a way that matters.
2. Score it: evidence (0–3), cost to kill-test (hours), time to a forward verdict (days), upside if real
   ($/day at < $500 capital, 1.25%/side fees). Expected value per hour = upside × P(real) ÷ hours.
3. Register: `uv run python tools/lab.py reg add <ID> <FAMILY> "<hypothesis>"`, and append a line to the vault
   `Strategies/Idea bank` note. Do not build anything yet.
