"""
Local mock of the RIT REST API for the RITCx Volatility Trading Case.

Lets you develop and test without practice-server access. Mimics the case rules:
  - RTM follows P_t = P_{t-1} * (1 + r_t), r_t ~ N(0, sigma_week / sqrt(ticks_per_year))
  - 300 ticks = 1 month; 4 weeks of 75 ticks; 3600 ticks per year
  - Market maker prices options with Black-Scholes (r = 0) using a LAGGED vol estimate
    that converges to the true vol (half-life configurable). 2-cent spread, unlimited depth.
  - News: start of each week (true vol), mid-week (range for next week)
  - Fees: $0.02/share RTM, $2/contract options. Limits: RTM 50k gross/net, options 2500 gross / 1000 net
  - Delta penalty: (|delta| - 7000) * $0.10 per tick outside the band
  - Options cash-settle at expiry; RTM closed at last price

This is an approximation of RIT, not the real thing. The real news wording and market-maker
behaviour may differ: check against the practice server once you have access.

Usage:
    python mock_rit_vol.py                    # 1 second per tick (real speed)
    python mock_rit_vol.py --tick 0.2         # 5x speed
    python mock_rit_vol.py --seed 7 --loop    # restart a new heat automatically after each one
"""
import argparse
import json
import math
import random
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import bs

STRIKES = [48, 49, 50, 51, 52]
OPTIONS = [f"RTM{k}{t}" for k in STRIKES for t in ("C", "P")]
TICKERS = ["RTM"] + OPTIONS
SIZE = {t: (1 if t == "RTM" else 100) for t in TICKERS}
FEE = {t: (0.02 if t == "RTM" else 2.00) for t in TICKERS}   # per share / per contract
MAX_ORDER = {t: (10000 if t == "RTM" else 100) for t in TICKERS}
DELTA_LIMIT, PENALTY_RATE = 7000, 0.10


def parse_option(t):
    return int(t[3:5]), ("c" if t[-1] == "C" else "p")


