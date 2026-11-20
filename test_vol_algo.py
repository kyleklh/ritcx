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
    for o in info.values():
        o.setdefault("pos_total", o["pos"])
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


class StrikeArbTest(unittest.TestCase):
    """Strike-vs-strike engine: S=50, T=0.06, all strikes 25% vol except the ones a test moves."""
    S, T = 50.0, 0.06

    def setUp(self):
        va.ST.reset()
        va.ST.run_delta = 0
        va.ST.rtm_pos = 0
        self.orders, self.net_path = [], []
        self._place = va.place
        va.place = self.fake_place

    def tearDown(self):
        va.place = self._place

    def fake_place(self, tk, side, q):
        self.orders.append((tk, side, int(q)))
        return int(q)

    def info(self, ivs, main_net=0):
        sec = {}
        for k in va.STRIKES:
            for f, tk in (("c", va.CALLS[k]), ("p", va.PUTS[k])):
                px = va.bs.price(f, self.S, k, self.T, ivs.get(k, 0.25))
                sec[tk] = {"bid": round(px - 0.01, 2), "ask": round(px + 0.01, 2), "position": 0}
        sec["RTM50C"]["position"] = main_net
        return va.option_info(sec, self.S, self.T, 0.25)

    def net_of(self):
        return sum(q if s == "BUY" else -q for t, s, q in self.orders if t != "RTM")

    def test_straddle_is_delta_neutral(self):
        info = self.info({})
        for k in va.STRIKES:
            legs = va.straddle(info, k, 700)
            d = sum(n * info[tk]["delta"] for tk, n in legs.items())
            self.assertEqual(sum(legs.values()), 700)
            self.assertLess(abs(d), 1.0, f"strike {k}")

    def test_orders_alternate_so_net_never_runs_away(self):
        info = self.info({}, main_net=950)
        va.arb_orders(info, 10, {"RTM49C": 300, "RTM49P": 400}, {"RTM51C": 350, "RTM51P": 350})
        running, worst = 0, 0
        for tk, side, q in self.orders:
            if tk != "RTM":
                running += q if side == "BUY" else -q
                worst = max(worst, running)
        self.assertLessEqual(worst, 0)                 # main book is long 950: sells go first, never above it
        self.assertEqual(self.net_of(), 0)

    def test_enters_on_big_gap_buying_cheap_selling_rich(self):
        info = self.info({49: 0.20, 51: 0.30})
        va.arb_step(info, 10, self.S, self.T)
        self.assertEqual(va.ST.arb["long_k"], 49)
        self.assertEqual(va.ST.arb["short_k"], 51)
        self.assertEqual(self.net_of(), 0)
        self.assertEqual(sum(n for tk, n in va.ST.arb_pos.items() if tk.startswith("RTM49")), 700)
        self.assertEqual(sum(n for tk, n in va.ST.arb_pos.items() if tk.startswith("RTM51")), -700)

    def test_small_gap_is_ignored(self):
        info = self.info({49: 0.248, 51: 0.252})        # 0.4 pts: a few $ per contract, below the threshold
        va.arb_step(info, 10, self.S, self.T)
        self.assertIsNone(va.ST.arb)
        self.assertEqual(self.orders, [])

    def test_unwinds_when_gap_closes(self):
        va.arb_step(self.info({49: 0.20, 51: 0.30}), 10, self.S, self.T)
        self.orders.clear()
        va.arb_step(self.info({49: 0.255, 51: 0.245}), 11, self.S, self.T)  # gap closed (1 pt the other way: too small to re-enter)
        self.assertIsNone(va.ST.arb)
        self.assertFalse(any(va.ST.arb_pos.values()))
        self.assertEqual(self.net_of(), 0)


class StrikeArbLimitTest(StrikeArbTest):
    """Same as StrikeArbTest but the fake server enforces the 1,000 net limit like RIT does."""
    def fake_place(self, tk, side, q):
        if tk != "RTM":
            sgn = 1 if side == "BUY" else -1
            if abs(self.total_net + sgn * q) > 1000:
                self.orders.append((tk, "REJECTED", int(q)))
                return 0
            self.total_net += sgn * q
        self.orders.append((tk, side, int(q)))
        return int(q)

    def setUp(self):
        super().setUp()
        self.total_net = 0

    def test_lopsided_leftovers_unwind_with_main_book_at_limit(self):
        # main book long 950; the engine was left holding a lopsided pair (more shorts than longs)
        self.total_net = 950 + 300 - 500
        va.ST.arb_pos = {"RTM49C": 120, "RTM49P": 180, "RTM51C": -300, "RTM51P": -200}
        va.ST.arb = {"long_k": 49, "short_k": 51, "t0": 5}
        info = self.info({49: 0.255, 51: 0.245}, main_net=950)
        for tk, n in va.ST.arb_pos.items():
            info[tk]["pos_total"] += n
        va.arb_step(info, 11, self.S, self.T)
        self.assertFalse(any(va.ST.arb_pos.values()))
        self.assertIsNone(va.ST.arb)
        self.assertNotIn("REJECTED", [s for _, s, _ in self.orders])

    def test_main_book_leaves_room_for_engine_legs(self):
        # engine holds a pair that is net +100; the main book must count it and stop at 950 total
        va.ST.arb_pos = {"RTM49C": 400, "RTM51C": -300}
        info = make_info({"RTM48C": {"pos": 850, "pos_total": 850}, "RTM49C": {"pos": 0, "pos_total": 400},
                          "RTM51C": {"pos": 0, "pos_total": -300}, "RTM50C": {"buy_edge": 0.5}})
        va.trade_options(info, 50, gcap=1e9)
        self.assertEqual(sum(q for tk, sd, q in self.orders if sd == "BUY" and tk != "RTM"), 0)

if __name__ == "__main__":
    unittest.main()
