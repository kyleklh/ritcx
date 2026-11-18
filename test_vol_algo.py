"""Unit tests for vol_algo logic that doesn't need a server.   python -m unittest test_vol_algo -v"""
import unittest

import vol_algo as va


def make_info(overrides):
    info = {}
    for k in va.STRIKES:
        for flag, tk in (("c", va.CALLS[k]), ("p", va.PUTS[k])):
            info[tk] = {"K": k, "flag": flag, "bid": 1.0, "ask": 1.02, "iv": 0.3, "fair": 1.01,
                        "buy_edge": -0.05, "sell_edge": -0.05, "delta": 0.5 if flag == "c" else -0.5,
                        "gamma": 0.1, "pos": 0}
    for tk, kv in overrides.items():
        info[tk].update(kv)
    return info


class TradeOptionsTest(unittest.TestCase):
    def setUp(self):
        va.ST.reset()
        va.ST.run_delta = 0
        va.ST.rtm_pos = 0
        self.orders = []
        self._place = va.place
        va.place = lambda tk, side, q: self.orders.append((tk, side, int(q))) or int(q)

    def tearDown(self):
        va.place = self._place

    def sold(self, tk):
        return sum(q for t, s, q in self.orders if t == tk and s == "SELL")

    def test_close_at_small_edge_stops_at_flat(self):
        # Run 1, t=117: long book, 64 x 49C long, $2-3 sell edge. It sold 364 (closed 64, opened 300 short).
        info = make_info({"RTM48C": {"pos": 400}, "RTM49C": {"pos": 64, "sell_edge": 0.03}})
        va.trade_options(info, 117, gcap=1e9)
        self.assertEqual(self.sold("RTM49C"), 64)

    def test_reversal_allowed_with_big_edge(self):
        info = make_info({"RTM48C": {"pos": 400}, "RTM49C": {"pos": 64, "sell_edge": 0.15}})
        va.trade_options(info, 117, gcap=1e9)
        self.assertGreater(self.sold("RTM49C"), 64)

    def test_same_direction_unaffected(self):
        # short book adding more shorts at MIN_EDGE is not a reversal
        info = make_info({"RTM48P": {"pos": -400}, "RTM49C": {"pos": -50, "sell_edge": 0.03}})
        va.trade_options(info, 117, gcap=1e9)
        net_room = va.MAX_NET_CONTRACTS - 450          # book is short 450 already
        self.assertEqual(self.sold("RTM49C"), min(100 * va.MAX_ORDERS_PER_LOOP, net_room))


class EmptyQuoteTest(unittest.TestCase):
    def test_option_with_no_ask_is_not_a_buy(self):
        sec = {tk: {"bid": 0.0, "ask": 0.0, "position": 0} for tk in va.OPTIONS}
        sec["RTM50C"] = {"bid": 1.0, "ask": 0.0, "position": 0}
        info = va.option_info(sec, 50.0, 0.08, 0.26)
        self.assertLess(info["RTM50C"]["buy_edge"], 0)
        self.assertLess(info["RTM48P"]["buy_edge"], 0)
        self.assertIsNone(info["RTM50C"]["iv"])


class FairVolTest(unittest.TestCase):
    def setUp(self):
        va.ST.reset()

    def test_unknown_week_uses_latest_known(self):
        # Run 1, t=115: week 2 known 33%, week 3 forecast 23.5%, week 4 unknown -> 33% (kept on purpose,
        # see vol_for_week). Fair vol matches the 29.5% the live log printed.
        va.ST.vol_known = {1: 0.30, 2: 0.33}
        va.ST.vol_est = {3: 0.235}
        self.assertAlmostEqual(va.vol_for_week(4), 0.33)
        self.assertAlmostEqual(va.fair_vol(115), 0.2952, places=3)

    def test_known_beats_estimate(self):
        va.ST.vol_known = {2: 0.33, 3: 0.23}
        va.ST.vol_est = {3: 0.235}
        self.assertAlmostEqual(va.vol_for_week(4), 0.23)



class GammaTrimTest(unittest.TestCase):
    """gamma 0.1/share -> 10 per contract; gcap 1000 = 100 contracts' worth."""
    def setUp(self):
        va.ST.reset()
        va.ST.run_delta = 0
        va.ST.rtm_pos = 0
        self.orders = []
        self._place, self._max = va.place, va.GAMMA_EDGE_MAX
        va.place = lambda tk, side, q: self.orders.append((tk, side, int(q))) or int(q)

    def tearDown(self):
        va.place, va.GAMMA_EDGE_MAX = self._place, self._max

    def sold(self, tk=None):
        return sum(q for t, s, q in self.orders if s == "SELL" and (tk is None or t == tk))

    RICH = {"fair": 1.30, "bid": 1.00, "ask": 1.02}      # long leg worth $0.30/share more than its bid

    def test_rich_legs_kept_below_hard_cap(self):
        info = make_info({"RTM50C": dict(pos=70, **self.RICH), "RTM50P": dict(pos=60, **self.RICH)})  # 1.3 caps
        va.trim_gamma(info, 230, 1000)
        self.assertEqual(self.sold(), 0)

    def test_low_edge_legs_cut_first(self):
        info = make_info({"RTM50C": dict(pos=100, **self.RICH), "RTM48C": {"pos": 40}})            # 1.4 caps
        va.trim_gamma(info, 230, 1000)
        self.assertEqual(self.sold("RTM50C"), 0)
        self.assertEqual(self.sold("RTM48C"), 40)

    def test_past_hard_cap_trims_back_to_it(self):
        info = make_info({"RTM50C": dict(pos=100, **self.RICH), "RTM50P": dict(pos=100, **self.RICH)})  # 2.0 caps
        va.trim_gamma(info, 230, 1000)
        self.assertEqual(self.sold(), 50)                # back to 1.5 caps, not to 1.0

    def test_off_and_near_expiry_behave_as_before(self):
        for tick, edge_max in ((230, 0.0), (280, 1.5)):
            self.orders.clear()
            va.GAMMA_EDGE_MAX = edge_max
            info = make_info({"RTM50C": dict(pos=70, **self.RICH), "RTM50P": dict(pos=60, **self.RICH)})
            va.trim_gamma(info, tick, 1000)
            self.assertEqual(self.sold(), 30, f"tick {tick} edge_max {edge_max}")

if __name__ == "__main__":
    unittest.main()
