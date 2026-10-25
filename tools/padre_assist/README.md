# Tradeslave Assist for padre.gg

A read-only browser panel for the token you have open on padre.gg. It shows:
- **Exit helper:** after you record your entry, it shows a live HOLD / SELL / TAKE PROFIT, with M12r's rules.
  - Below 2x, the stop is −10%.
  - From 2x the stop ratchets up: 1.25x at 2x, then N−1 at each new whole multiple N.
  - It takes profit when the run stalls (no new high for 2 min and net selling) or dies (activity below a quarter of its peak).
- **Bot checks:** buyer acceleration (the losers' pattern), M9's flow rule, the serial-funder and funding-cluster skips (M6–M8), and M12's hype gate.
- **Risk flags:** creator holdings, sells and history, top-10 / top-1 concentration, how much the launch-burst buyers still hold, creator-linked holders, and fresh wallets.
- **Tracked wallets & hype:** decu, the 17 WL study wallets, the top pump.fun callers, livestream viewers and DexScreener boosts.

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
