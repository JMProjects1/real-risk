#!/usr/bin/env python3
"""
REAL Risk Dashboard (unofficial community tool, not affiliated with REAL)
===================

A live dashboard for your REAL positions, in your browser.

    python dashboard.py                      (asks for your account ID the first time)
    python dashboard.py --account 0x...      (or pass it directly)

Then open http://127.0.0.1:8787 (it opens automatically).

Read-only: it uses REAL's public data API and never needs your private key. It only
listens on your own computer (127.0.0.1); nothing is exposed to the internet.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Deque, Dict, List, Optional, Tuple

import real_risk_monitor as rrm

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = 8787
PRICE_HISTORY_MAX = 6000      # points per market
SLOW_REFRESH_SECONDS = 60      # trade history & funding payments
MARKET_INFO_SECONDS = 600      # market parameters (margin rates, fees)
HISTORY_MAX_PAGES = 20         # up to 2,000 fills / funding payments

log = logging.getLogger("real-risk-dashboard")


def f(value: Any) -> Optional[float]:
    d = rrm.dec(value)
    return None if d is None else float(d)


SCAN_SECONDS = 60
SCAN_MAX_PAGES = 40


def aggregate_positions(raw: List[dict]) -> Dict[str, dict]:
    """
    Turn the public list of everyone's positions into per-market liquidation data.
    Account IDs are dropped on purpose: the map only ever shows totals by price.
    Each entry is [side (+1 long / -1 short), liquidation price, size].
    """
    markets: Dict[str, dict] = {}
    for p in raw:
        if p.get("status") != "Open" or p.get("direction") not in ("Long", "Short"):
            continue
        size = f(p.get("size")) or 0.0
        if size <= 0:
            continue
        lv = p.get("live_values") or {}
        mid = p.get("market_id", "")
        m = markets.setdefault(mid, {"positions": [], "open": 0, "no_liq": 0, "stale": 0,
                                     "long_size": 0.0, "short_size": 0.0, "long_count": 0, "short_count": 0})
        m["open"] += 1
        side = 1 if p["direction"] == "Long" else -1
        if side > 0:
            m["long_size"] += size
            m["long_count"] += 1
        else:
            m["short_size"] += size
            m["short_count"] += 1
        if lv.get("has_stale_price"):
            m["stale"] += 1
            continue
        liq = f(lv.get("estimated_liquidation_price"))
        if liq is None or liq <= 0:
            m["no_liq"] += 1
            continue
        m["positions"].append([side, liq, size])
    return markets


class MarketScanner:
    """Scans every open position on REAL once a minute for the liquidation map."""

    def __init__(self, api: rrm.RealIndexer, monitor: rrm.RiskMonitor):
        self.api = api
        self.monitor = monitor
        self.lock = threading.Lock()
        self.data: Dict[str, Any] = {"scanned_ms": None, "markets": {}, "recent": [], "error": None, "truncated": False}

    def scan(self) -> None:
        try:
            raw = self.api._paginate("/api/v1/positions/live", [("p[s]", "100")], max_pages=SCAN_MAX_PAGES)
            liqs = self.api._get("/api/v1/liquidations", [("p[o]", "desc"), ("p[s]", "15")]).get("data") or []
        except rrm.ApiError as e:
            with self.lock:
                self.data["error"] = str(e)
            return
        markets = aggregate_positions(raw)
        for mid, m in markets.items():
            m["symbol"] = self.monitor.symbols.get(mid) or self.monitor.symbol(mid)
        recent = []
        for liq in liqs:
            for pos in liq.get("positions") or []:
                recent.append({
                    "t": liq.get("created_at_ms"),
                    "symbol": self.monitor.symbols.get(pos.get("market_id", ""), rrm.short_id(pos.get("market_id", ""))),
                    "size": f(pos.get("liquidated_position_size")),
                    "status": pos.get("status"),
                })
        with self.lock:
            self.data = {"scanned_ms": int(time.time() * 1000), "markets": markets, "recent": recent[:15],
                         "error": None, "truncated": len(raw) >= SCAN_MAX_PAGES * 100}

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.data)

    def run_forever(self) -> None:
        time.sleep(3)  # let the first risk poll go first
        while True:
            try:
                self.scan()
            except Exception as e:
                log.exception("Market scan failed: %s", e)
            time.sleep(SCAN_SECONDS)


class Dashboard:
    def __init__(self, cfg: Dict[str, Any], monitor: rrm.RiskMonitor, scanner: Optional[MarketScanner] = None):
        self.scanner = scanner
        self.cfg = cfg
        self.monitor = monitor
        self.api = monitor.api
        self.lock = threading.Lock()
        self.tickers: Dict[str, dict] = {}
        self.price_history: Dict[str, Deque[Tuple[int, float]]] = {}
        self.backfilled: set = set()
        self.accounts: Dict[str, dict] = {}
        self.market_info: Dict[str, dict] = {}
        self.last_market_info = 0.0
        self.depth: Dict[str, dict] = {}
        self.orders: List[dict] = []
        self.fills: List[dict] = []
        self.funding: List[dict] = []
        self.last_slow_refresh = 0.0
        self.last_success_ms: Optional[int] = None
        self.last_error: Optional[str] = None
        self.started_ms = int(time.time() * 1000)

    # -- data collection --------------------------------------------------------

    def backfill_prices(self, market_id: str) -> None:
        """Seed the price chart with recent trades so it isn't empty at startup."""
        if market_id in self.backfilled:
            return
        self.backfilled.add(market_id)
        points: List[Tuple[int, float]] = []
        cursor = None
        try:
            for _ in range(3):
                params = [("f[market_ids]", market_id), ("p[o]", "desc"), ("p[s]", "200")]
                if cursor:
                    params.append(("p[c]", cursor))
                resp = self.api._get("/api/v1/trades", params)
                for t in resp.get("data") or []:
                    price = f(t.get("price"))
                    if price and t.get("created_at_ms"):
                        points.append((int(t["created_at_ms"]), price))
                cursor = (resp.get("pagination") or {}).get("next_cursor")
                if not cursor:
                    break
        except rrm.ApiError as e:
            log.warning("Could not load price history: %s", e)
        points.sort()
        hist = self.price_history.setdefault(market_id, deque(maxlen=PRICE_HISTORY_MAX))
        existing = list(hist)
        hist.clear()
        for p in points + existing:
            hist.append(p)

    def _fetch_all(self, path: str, params: List[Tuple[str, str]], max_pages: int) -> List[dict]:
        return self.api._paginate(path, params, max_pages=max_pages)

    def refresh(self) -> None:
        """Runs after every monitor poll (in the monitor's thread)."""
        now = int(time.time() * 1000)
        try:
            tickers = self.api.tickers()
            accounts = {aid: self.api.account(aid) for aid in self.monitor.accounts}
        except rrm.ApiError as e:
            with self.lock:
                self.last_error = str(e)
            return

        # Market parameters (maintenance margin, fees) rarely change.
        market_info = None
        if time.time() - self.last_market_info >= MARKET_INFO_SECONDS:
            try:
                market_info = {m["id"]: m for m in self._fetch_all("/api/v1/markets", [], 5) if m.get("id")}
                self.last_market_info = time.time()
            except rrm.ApiError as e:
                log.warning("Could not load market parameters: %s", e)

        depth: Dict[str, dict] = {}
        for t in tickers:
            mid = t.get("market_id")
            if not mid:
                continue
            try:
                depth[mid] = self.api._get(f"/api/v1/markets/{mid}/depth").get("data") or {}
            except rrm.ApiError as e:
                log.debug("Could not load depth for %s: %s", mid, e)

        orders: Optional[List[dict]] = []
        try:
            for aid in self.monitor.accounts:
                orders.extend(self._fetch_all(f"/api/v1/accounts/{aid}/orders",
                                              [("f[statuses]", "Active"), ("p[s]", "100")], 5))
        except rrm.ApiError as e:
            log.debug("Could not load open orders: %s", e)
            orders = None

        fills, funding = None, None
        if time.time() - self.last_slow_refresh >= SLOW_REFRESH_SECONDS:
            try:
                fills = []
                for aid in self.monitor.accounts:
                    fills.extend(self._fetch_all(f"/api/v1/accounts/{aid}/fills",
                                                 [("p[o]", "desc"), ("p[s]", "100")], HISTORY_MAX_PAGES))
                funding = self._fetch_all("/api/v1/funding-payments",
                                          [("f[account_ids]", ",".join(self.monitor.accounts)), ("p[o]", "desc"),
                                           ("p[s]", "100")], HISTORY_MAX_PAGES)
                self.last_slow_refresh = time.time()
            except rrm.ApiError as e:
                log.warning("Could not load trade history: %s", e)
                fills, funding = None, None

        for t in tickers:
            if t.get("market_id") not in self.backfilled:
                self.backfill_prices(t["market_id"])

        with self.lock:
            self.last_success_ms = now
            self.last_error = None
            for t in tickers:
                mid = t.get("market_id")
                if not mid:
                    continue
                self.tickers[mid] = t
                mark = f(t.get("mark_price"))
                if mark:
                    self.price_history.setdefault(mid, deque(maxlen=PRICE_HISTORY_MAX)).append((now, mark))
            if market_info is not None:
                self.market_info = market_info
            self.depth.update(depth)
            if orders is not None:
                self.orders = orders
            for aid, a in accounts.items():
                self.accounts[aid] = a
            if fills is not None:
                fills.sort(key=lambda x: x.get("created_at_ms") or 0, reverse=True)
                self.fills = fills
            if funding is not None:
                funding.sort(key=lambda x: x.get("updated_at_ms") or 0, reverse=True)
                self.funding = funding

    # -- state for the page -------------------------------------------------------

    def _market(self, mid: str) -> dict:
        t = self.tickers.get(mid, {})
        info = self.market_info.get(mid, {})
        risk = info.get("risk_engine") or {}
        rules = info.get("order_rules") or {}
        fees = info.get("fees") or {}
        book = self.depth.get(mid) or {}
        bids = [[f(x.get("price_level")), f(x.get("volume"))] for x in book.get("buys") or []]
        asks = [[f(x.get("price_level")), f(x.get("volume"))] for x in book.get("sells") or []]
        return {
            "symbol": t.get("symbol") or info.get("symbol"),
            "mark": f(t.get("mark_price")),
            "index": f(t.get("index_price")),
            "last": f(t.get("last_traded_price")),
            "change_24h": f(t.get("price_change_24h")),
            "high_24h": f(t.get("high_price_24h")),
            "low_24h": f(t.get("low_price_24h")),
            "volume_24h": f(t.get("volume_24h")),
            "turnover_24h": f(t.get("turnover_24h")),
            "open_interest": f(t.get("open_interest")),
            "open_interest_value": f(t.get("open_interest_value")),
            "funding_rate": f(t.get("perp_next_funding_rate")),
            "funding_time_ms": t.get("perp_next_funding_time_ms"),
            "max_leverage": (1 / f(t.get("imr"))) if f(t.get("imr")) else None,
            "mmr": f(risk.get("mmr")),
            "taker_fee": f((fees.get("taker_rates") or [None])[0]),
            "maker_fee": f((fees.get("maker_rates") or [None])[0]),
            "tick_size": f(rules.get("tick_size")),
            "min_order_value": f(rules.get("min_order_value")),
            "mode": t.get("effective_mode"),
            "bids": [b for b in bids if b[0] and b[1]],
            "asks": [a for a in asks if a[0] and a[1]],
            "book_ms": book.get("updated_at_ms"),
            "history": list(self.price_history.get(mid, [])),
        }

    def state(self) -> dict:
        m = self.monitor
        with self.lock:
            positions = []
            for v in sorted(m.last_views, key=lambda x: x.distance if x.distance is not None else 1e9):
                st = m.states.get(v.key)
                account = self.accounts.get(v.account_id.lower()) or self.accounts.get(v.account_id) or {}
                positions.append({
                    "key": f"{v.account_id}:{v.market_id}",
                    "account_id": v.account_id.lower(),
                    "account": m.label(v.account_id),
                    "market_id": v.market_id,
                    "symbol": v.symbol,
                    "direction": v.direction,
                    "size": float(v.size),
                    "entry": f(v.entry),
                    "mark": f(v.mark),
                    "liq": f(v.liq) if v.liq is not None and v.liq > 0 else None,
                    "distance": None if v.distance is None or v.distance == float("inf") else v.distance,
                    "no_liq": v.distance == float("inf"),
                    "tier": "stale" if v.stale_price else (st.tier if st else "ok"),
                    "upnl": f(v.upnl),
                    "notional": f(v.notional),
                    "margin": f(v.margin),
                    "margin_mode": v.margin_mode,
                    "max_leverage": v.leverage,
                    "account_equity": f(account.get("equity")),
                })

            accounts = []
            for aid, label in m.accounts.items():
                a = self.accounts.get(aid, {})
                accounts.append({
                    "id": aid,
                    "label": label,
                    "equity": f(a.get("equity")),
                    "available": f(a.get("available_balance")),
                    "upnl": f(a.get("unrealised_pnl")),
                    "realised_pnl": f(a.get("realised_pnl")),
                    "margin_contracts": (f(a.get("margin_allocated_cross_contracts")) or 0)
                                        + (f(a.get("margin_allocated_isolated_contracts")) or 0),
                    "margin_orders": f(a.get("margin_allocated_orders")),
                })

            orders = [{
                "t": o.get("created_at_ms"),
                "symbol": m.symbols.get(o.get("market_id", ""), rrm.short_id(o.get("market_id", ""))),
                "side": o.get("side"),
                "type": o.get("order_type"),
                "price": f(o.get("price")),
                "quantity": f(o.get("quantity")),
                "filled": f(o.get("filled_quantity")),
                "reduce_only": bool(o.get("reduce_only")),
                "trigger": f((o.get("conditional") or {}).get("trigger_price")),
            } for o in sorted(self.orders, key=lambda o: o.get("created_at_ms") or 0, reverse=True)]

            recent_fills = [self._fill(x) for x in self.fills[:8]]

            return {
                "now_ms": int(time.time() * 1000),
                "network": self.cfg["network"],
                "poll_seconds": self.cfg["poll_seconds"],
                "thresholds": m.thresholds,
                "status": {
                    "ok": self.last_error is None and m.api_failing_since is None and self.last_success_ms is not None,
                    "last_success_ms": self.last_success_ms,
                    "error": self.last_error or (None if m.api_failing_since is None else "Can't reach the REAL API"),
                },
                "accounts": accounts,
                "positions": positions,
                "markets": {mid: self._market(mid) for mid in self.tickers},
                "orders": orders,
                "fills": recent_fills,
            }

    def _fill(self, x: dict) -> dict:
        attr = x.get("realised_pnl_attribution") or {}
        return {
            "t": x.get("created_at_ms"),
            "symbol": self.monitor.symbols.get(x.get("market_id", ""), rrm.short_id(x.get("market_id", ""))),
            "direction": x.get("direction"),
            "context": x.get("trade_context"),
            "order_type": x.get("order_type"),
            "taker": x.get("is_taker"),
            "fill_type": x.get("fill_type"),
            "price": f(x.get("price")),
            "volume": f(x.get("volume")),
            "value": f(x.get("filled_value")),
            "pnl": f(x.get("realised_pnl")),
            "trade_pnl": f(attr.get("trade_pnl")),
            "fee": f(attr.get("trade_fee")),
        }

    def performance(self) -> dict:
        with self.lock:
            fills = [self._fill(x) for x in self.fills]
            funding = [{"t": x.get("updated_at_ms"), "payment": f(x.get("payment")), "rate": f(x.get("rate")),
                        "side": x.get("position_side"), "size": f(x.get("position_size")),
                        "symbol": self.monitor.symbols.get(x.get("market_id", ""), "")} for x in self.funding]
            loaded = self.last_slow_refresh > 0
        return {"loaded": loaded, "fills": fills, "funding": funding,
                "truncated": len(fills) >= HISTORY_MAX_PAGES * 100, "now_ms": int(time.time() * 1000)}


    # -- liquidation map ----------------------------------------------------------

    def liqmap(self) -> dict:
        data = self.scanner.snapshot() if self.scanner else {"scanned_ms": None, "markets": {}, "recent": [], "error": None}
        with self.lock:
            marks = {mid: f(t.get("mark_price")) for mid, t in self.tickers.items()}
        mine = [{"market_id": v.market_id, "direction": v.direction, "liq": float(v.liq)}
                for v in self.monitor.last_views if v.liq is not None and v.liq > 0]
        return {**data, "marks": marks, "mine": mine, "now_ms": int(time.time() * 1000)}

