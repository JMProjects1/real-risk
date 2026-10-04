#!/usr/bin/env python3
"""
REAL Risk Monitor (unofficial community tool, not affiliated with REAL)
=================

Watches your open positions on the REAL perpetuals DEX (real.xyz) and alerts you
before you get liquidated.

* Read-only. Uses only the public indexer API. Never needs your private key.
* No dependencies beyond the Python standard library (Python 3.9+).
* Alerts go to the console and, optionally, a Discord webhook.

Quick start:
    python real_risk_monitor.py --find-accounts 0xYOUR_WALLET_ADDRESS
    python real_risk_monitor.py --account 0xYOUR_TRADING_ACCOUNT_ID --once
    python real_risk_monitor.py --config config.json

See README.md for the full guide.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

__version__ = "1.1.2"

NETWORKS = {
    "mainnet": "https://indexer.api.real.xyz",
    "testnet": "https://indexer.api.testnet.real.xyz",
}

USER_AGENT = f"real-risk-monitor/{__version__}"

DEFAULT_CONFIG: Dict[str, Any] = {
    "network": "mainnet",
    # Trading account IDs to watch. Either "0x..." strings or {"id": "0x...", "label": "Main"}.
    "accounts": [],
    # Discord webhook URL. Can also be set with the REAL_DISCORD_WEBHOOK environment variable.
    "discord_webhook_url": "",
    # Text prepended to danger/critical/liquidation alerts, e.g. "<@123456789>" to ping yourself.
    "mention": "",
    "poll_seconds": 15,
    # Alert when the mark price is within this % of the liquidation price.
    "thresholds_pct": {"warning": 15.0, "danger": 8.0, "critical": 4.0},
    # Re-send an alert while a position stays in a tier (minutes; 0 = never repeat).
    "repeat_minutes": {"warning": 0, "danger": 30, "critical": 5},
    # A position must recover this many percentage points past a threshold before it is
    # downgraded, so a price hovering on a line doesn't spam you.
    "recovery_buffer_pct": 1.0,
    # Tell you when positions are opened, closed or flipped.
    "notify_position_changes": True,
    # Alert if the indexer's computed values for a position are older than this.
    "stale_data_minutes": 2,
    # Alert if the API has been unreachable for this long (the monitor is blind).
    "api_down_alert_minutes": 3,
    # Print a status table to the console this often (0 = only at startup).
    "status_every_minutes": 10,
}

TIERS = ["ok", "warning", "danger", "critical"]
SEVERITY = {t: i for i, t in enumerate(TIERS)}

log = logging.getLogger("real-risk-monitor")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def dec(value: Any) -> Optional[Decimal]:
    """Parse the API's decimal strings. Returns None for null/garbage."""
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def fmt_price(value: Optional[Decimal]) -> str:
    if value is None:
        return "n/a"
    v = float(value)
    if v == 0:
        return "0"
    if abs(v) >= 1000:
        return f"{v:,.2f}"
    if abs(v) >= 1:
        return f"{v:,.4f}"
    return f"{v:.6g}"


def fmt_money(value: Optional[Decimal]) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):,.2f}"


def fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if value == float("inf"):
        return "no liq. price"
    return f"{value:.2f}%"


def short_id(value: str) -> str:
    return value if len(value) <= 12 else f"{value[:6]}…{value[-4:]}"


def now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Risk maths
# ---------------------------------------------------------------------------

def distance_to_liquidation_pct(
    direction: Optional[str], mark: Optional[Decimal], liq: Optional[Decimal]
) -> Optional[float]:
    """
    How far the mark price can move against the position before it hits the
    estimated liquidation price, as a percentage of the mark price.

    Returns:
        float('inf') when there is no reachable liquidation price (e.g. an
            over-collateralised long reports 0),
        0.0 when the mark is already at/through the liquidation price,
        None when it can't be computed (missing or stale data).
    """
    if mark is None or mark <= 0 or liq is None or direction not in ("Long", "Short"):
        return None
    if liq <= 0:
        return float("inf")
    if direction == "Long":
        dist = (mark - liq) / mark
    else:
        dist = (liq - mark) / mark
    return max(0.0, float(dist * 100))


