"""Offline checks for ETF decisions; these tests never contact the RIT server."""
import unittest
from unittest.mock import Mock, patch

import requests
import etf_algo as bot


def sample_snap():
    return {"pos": {"RITC": 10000, "BULL": -10000, "BEAR": -10000},
            "q": {"USD": (1.0, 1.0), "RITC": (24.0, 24.02),
                  "BULL": (9.98, 10.0), "BEAR": (14.98, 15.0)},
            "books": {"RITC": {"bids": [(24.0, 10000)], "asks": [(24.02, 10000)]},
                      "BULL": {"bids": [(9.98, 10000)], "asks": [(10.0, 10000)]},
                      "BEAR": {"bids": [(14.98, 10000)], "asks": [(15.0, 10000)]}},
            "gross_limit": 300000, "net_limit": 200000, "tick": 50, "ticks": 300}


class ETFDecisionTests(unittest.TestCase):
    def setUp(self):
        bot.ST.unknown_order_tickers = set()
        bot.ST.unknown_tender = False
        bot.ST.inflight = {}
        bot.ST.conv_hold_until = -1
        bot.ST.now_tick = 50
        bot.ST.last_conversion_alert = {}
        bot.ST.basket_hist = {}
        bot.ST.arb_extra = 0

    def test_converter_api_cannot_be_enabled(self):
        with patch.object(bot.s, "request") as request:
            self.assertIsNone(bot._req("POST", "/leases/1", from1="RITC"))
            bot.lease_converters(2)
            self.assertFalse(bot._use_converter("redeem"))
            request.assert_not_called()

    def test_mutating_timeout_is_not_retried_and_blocks_ticker(self):
        with patch.object(bot.s, "request", side_effect=requests.Timeout("unknown")) as request:
            self.assertIsNone(bot._req("POST", "/orders", ticker="RITC"))
            self.assertEqual(request.call_count, 1)
        self.assertTrue(bot.busy("RITC"))
        self.assertTrue(bot.still_hedging(sample_snap()))

    def test_uncertain_tender_does_not_retry_or_open_new_risk(self):
        with patch.object(bot.s, "request", side_effect=requests.Timeout("unknown")) as request:
            self.assertIsNone(bot._req("POST", "/tenders/1"))
            self.assertEqual(request.call_count, 1)
        self.assertTrue(bot.ST.unknown_tender)
        self.assertTrue(bot.still_hedging(sample_snap()))
        self.assertFalse(bot.arb(sample_snap(), 50))

    def test_unreadable_tender_reply_is_not_assumed_accepted(self):
        response = Mock(ok=True)
        response.json.side_effect = ValueError("malformed")
        self.assertFalse(bot.tender_response_ok(response))
        self.assertTrue(bot.ST.unknown_tender)

    def test_unreadable_order_reply_blocks_ticker(self):
        response = Mock(ok=True)
        response.json.side_effect = ValueError("malformed")
        with patch.object(bot, "_req", return_value=response):
            self.assertEqual(bot.market("RITC", "BUY", 100, 0), 0)
        self.assertIn("RITC", bot.ST.unknown_order_tickers)

    def test_partial_market_order_uses_reported_fill(self):
        response = Mock(ok=True)
        response.json.return_value = {"status": "OPEN", "quantity_filled": 2000, "vwap": 24.0}
        with patch.object(bot, "_req", return_value=response), patch.object(bot, "jlog"):
            self.assertEqual(bot.market("RITC", "BUY", 10000, 0), 2000)
        self.assertEqual(bot.ST.inflight["RITC"]["total"], 2000)
        self.assertIn("RITC", bot.ST.unknown_order_tickers)

    def test_unseen_fill_remains_blocked_after_timeout(self):
        bot.ST.inflight = {"RITC": {"base": 0, "total": 10000, "t": 0}}
        with patch.object(bot.time, "time", return_value=100):
            bot.check_inflight({"RITC": 0})
        self.assertTrue(bot.busy("RITC"))
        self.assertIn("RITC", bot.ST.inflight)
        self.assertIn("RITC", bot.ST.unknown_order_tickers)

    def test_depth_model_is_conservative_and_fallback_exists(self):
        levels = [(9.98, 1000), (9.60, 30000)]
        with patch.object(bot, "TENDER_BOOK_PRICING", True):
            deep = bot.leg_value(levels, 10.0, 10000, -1, 1.0)
        with patch.object(bot, "TENDER_BOOK_PRICING", False):
            fixed = bot.leg_value(levels, 10.0, 10000, -1, 1.0)
        self.assertLess(deep, fixed)
        self.assertAlmostEqual(fixed, 10.0 - bot.TENDER_HEDGE_COST / 2)

    def test_invalid_fixed_tender_cannot_be_accepted(self):
        snap = sample_snap()
        invalid = {"ticker": "RITC", "action": "BUY", "quantity": 10000,
                   "price": None, "is_fixed_bid": True}
        self.assertEqual(bot.evaluate_tender(invalid, snap)[2], "invalid tender")
        invalid.update(price=1.0, action="UNKNOWN")
        self.assertEqual(bot.evaluate_tender(invalid, snap)[2], "invalid tender")

    def test_good_and_bad_tenders_respect_edge(self):
        snap = sample_snap()
        snap["pos"] = {"RITC": 0, "BULL": 0, "BEAR": 0}
        t = {"ticker": "RITC", "action": "BUY", "quantity": 1000,
             "price": 24.0, "is_fixed_bid": True}
        self.assertTrue(bot.evaluate_tender(t, snap)[2].startswith("ok"))
        t["price"] = 26.0
        self.assertTrue(bot.evaluate_tender(t, snap)[2].startswith("edge too small"))

    def test_hedge_does_not_stack_after_cancel_request(self):
        snap = sample_snap()
        snap["pos"] = {"RITC": 10000, "BULL": 0, "BEAR": -10000}
        bot.ST.fast_until = -1
        bot.ST.mm_hedge_until = -1
        bot.ST.gap_since = {}
        orders = [{"ticker": "BULL", "order_id": 1, "action": "SELL", "quantity": 10000,
                   "quantity_filled": 0, "price": 9.98}]
        with patch.object(bot, "_req", return_value=Mock(ok=True)) as req, patch.object(bot, "market") as market:
            bot.hedge(snap, 50, orders=orders)
            req.assert_called_once_with("POST", "/commands/cancel", ticker="BULL")
            market.assert_not_called()

    def test_unknown_open_order_list_does_not_trigger_hedges(self):
        snap = sample_snap()
        with patch.object(bot, "open_orders", return_value=None), patch.object(bot, "market") as market:
            self.assertFalse(bot.hedge(snap, 50))
            market.assert_not_called()

    def test_snapshot_uses_stock_limit_even_when_cash_appears_first(self):
        securities = [{"ticker": tk, "position": 0, "bid": 1, "ask": 1}
                      for tk in ("RITC", "BULL", "BEAR", "USD")]
        def fake_get(path, **params):
            if path == "/securities":
                return securities
            if path == "/securities/book":
                return {"bids": [], "asks": []}
            if path == "/limits":
                return [{"name": "LIMIT-CASH", "gross_limit": 10000000, "net_limit": 10000000},
                        {"name": "LIMIT-STOCK", "gross_limit": 300000, "net_limit": 200000}]
            self.fail(f"unexpected request {path}")
        with patch.object(bot, "get", side_effect=fake_get):
            snap = bot.snapshot()
        self.assertEqual((snap["gross_limit"], snap["net_limit"]), (300000, 200000))

    def test_conversion_alert_is_manual_and_fee_aware(self):
        snap = sample_snap()
        with patch.object(bot, "event") as event, patch.object(bot.s, "request") as request:
            bot.conversion_opportunity(snap, 50)
            event.assert_called_once()
            self.assertIn("manual redeem", event.call_args.args[2])
            request.assert_not_called()

    def test_adaptive_threshold_never_reduces_base(self):
        snap = sample_snap()
        base = bot.ARB_OPEN_EDGE + bot.ST.arb_extra
        with patch.object(bot, "ADAPTIVE_ARB", True):
            self.assertGreaterEqual(bot.arb_open_threshold(snap, 1, 20000, 250), base)
        with patch.object(bot, "ADAPTIVE_ARB", False):
            self.assertEqual(bot.arb_open_threshold(snap, 1, 20000, 250), base)

    def test_executable_pair_edge_converts_usd_fee(self):
        snap = sample_snap()
        snap["q"]["USD"] = (1.1, 1.2)
        sell = 24.0 * 1.1 - 10.0 - 15.0 - bot.FEE * (2 + 1.1)
        buy = 9.98 + 14.98 - 24.02 * 1.2 - bot.FEE * (2 + 1.2)
        self.assertAlmostEqual(bot.pair_edge(snap, -1, 10000), sell)
        self.assertAlmostEqual(bot.pair_edge(snap, 1, 10000), buy)


if __name__ == "__main__":
    unittest.main()
