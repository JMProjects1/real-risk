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

# Shape taken from a mainnet /api/v1/positions/live response (account ID replaced).
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

    def test_whatif_maths_in_node(self):
        # The calculator maths lives in riskmath.js and is tested there, against the same code the page runs.
        import shutil, subprocess, os
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js not installed")
        here = os.path.dirname(os.path.abspath(__file__))
        out = subprocess.run([node, "test_riskmath.js"], cwd=here, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_not_loaded_until_first_successful_update(self):
        import dashboard as dash
        mon, api, rec, _ = make_monitor()
        api.fail = True                       # REAL unreachable from the start
        d = dash.Dashboard(mon.cfg, mon)
        quiet(mon.poll)
        state = json.loads(json.dumps(d.state(), allow_nan=False))
        self.assertFalse(state["loaded"])     # the page must not claim "No open positions"
        self.assertFalse(state["status"]["ok"])
        self.assertIn("boom", state["status"]["error"])
        api.fail = False
        api._paginate = lambda path, params, max_pages=50: []
        api._get = lambda path, params=None, retries=3: {"data": []}
        quiet(mon.poll)
        d.refresh()
        self.assertTrue(d.state()["loaded"])

    def test_wrong_account_id_is_reported_per_account(self):
        import dashboard as dash
        cfg = rrm.load_config(None)
        cfg["accounts"] = [ACCT, "0x" + "f" * 64]
        cfg["status_every_minutes"] = 0
        api = FakeApi()
        good_account = api.account

        def account(aid):
            if aid == "0x" + "f" * 64:
                raise rrm.ApiError("HTTP 404", 404, "not_found")
            return good_account(aid)
        api.account = account
        api.accounts_for_address = lambda addr: [{"id": "0x" + "b" * 64}]
        api._paginate = lambda path, params, max_pages=50: []
        api._get = lambda path, params=None, retries=3: {"data": []}
        mon = rrm.RiskMonitor(cfg, api, [], clock=Clock())
        d = dash.Dashboard(cfg, mon)
        quiet(mon.poll)
        d.refresh()
        state = d.state()
        bad = next(a for a in state["accounts"] if a["id"] == "0x" + "f" * 64)
        good = next(a for a in state["accounts"] if a["id"] == ACCT)
        self.assertIn("wallet address", bad["error"])
        self.assertIn("0x" + "b" * 64, bad["error"])
        self.assertIsNone(good["error"])
        self.assertEqual(good["equity"], 100.0)   # the good account still loads
        self.assertTrue(state["status"]["ok"])

    def test_history_is_fetched_incrementally(self):
        d = self.make_dashboard()
        calls = []
        new_fill = {"account_id": ACCT, "trade_id": "99", "created_at_ms": 5, "market_id": BTC, "direction": "Long",
                    "price": "1", "volume": "1", "realised_pnl": "0", "realised_pnl_attribution": {}}

        def paginate(path, params, max_pages=50):
            calls.append((path, max_pages))
            return [new_fill] if path.endswith("/fills") else []
        d.api._paginate = paginate
        d.refresh_history([ACCT])
        self.assertTrue(all(pages == 1 for _, pages in calls), calls)   # only the newest page after the first load
        self.assertEqual([f["t"] for f in d.performance()["fills"]], [5, 2, 1])

    def test_nan_from_api_becomes_none(self):
        import dashboard as dash
        self.assertIsNone(dash.f("NaN"))
        self.assertIsNone(dash.f("Infinity"))
        self.assertEqual(dash.f("1.5"), 1.5)

    def test_performance_uses_reals_lifetime_total(self):
        perf = self.make_dashboard().performance()
        self.assertEqual(perf["account_realised_pnl"], None)   # FakeApi account has no realised_pnl
        self.assertEqual(perf["truncated"], {"fills": False, "funding": False})

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


class SetupTests(unittest.TestCase):
    def setUp(self):
        import tempfile, os
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "config.json")

    def run_prompt(self, answers, api):
        import dashboard as dash
        it = iter(answers)
        with redirect_stdout(io.StringIO()) as out:
            result = dash.ask_for_account(self.path, api, read=lambda _: next(it))
        return result, out.getvalue()

    class Api:
        def __init__(self):
            self.looked_up = []

        def account(self, aid):
            self.looked_up.append(aid)
            if aid != "0x" + "c" * 64:
                raise rrm.ApiError("HTTP 404", 404, "not_found")
            return {}

        def accounts_for_address(self, addr):
            self.looked_up.append(addr)
            return [{"id": "0x" + "c" * 64}] if addr == "0x" + "d" * 64 else []

    def test_private_key_and_phrase_are_refused_and_never_sent(self):
        api = self.Api()
        key = "iotaprivkey1qq" + "x" * 50
        phrase = "abandon ability able about above absent absorb abstract absurd abuse access accident"
        result, out = self.run_prompt([key, phrase, "0x" + "c" * 64], api)
        self.assertIn("PRIVATE KEY", out)
        self.assertIn("RECOVERY PHRASE", out)
        self.assertNotIn(key, api.looked_up)
        self.assertEqual(result, "0x" + "c" * 64)
        with open(self.path) as fh:
            self.assertNotIn("privkey", fh.read())

    def test_wallet_address_resolves_to_account(self):
        result, out = self.run_prompt(["0x" + "d" * 64], self.Api())
        self.assertIn("wallet address", out)
        self.assertEqual(result, "0x" + "c" * 64)

    def test_unknown_id_asks_again(self):
        result, out = self.run_prompt(["0x" + "e" * 64, "not an id", "0x" + "c" * 64], self.Api())
        self.assertIn("no account with that ID", out)
        self.assertIn("doesn't look like an Account ID", out)
        self.assertEqual(result, "0x" + "c" * 64)

    def test_config_typo_gives_readable_error(self):
        with open(self.path, "w") as fh:
            fh.write('{\n  "accounts": ["0xabc"],\n}\n')
        with self.assertRaises(ValueError) as ctx:
            rrm.load_config(self.path)
        self.assertRegex(str(ctx.exception), r"line [23]")   # Python versions point at the comma or the brace
        self.assertIn("comma", str(ctx.exception))


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading
        import dashboard as dash
        mon, api, rec, _ = make_monitor()
        api._paginate = lambda path, params, max_pages=50: []
        api._get = lambda path, params=None, retries=3: {"data": []}
        cls.server = dash.LocalServer(("127.0.0.1", 0), dash.make_handler(dash.Dashboard(mon.cfg, mon)))
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path, host=None):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path, headers={"Host": host or f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        return r.status, r.read()

    def test_serves_page_maths_and_fonts(self):
        self.assertEqual(self.get("/")[0], 200)
        status, body = self.get("/riskmath.js")
        self.assertEqual(status, 200)
        self.assertIn(b"liqPrice", body)
        self.assertEqual(self.get("/fonts/barlow-latin-400-normal.woff2")[0], 200)
        self.assertEqual(self.get("/api/state")[0], 200)

    def test_rejects_other_hosts_and_paths(self):
        self.assertEqual(self.get("/api/state", host="evil.example")[0], 403)     # DNS rebinding
        self.assertEqual(self.get("/fonts/../dashboard.py")[0], 404)
        self.assertEqual(self.get("/config.json")[0], 404)
        self.assertEqual(self.get("/dashboard.py")[0], 404)

    def test_windows_doesnt_share_the_port(self):
        import os
        import dashboard as dash
        self.assertEqual(dash.LocalServer.allow_reuse_address, os.name != "nt")


class ApiErrorTests(unittest.TestCase):
    def test_certificate_errors_are_recognised(self):
        import ssl, urllib.error
        err = urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))
        self.assertTrue(rrm._is_cert_error(err))
        self.assertFalse(rrm._is_cert_error(urllib.error.URLError("timed out")))
        self.assertIn("Install Certificates", rrm.CERT_HELP)


if __name__ == "__main__":
    unittest.main()
