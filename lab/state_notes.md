<!-- Hand-written "current work" section, copied into vault STATE.md by `tools/lab.py brief`.
Keep it short: what step is in progress, what's next, blockers. Overwrite, don't append. -->
- Round 2 batch 1 RANKED and committed before results: M13v2 > PLS1 > CLN1 > BRD1 > PMN1 (Strategies/Hypotheses/Ranking-v2.md). None can reach 300 OOS yet: train-only killtests first.
- Running: recorder, chain_backfill (~21k/h, 142k left), gecko_grads (~2 graduates/min, slow: free-API 429s).
- Next: PLS1 + PMN1 killtests (existing data), then M13v2 when >= ~300 train graduates have pool candles.
