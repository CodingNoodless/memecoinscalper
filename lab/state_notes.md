<!-- Hand-written "current work" section, copied into vault STATE.md by `tools/lab.py brief`.
Keep it short: what step is in progress, what's next, blockers. Overwrite, don't append. -->
- Round 2: Phase 0 DONE (7b4c5d2). Phase 1 NOT started (waiting for Rohit's OK on the plan).
- Measured 2026-10-30: public-RPC curve backfill ~57 s/token (7 tokens, 0.2-0.35 calls/s, dead coins 1-33 txs); GeckoTerminal pool OHLCV 1 call/graduate (642 candles in 0.55 s).
- Create log data/c1/creates.jsonl ~21-25k/day from Oct 21 (46% zero dev buy; outages Oct 25-26); Oct 17-20 only launch_watch (biased).
- Plan: forward all-trades recorder first, GeckoTerminal for graduates, sampled curve backfill Oct 21-29; ETA 14 days ~Nov 13 (free RPC only).
