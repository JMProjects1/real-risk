"""Offline tests. Run with: python -m unittest -v"""

import copy
import json
import io
import unittest
from contextlib import redirect_stdout
from decimal import Decimal

import real_risk_monitor as rrm

ACCT = "0x00000000000000000000000000000000000000000000000000000000000acc01"
BTC = "0x03d9f492be9ec3289c0ecbd5b60e9df3c8c74bb5fd51f697a2b85f065f51a9d5"

# Shape taken from a real mainnet /api/v1/positions/live response.
REAL_LONG = {
    "account_id": ACCT,
    "market_id": BTC,
    "status": "Open",
    "direction": "Long",
    "size": "0.0047",
    "average_entry_price": "85249.3",
    "current_imr": "0.025",
    "current_margin_mode": "Cross",
    "live_values": {
        "fresh": True,
        "refreshed_at_ms": 1791131816460,
        "mark_price": "85306.465786049644053112",
        "has_stale_price": False,
        "notional_value": "400.9403891944333270496264",
        "margin_allocated_contracts": "10.023509729860833176",
        "margin_allocated_orders": "0",
        "unrealised_pnl": "0.268679194433327049",
        "estimated_liquidation_price": "84144.932507406409911123",
    },
}


def position(mark, liq, direction="Long", **extra):
    p = copy.deepcopy(REAL_LONG)
    p["direction"] = direction
    p["live_values"]["mark_price"] = str(mark)
    p["live_values"]["estimated_liquidation_price"] = None if liq is None else str(liq)
    p["live_values"].update(extra.pop("live", {}))
    p.update(extra)
    return p


class FakeApi:
    def __init__(self):
        self.positions = []
        self.liquidations = []
        self.fail = False

    def tickers(self):
        return [{"market_id": BTC, "symbol": "BTC-PERP"}]

    def positions_live(self, ids):
        if self.fail:
            raise rrm.ApiError("boom")
        return self.positions

    def recent_liquidations(self, ids, limit=25):
        if self.fail:
            raise rrm.ApiError("boom")
        return self.liquidations

    def account(self, aid):
        return {"equity": "100", "available_balance": "50", "unrealised_pnl": "0"}


class Recorder:
    def __init__(self):
        self.alerts = []

    def send(self, alert):
        self.alerts.append(alert)

    def titles(self):
        return [a.title for a in self.alerts]


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def advance(self, minutes):
        self.t += minutes * 60


def make_monitor(**cfg_overrides):
    cfg = rrm.load_config(None)
    cfg["accounts"] = [{"id": ACCT, "label": "Main"}]
    cfg["status_every_minutes"] = 0
    cfg.update(cfg_overrides)
    api, rec, clock = FakeApi(), Recorder(), Clock()
    mon = rrm.RiskMonitor(cfg, api, [rec], clock=clock)
    return mon, api, rec, clock


def quiet(fn):
    with redirect_stdout(io.StringIO()):
        return fn()


class DistanceTests(unittest.TestCase):
    def test_real_long(self):
        d = rrm.distance_to_liquidation_pct("Long", Decimal("85306.465786"), Decimal("84144.932507"))
        self.assertAlmostEqual(d, 1.3616, places=3)

    def test_short(self):
        d = rrm.distance_to_liquidation_pct("Short", Decimal("100"), Decimal("110"))
        self.assertAlmostEqual(d, 10.0)

    def test_zero_liq_price_means_safe(self):
        self.assertEqual(rrm.distance_to_liquidation_pct("Long", Decimal("85306"), Decimal("0")), float("inf"))

    def test_through_liq_is_zero(self):
        self.assertEqual(rrm.distance_to_liquidation_pct("Long", Decimal("90"), Decimal("95")), 0.0)

    def test_missing_data(self):
        self.assertIsNone(rrm.distance_to_liquidation_pct("Long", None, Decimal("1")))
        self.assertIsNone(rrm.distance_to_liquidation_pct("Long", Decimal("1"), None))

    def test_hysteresis(self):
        t = {"warning": 15, "danger": 8, "critical": 4}
        self.assertEqual(rrm.next_tier(4.5, "critical", t, 1.0), "critical")  # not recovered enough
        self.assertEqual(rrm.next_tier(5.5, "critical", t, 1.0), "danger")
        self.assertEqual(rrm.next_tier(30, "critical", t, 1.0), "ok")
        self.assertEqual(rrm.next_tier(3, "ok", t, 1.0), "critical")  # worsening is immediate