def classify(distance: Optional[float], thresholds: Dict[str, float]) -> str:
    if distance is None:
        return "ok"
    if distance <= thresholds["critical"]:
        return "critical"
    if distance <= thresholds["danger"]:
        return "danger"
    if distance <= thresholds["warning"]:
        return "warning"
    return "ok"


def next_tier(
    distance: Optional[float], previous: str, thresholds: Dict[str, float], buffer: float
) -> str:
    """Classify with hysteresis: getting worse is immediate, recovering needs a buffer.
    An unknown distance never changes the tier: no news is not good news."""
    if distance is None:
        return previous
    raw = classify(distance, thresholds)
    if SEVERITY[raw] >= SEVERITY[previous]:
        return raw
    padded = {k: v + buffer for k, v in thresholds.items()}
    recovered = classify(distance, padded)
    return TIERS[min(SEVERITY[previous], SEVERITY[recovered])]


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, kind: str = "error"):
        super().__init__(message)
        self.status = status
        self.kind = kind  # "not_found", "certificate", "rate_limited", "network", "error"


CERT_HELP = ("Python can't verify REAL's security certificate. On a Mac with Python from python.org, "
             "open Finder > Applications > Python 3.x and double-click 'Install Certificates.command', "
             "then start the dashboard again.")


def _is_cert_error(err: BaseException) -> bool:
    reason = getattr(err, "reason", err)
    return isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason)


class RealIndexer:
    """Minimal read-only client for the REAL indexer REST API."""

    # One pause shared by every client and thread: when REAL says "slow down", everyone waits.
    _cooldown_until = 0.0
    _cooldown_lock = threading.Lock()

    def __init__(self, network: str = "mainnet", base_url: Optional[str] = None, timeout: float = 15.0):
        if base_url is None:
            if network not in NETWORKS:
                raise ValueError(f"Unknown network {network!r}; use one of {list(NETWORKS)}")
            base_url = NETWORKS[network]
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @classmethod
    def _wait_for_cooldown(cls) -> None:
        with cls._cooldown_lock:
            wait = cls._cooldown_until - time.time()
        if wait > 0:
            time.sleep(min(wait, 60))

    @classmethod
    def _set_cooldown(cls, seconds: float) -> None:
        with cls._cooldown_lock:
            cls._cooldown_until = max(cls._cooldown_until, time.time() + seconds)

    def _get(self, path: str, params: Optional[List[Tuple[str, str]]] = None, retries: int = 3) -> Any:
        query = urllib.parse.urlencode(params or [], safe="[],")
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        delay = 1.0
        for attempt in range(retries + 1):
            self._wait_for_cooldown()
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:300]
                if e.code == 429 or e.code >= 500:
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    wait = float(retry_after) if retry_after and retry_after.replace(".", "").isdigit() else delay
                    if e.code == 429:
                        self._set_cooldown(min(wait, 60))
                    if attempt < retries:
                        log.debug("HTTP %s on %s, retrying in %.1fs", e.code, path, wait)
                        time.sleep(min(wait, 30))
                        delay *= 2
                        continue
                    kind = "rate_limited" if e.code == 429 else "error"
                    raise ApiError(f"HTTP {e.code} for {path}: {body}", e.code, kind) from None
                raise ApiError(f"HTTP {e.code} for {path}: {body}", e.code,
                               "not_found" if e.code == 404 else "error") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                if _is_cert_error(e):
                    raise ApiError(CERT_HELP, kind="certificate") from None
                if attempt < retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise ApiError(f"Network error for {path}: {e}", kind="network") from None
            except json.JSONDecodeError as e:
                raise ApiError(f"Bad JSON from {path}: {e}") from None
        raise ApiError(f"Gave up on {path}")

    def _paginate(self, path: str, params: List[Tuple[str, str]], max_pages: int = 50) -> List[dict]:
        items: List[dict] = []
        cursor: Optional[str] = None
        for _ in range(max_pages):
            page_params = list(params) + ([("p[c]", cursor)] if cursor else [])
            resp = self._get(path, page_params)
            items.extend(resp.get("data") or [])
            cursor = (resp.get("pagination") or {}).get("next_cursor")
            if not cursor:
                break
        return items

    def positions_live(self, account_ids: Iterable[str]) -> List[dict]:
        return self._paginate(
            "/api/v1/positions/live",
            [("f[account_ids]", ",".join(account_ids)), ("p[s]", "100")],
        )

    def account(self, account_id: str) -> dict:
        return self._get(f"/api/v1/accounts/{account_id}").get("data") or {}

    def accounts_for_address(self, address: str) -> List[dict]:
        return self._paginate("/api/v1/accounts", [("f[address]", address)])

    def tickers(self) -> List[dict]:
        return self._paginate("/api/v1/markets/tickers", [])

    def recent_liquidations(self, account_ids: Iterable[str], limit: int = 25) -> List[dict]:
        resp = self._get(
            "/api/v1/liquidations",
            [("f[account_ids]", ",".join(account_ids)), ("p[o]", "desc"), ("p[s]", str(limit))],
        )
        return resp.get("data") or []