def make_handler(dashboard: Dashboard):
    html_path = os.path.join(HERE, "dashboard.html")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep the console clean
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj, separators=(",", ":")).encode("utf-8"), "application/json")

        def _local_host(self) -> bool:
            # Blocks DNS-rebinding: only answer requests addressed to this computer.
            host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
            return host in ("127.0.0.1", "localhost", "::1")

        def do_GET(self):
            if not self._local_host():
                return self._send(403, b"Forbidden", "text/plain")
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                try:
                    with open(html_path, "rb") as fh:
                        self._send(200, fh.read(), "text/html; charset=utf-8")
                except FileNotFoundError:
                    self._send(500, b"dashboard.html is missing; keep it next to dashboard.py", "text/plain")
            elif path == "/api/state":
                self._json(200, dashboard.state())
            elif path == "/api/liqmap":
                self._json(200, dashboard.liqmap())
            elif path == "/api/performance":
                self._json(200, dashboard.performance())
            else:
                self._send(404, b"Not found", "text/plain")

    return Handler


def run_monitor(monitor: rrm.RiskMonitor, dashboard: Dashboard, poll_seconds: float) -> None:
    while True:
        try:
            monitor.poll()
            if monitor.first_poll_done:
                dashboard.refresh()
        except Exception as e:
            log.exception("Poll failed: %s", e)
        time.sleep(poll_seconds)


