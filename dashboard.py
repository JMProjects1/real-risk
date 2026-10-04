#!/usr/bin/env python3
"""
REAL Risk Dashboard (unofficial community tool, not affiliated with REAL)
=========================================================================

A live dashboard for your REAL positions, in your browser.

    python dashboard.py                      (asks for your Account ID the first time)
    python dashboard.py --account 0x...      (or pass it directly)
    python dashboard.py --change-account     (enter a different Account ID)

Then open http://127.0.0.1:8787 (it opens automatically).

Read-only: it uses REAL's public data API and never needs your private key. It only
listens on your own computer (127.0.0.1); nothing is exposed to the internet.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
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
PRICE_HISTORY_MAX = 6000       # points per market
HISTORY_SECONDS = 60           # how often trade history and funding are checked for new rows
HISTORY_MAX_PAGES = 20         # first load: up to 2,000 fills / funding payments per account
MARKET_INFO_SECONDS = 600      # market parameters (margin rates, fees) rarely change
SCAN_SECONDS = 60              # liquidation map refresh, only while the Market tab is open
SCAN_MAX_PAGES = 40            # up to 4,000 positions
SCAN_IDLE_SECONDS = 90         # stop scanning this long after the Market tab was last viewed

HEX_ID = re.compile(r"^0x[0-9a-fA-F]{64}$")
STATIC_FILES = {
    "/riskmath.js": ("riskmath.js", "text/javascript; charset=utf-8"),
}
FONT_FILE = re.compile(r"^/fonts/([a-z0-9-]+\.woff2)$")

log = logging.getLogger("real-risk-dashboard")


def f(value: Any) -> Optional[float]:
    """API decimal string -> float, or None (never NaN/Infinity, which JSON can't carry)."""
    d = rrm.dec(value)
    return None if d is None else float(d)


# ---------------------------------------------------------------------------
# Liquidation map
# ---------------------------------------------------------------------------

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
    """Scans every open position on REAL for the liquidation map, only while someone is looking at it."""

    def __init__(self, api: rrm.RealIndexer, monitor: rrm.RiskMonitor):
        self.api = api
        self.monitor = monitor
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.wanted_until = 0.0
        self.data: Dict[str, Any] = {"scanned_ms": None, "markets": {}, "recent": [], "error": None, "truncated": False}

    def request(self) -> None:
        """Called whenever the page asks for the map; keeps scanning alive for a while."""
        self.wanted_until = time.time() + SCAN_IDLE_SECONDS
        if self.data["scanned_ms"] is None or time.time() * 1000 - self.data["scanned_ms"] > SCAN_SECONDS * 1000:
            self.wake.set()

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
                mid = pos.get("market_id", "")
                recent.append({"t": liq.get("created_at_ms"), "symbol": self.monitor.symbols.get(mid, rrm.short_id(mid)),
                               "size": f(pos.get("liquidated_position_size")), "status": pos.get("status")})
        with self.lock:
            self.data = {"scanned_ms": int(time.time() * 1000), "markets": markets, "recent": recent[:15],
                         "error": None, "truncated": len(raw) >= SCAN_MAX_PAGES * 100}

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.data)

    def run_forever(self) -> None:
        last = 0.0
        while True:
            self.wake.wait(timeout=5)
            self.wake.clear()
            now = time.time()
            if now >= self.wanted_until:                 # nobody is looking at the map
                continue
            if self.data["scanned_ms"] is not None and now - last < SCAN_SECONDS:
                continue
            last = now
            try:
                self.scan()
            except Exception as e:
                log.exception("Market scan failed: %s", e)