class MonitorTests(unittest.TestCase):
    def test_real_position_starts_critical(self):
        mon, api, rec, _ = make_monitor()
        api.positions = [REAL_LONG]
        quiet(mon.poll)
        self.assertEqual(len(rec.alerts), 1)
        self.assertEqual(rec.alerts[0].level, "critical")
        self.assertIn("BTC-PERP Long", rec.alerts[0].title)
        self.assertTrue(rec.alerts[0].ping)

    def test_escalation_reminder_recovery(self):
        mon, api, rec, clock = make_monitor()
        api.positions = [position(100, 50)]  # 50% away
        quiet(mon.poll)
        self.assertEqual(rec.alerts, [])

        api.positions = [position(100, 88)]  # 12% -> warning
        quiet(mon.poll)
        self.assertEqual(rec.alerts[-1].level, "warning")

        api.positions = [position(100, 93)]  # 7% -> danger
        quiet(mon.poll)
        self.assertEqual(rec.alerts[-1].level, "danger")

        n = len(rec.alerts)
        clock.advance(10)
        quiet(mon.poll)  # same tier, within repeat window
        self.assertEqual(len(rec.alerts), n)
        clock.advance(25)
        quiet(mon.poll)  # 35 min > 30 min repeat
        self.assertEqual(len(rec.alerts), n + 1)
        self.assertIn("Reminder", rec.alerts[-1].lines[-1])

        api.positions = [position(100, 97)]  # 3% -> critical
        quiet(mon.poll)
        self.assertEqual(rec.alerts[-1].level, "critical")

        api.positions = [position(100, 80)]  # 20% -> back to ok
        quiet(mon.poll)
        self.assertEqual(rec.alerts[-1].level, "ok")
        self.assertIn("back to safe", rec.alerts[-1].title)

    def test_short_side(self):
        mon, api, rec, _ = make_monitor()
        api.positions = [position(100, 103, direction="Short")]
        quiet(mon.poll)
        self.assertEqual(rec.alerts[-1].level, "critical")
        self.assertIn("rise", rec.alerts[-1].lines[0])

    def test_open_close_and_liquidation(self):
        mon, api, rec, _ = make_monitor()
        api.liquidations = [{"liquidation_id": 1, "account_id": ACCT, "positions": []}]  # history
        quiet(mon.poll)
        self.assertEqual(rec.alerts, [])  # old liquidation not reported

        api.positions = [position(100, 50)]
        quiet(mon.poll)
        self.assertIn("Position opened", rec.titles()[-1])

        api.positions = []
        quiet(mon.poll)
        self.assertIn("Position closed", rec.titles()[-1])

        api.positions = [position(100, 50)]
        quiet(mon.poll)
        api.positions = []
        api.liquidations.insert(0, {"liquidation_id": 2, "account_id": ACCT, "positions": [
            {"market_id": BTC, "liquidated_position_size": "0.0047", "status": "FullPosition"}]})
        quiet(mon.poll)
        self.assertEqual(rec.alerts[-1].level, "liquidated")
        self.assertIn("fully liquidated", rec.alerts[-1].lines[0])
        self.assertNotIn("Position closed", rec.titles()[-1])

    def test_stale_price(self):
        mon, api, rec, _ = make_monitor()
        api.positions = [position(100, 99, live={"has_stale_price": True})]
        quiet(mon.poll)
        self.assertIn("Stale price", rec.titles()[-1])
        n = len(rec.alerts)
        quiet(mon.poll)
        self.assertEqual(len(rec.alerts), n)  # not repeated
        api.positions = [position(100, 50)]
        quiet(mon.poll)
        self.assertIn("Price feed back", rec.titles()[-1])

    def test_api_down_alert(self):
        mon, api, rec, clock = make_monitor()
        quiet(mon.poll)
        api.fail = True
        quiet(mon.poll)
        self.assertEqual(rec.alerts, [])
        clock.advance(4)
        quiet(mon.poll)
        self.assertIn("can't reach", rec.titles()[-1])
        api.fail = False
        quiet(mon.poll)
        self.assertIn("reconnected", rec.titles()[-1])

    def test_ignores_pending_and_other_accounts(self):
        mon, api, rec, _ = make_monitor()
        other = position(100, 99)
        other["account_id"] = "0xdead"
        pending = position(100, 99, status="Pending")
        api.positions = [other, pending]
        quiet(mon.poll)
        self.assertEqual(rec.alerts, [])

    def test_status_table(self):
        mon, api, rec, _ = make_monitor()
        api.positions = [REAL_LONG]
        out = io.StringIO()
        with redirect_stdout(out):
            mon.poll()
        text = out.getvalue()
        self.assertIn("BTC-PERP", text)
        self.assertIn("1.36%", text)
        self.assertIn("84,144.93", text)


