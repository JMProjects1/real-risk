# REAL Risk

A free, open-source risk dashboard for traders on the [REAL](https://real.xyz) perpetuals DEX on IOTA. It shows how close your positions are to liquidation, lets you test "what if" scenarios before you act, tracks your trading performance, and maps where other traders' liquidations sit across the market.

> **Unofficial community tool.** Not made by, affiliated with or endorsed by the REAL team. Use at your own risk and double-check anything important on REAL itself.

![Positions tab](docs/positions.png)

## Is it safe?

- **Read-only.** It only reads REAL's public data API. It can't place orders, move funds or touch your wallet.
- **No private key.** It only needs your public Account ID. Never paste your seed phrase or private key into this or any other tool.
- **Runs on your computer.** The dashboard is served from your own machine at `127.0.0.1` and can't be opened by anyone else. It only talks to REAL's public API; fonts and everything else ship with it.
- **Small and readable.** A few Python and JavaScript files using only Python's standard library. Read them before you run them.
- **Only download it from this page.** If someone sends you a copy elsewhere, or a version that asks for your private key or recovery phrase, it isn't this tool.

## What's in it

**Positions**
- Equity, available balance, unrealised PnL, margin used and leverage.
- Distance to liquidation, with a risk gauge, liquidation price, the price move needed to get there, entry, margin and mode.
- Live price chart with your entry and liquidation price.
- **What-if calculator.** Change the price, your position size or your margin and instantly see the new liquidation price, distance, equity and leverage. It also shows how much margin you'd need to push your liquidation price 10%, 20%, 30% or 50% away.
- Open orders and latest fills.

**Performance**
- Net realised PnL, trading PnL, fees, funding, volume and win rate.
- Cumulative realised PnL chart, full trade history and funding payments.

![Performance tab](docs/performance.png)

**Market**
- Mark price, 24h change, range and volume, open interest, funding rate and countdown to the next funding.
- **Liquidation map.** While the tab is open, it scans all open positions on REAL every minute and shows REAL's estimated liquidation prices grouped by price level, plus a running total as price moves away. Your own liquidation price is marked on it. Only totals are shown; no account is identified.
- Order book depth, and what a market order of a given size (in USDT) would cost you in slippage plus fees.
- Largest liquidation levels and recent liquidations.

![Market tab](docs/market.png)

*Screenshots use example data.*

## Get started

### Windows

1. **Install Python** (once). Go to [python.org/downloads](https://www.python.org/downloads/) and install it with the **Python install manager**, accepting the defaults. If it asks whether to add Python to your PATH, say yes.
2. **Download the dashboard.** On this page click **Code**, then **Download ZIP**.
3. **Extract it.** In your Downloads folder, right-click the ZIP and choose **Extract All**. Don't run anything from inside the ZIP itself.
4. **Start it.** Open the extracted folder and double-click `start-dashboard.bat`. If Windows shows "Windows protected your PC", click **More info**, then **Run anyway**. (It appears for any downloaded script that isn't from a known publisher.)
5. **Paste your Account ID** when asked. Find it on REAL under **Settings → Account → Account ID**. It is not your wallet address, and never your private key or recovery phrase. It's checked with REAL and saved in `config.json`, so you're only asked once.
6. Your browser opens the dashboard at http://127.0.0.1:8787. Keep the black window open while you use it; closing it stops the dashboard.

### Mac

1. **Install Python** (once) from [python.org/downloads](https://www.python.org/downloads/macos/). After installing, open **Finder → Applications → Python 3.x** and double-click **Install Certificates.command**. Without this step, Python can't make secure connections to REAL.
2. **Download the dashboard.** On this page click **Code**, then **Download ZIP**. Safari usually unzips it for you; otherwise double-click the ZIP.
3. **Open Terminal.** Press **Cmd+Space**, type `Terminal` and press Enter.
4. **Go to the folder.** Type `cd` followed by a space, drag the unzipped folder from Finder into the Terminal window, and press Enter.
5. **Start it.** Type `python3 dashboard.py` and press Enter.
6. Paste your Account ID when asked (see step 5 for Windows), and your browser opens the dashboard. Keep the Terminal window open while you use it.

### Linux

Python 3.9+ is usually installed already. Download and unzip, then run `python3 dashboard.py` in the folder.

### Changing account

Close the dashboard, delete `config.json` from its folder and start it again; it will ask for an Account ID. Or start it with `py dashboard.py --change-account` (Windows) or `python3 dashboard.py --change-account` (Mac/Linux). To watch several accounts at once, list them in `config.json` (see [Configuration](#configuration)).

## How the numbers work

- **Distance to liquidation** is how far the mark price must move against you to reach REAL's estimated liquidation price: `(mark − liq) / mark` for a long, `(liq − mark) / mark` for a short.
- **Cross margin.** REAL's estimate, and therefore this dashboard, assumes your other positions and balance stay where they are. Crypto prices tend to move together, so if your other positions lose money at the same time, liquidation can come sooner than shown.
- **What-if** uses the standard condition for liquidation: collateral plus PnL falls to the maintenance margin (REAL's published rate) plus whatever else that collateral has to cover, such as your other positions' margin. That last amount is calibrated from REAL's own liquidation price, so with nothing changed the calculator matches REAL exactly, and when you change size or margin, your other positions' requirement stays fixed. Size changes are assumed to fill at the mark price; fees and funding aren't included. For isolated margin, the position's margin is assumed to stay the same when its size changes.
- **Liquidation map** values each position at its size times its estimated liquidation price. Estimates move as traders add margin, trade or get funded, liquidations can be partial, and the scan can be up to a minute older than the current price, so treat the map as a snapshot rather than a forecast. Positions already past their liquidation price are counted separately.
- **Performance** shows REAL's own lifetime realised PnL as the headline figure. The breakdown (trading PnL, fees, funding, win rate) is built from your recent history: up to 2,000 fills and 2,000 funding payments, and the page says how far back that goes. "Profitable closes" counts closing fills, so a position closed in several pieces counts several times.
- **Market order cost** walks the current order book and adds the taker fee. The book can change before your order arrives.

## Alerts (optional)

`real_risk_monitor.py` is a separate background monitor that warns you as a position approaches liquidation, in the terminal and optionally in Discord. The dashboard doesn't send alerts itself, so run this alongside it if you want them.

| Event | Default | Discord ping |
|---|---|---|
| Warning: within 15% of liquidation | once | no |
| Danger: within 8% | repeats every 30 min | yes |
| Critical: within 4% | repeats every 5 min | yes |
| Back to a safer level | once | no |
| Liquidation recorded on your account | once | yes |
| Position opened, closed or flipped | once | no |
| Can't reach the API for 3+ minutes | once | yes |

On Windows use `py` where these say `python3`:

```
python3 real_risk_monitor.py --account 0xYOUR_ACCOUNT_ID --once     # check it works
python3 real_risk_monitor.py --config config.json                    # run it
python3 real_risk_monitor.py --config config.json --test-alert       # test Discord
python3 real_risk_monitor.py --find-accounts 0xYOUR_WALLET_ADDRESS   # find Account IDs from a wallet address
```

**Discord setup:** in a server you own, go to **Server Settings → Integrations → Webhooks → New Webhook**, pick a channel and **Copy Webhook URL**. Put it in `config.json` as `discord_webhook_url` (or set the `REAL_DISCORD_WEBHOOK` environment variable). Treat the URL like a password. To be @mentioned on serious alerts, turn on Developer Mode in Discord, right-click your name, **Copy User ID**, and set `"mention": "<@YOUR_USER_ID>"`.

An alert only helps if the monitor is running when price moves. A laptop that goes to sleep won't do; a small always-on machine or a cheap VPS (run it in `tmux` or as a `systemd` service) will. It's an early warning, not a stop-loss, so use stop orders on REAL too.

## Configuration

Copy `config.example.json` to `config.json` and edit it. Every key is optional except `accounts`.

| Key | Default | Meaning |
|---|---|---|
| `network` | `mainnet` | `mainnet` or `testnet` |
| `accounts` | | List of `"0x..."` IDs or `{"id": "0x...", "label": "Name"}` |
| `poll_seconds` | `15` | How often to refresh (minimum 5; REAL's API is rate limited) |
| `thresholds_pct` | `15 / 8 / 4` | Warning, danger and critical distance to liquidation, in % |
| `discord_webhook_url` | | Discord webhook for alerts |
| `mention` | | Who to ping on serious alerts |
| `repeat_minutes` | `0 / 30 / 5` | Re-alert interval while a position stays in a level |
| `recovery_buffer_pct` | `1.0` | How far a position must recover past a level before it's downgraded |
| `notify_position_changes` | `true` | Alerts for opened, closed or flipped positions |
| `api_down_alert_minutes` | `3` | Alert if the API is unreachable this long |
| `stale_data_minutes` | `2` | Log a warning when REAL's computed values for a position are older than this |
| `status_every_minutes` | `10` | How often the alert monitor prints a status table in its window (0 = only at startup) |

## Troubleshooting

- **"Python isn't installed, or Windows can't find it"**: install Python with the Python install manager from python.org, then run `start-dashboard.bat` again.
- **"The dashboard files are missing"**: you ran it from inside the ZIP. Right-click the ZIP, choose **Extract All**, and run it from the extracted folder.
- **"can't open file" (Mac)**: Terminal isn't in the dashboard folder. Repeat the `cd` step, dragging in the folder that contains `dashboard.py`.
- **A message about certificates**: on a Mac, run **Install Certificates.command** from **Applications → Python 3.x**, then start again.
- **"This is a wallet address" or "REAL has no account with this ID"**: the saved ID is wrong. Delete `config.json`, start again and paste the Account ID from REAL's **Settings → Account**.
- **"Port 8787 is busy"**: the dashboard is already running in another window. Use that one, or close it first.
- **"Reconnecting" or "Data delayed" in the top right**: REAL's API is unreachable or asking for fewer requests. It retries by itself. If it happens a lot, raise `poll_seconds` in `config.json` (for example to `30`).
- **Liquidation map says "Scanning"**: the scan starts when you open the Market tab and takes up to a minute.

## For developers

```
python3 -m unittest -v
```

Tests run offline using response shapes captured from REAL's mainnet API. If Node.js is installed, they also run `test_riskmath.js`, which tests the calculator maths in `riskmath.js`, the same file the dashboard uses. Data comes from REAL's public indexer (`https://indexer.api.real.xyz`); the official Rust SDK at [realmarkets/rust-sdk](https://github.com/realmarkets/rust-sdk) documents the endpoints.

Issues and pull requests are welcome.

## License

MIT. See [LICENSE](LICENSE).