# ---------------------------------------------------------------------------
# Notifiers
# ---------------------------------------------------------------------------

COLORS = {
    "ok": 0x2ECC71,
    "info": 0x3498DB,
    "warning": 0xF1C40F,
    "danger": 0xE67E22,
    "critical": 0xE74C3C,
    "liquidated": 0x8E44AD,
    "system": 0x95A5A6,
}

EMOJI = {
    "ok": "🟢",
    "info": "🔵",
    "warning": "🟡",
    "danger": "🟠",
    "critical": "🔴",
    "liquidated": "💀",
    "system": "⚙️",
}


@dataclass
class Alert:
    level: str  # one of COLORS keys
    title: str
    lines: List[str] = field(default_factory=list)
    fields: List[Tuple[str, str]] = field(default_factory=list)
    ping: bool = False


class ConsoleNotifier:
    def send(self, alert: Alert) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{stamp}] {EMOJI.get(alert.level, '')} {alert.title}")
        for line in alert.lines:
            print(f"    {line}")
        for name, value in alert.fields:
            print(f"    {name}: {value}")
        sys.stdout.flush()


class DiscordNotifier:
    def __init__(self, webhook_url: str, mention: str = ""):
        self.webhook_url = webhook_url
        self.mention = mention

    def send(self, alert: Alert) -> None:
        error = self.post(alert)
        if error:
            log.error("Discord webhook failed: %s", error)

    def post(self, alert: Alert) -> Optional[str]:
        """Send an alert. Returns None on success or an error message."""
        embed = {
            "title": f"{EMOJI.get(alert.level, '')} {alert.title}"[:256],
            "description": "\n".join(alert.lines)[:4000],
            "color": COLORS.get(alert.level, COLORS["info"]),
            "fields": [{"name": n[:256], "value": (v or "-")[:1024], "inline": True} for n, v in alert.fields[:25]],
            "footer": {"text": "REAL Risk Monitor"},
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        payload: Dict[str, Any] = {"embeds": [embed], "username": "REAL Risk Monitor"}
        if alert.ping and self.mention:
            payload["content"] = self.mention
            payload["allowed_mentions"] = {"parse": ["users", "roles", "everyone"]}
        data = json.dumps(payload).encode("utf-8")
        for attempt in range(3):
            req = urllib.request.Request(
                self.webhook_url,
                data=data,
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=15):
                    return None
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:
                    try:
                        wait = float(json.loads(e.read().decode()).get("retry_after", 2))
                    except Exception:
                        wait = 2.0
                    time.sleep(min(wait, 30))
                    continue
                if e.code in (401, 404):
                    return "Discord rejected the webhook (it may have been deleted). Create a new one and paste it in."
                return f"Discord returned HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
            except Exception as e:  # never let a notification failure kill the monitor
                if attempt < 2:
                    time.sleep(2)
                    continue
                return f"Couldn't reach Discord: {e}"
        return "Discord kept rate-limiting the request"


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

@dataclass
class PositionView:
    account_id: str
    market_id: str
    symbol: str
    direction: str
    size: Decimal
    entry: Optional[Decimal]
    mark: Optional[Decimal]
    liq: Optional[Decimal]
    upnl: Optional[Decimal]
    notional: Optional[Decimal]
    margin: Optional[Decimal]
    margin_mode: str
    leverage: Optional[float]
    stale_price: bool
    fresh: bool
    refreshed_at_ms: Optional[int]
    distance: Optional[float]

    @property
    def key(self) -> Tuple[str, str]:
        return (self.account_id, self.market_id)


@dataclass
class PositionState:
    tier: str = "ok"
    last_alert_s: float = 0.0
    direction: str = ""
    stale_alerted: bool = False
    unknown_alerted: bool = False


class RiskMonitor:
    def __init__(
        self,
        config: Dict[str, Any],
        api: RealIndexer,
        notifiers: List[Any],
        clock: Callable[[], float] = time.time,
    ):
        self.cfg = config
        self.api = api
        self.notifiers = notifiers
        self.clock = clock
        self.accounts: Dict[str, str] = {}  # id -> label
        for entry in config["accounts"]:
            if isinstance(entry, str):
                self.accounts[entry.lower()] = short_id(entry)
            else:
                self.accounts[entry["id"].lower()] = entry.get("label") or short_id(entry["id"])
        if not self.accounts:
            raise ValueError("No accounts configured. Use --account or the 'accounts' config key.")
        self.thresholds = {k: float(v) for k, v in config["thresholds_pct"].items()}
        self.repeat = {k: float(v) for k, v in config["repeat_minutes"].items()}
        self.symbols: Dict[str, str] = {}
        self.symbols_loaded_s = 0.0
        self.states: Dict[Tuple[str, str], PositionState] = {}
        self.seen_liquidations: set = set()
        self.first_poll_done = False
        self.api_failing_since: Optional[float] = None
        self.api_down_alerted = False
        self.last_api_error: Optional[ApiError] = None
        self.watch_liquidations = True  # the dashboard turns this off: it doesn't send alerts
        self.last_status_s = 0.0
        self.last_views: List[PositionView] = []

    # -- plumbing ----------------------------------------------------------

    def notify(self, alert: Alert) -> None:
        for n in self.notifiers:
            try:
                n.send(alert)
            except Exception as e:
                log.error("Notifier %s failed: %s", type(n).__name__, e)

    def refresh_symbols(self, force: bool = False) -> None:
        if not force and self.symbols and self.clock() - self.symbols_loaded_s < 600:
            return
        try:
            for t in self.api.tickers():
                if t.get("market_id") and t.get("symbol"):
                    self.symbols[t["market_id"]] = t["symbol"]
            self.symbols_loaded_s = self.clock()
        except ApiError as e:
            log.warning("Could not load market symbols: %s", e)

    def symbol(self, market_id: str) -> str:
        if market_id not in self.symbols:
            self.refresh_symbols(force=True)
        return self.symbols.get(market_id, short_id(market_id))

    def label(self, account_id: str) -> str:
        return self.accounts.get(account_id.lower(), short_id(account_id))

    # -- data --------------------------------------------------------------

    def to_view(self, p: dict) -> Optional[PositionView]:
        if p.get("status") != "Open":
            return None
        size = dec(p.get("size")) or Decimal(0)
        direction = p.get("direction")
        if size == 0 or direction not in ("Long", "Short"):
            return None
        lv = p.get("live_values") or {}
        mark = dec(lv.get("mark_price"))
        liq = dec(lv.get("estimated_liquidation_price"))
        stale = bool(lv.get("has_stale_price"))
        imr = dec(p.get("current_imr"))
        leverage = float(1 / imr) if imr and imr > 0 else None
        distance = None if stale else distance_to_liquidation_pct(direction, mark, liq)
        return PositionView(
            account_id=p.get("account_id", ""),
            market_id=p.get("market_id", ""),
            symbol=self.symbol(p.get("market_id", "")),
            direction=direction,
            size=size,
            entry=dec(p.get("average_entry_price")),
            mark=mark,
            liq=liq,
            upnl=dec(lv.get("unrealised_pnl")),
            notional=dec(lv.get("notional_value")),
            margin=dec(lv.get("margin_allocated_contracts")),
            margin_mode=p.get("current_margin_mode") or "?",
            leverage=leverage,
            stale_price=stale,
            fresh=bool(lv.get("fresh", True)),
            refreshed_at_ms=lv.get("refreshed_at_ms"),
            distance=distance,
        )

    def position_fields(self, v: PositionView) -> List[Tuple[str, str]]:
        f = [
            ("Distance to liq.", fmt_pct(v.distance)),
            ("Mark", fmt_price(v.mark)),
            ("Liq. price", fmt_price(v.liq) if v.liq and v.liq > 0 else "none"),
            ("Entry", fmt_price(v.entry)),
            ("Size", f"{v.size} ({v.direction})"),
            ("Unrealised PnL", fmt_money(v.upnl)),
            ("Margin mode", v.margin_mode + (f" · max {v.leverage:.0f}x" if v.leverage else "")),
        ]
        if len(self.accounts) > 1:
            f.insert(0, ("Account", self.label(v.account_id)))
        return f

    # -- one polling cycle ---------------------------------------------------

    def poll(self) -> None:
        try:
            self.refresh_symbols()
            raw = self.api.positions_live(self.accounts.keys())
            liquidations = self.api.recent_liquidations(self.accounts.keys()) if self.watch_liquidations else []
        except ApiError as e:
            self.on_api_failure(e)
            return
        self.on_api_success()

        views = [v for v in (self.to_view(p) for p in raw) if v and v.account_id.lower() in self.accounts]
        current = {v.key: v for v in views}

        new_liqs = self.check_liquidations(liquidations)
        liquidated_keys = {(liq.get("account_id", ""), pos.get("market_id")) for liq in new_liqs for pos in liq.get("positions") or []}

        # Positions that disappeared.
        for key in list(self.states):
            if key not in current:
                st = self.states.pop(key)
                if key in liquidated_keys:
                    continue  # already reported as a liquidation
                if self.first_poll_done and self.cfg["notify_position_changes"]:
                    self.notify(Alert("info", f"Position closed: {self.symbol(key[1])} {st.direction}",
                                      [f"Account: {self.label(key[0])}"]))

        for v in views:
            self.evaluate(v, is_new=v.key not in self.states)

        self.last_views = views
        if not self.first_poll_done:
            self.first_poll_done = True
            self.print_status(views, title="Monitoring started")
        elif self.cfg["status_every_minutes"] and self.clock() - self.last_status_s >= self.cfg["status_every_minutes"] * 60:
            self.print_status(views)

    def evaluate(self, v: PositionView, is_new: bool) -> None:
        now = self.clock()
        st = self.states.setdefault(v.key, PositionState(direction=v.direction))

        if is_new and self.first_poll_done and self.cfg["notify_position_changes"]:
            self.notify(Alert("info", f"Position opened: {v.symbol} {v.direction}", [], self.position_fields(v)))
        elif not is_new and st.direction and st.direction != v.direction and self.cfg["notify_position_changes"]:
            self.notify(Alert("info", f"Position flipped: {v.symbol} {st.direction} → {v.direction}", [], self.position_fields(v)))
            st.tier = "ok"
        st.direction = v.direction

        # Stale price: the exchange can't compute a liquidation price right now.
        if v.stale_price:
            if not st.stale_alerted:
                st.stale_alerted = True
                self.notify(Alert("warning", f"Stale price on {v.symbol}",
                                  ["The indexer flags this market's mark price as stale, so the liquidation "
                                   "distance can't be computed. Keep an eye on it manually."],
                                  self.position_fields(v)))
            return
        if st.stale_alerted:
            st.stale_alerted = False
            self.notify(Alert("ok", f"Price feed back for {v.symbol}", [], self.position_fields(v)))

        # No liquidation price from REAL: the risk can't be measured, which is not the same as safe.
        if v.distance is None:
            if not st.unknown_alerted:
                st.unknown_alerted = True
                self.notify(Alert("warning", f"Liquidation price unavailable for {v.symbol} {v.direction}",
                                  ["REAL isn't returning a liquidation price for this position, so its risk can't be "
                                   "measured right now. Check it on REAL."], self.position_fields(v)))
            return
        if st.unknown_alerted:
            st.unknown_alerted = False
            self.notify(Alert("info", f"Liquidation price available again for {v.symbol} {v.direction}", [],
                              self.position_fields(v)))

        # Stale computed values.
        if v.refreshed_at_ms and not v.fresh:
            age_min = (now_ms() - int(v.refreshed_at_ms)) / 60000
            if age_min > self.cfg["stale_data_minutes"]:
                log.warning("%s values are %.1f min old", v.symbol, age_min)

        new = next_tier(v.distance, st.tier, self.thresholds, float(self.cfg["recovery_buffer_pct"]))
        old = st.tier
        st.tier = new

        if SEVERITY[new] > SEVERITY[old]:
            st.last_alert_s = now
            self.send_risk_alert(v, new, escalated=True)
        elif SEVERITY[new] < SEVERITY[old]:
            st.last_alert_s = now
            if new == "ok":
                self.notify(Alert("ok", f"{v.symbol} {v.direction} back outside alert levels", [f"Recovered from {old}."], self.position_fields(v)))
            else:
                self.notify(Alert(new, f"{v.symbol} {v.direction} improved: {old} → {new}", [], self.position_fields(v)))
        elif new != "ok":
            every = self.repeat.get(new, 0)
            if every > 0 and now - st.last_alert_s >= every * 60:
                st.last_alert_s = now
                self.send_risk_alert(v, new, escalated=False)

    def send_risk_alert(self, v: PositionView, tier: str, escalated: bool) -> None:
        headline = {
            "warning": "getting close to liquidation",
            "danger": "close to liquidation",
            "critical": "about to be liquidated",
        }[tier]
        title = f"{tier.upper()}: {v.symbol} {v.direction} {headline}"
        lines = []
        if v.distance is not None and v.mark is not None and v.liq is not None:
            move = "drop" if v.direction == "Long" else "rise"
            lines.append(f"A **{fmt_pct(v.distance)}** {move} in {v.symbol} (to {fmt_price(v.liq)}) triggers liquidation.")
        if v.margin_mode == "Cross":
            lines.append("Cross margin: the liquidation price also moves with your other positions and balance.")
        if not escalated:
            lines.append("_Reminder: still in this zone._")
        self.notify(Alert(tier, title, lines, self.position_fields(v), ping=SEVERITY[tier] >= SEVERITY["danger"]))

    def check_liquidations(self, liquidations: List[dict]) -> List[dict]:
        fresh = []
        for liq in liquidations:
            lid = liq.get("liquidation_id")
            if lid is None or lid in self.seen_liquidations:
                continue
            self.seen_liquidations.add(lid)
            if self.first_poll_done:  # don't report history on startup
                fresh.append(liq)
        for liq in reversed(fresh):  # oldest first
            parts = []
            for pos in liq.get("positions") or []:
                status = pos.get("status", "?")
                nice = {"FullPosition": "fully liquidated", "PartialPosition": "partially liquidated",
                        "OrdersCancelledOnly": "orders cancelled"}.get(status, status)
                parts.append(f"{self.symbol(pos.get('market_id', ''))}: {nice} (size {pos.get('liquidated_position_size', '?')})")
            self.notify(Alert("liquidated", f"LIQUIDATION on {self.label(liq.get('account_id', ''))}",
                              parts or ["A liquidation was recorded for this account."], ping=True))
        return fresh

    # -- health --------------------------------------------------------------

    def on_api_failure(self, err: Exception) -> None:
        now = self.clock()
        if self.api_failing_since is None:
            self.api_failing_since = now
        self.last_api_error = err if isinstance(err, ApiError) else ApiError(str(err))
        log.warning("API error: %s", err)
        down_for = now - self.api_failing_since
        if not self.api_down_alerted and down_for >= self.cfg["api_down_alert_minutes"] * 60:
            self.api_down_alerted = True
            self.notify(Alert("system", "Monitor can't reach the REAL API",
                              [f"No data for {down_for / 60:.0f} min. Your positions are NOT being watched "
                               "until this clears. Check them manually.", f"Last error: {err}"], ping=True))

    def on_api_success(self) -> None:
        if self.api_down_alerted:
            down = (self.clock() - (self.api_failing_since or self.clock())) / 60
            self.notify(Alert("system", "Monitor reconnected", [f"API was unreachable for about {down:.0f} min."]))
        self.api_failing_since = None
        self.api_down_alerted = False
        self.last_api_error = None

    # -- reporting -----------------------------------------------------------

    def status_table(self, views: List[PositionView]) -> str:
        if not views:
            return "No open positions."
        rows = [("Account", "Market", "Side", "Size", "Mark", "Liq.", "Distance", "uPnL", "Tier")]
        for v in sorted(views, key=lambda x: (x.distance if x.distance is not None else 1e9)):
            tier = "stale" if v.stale_price else "unknown" if v.distance is None else self.states.get(v.key, PositionState()).tier
            rows.append((self.label(v.account_id), v.symbol, v.direction, str(v.size), fmt_price(v.mark),
                         fmt_price(v.liq) if v.liq and v.liq > 0 else "none", fmt_pct(v.distance),
                         fmt_money(v.upnl), tier))
        widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
        out = []
        for i, r in enumerate(rows):
            out.append("  ".join(c.ljust(widths[j]) for j, c in enumerate(r)))
            if i == 0:
                out.append("  ".join("-" * w for w in widths))
        return "\n".join(out)

    def account_summary(self) -> List[str]:
        lines = []
        for aid, label in self.accounts.items():
            try:
                a = self.api.account(aid)
            except ApiError as e:
                lines.append(f"{label}: could not load account ({e})")
                continue
            lines.append(f"{label}: equity {fmt_money(dec(a.get('equity')))}, "
                         f"available {fmt_money(dec(a.get('available_balance')))}, "
                         f"uPnL {fmt_money(dec(a.get('unrealised_pnl')))}")
        return lines

    def print_status(self, views: List[PositionView], title: str = "Status") -> None:
        self.last_status_s = self.clock()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{stamp}] {title}")
        for line in self.account_summary():
            print(f"  {line}")
        print()
        for line in self.status_table(views).splitlines():
            print(f"  {line}")
        sys.stdout.flush()

    def startup_alert(self) -> None:
        views = self.last_views
        worst = min((v.distance for v in views if v.distance is not None), default=None)
        lines = [f"Watching {len(self.accounts)} account(s) on {self.cfg['network']}, "
                 f"{len(views)} open position(s), polling every {self.cfg['poll_seconds']}s."]
        if worst is not None:
            lines.append(f"Closest to liquidation: {fmt_pct(worst)}")
        t = self.thresholds
        lines.append(f"Alert levels: warning ≤{t['warning']:g}%, danger ≤{t['danger']:g}%, critical ≤{t['critical']:g}%")
        lines.extend(self.account_summary())
        for n in self.notifiers:
            if isinstance(n, DiscordNotifier):
                n.send(Alert("system", "Risk monitor started", lines))

    def run(self) -> None:
        self.poll()
        if self.first_poll_done:
            self.startup_alert()
        while True:
            time.sleep(self.cfg["poll_seconds"])
            try:
                self.poll()
            except Exception as e:  # keep running whatever happens
                log.exception("Unexpected error during poll: %s", e)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def load_config(path: Optional[str]) -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                user = json.load(fh)
        except json.JSONDecodeError as e:
            raise ValueError(f"{os.path.basename(path)} has a typo at line {e.lineno}, column {e.colno} ({e.msg}). "
                             "Check for a missing or extra comma, quote or bracket near there.") from None
        if not isinstance(user, dict):
            raise ValueError(f"{os.path.basename(path)} should contain a JSON object, like config.example.json.")
        for k, v in user.items():
            if k.startswith("_"):
                continue
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    env_hook = os.environ.get("REAL_DISCORD_WEBHOOK")
    if env_hook:
        cfg["discord_webhook_url"] = env_hook
    return cfg