# ---------------------------------------------------------------------------
# Dashboard data
# ---------------------------------------------------------------------------

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
        self.account_errors: Dict[str, str] = {}
        self.market_info: Dict[str, dict] = {}
        self.last_market_info = 0.0
        self.active_market: Optional[str] = None
        self.depth: Dict[str, dict] = {}
        self.depth_fetched: Dict[str, int] = {}
        self.orders: List[dict] = []
        self.fills: Dict[Tuple[str, str], dict] = {}
        self.funding: Dict[str, dict] = {}
        self.history_loaded = False
        self.history_truncated = {"fills": False, "funding": False}
        self.last_history = 0.0
        self.last_success_ms: Optional[int] = None
        self.last_error: Optional[str] = None

    # -- data collection --------------------------------------------------------

    def backfill_prices(self, market_id: str) -> None:
        """Seed the price chart with recent trade prices so it isn't empty at startup."""
        if market_id in self.backfilled:
            return
        self.backfilled.add(market_id)
        points: List[Tuple[int, float]] = []
        try:
            for t in self.api._paginate("/api/v1/trades", [("f[market_ids]", market_id), ("p[o]", "desc"),
                                                            ("p[s]", "200")], max_pages=3):
                price = f(t.get("price"))
                if price and t.get("created_at_ms"):
                    points.append((int(t["created_at_ms"]), price))
        except rrm.ApiError as e:
            log.warning("Could not load price history: %s", e)
        points.sort()
        with self.lock:
            hist = self.price_history.setdefault(market_id, deque(maxlen=PRICE_HISTORY_MAX))
            existing = list(hist)
            hist.clear()
            hist.extend(points + existing)

    def _check_account(self, aid: str, err: rrm.ApiError) -> str:
        if err.kind != "not_found":
            return f"Couldn't load this account: {err}"
        try:
            owned = self.api.accounts_for_address(aid)
        except rrm.ApiError:
            owned = []
        if owned:
            ids = ", ".join(a.get("id", "") for a in owned[:3])
            return (f"This is a wallet address, not an Account ID. Its trading account is {ids}. To fix it, close the "
                    "dashboard, delete config.json from its folder, start it again and paste the Account ID.")
        return ("REAL has no account with this ID. Copy your Account ID from REAL (Settings > Account), then close the "
                "dashboard, delete config.json from its folder, start it again and paste it in.")

    def _pick_active_market(self) -> Optional[str]:
        if self.active_market in self.tickers:
            return self.active_market
        for v in self.monitor.last_views:
            if v.market_id in self.tickers:
                return v.market_id
        return next(iter(self.tickers), None)

    def refresh(self) -> None:
        """Runs after every monitor poll (in the monitor's thread)."""
        now = int(time.time() * 1000)
        try:
            tickers = [t for t in self.api.tickers() if t.get("market_id")]
        except rrm.ApiError as e:
            with self.lock:
                self.last_error = str(e)
            return

        # Each account on its own, so one wrong ID doesn't blank everything else.
        accounts: Dict[str, dict] = {}
        errors: Dict[str, str] = {}
        for aid in self.monitor.accounts:
            try:
                accounts[aid] = self.api.account(aid)
            except rrm.ApiError as e:
                errors[aid] = self.account_errors.get(aid) if aid in self.account_errors and e.kind == "not_found" \
                    else self._check_account(aid, e)

        market_info = None
        if time.time() - self.last_market_info >= MARKET_INFO_SECONDS:
            try:
                market_info = {m["id"]: m for m in self.api._paginate("/api/v1/markets", [], 5) if m.get("id")}
                self.last_market_info = time.time()
            except rrm.ApiError as e:
                log.warning("Could not load market parameters: %s", e)

        with self.lock:
            for t in tickers:
                self.tickers[t["market_id"]] = t
        # Order book only for the market being looked at.
        depth_mid, depth = self._pick_active_market(), None
        if depth_mid:
            try:
                depth = self.api._get(f"/api/v1/markets/{depth_mid}/depth").get("data") or {}
            except rrm.ApiError as e:
                log.debug("Could not load depth for %s: %s", depth_mid, e)

        orders: Optional[List[dict]] = []
        for aid in accounts:
            try:
                orders.extend(self.api._paginate(f"/api/v1/accounts/{aid}/orders",
                                                 [("f[statuses]", "Active"), ("p[s]", "100")], 5))
            except rrm.ApiError as e:
                log.debug("Could not load open orders: %s", e)
                orders = None
                break

        if time.time() - self.last_history >= HISTORY_SECONDS and accounts:
            self.refresh_history(list(accounts))

        for t in tickers:
            if t["market_id"] not in self.backfilled:
                self.backfill_prices(t["market_id"])

        with self.lock:
            self.last_success_ms = now
            self.last_error = None
            for t in tickers:
                mark = f(t.get("mark_price"))
                if mark:
                    self.price_history.setdefault(t["market_id"], deque(maxlen=PRICE_HISTORY_MAX)).append((now, mark))
            if market_info is not None:
                self.market_info = market_info
            if depth is not None:
                self.depth[depth_mid] = depth
                self.depth_fetched[depth_mid] = now
            if orders is not None:
                self.orders = orders
            self.accounts.update(accounts)
            self.account_errors = errors

    def fetch_depth(self, mid: str) -> None:
        try:
            depth = self.api._get(f"/api/v1/markets/{mid}/depth").get("data") or {}
        except rrm.ApiError as e:
            log.debug("Could not load depth for %s: %s", mid, e)
            return
        with self.lock:
            self.depth[mid] = depth
            self.depth_fetched[mid] = int(time.time() * 1000)

    def refresh_history(self, account_ids: List[str]) -> None:
        """Full load the first time, then only the newest page, merged in."""
        full = not self.history_loaded
        pages = HISTORY_MAX_PAGES if full else 1
        try:
            fills: List[dict] = []
            for aid in account_ids:
                fills.extend(self.api._paginate(f"/api/v1/accounts/{aid}/fills", [("p[o]", "desc"), ("p[s]", "100")], pages))
            funding = self.api._paginate("/api/v1/funding-payments", [("f[account_ids]", ",".join(account_ids)),
                                                                        ("p[o]", "desc"), ("p[s]", "100")], pages)
        except rrm.ApiError as e:
            log.warning("Could not load trade history: %s", e)
            return
        fill_key = lambda x: (str(x.get("account_id", "")).lower(), str(x.get("trade_id", "")) + str(x.get("direction", "")))
        with self.lock:
            if full:
                self.fills, self.funding = {}, {}
                per_account = [sum(1 for x in fills if fill_key(x)[0] == a.lower()) for a in account_ids]
                self.history_truncated = {"fills": max(per_account, default=0) >= HISTORY_MAX_PAGES * 100,
                                          "funding": len(funding) >= HISTORY_MAX_PAGES * 100}
            elif fills and not any(fill_key(x) in self.fills for x in fills) and len(fills) >= 100:
                self.history_loaded = False   # more than a page arrived since last check: reload everything next time
            for x in fills:
                self.fills[fill_key(x)] = x
            for x in funding:
                self.funding[str(x.get("id") or (x.get("tx_digest", ""), x.get("account_id", "")))] = x
            if full or self.history_loaded:
                self.history_loaded = True
            self.last_history = time.time()

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
        imr = f(t.get("imr"))
        return {
            "symbol": t.get("symbol") or info.get("symbol"),
            "mark": f(t.get("mark_price")),
            "index": f(t.get("index_price")),
            "change_24h": f(t.get("price_change_24h")),
            "high_24h": f(t.get("high_price_24h")),
            "low_24h": f(t.get("low_price_24h")),
            "turnover_24h": f(t.get("turnover_24h")),
            "open_interest_value": f(t.get("open_interest_value")),
            "funding_rate": f(t.get("perp_next_funding_rate")),
            "funding_time_ms": t.get("perp_next_funding_time_ms"),
            "max_leverage": (1 / imr) if imr else None,
            "mmr": f(risk.get("mmr")),
            "taker_fee": f((fees.get("taker_rates") or [None])[0]),
            "tick_size": f(rules.get("tick_size")),
            "min_order_value": f(rules.get("min_order_value")),
            "bids": [b for b in bids if b[0] and b[1]],
            "asks": [a for a in asks if a[0] and a[1]],
            "book_ms": self.depth_fetched.get(mid),
            "history": list(self.price_history.get(mid, [])),
        }

    def state(self, market: Optional[str] = None) -> dict:
        m = self.monitor
        if market and HEX_ID.match(market) and market in self.tickers and market != self.active_market:
            self.active_market = market
            if market not in self.depth:   # switching markets: fetch its order book now, not on the next cycle
                threading.Thread(target=self.fetch_depth, args=(market,), daemon=True).start()
        with self.lock:
            positions = []
            for v in sorted(m.last_views, key=lambda x: x.distance if x.distance is not None else 1e9):
                st = m.states.get(v.key)
                account = self.accounts.get(v.account_id.lower()) or self.accounts.get(v.account_id) or {}
                no_liq = v.distance == float("inf")
                tier = "stale" if v.stale_price else "unknown" if v.distance is None else (st.tier if st else "ok")
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
                    "distance": None if v.distance is None or no_liq else v.distance,
                    "no_liq": no_liq,
                    "tier": tier,
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
                    "error": self.account_errors.get(aid),
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
                "account": m.label(o.get("account_id", "")),
                "symbol": m.symbols.get(o.get("market_id", ""), rrm.short_id(o.get("market_id", ""))),
                "side": o.get("side"),
                "type": o.get("order_type"),
                "price": f(o.get("price")),
                "quantity": f(o.get("quantity")),
                "filled": f(o.get("filled_quantity")),
                "reduce_only": bool(o.get("reduce_only")),
                "trigger": f((o.get("conditional") or {}).get("trigger_price")),
            } for o in sorted(self.orders, key=lambda o: o.get("created_at_ms") or 0, reverse=True)]

            api_err = m.last_api_error if m.api_failing_since is not None else None
            error = self.last_error or (str(api_err) if api_err else None)
            return {
                "now_ms": int(time.time() * 1000),
                "loaded": m.first_poll_done and self.last_success_ms is not None,
                "network": self.cfg["network"],
                "poll_seconds": self.cfg["poll_seconds"],
                "thresholds": m.thresholds,
                "active_market": self._pick_active_market(),
                "status": {
                    "ok": error is None and self.last_success_ms is not None,
                    "last_success_ms": self.last_success_ms,
                    "error": error,
                    "error_kind": api_err.kind if api_err else None,
                },
                "accounts": accounts,
                "positions": positions,
                "markets": {mid: self._market(mid) for mid in self.tickers},
                "orders": orders,
                "fills": [self._fill(x) for x in self._sorted_fills()[:8]],
            }

    def _sorted_fills(self) -> List[dict]:
        return sorted(self.fills.values(), key=lambda x: x.get("created_at_ms") or 0, reverse=True)

    def _fill(self, x: dict) -> dict:
        attr = x.get("realised_pnl_attribution") or {}
        return {
            "t": x.get("created_at_ms"),
            "account": self.monitor.label(x.get("account_id", "")),
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
            fills = [self._fill(x) for x in self._sorted_fills()]
            funding = sorted(self.funding.values(), key=lambda x: x.get("updated_at_ms") or 0, reverse=True)
            funding = [{"t": x.get("updated_at_ms"), "payment": f(x.get("payment")), "rate": f(x.get("rate")),
                        "side": x.get("position_side"), "size": f(x.get("position_size")),
                        "account": self.monitor.label(x.get("account_id", "")),
                        "symbol": self.monitor.symbols.get(x.get("market_id", ""), "")} for x in funding]
            account_realised = [f(a.get("realised_pnl")) for a in self.accounts.values()]
            return {
                "loaded": self.history_loaded or bool(self.fills),
                "fills": fills,
                "funding": funding,
                "truncated": dict(self.history_truncated),
                "account_realised_pnl": sum(v for v in account_realised if v is not None) if any(v is not None for v in account_realised) else None,
                "now_ms": int(time.time() * 1000),
            }

    def liqmap(self) -> dict:
        if self.scanner:
            self.scanner.request()
        data = self.scanner.snapshot() if self.scanner else {"scanned_ms": None, "markets": {}, "recent": [], "error": None}
        with self.lock:
            marks = {mid: f(t.get("mark_price")) for mid, t in self.tickers.items()}
        mine = [{"market_id": v.market_id, "direction": v.direction, "liq": float(v.liq)}
                for v in self.monitor.last_views if v.liq is not None and v.liq > 0]
        return {**data, "marks": marks, "mine": mine, "now_ms": int(time.time() * 1000)}


# ---------------------------------------------------------------------------
# Web server
# ---------------------------------------------------------------------------

class LocalServer(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR lets a second program bind the same port, which would hide
    # "already running" errors. Elsewhere it only speeds up restarts.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def make_handler(dashboard: Dashboard):
    html_path = os.path.join(HERE, "dashboard.html")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep the console clean
            pass

        def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj, separators=(",", ":"), allow_nan=False).encode("utf-8"), "application/json")

        def _file(self, path: str, ctype: str, cache: str = "no-store") -> None:
            try:
                with open(path, "rb") as fh:
                    self._send(200, fh.read(), ctype, cache)
            except FileNotFoundError:
                self._send(404, f"{os.path.basename(path)} is missing; re-download the dashboard.".encode(), "text/plain")

        def _local_host(self) -> bool:
            # Blocks DNS-rebinding: only answer requests addressed to this computer.
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            return host in ("127.0.0.1", "localhost", "::1")

        def do_GET(self):
            if not self._local_host():
                return self._send(403, b"Forbidden", "text/plain")
            path, _, query = self.path.partition("?")
            params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            try:
                if path in ("/", "/index.html"):
                    self._file(html_path, "text/html; charset=utf-8")
                elif path in STATIC_FILES:
                    name, ctype = STATIC_FILES[path]
                    self._file(os.path.join(HERE, name), ctype)
                elif FONT_FILE.match(path):
                    self._file(os.path.join(HERE, "fonts", FONT_FILE.match(path).group(1)), "font/woff2", "max-age=86400")
                elif path == "/api/state":
                    self._json(200, dashboard.state(params.get("market")))
                elif path == "/api/liqmap":
                    self._json(200, dashboard.liqmap())
                elif path == "/api/performance":
                    self._json(200, dashboard.performance())
                else:
                    self._send(404, b"Not found", "text/plain")
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


