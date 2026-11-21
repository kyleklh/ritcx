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
        va.place = lambda tk, side, q, limit=None: self.orders.append((tk, side, int(q))) or int(q)

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

    def test_gross_room_uses_combined_position(self):
        info = make_info({"RTM48C": {"pos": 0, "pos_total": 1200},
                          "RTM48P": {"pos": 0, "pos_total": -1200},
                          "RTM50C": {"pos": -100, "pos_total": 100, "buy_edge": 0.5}})
        va.trade_options(info, 50, gcap=1e9)
        self.assertFalse(self.orders)


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
        va.place = lambda tk, side, q, limit=None: self.orders.append((tk, side, int(q))) or int(q)

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
        self.assertEqual(info["RTM48C"]["pos_total"], 0)

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

    def fake_place(self, tk, side, q, limit=None):
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

    def test_floor_quoted_strike_is_ignored(self):
        # live heat 130427, t=253: 52C quoted 0.01/0.03 at 8% vol made strike 52 look 11 pts rich. Fake gap.
        info = self.info({52: 0.40})
        info["RTM52C"]["bid"] = 0.01
        va.arb_step(info, 10, self.S, self.T)
        self.assertNotIn("RTM52C", {t for t, s, q in self.orders})
        self.assertNotIn("RTM52P", {t for t, s, q in self.orders})

    def test_unwinds_when_gap_closes(self):
        va.arb_step(self.info({49: 0.20, 51: 0.30}), 10, self.S, self.T)
        self.orders.clear()
        va.arb_step(self.info({49: 0.255, 51: 0.245}), 11, self.S, self.T)  # gap closed (1 pt the other way: too small to re-enter)
        self.assertIsNone(va.ST.arb)
        self.assertFalse(any(va.ST.arb_pos.values()))
        self.assertEqual(self.net_of(), 0)

    def test_missing_iv_does_not_signal_convergence(self):
        va.arb_step(self.info({49: 0.20, 51: 0.30}), 10, self.S, self.T)
        self.orders.clear()
        info = self.info({49: 0.20, 51: 0.30})
        info["RTM49C"]["iv"] = None
        va.arb_step(info, 11, self.S, self.T)
        self.assertIsNotNone(va.ST.arb)
        self.assertEqual(self.orders, [])

    def test_basket_does_not_submit_zero_price(self):
        info = self.info({})
        info["RTM49C"]["bid"] = 0
        info["RTM51C"]["ask"] = 0
        va.arb_orders(info, 10, {"RTM51C": 100}, {"RTM49C": 100})
        self.assertEqual(self.orders, [])


class EngineStuckTest(unittest.TestCase):
    """Heat 20261120_123405: a pair went in lopsided (limit buys missed), the main book filled the net limit,
    and the engine retried its unwind every loop for 80+ ticks."""
    S, T = 50.0, 0.06

    def setUp(self):
        va.ST.reset()
        va.ST.run_delta = 0
        va.ST.rtm_pos = 0
        self.orders = []
        self._place = va.place

    def tearDown(self):
        va.place = self._place

    def info(self, ivs):
        sec = {}
        for k in va.STRIKES:
            for f, tk in (("c", va.CALLS[k]), ("p", va.PUTS[k])):
                px = va.bs.price(f, self.S, k, self.T, ivs.get(k, 0.25))
                sec[tk] = {"bid": round(px - 0.01, 2), "ask": round(px + 0.01, 2), "position": 0}
        return va.option_info(sec, self.S, self.T, 0.25)

    def test_lopsided_entry_is_evened_up(self):
        def place(tk, side, q, limit=None):
            self.orders.append((tk, side, q, limit))
            return 0 if (tk != "RTM" and side == "BUY" and limit is not None) else int(q)   # limit buys all miss
        va.place = place
        va.arb_step(self.info({49: 0.20, 51: 0.30}), 10, self.S, self.T)
        self.assertEqual(sum(va.ST.arb_pos.values()), 0)                  # never lopsided
        self.assertFalse(any(va.ST.arb_pos.values()))                     # sold legs were bought back
        self.assertIsNone(va.ST.arb)

    def test_stuck_unwind_hands_over_after_three_tries(self):
        rejected = []
        def place(tk, side, q, limit=None):
            if tk != "RTM" and side == "BUY":
                rejected.append(tk)
                return 0                                                  # every buy hits the net limit
            return int(q)
        va.place = place
        va.ST.arb_pos = {"RTM50C": -300, "RTM50P": -400, "RTM48C": 100, "RTM48P": 169}
        va.ST.arb = {"long_k": 48, "short_k": 50, "t0": 2}
        for t in (60, 61, 62, 63, 64):
            va.arb_step(self.info({48: 0.255, 50: 0.245}), t, self.S, self.T)
        self.assertIsNone(va.ST.arb)
        self.assertFalse(any(va.ST.arb_pos.values()))
        self.assertLessEqual(len(rejected), 3 * 8)                        # three tries, then it stops

    def test_main_book_counts_engine_legs(self):
        bought = []

        def place(tk, side, q, limit=None):
            if side == "BUY" and tk != "RTM":
                bought.append(q)
            return int(q)
        va.place = place
        # main book 850 (delta-neutral), engine legs net +100 -> total 950: no room left for the main book
        info = make_info({"RTM48C": {"pos": 425, "pos_total": 425}, "RTM48P": {"pos": 425, "pos_total": 425},
                          "RTM49C": {"pos": 0, "pos_total": 400}, "RTM51P": {"pos": 0, "pos_total": -300},
                          "RTM50C": {"buy_edge": 0.5}})
        va.trade_options(info, 50, gcap=1e9)
        self.assertEqual(sum(bought), 0)

if __name__ == "__main__":
    unittest.main()