def ask_for_account(config_path: str) -> Optional[str]:
    print("\nPaste your REAL Account ID (Settings > Account > Account ID) and press Enter:")
    try:
        value = input("> ").strip()
    except EOFError:
        return None
    if not (value.startswith("0x") and len(value) == 66):
        print("That doesn't look like an account ID (it should start with 0x and be 66 characters).")
        return None
    data: Dict[str, Any] = {}
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    data["accounts"] = [{"id": value, "label": "Main"}]
    with open(config_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    print(f"Saved to {os.path.basename(config_path)}; you won't be asked again.\n")
    return value


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Live risk dashboard for your REAL positions.")
    parser.add_argument("--config", help="Config file (default: config.json next to this script)")
    parser.add_argument("--account", action="append", help="Trading account ID (repeatable)")
    parser.add_argument("--network", choices=list(rrm.NETWORKS))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="Don't open the browser automatically")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config_path = args.config or os.path.join(HERE, "config.json")
    cfg = rrm.load_config(config_path if os.path.exists(config_path) else None)
    if args.account:
        cfg["accounts"] = args.account
    if args.network:
        cfg["network"] = args.network
    if not cfg["accounts"] or any("YOUR_" in (a if isinstance(a, str) else a.get("id", "")) for a in cfg["accounts"]):
        account = ask_for_account(config_path)
        if not account:
            return 2
        cfg["accounts"] = [{"id": account, "label": "Main"}]
    cfg["status_every_minutes"] = 0
    try:
        rrm.validate_config(cfg)
    except ValueError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    api = rrm.RealIndexer(cfg["network"])
    monitor = rrm.RiskMonitor(cfg, api, [])  # the dashboard shows risk; it doesn't send alerts
    scanner = MarketScanner(api, monitor)
    dashboard = Dashboard(cfg, monitor, scanner=scanner)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(dashboard))
    except OSError:
        print(f"Port {args.port} is busy. Is the dashboard already running? Try --port {args.port + 1}", file=sys.stderr)
        return 1

    threading.Thread(target=run_monitor, args=(monitor, dashboard, cfg["poll_seconds"]), daemon=True).start()
    threading.Thread(target=scanner.run_forever, daemon=True).start()

    url = f"http://127.0.0.1:{args.port}"
    print(f"\nREAL Risk Dashboard running at {url}")
    print("Keep this window open. Close it (or press Ctrl+C) to stop.\n")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