def run_monitor(monitor: rrm.RiskMonitor, dashboard: Dashboard, poll_seconds: float) -> None:
    while True:
        try:
            monitor.poll()
            if monitor.first_poll_done:
                dashboard.refresh()
        except Exception as e:  # keep running, but make sure the page shows it isn't updating
            log.exception("Update failed: %s", e)
            with dashboard.lock:
                dashboard.last_error = f"Update failed: {e}"
        time.sleep(poll_seconds)


# ---------------------------------------------------------------------------
# First-run setup
# ---------------------------------------------------------------------------

PROMPT = """
Paste your REAL Account ID and press Enter.
  Find it on REAL under Settings > Account > Account ID.
  This is a public ID. Never paste your private key or recovery phrase here, or anywhere else.
"""


def looks_secret(value: str) -> Optional[str]:
    low = value.lower()
    if "privkey" in low or low.startswith("suiprivkey") or low.startswith("iotaprivkey"):
        return "That's a PRIVATE KEY. Don't paste it anywhere. Anyone who has it can take your funds."
    if len(value.split()) >= 6:
        return "That looks like a RECOVERY PHRASE. Don't paste it anywhere. Anyone who has it can take your funds."
    return None


def save_account(config_path: str, account_id: str) -> None:
    data: Dict[str, Any] = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            data = {}
    data["accounts"] = [{"id": account_id, "label": "Main"}]
    with open(config_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def ask_for_account(config_path: str, api: rrm.RealIndexer, read=input) -> Optional[str]:
    print(PROMPT)
    while True:
        try:
            value = read("> ").strip().strip('"').strip("'")
        except (EOFError, KeyboardInterrupt):
            return None
        if not value:
            continue
        warning = looks_secret(value)
        if warning:
            print(f"\n  {warning}\n  It has not been saved or sent anywhere. Paste your Account ID instead.\n")
            continue
        if not HEX_ID.match(value):
            print("  That doesn't look like an Account ID. It starts with 0x followed by 64 letters and numbers.\n")
            continue
        value = value.lower()
        try:
            api.account(value)
        except rrm.ApiError as e:
            if e.kind == "not_found":
                try:
                    owned = api.accounts_for_address(value)
                except rrm.ApiError:
                    owned = []
                if owned:
                    choice = owned[0].get("id", "")
                    print(f"  That's a wallet address. Its REAL trading account is {choice}.")
                    if len(owned) > 1:
                        for i, a in enumerate(owned, 1):
                            print(f"    {i}. {a.get('id')}")
                        pick = read("  Which one? Enter a number (default 1): ").strip() or "1"
                        if pick.isdigit() and 1 <= int(pick) <= len(owned):
                            choice = owned[int(pick) - 1].get("id", "")
                    value = choice.lower()
                else:
                    print("  REAL has no account with that ID. Check you copied the Account ID, not another address.\n")
                    continue
            else:
                print(f"  Couldn't check it with REAL right now ({e}).")
                if read("  Save it anyway? [y/N] ").strip().lower() not in ("y", "yes"):
                    continue
        save_account(config_path, value)
        print(f"\n  Saved to {os.path.basename(config_path)}. To change it later, run with --change-account.\n")
        return value


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Live risk dashboard for your REAL positions.")
    parser.add_argument("--config", help="Config file (default: config.json next to this script)")
    parser.add_argument("--account", action="append", help="Trading account ID (repeatable)")
    parser.add_argument("--change-account", action="store_true", help="Enter a different Account ID")
    parser.add_argument("--network", choices=list(rrm.NETWORKS))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="Don't open the browser automatically")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config_path = args.config or os.path.join(HERE, "config.json")
    try:
        cfg = rrm.load_config(config_path if os.path.exists(config_path) else None)
    except (ValueError, OSError) as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    if args.account:
        cfg["accounts"] = args.account
    if args.network:
        cfg["network"] = args.network
    api = rrm.RealIndexer(cfg["network"])
    placeholder = any("YOUR_" in (a if isinstance(a, str) else a.get("id", "")) for a in cfg["accounts"])
    if args.change_account or not cfg["accounts"] or placeholder:
        account = ask_for_account(config_path, api)
        if not account:
            return 2
        cfg["accounts"] = [{"id": account, "label": "Main"}]
    cfg["status_every_minutes"] = 0
    try:
        rrm.validate_config(cfg)
    except ValueError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    monitor = rrm.RiskMonitor(cfg, api, [])  # the dashboard shows risk; it doesn't send alerts
    monitor.watch_liquidations = False
    scanner = MarketScanner(api, monitor)
    dashboard = Dashboard(cfg, monitor, scanner=scanner)

    try:
        server = LocalServer(("127.0.0.1", args.port), make_handler(dashboard))
    except OSError:
        print(f"Port {args.port} is busy. The dashboard may already be running in another window.\n"
              f"Close that window, or start this one with --port {args.port + 1}", file=sys.stderr)
        return 1

    threading.Thread(target=run_monitor, args=(monitor, dashboard, cfg["poll_seconds"]), daemon=True).start()
    threading.Thread(target=scanner.run_forever, daemon=True).start()

    url = f"http://127.0.0.1:{args.port}"
    print(f"\nREAL Risk Dashboard (unofficial) running at {url}")
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
