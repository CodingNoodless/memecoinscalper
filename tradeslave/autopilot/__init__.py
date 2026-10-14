"""Autopilot: a scalper that keeps recording, relearning and re-testing itself.

record (scalp.py) -> backfill exact outcomes (Helius) -> build samples
-> train candidates on older data -> test on newer, unseen data
-> promote only what wins out of sample -> paper-trade the champion -> repeat.

It never touches real money. Promotion to real trading is a separate,
manual step behind the gates in the vault (Project/Profit plan v2).
"""
