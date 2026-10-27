---
description: Cheapest falsification of an idea on existing data, time-boxed to 2 hours
argument-hint: <registry id>
---
Kill-test for $ARGUMENTS (time box: 2 hours of work; stop and report at the limit).
1. Write down, before computing anything: the one number that would kill the idea (e.g. "mean net < 0 on the
   2,000 most recent exact launches"), the data you'll use, and the costs (1.25%/side + network + latency).
2. Use existing recorded data only (data/, backfills, candles). No new collectors, no new bots.
3. Report n, mean, median, t, halves. Mark the registry `killtest` → `killed` or `prereg`.
4. Log a short Experiments note (even for a kill) and a row in `Experiments/Experiments.md`.
