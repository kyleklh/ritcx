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
        self.assertEqual(self.sold("RTM49C"), 400)   # 4 orders x 100


class FairVolTest(unittest.TestCase):
    def setUp(self):
        va.ST.reset()

    def test_unknown_week_uses_latest_forecast(self):
        # Run 1, t=115: week 2 known 33%, week 3 forecast 23.5%, week 4 unknown.
        va.ST.vol_known = {1: 0.30, 2: 0.33}
        va.ST.vol_est = {3: 0.235}
        self.assertAlmostEqual(va.vol_for_week(4), 0.235)
        self.assertAlmostEqual(va.fair_vol(115), 0.2557, places=3)   # was 29.5% (week 4 assumed 33%)

    def test_known_beats_estimate(self):
        va.ST.vol_known = {2: 0.33, 3: 0.23}
        va.ST.vol_est = {3: 0.235}
        self.assertAlmostEqual(va.vol_for_week(4), 0.23)


if __name__ == "__main__":
    unittest.main()
