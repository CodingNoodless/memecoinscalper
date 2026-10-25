# Tradeslave Assist for padre.gg

A read-only browser panel for the token you have open on padre.gg. It's a compact board of five verdicts, each with one short reason:

| Row | What it tells you |
| --- | --- |
| **Rug risk** | LOW / MEDIUM / HIGH in about 1 s: current top holders (on-chain, bonding curve excluded), launch-second bundle and snipers, and the creator's sells, history and dev buy |
| **Narrative** | 0–10 from M1's LLM scorer (name, description and linked X post) |
| **Runner** | LIKELY / MAYBE / UNLIKELY from M5's runner model (M5 buys at 37%+) |
| **Hype** | YES / SOME / NO: top pump.fun callers bought, livestream viewers, DexScreener boosts, and tracked wallets in it |
| **Dead coin?** | ALIVE / FADING / DEAD, from trades in the last minute vs its busiest minute, and price vs its peak |

**Optional exit helper:** click **I bought now** to add a **My position** row that shows HOLD / SELL / TAKE PROFIT using M12r's ratchet rules. **details** shows every reason.

It **never trades, never clicks or types on padre.gg, and never reads your wallet**. It only reads the page URL to find the token address. Data comes from the local dashboard (`/api/assist`).

## Install (Chrome, Edge or Brave)
1. Start the dashboard: `python dashboard.py` (serves http://127.0.0.1:8765).
2. Open `chrome://extensions` (Edge: `edge://extensions`) and turn on **Developer mode**.
3. Click **Load unpacked** and pick this folder: `tools/padre_assist`.
4. Open a token on padre.gg. The panel appears at the top right; drag it by its title and collapse it with the "–" button.

If the token isn't detected from the URL, paste its address into the panel. After a code change, click the reload arrow on the extension card.

## Notes
- **First look at a coin the feed hasn't recorded:** the history comes from Helius (last 30 min only), and the panel says so. From then on the live feed records the coin.
- **The funding checks** (serial funder, cluster) run Helius lookups once per coin, so they show "looking up" for a few seconds.
- **Not financial advice.** The bot results behind these checks are weak evidence so far (see the dashboard scoreboard).
