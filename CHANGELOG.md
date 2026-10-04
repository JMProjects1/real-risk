# Changelog

## 1.1.2

- A bad account ID no longer blocks `--find-accounts`, and the error points to both `--account` and `config.json`.
- The risk gauge greys out along with the figures while data is delayed.

## 1.1.1

Fixes from a re-check of 1.1.0.

- Alert monitor: if REAL stops returning a position's liquidation price, it no longer sends a false "back to safe". It keeps the current alert level and warns once that the liquidation price is unavailable. "Back to safe" is now "back outside alert levels".
- `start-dashboard.bat` says it's checking for Python, since the first run can download Python and take a minute.
- Account IDs from `config.json` or `--account` get the same checks as the prompt, and a missing ID gives a clear message.
- The dashboard greys out the risk figures while data is delayed.
- A failed liquidation-map scan retries once a minute, not every 5 seconds.
- Trade history checks each account separately for missed fills.

## 1.1.0

Fixes from an independent pre-release review.

**Correctness**
- What-if calculator: on cross-margin accounts with other positions it could disagree with REAL, show "None" for the liquidation price, or misjudge size changes. It now calibrates a fixed amount for everything else on the account, so it always matches REAL with nothing changed and models size and margin changes correctly. The maths moved to `riskmath.js`, which the page and the tests share.
- Market order cost uses order sizes in USDT, so it works on low-priced markets. 24h range and spread use each market's own precision.
- Liquidation map leaves out positions already past their liquidation price and counts them separately, and says when a scan is partial.
- A position without a liquidation price is shown as "Liquidation price unavailable" instead of safe. The safest zone is now labelled "Outside alert levels".

**First run and errors**
- The Account ID is checked with REAL. A wallet address is recognised and its trading account offered; private keys and recovery phrases are refused and never sent anywhere.
- A wrong ID gets a clear message for that account instead of looking like an outage, and other accounts keep working.
- Nothing claims "No open positions" before data has loaded.
- "Data delayed" shows when updates stop for any reason.
- Clear message for the macOS certificate problem, and for typos in `config.json`.
- `start-dashboard.bat` finds Python reliably, including the new Python install manager, and explains what to do if it can't.
- On Windows, a second copy can no longer silently share the same port.

**Lighter on REAL's API**
- Order book only for the market you're looking at, the market scan only while the Market tab is open, trade history fetched incrementally, and one shared pause when REAL asks for fewer requests.

**Other**
- Fonts ship with the dashboard, so it only talks to REAL.
- Market and account columns appear when you have more than one. Funding countdowns show hours.
- Performance shows REAL's lifetime realised PnL and says how much history the breakdown covers.
- README: new Windows and Mac install steps, changing accounts, cross-margin caveat, troubleshooting.
- Many new tests, including the calculator maths in Node.

## 1.0.0

First release.
