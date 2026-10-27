---
description: Run a pre-registered test exactly once with the experiment-runner agent, then the skeptic
argument-hint: <registry id>
---
1. Confirm the pre-registration for $ARGUMENTS exists and was committed before any holdout results.
2. Hand it to the `experiment-runner` agent. It runs the spec exactly and writes the Result section.
3. If the gate passed, hand the result to the `skeptic` agent before calling it a pass.
4. Update the registry (`backtest` → `forward` or `killed`) and, on pass, propose the forward bot (WIP limit!).