class Sim:
    def __init__(self, tick_seconds, seed, ticks=300, half_life=20.0, mm_start=None, range_miss=0.0, reset_sd=0.0,
                 strike_sd=0.0, strike_rho=0.35):
        self.rng = random.Random(seed)
        self.tick_seconds = tick_seconds
        self.ticks = ticks
        self.week_len = ticks // 4
        self.tpy = ticks * 12
        self.half_life = half_life
        self.lock = threading.Lock()
        self.true_vols = [round(self.rng.uniform(0.15, 0.40), 2) for _ in range(4)]
        # scenario knobs use their own RNG so the price path for a seed stays the same across scenarios
        alt = random.Random(None if seed is None else seed + 1_000_000)
        self.alt = alt
        self.range_miss = range_miss
        self.reset_sd = reset_sd
        self.strike_sd, self.strike_rho = strike_sd, strike_rho   # real server (19 Nov): each strike's vol is
        self.dev = {k: 0.0 for k in STRIKES}                        # re-drawn every tick, sd ~1.2 pts, lag-1 corr 0.35
        self.noise = random.Random(None if seed is None else seed + 2_000_000)        # real server (16 Nov logs): at each week start the MM's vol JUMPS to
                                        # roughly true vol +- 15 pts (often the wrong way), then drifts back
        if mm_start is None:
            mm_start = 0.25
        elif mm_start < 0:                      # random starting guess for the market maker
            mm_start = round(alt.uniform(0.15, 0.40), 3)
        self.mm_vol = mm_start
        self.S = 50.0
        self.tick = 0
        self.status = "ACTIVE"
        self.pos = {t: 0 for t in TICKERS}
        self.cash = 0.0
        self.fees = 0.0
        self.penalty = 0.0
        self.news = []
        self.orders = []
        self.volume = {t: 0 for t in TICKERS}   # market volume per ticker (all traders; here only us)
        self.tas = []                            # time & sales: every trade in the market
        # (the real server's t=1 message is added in step(); nothing is published at tick 0)

    # ---------- pricing ----------
    def T(self):
        return max(0.0, (self.ticks - self.tick) / self.tpy)

    def mid(self, t):
        if t == "RTM":
            return self.S
        K, f = parse_option(t)
        return bs.price(f, self.S, K, self.T(), max(0.01, self.mm_vol + self.dev[K]))

    def quote(self, t):
        m = self.mid(t)
        bid = max(0.0, round(m - 0.01, 2))
        return bid, round(bid + 0.02, 2)

    def portfolio_delta(self):
        d = self.pos["RTM"]
        for t in OPTIONS:
            if self.pos[t]:
                K, f = parse_option(t)
                d += self.pos[t] * 100 * bs.delta(f, self.S, K, self.T(), self.mm_vol)
        return d

    def nlv(self):
        v = self.cash
        for t in TICKERS:
            if self.pos[t]:
                v += self.pos[t] * SIZE[t] * self.mid(t)
        return v - self.penalty

    # ---------- simulation ----------
    def _add_news(self, headline, body, ticker=""):
        self.news.append({"news_id": len(self.news) + 1, "period": 1, "tick": self.tick,
                          "ticker": ticker, "headline": headline, "body": body})

    def week(self):
        return min(4, max(0, self.tick - 1) // self.week_len + 1)

    def step(self):
        with self.lock:
            if self.status != "ACTIVE":
                return
            self.tick += 1
            w = self.week()
            sigma = self.true_vols[w - 1]
            if self.tick > 1:
                self.S = round(self.S * (1 + self.rng.gauss(0, sigma / math.sqrt(self.tpy))), 2)
            # market maker learns the true vol with a lag
            k = 1 - 0.5 ** (1 / self.half_life)
            self.mm_vol += (sigma - self.mm_vol) * k
            if self.strike_sd:
                for k in STRIKES:
                    self.dev[k] = (self.strike_rho * self.dev[k] +
                                   math.sqrt(1 - self.strike_rho ** 2) * self.noise.gauss(0, self.strike_sd))
            if self.reset_sd and w > 1 and self.tick == (w - 1) * self.week_len + 1:
                self.mm_vol = max(0.05, sigma + self.alt.gauss(0, self.reset_sd))
            # news
            # News wording and timing copied from the real RITCx practice server (Nov 2026):
            #   t=1   ticker "Week 1", headline "Risk free rate and current annualized volatility of RTM"
            #   t=36  "News 1": a 5-point range for next week
            #   t=74  ticker "Week 2", "Announcement 1": the vol for the week now starting
            #   t=111 "News 2", t=149 "Week 3"/"Announcement 2", t=186 "News 3", ...
            start = (w - 1) * self.week_len + 1
            if self.tick == 1:
                self._add_news("Risk free rate and current annualized volatility of RTM",
                               f"The current risk free rate is 0%. RTM is an ETF that mimics one of the major indices "
                               f"in the simulated world and its current annualized realized volatility is "
                               f"{round(sigma * 100)}%. This simulation consists of 20 trading days that are each "
                               f"15 ticks in length.", ticker="Week 1")
            elif w < 4 and self.tick == w * self.week_len - 1:
                # one tick before week w+1 starts: announce week w+1's vol (real server: tick 74 -> "Week 2")
                nxt = self.true_vols[w]
                self._add_news(f"Announcement {w}",
                               f"The realized volatility of RTM for this week will be {round(nxt * 100)}%",
                               ticker=f"Week {w + 1}")
            if self.tick == start + self.week_len // 2 - 2 and w < 4:
                nv = self.true_vols[w] * 100
                lo = round(nv - self.rng.uniform(0, 5))
                if self.alt.random() < self.range_miss:      # forecast range that MISSES the true vol
                    miss = self.alt.uniform(1, 3)          # true vol lies 1-3 points outside the range
                    lo = round(nv + miss) if self.alt.random() < 0.5 else round(nv - 5 - miss)
                self._add_news(f"News {w}",
                               f"The realized volatility of RTM for next week will be between {lo}% and {lo + 5}%")
            # CRO delta penalty
            d = abs(self.portfolio_delta())
            if d > DELTA_LIMIT:
                self.penalty += (d - DELTA_LIMIT) * PENALTY_RATE
                print(f"[mock] t={self.tick} delta {d:,.0f} over limit, penalty {(d - DELTA_LIMIT) * PENALTY_RATE:,.0f}", flush=True)
            if self.tick >= self.ticks:
                self._settle()

    def _settle(self):
        for t in OPTIONS:
            if self.pos[t]:
                K, f = parse_option(t)
                payoff = max(0.0, self.S - K) if f == "c" else max(0.0, K - self.S)
                self.cash += self.pos[t] * 100 * payoff
                self.pos[t] = 0
        self.cash += self.pos["RTM"] * self.S
        self.pos["RTM"] = 0
        self.status = "STOPPED"
        print(f"[mock] heat over. true vols={self.true_vols}  final P&L={self.nlv():,.2f} "
              f"(fees {self.fees:,.2f}, penalty {self.penalty:,.2f})", flush=True)

    # ---------- orders ----------
    def order(self, ticker, action, qty, otype):
        with self.lock:
            if self.status != "ACTIVE":
                return 400, {"code": "INVALID", "message": "Case is not active"}
            if ticker not in TICKERS:
                return 400, {"code": "INVALID", "message": "Unknown ticker"}
            if otype != "MARKET":
                return 400, {"code": "INVALID", "message": "Mock supports MARKET orders only"}
            if qty <= 0 or qty > MAX_ORDER[ticker]:
                return 400, {"code": "INVALID", "message": f"Quantity must be 1..{MAX_ORDER[ticker]}"}
            sign = 1 if action == "BUY" else -1
            new = dict(self.pos)
            new[ticker] += sign * qty
            if ticker == "RTM":
                if abs(new["RTM"]) > 50000:
                    return 400, {"code": "INVALID", "message": "RTM limit exceeded"}
            else:
                gross = sum(abs(new[t]) for t in OPTIONS)
                net = sum(new[t] for t in OPTIONS)
                if gross > 2500 or abs(net) > 1000:
                    return 400, {"code": "INVALID", "message": "Options limit exceeded"}
            bid, ask = self.quote(ticker)
            px = ask if sign > 0 else bid
            fee = FEE[ticker] * qty
            self.cash -= sign * qty * SIZE[ticker] * px + fee
            self.fees += fee
            self.pos = new
            oid = len(self.orders) + 1
            o = {"order_id": oid, "period": 1, "tick": self.tick, "trader_id": "mock", "ticker": ticker,
                 "type": "MARKET", "quantity": qty, "action": action, "price": None,
                 "quantity_filled": qty, "vwap": px, "status": "TRANSACTED"}
            self.orders.append(o)
            self.volume[ticker] += qty
            self.tas.append({"id": len(self.tas) + 1, "period": 1, "tick": self.tick, "ticker": ticker,
                             "price": px, "quantity": qty})
            return 200, o

    # ---------- API views ----------
    def securities(self):
        out = []
        for t in TICKERS:
            bid, ask = self.quote(t)
            out.append({"ticker": t, "type": "STOCK" if t == "RTM" else "OPTION", "size": SIZE[t],
                        "position": self.pos[t], "last": round(self.mid(t), 2), "bid": bid, "ask": ask,
                        "bid_size": 1_000_000, "ask_size": 1_000_000, "is_tradeable": True,
                        "volume": self.volume[t],
                        "max_trade_size": MAX_ORDER[t], "trading_fee": FEE[t]})
        return out


SIM = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        if self.headers.get("X-API-Key") != "Rotman":
            self._send(401, {"code": "UNAUTHORIZED", "message": "Bad API key"})
            return False
        return True

    def do_GET(self):
        if not self._auth():
            return
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        s = SIM
        with s.lock:
            if u.path == "/v1/case":
                return self._send(200, {"name": "Volatility Trading Case (mock)", "period": 1, "tick": s.tick,
                                        "ticks_per_period": s.ticks, "total_periods": 1,
                                        "status": s.status, "is_enforce_trading_limits": True})
            if u.path == "/v1/trader":
                return self._send(200, {"trader_id": "mock", "first_name": "Mock", "last_name": "Trader",
                                        "nlv": round(s.nlv(), 2)})
            if u.path == "/v1/securities":
                secs = s.securities()
                if "ticker" in q:
                    secs = [x for x in secs if x["ticker"] == q["ticker"]]
                return self._send(200, secs)
            if u.path == "/v1/securities/book":
                bid, ask = s.quote(q.get("ticker", "RTM"))
                return self._send(200, {"bids": [{"price": bid, "quantity": 1_000_000}],
                                        "asks": [{"price": ask, "quantity": 1_000_000}]})
            if u.path == "/v1/securities/tas":
                after = int(q.get("after", 0))
                rows = [{k: v for k, v in x.items() if k != "ticker"} for x in s.tas
                        if x["ticker"] == q.get("ticker") and x["id"] > after]
                return self._send(200, list(reversed(rows)))   # newest first, like RIT
            if u.path == "/v1/news":
                since = int(q.get("since", 0))
                items = [n for n in s.news if n["news_id"] > since]
                return self._send(200, list(reversed(items)))  # RIT returns newest first
            if u.path == "/v1/limits":
                return self._send(200, [
                    {"name": "RTM", "gross": abs(s.pos["RTM"]), "net": s.pos["RTM"],
                     "gross_limit": 50000, "net_limit": 50000},
                    {"name": "OPTIONS", "gross": sum(abs(s.pos[t]) for t in OPTIONS),
                     "net": sum(s.pos[t] for t in OPTIONS), "gross_limit": 2500, "net_limit": 1000}])
        self._send(404, {"code": "NOT_FOUND", "message": u.path})

    def do_POST(self):
        if not self._auth():
            return
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/v1/orders":
            code, obj = SIM.order(q.get("ticker"), q.get("action", "").upper(),
                                  int(float(q.get("quantity", 0))), q.get("type", "MARKET").upper())
            return self._send(code, obj)
        self._send(404, {"code": "NOT_FOUND", "message": u.path})


def run_clock(args):
    global SIM
    seed = args.seed
    while True:
        SIM = Sim(args.tick, seed, half_life=args.half_life, mm_start=args.mm_start, range_miss=args.range_miss,
                  reset_sd=args.reset_sd, strike_sd=args.strike_sd)
        print(f"[mock] new heat, seed={seed}. (true vols hidden; printed at the end)", flush=True)
        time.sleep(args.start_delay)
        while SIM.status == "ACTIVE":
            time.sleep(args.tick)
            SIM.step()
        if not args.loop:
            return
        time.sleep(args.start_delay)
        seed = None if seed is None else seed + 1


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=9999)
    p.add_argument("--tick", type=float, default=1.0, help="seconds per tick (1.0 = real speed)")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--half-life", type=float, default=20.0, help="ticks for market maker to close half the vol gap")
    p.add_argument("--mm-start", type=float, default=None, help="market maker's starting vol (-1 = random 15-40%%)")
    p.add_argument("--range-miss", type=float, default=0.0, help="probability a forecast range misses the true vol")
    p.add_argument("--strike-sd", type=float, default=0.0,
                   help="per-strike vol noise each tick, like the real server (try 0.012)")
    p.add_argument("--reset-sd", type=float, default=0.0,
                   help="MM vol jumps to true vol + N(0, this) at each week start, like the real server (try 0.15)")
    p.add_argument("--start-delay", type=float, default=2.0)
    p.add_argument("--loop", action="store_true", help="start a new heat after each one ends")
    args = p.parse_args()
    global SIM
    SIM = Sim(args.tick, args.seed, half_life=args.half_life)
    # Listen on IPv4 AND IPv6 localhost. Windows tries IPv6 (::1) first for "localhost"; if nothing
    # listens there it waits ~2 s per request before falling back, which makes the algos crawl.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        class V6Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        server6 = V6Server(("::1", args.port), Handler)
        threading.Thread(target=server6.serve_forever, daemon=True).start()
    except OSError:
        pass
    threading.Thread(target=run_clock, args=(args,), daemon=True).start()
    print(f"[mock] RIT mock on http://localhost:{args.port}/v1  ({args.tick}s per tick)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
