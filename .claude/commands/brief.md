---
description: Start-of-session brief - forward scoreboard, pipeline, programs, today's plan
---
1. Run `uv run python tools/lab.py brief --write D:/vault/tradeslave` and `bash tools/start_all.sh --list`.
2. Read vault `STATE.md`, `Project/Roadmap.md` and the newest `Sessions/` note (only those).
3. Act on the rules first: for every KILL, propose stopping the bot and recording the verdict; for every PASS, run
   the `skeptic` agent on it, then ask Rohit about a live micro-test.
4. Propose today's plan as at most 3 items, ordered by expected value per hour, respecting the WIP limit
   (≤ 3 forward bots beyond baselines). Each item names its registry id and its exit criterion.
Wait for Rohit's go before building anything large. $ARGUMENTS
