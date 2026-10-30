<!-- Hand-written "current work" section, copied into vault STATE.md by `tools/lab.py brief`.
Keep it short: what step is in progress, what's next, blockers. Overwrite, don't append. -->
- Overnight run (round 2). Recorder running since 09:46 (data/recorder, ~70 MB/day). chain_backfill running since 09:51: Helius gTFA, 5 keys, ~34k tokens/h, 153k creates Oct 21+, ETA ~14:30 PDT.
- Endpoints: public 1/1, helius 5/7 (2 exhausted to 11-01), rpcfast 1/1, shyft 1/1.
- Next: candle builder (raw trades -> 1-min OHLCV + reserves) + match check vs v1 on data/full_trades; GeckoTerminal graduates.