HEX_ID_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def looks_secret(value: str) -> Optional[str]:
    """A warning if the text looks like a private key or recovery phrase, else None."""
    low = value.lower()
    if "privkey" in low:
        return "That's a PRIVATE KEY. Don't paste it anywhere. Anyone who has it can take your funds."
    if len(value.split()) >= 6:
        return "That looks like a RECOVERY PHRASE. Don't paste it anywhere. Anyone who has it can take your funds."
    return None


def account_id_problem(value: Any) -> Optional[str]:
    """Why a configured account ID can't be used, or None if it looks fine."""
    if not isinstance(value, str) or not value.strip():
        return "is missing"
    secret = looks_secret(value)
    if secret:
        return "looks like a private key or recovery phrase. Remove it from your config now and use your public Account ID"
    if not HEX_ID_RE.match(value.strip()):
        return "isn't in the right format (it should be 0x followed by 64 letters and numbers)"
    return None


def validate_config(cfg: Dict[str, Any], check_accounts: bool = True) -> None:
    for i, entry in enumerate((cfg.get("accounts") or []) if check_accounts else [], 1):
        aid = entry if isinstance(entry, str) else entry.get("id") if isinstance(entry, dict) else None
        problem = account_id_problem(aid)
        if problem:
            where = "" if len(cfg["accounts"]) == 1 else f" #{i}"
            raise ValueError(f"Account ID{where} {problem}. Check the --account you typed, or the accounts in config.json.")
    t = cfg["thresholds_pct"]
    if not (t["warning"] > t["danger"] > t["critical"] > 0):
        raise ValueError("thresholds_pct must satisfy warning > danger > critical > 0")
    if cfg["poll_seconds"] < 5:
        raise ValueError("poll_seconds must be at least 5 (the API is rate limited)")
    if cfg["network"] not in NETWORKS:
        raise ValueError(f"network must be one of {list(NETWORKS)}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Alerts you before your REAL positions get liquidated.")
    parser.add_argument("--config", help="Path to a JSON config file")
    parser.add_argument("--account", action="append", help="Trading account ID to watch (repeatable)")
    parser.add_argument("--network", choices=list(NETWORKS), help="mainnet (default) or testnet")
    parser.add_argument("--webhook", help="Discord webhook URL (or set REAL_DISCORD_WEBHOOK)")
    parser.add_argument("--once", action="store_true", help="Print current risk once and exit")
    parser.add_argument("--find-accounts", metavar="WALLET", help="List trading accounts owned by a wallet address")
    parser.add_argument("--test-alert", action="store_true", help="Send a sample alert to check Discord works")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    try:
        cfg = load_config(args.config)
    except (ValueError, OSError) as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    if args.account:
        cfg["accounts"] = args.account
    if args.network:
        cfg["network"] = args.network
    if args.webhook:
        cfg["discord_webhook_url"] = args.webhook
    try:
        validate_config(cfg, check_accounts=not args.find_accounts)   # --find-accounts is how you fix a wrong ID
    except ValueError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    api = RealIndexer(cfg["network"])

    if args.find_accounts:
        try:
            accounts = api.accounts_for_address(args.find_accounts)
        except ApiError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        if not accounts:
            print("No trading accounts found for that wallet on", cfg["network"])
            return 1
        print(f"Trading accounts for {args.find_accounts} on {cfg['network']}:\n")
        for a in accounts:
            print(f"  {a.get('id')}  (index {a.get('account_index')}, equity {fmt_money(dec(a.get('equity')))})")
        print("\nUse one of these IDs with --account or in config.json.")
        return 0

    notifiers: List[Any] = [ConsoleNotifier()]
    if cfg["discord_webhook_url"]:
        notifiers.append(DiscordNotifier(cfg["discord_webhook_url"], cfg.get("mention", "")))

    if args.test_alert:
        if not cfg["discord_webhook_url"]:
            print("No Discord webhook configured.", file=sys.stderr)
            return 2
        sample = Alert("critical", "TEST: BTC-PERP Long about to be liquidated",
                       ["This is a test alert from REAL Risk Monitor. If you can read this, Discord alerts work."],
                       [("Distance to liq.", "3.10%"), ("Mark", "85,306.47"), ("Liq. price", "82,662.00")], ping=True)
        for n in notifiers:
            n.send(sample)
        return 0

    try:
        monitor = RiskMonitor(cfg, api, notifiers if not args.once else [ConsoleNotifier()])
    except ValueError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    if args.once:
        monitor.poll()
        return 0 if monitor.first_poll_done else 1

    print(f"REAL Risk Monitor {__version__}: watching {len(monitor.accounts)} account(s) on {cfg['network']}. Ctrl+C to stop.")
    try:
        monitor.run()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