class DashboardTests(unittest.TestCase):
    def make_dashboard(self):
        import dashboard as dash
        mon, api, rec, _ = make_monitor()
        api.positions = [REAL_LONG]
        fills = [{"created_at_ms": 2, "market_id": BTC, "direction": "Long", "price": "86000", "volume": "0.002",
                  "filled_value": "172", "is_taker": True, "order_type": "Market", "trade_context": "CloseShort",
                  "realised_pnl": "-0.6", "realised_pnl_attribution": {"trade_pnl": "-0.57", "trade_fee": "-0.03"}},
                 {"created_at_ms": 1, "market_id": BTC, "direction": "Short", "price": "85500", "volume": "0.002",
                  "filled_value": "171", "is_taker": True, "order_type": "Market", "trade_context": "OpenShort",
                  "realised_pnl": "-0.03", "realised_pnl_attribution": {"trade_pnl": "0", "trade_fee": "-0.03"}}]
        routes = {
            "/api/v1/markets": {"data": [{"id": BTC, "risk_engine": {"mmr": "0.0125"}, "fees": {"taker_rates": ["0.00019"]},
                                          "order_rules": {"min_order_value": "10"}}], "pagination": {}},
            f"/api/v1/markets/{BTC}/depth": {"data": {"buys": [{"price_level": "99", "volume": "1"}],
                                                      "sells": [{"price_level": "101", "volume": "2"}]}},
            f"/api/v1/accounts/{ACCT}/orders": {"data": [{"side": "Buy", "order_type": "Limit", "price": "90",
                                                         "quantity": "0.01", "filled_quantity": "0", "market_id": BTC}],
                                                "pagination": {}},
            f"/api/v1/accounts/{ACCT}/fills": {"data": fills, "pagination": {}},
            "/api/v1/funding-payments": {"data": [{"updated_at_ms": 3, "payment": "0.002", "rate": "0.0000125",
                                                   "market_id": BTC}], "pagination": {}},
        }
        api._get = lambda path, params=None, retries=3: routes.get(path, {"data": [], "pagination": {}})
        api._paginate = lambda path, params, max_pages=50: routes.get(path, {"data": []})["data"]
        d = dash.Dashboard(mon.cfg, mon)
        quiet(mon.poll)
        d.refresh()
        return d

    def test_state_has_everything_the_page_needs(self):
        state = json.loads(json.dumps(self.make_dashboard().state()))
        p = state["positions"][0]
        self.assertAlmostEqual(p["distance"], 1.3616, places=3)
        self.assertEqual(p["account_equity"], 100.0)
        m = state["markets"][BTC]
        self.assertEqual(m["mmr"], 0.0125)
        self.assertEqual(m["bids"], [[99.0, 1.0]])
        self.assertEqual(m["asks"], [[101.0, 2.0]])
        self.assertEqual(state["orders"][0]["side"], "Buy")
        self.assertEqual(state["fills"][0]["context"], "CloseShort")
        self.assertNotIn("alerts", state)

    def test_performance_history(self):
        perf = json.loads(json.dumps(self.make_dashboard().performance()))
        self.assertTrue(perf["loaded"])
        self.assertEqual([f["t"] for f in perf["fills"]], [2, 1])  # newest first
        self.assertAlmostEqual(sum(f["pnl"] for f in perf["fills"]) + perf["funding"][0]["payment"], -0.628)
        self.assertAlmostEqual(perf["fills"][0]["fee"], -0.03)

    def test_whatif_formula_matches_real(self):
        # Liquidation: C + s*sig*(P - M) = m*s*P  ->  P = (C - sig*s*M) / (s*(m - sig)).
        # Numbers from the user's live short on REAL: equity 44.95, 0.002 BTC, mark 86,062.83,
        # REAL's estimated liquidation price 107,188.26, maintenance margin 1.25%.
        C, s, M, sig, real_liq = 44.95, 0.002, 86062.83, -1, 107188.26
        liq = lambda m, c=C, size=s: (c - sig * size * M) / (size * (m - sig))
        self.assertLess(abs(liq(0.0125) / real_liq - 1), 0.0002)      # textbook formula within 0.02%
        m_cal = sig + (C - sig * s * M) / (s * real_liq)              # what the page calibrates to
        self.assertAlmostEqual(liq(m_cal), real_liq, places=6)
        self.assertAlmostEqual(liq(m_cal, c=C + 20), 117063.65, delta=0.5)   # +20 USDT margin
        self.assertGreater(liq(m_cal, size=0.001), liq(m_cal))                # smaller short -> further liq

    def test_aggregate_hides_accounts_and_skips_unliquidatable(self):
        import dashboard as dash
        raw = [
            position(100, 90),                                   # long, liq 90
            position(100, 110, direction="Short"),               # short, liq 110
            position(100, 0),                                    # long that can't be liquidated
            position(100, 95, live={"has_stale_price": True}),   # stale price
            position(100, 80, status="Pending"),                 # not open
        ]
        out = dash.aggregate_positions(raw)[BTC]
        self.assertEqual(out["open"], 4)
        self.assertEqual(out["no_liq"], 1)
        self.assertEqual(out["stale"], 1)
        self.assertEqual(sorted(p[0] for p in out["positions"]), [-1, 1])
        self.assertNotIn(ACCT, json.dumps(out))  # no account IDs leak into the map


if __name__ == "__main__":
    unittest.main()
