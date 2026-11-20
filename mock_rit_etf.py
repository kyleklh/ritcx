"""
Local mock of the RIT REST API for the RITCx Algorithmic ETF Arbitrage Case.

Approximates the case so you can develop without practice-server access:
  - BULL, BEAR (CAD), RITC (USD), USD (CAD per USD), CAD
  - Equilibrium: RITC * USD = BULL + BEAR, plus periodic ETF shocks that decay
  - Order books with LIMITED depth: market orders walk the book and push the price (impact decays)
  - Limit orders rest and fill when other traders' flow hits them (rebate $0.01)
  - Market fee $0.02/share. Max order 10,000 (equities). RITC counts 2x for limits.
  - Private tender offers (fixed price, and some competitive auctions) for RITC
  - Close-out at end at the CORRECT price (RITC valued at (BULL+BEAR)/USD)
  - Converters (ETF-Creation/Redemption, $1,500 USD per 10,000; each use takes 2 ticks, inputs leave at the
    start, outputs arrive at the end), two ways:
      * the RIT lease API, as the algo uses it: POST /leases?ticker=ETF-Redemption, then
        POST /leases/{id}?from1=RITC&quantity1=10000&from2=USD&quantity2=1500
      * a MOCK-ONLY endpoint standing in for a click in the RIT Client: python convert.py redeem
  - Limits and tender sizes match the practice-server reports: gross 300k, net 200k, tenders 70k-100k

Depth, spreads, tender discounts and shocks are still guesses.

Usage:
    python mock_rit_etf.py                 # real speed (1 tick per second)
    python mock_rit_etf.py --tick 0.2 --seed 3 --loop
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

EQUITIES = ["BULL", "BEAR", "RITC"]
TICKERS = ["CAD", "USD", "BULL", "BEAR", "RITC"]
FEE_MKT, REBATE_LMT = 0.02, 0.03     # case info sheet: fee 0.02, limit order rebate 0.03

# --real: tender prices drawn from the 62 tenders logged on the real server (Nov 16 competition heats + 4 solo
# practices): tender price vs the BULL+BEAR mid when it arrived, CAD/share, + = good for whoever takes it.
REAL_TENDER_EDGES = [0.127, 0.263, 0.009, 0.254, 0.206, 0.283, -0.115, 0.240, 0.068, 0.005, 0.153, 0.129, -0.066,
                     -0.069, 0.048, 0.104, 0.455, 0.394, 0.289, -0.293, 0.358, -0.051, -0.045, 0.284, -0.017, -0.101,
                     0.048, 0.081, 0.078, 0.253, 0.256, -0.216, -0.023, 0.014, 0.158, 0.728, 0.495, 0.089, 0.151,
                     -0.086, 0.037, 0.140, 0.158, 0.132, 0.170, -0.038, 0.008, -0.006, 0.361, -0.021, 0.009, 0.143,
                     -0.139, 0.346, 0.415, 0.117, 0.340, 0.388, 0.214, 0.382, 0.138, 0.394]
MAX_ORDER = {"BULL": 10000, "BEAR": 10000, "RITC": 10000, "USD": 2_500_000, "CAD": 2_500_000}
CONVERT_COST_USD = 1500.0


class Sim:
    def __init__(self, seed, ticks=300, gross_limit=300_000, net_limit=200_000,
                 depth_mult=1.0, vol_mult=1.0, flow_mult=1.0, gift_mean=0.10, shock_mult=1.0,
                 tender_min=70_000, tender_max=100_000, pos_lag=0.0, crowd=0, crowd_impact=1.0, real=False):
        self.rng = random.Random(seed)
        r = self.rng
        self.ticks = ticks
        self.tick = 0
        self.status = "ACTIVE"
        self.lock = threading.Lock()
        self.gross_limit, self.net_limit = gross_limit, net_limit
        # fair values
        self.F = {"BULL": 10.0, "BEAR": 15.0, "USD": 1.00}
        self.vol = {"BULL": 0.015 * vol_mult, "BEAR": 0.02 * vol_mult, "USD": 0.0004 * vol_mult}
        self.gift_mean, self.shock_mult = gift_mean, shock_mult
        self.shock = 0.0                       # RITC deviation from basket fair (USD)
        self.shock_decay = 0.93
        # market microstructure (per heat randomness)
        self.half_spread = {"BULL": 0.01, "BEAR": 0.01, "RITC": 0.01, "USD": 0.0001}
        self.level_step = {"BULL": 0.01, "BEAR": 0.01, "RITC": 0.01, "USD": 0.0001}
        self.depth = {"BULL": r.choice([2000, 3000, 4000]), "BEAR": r.choice([2000, 3000, 4000]),
                      "RITC": r.choice([1000, 1500, 2000]), "USD": 1_000_000}
        self.real = real
        if real:
            # measured on the real books (median over 2 sessions): ~9.5k per level, steps 0.02/0.025/0.035,
            # spreads 0.065/0.075/0.11 for BULL/BEAR/RITC
            self.half_spread.update({"BULL": 0.03, "BEAR": 0.04, "RITC": 0.05})
            self.level_step.update({"BULL": 0.02, "BEAR": 0.025, "RITC": 0.035})
            self.depth.update({t: r.choice([8500, 9500, 10500]) for t in ("BULL", "BEAR", "RITC")})
        self.disp = {t: 0.0 for t in ["BULL", "BEAR", "RITC", "USD"]}   # temporary price impact
        self.depth = {k: max(100, int(v * depth_mult)) for k, v in self.depth.items()}
        self.flow = {k: int(v * flow_mult) for k, v in {"BULL": 3000, "BEAR": 3000, "RITC": 2000, "USD": 500_000}.items()}
        # account
        self.pos = {t: 0 for t in TICKERS}     # USD/CAD positions = cash balances
        self.fees = 0.0
        self.orders = []
        self.open_orders = {}
        self.tenders = []
        self.next_tender_tick = r.randint(15, 28) if real else r.randint(5, 15)
        self.tender_id = 0
        self.tender_log = []
        self.tender_min, self.tender_max = tender_min, tender_max
        # Reported positions lag the real ones by pos_lag seconds (the real server can be slow to show fills)
        self.pos_lag = pos_lag
        # Other teams (the competition): they get the same tenders, accept the good ones and hedge them in
        # the same books over the next few ticks (pushing prices against us), and they arb RITC vs the basket
        # too (shocks close faster). crowd = number of competing teams.
        self.crowd, self.crowd_impact = crowd, crowd_impact
        self.crowd_rng = random.Random((seed or 0) + 99991)
        self.crowd_flow = []      # [ticks_left, {ticker: signed shares per tick}]
        if crowd:
            self.shock_decay = 0.93 ** (1 + 0.5 * crowd)
        self.pos_hist = []        # (time, copy of positions) after every change
        # converters: each use takes CONVERT_TICKS; inputs leave at the start, outputs arrive at the end
        self.conv = {"redeem": {"busy_until": 0, "outputs": None}, "create": {"busy_until": 0, "outputs": None}}
        self.conversions = 0
        self.leased = set()
        self.last = {t: self.mid(t) for t in ["BULL", "BEAR", "RITC", "USD"]}

    # ---------- prices ----------
    def ritc_fair(self):
        return (self.F["BULL"] + self.F["BEAR"]) / self.F["USD"]

    def mid(self, t):
        if t == "CAD":
            return 1.0
        if t == "RITC":
            return self.ritc_fair() + self.shock + self.disp["RITC"]
        return self.F[t] + self.disp[t]

    def best(self, t):
        m, hs = self.mid(t), self.half_spread[t]
        dec = 4 if t == "USD" else 2
        return round(m - hs, dec), round(m + hs, dec)

    def book(self, t, levels=20):
        bid, ask = self.best(t)
        st, d = self.level_step[t], self.depth[t]
        dec = 4 if t == "USD" else 2
        bids = [{"price": round(bid - i * st, dec), "quantity": d, "quantity_filled": 0} for i in range(levels)]
        asks = [{"price": round(ask + i * st, dec), "quantity": d, "quantity_filled": 0} for i in range(levels)]
        # show our resting orders in the book too
        for o in self.open_orders.values():
            if o["ticker"] == t:
                (bids if o["action"] == "BUY" else asks).append(
                    {"price": o["price"], "quantity": o["quantity"] - o["quantity_filled"], "quantity_filled": 0,
                     "trader_id": "mock"})
        bids.sort(key=lambda x: -x["price"])
        asks.sort(key=lambda x: x["price"])
        return {"bids": bids, "asks": asks}

    def _walk(self, t, side, q):
        """Fill q against the book. Returns vwap and moves the price (impact)."""
        bid, ask = self.best(t)
        st, d = self.level_step[t], self.depth[t]
        start = ask if side > 0 else bid
        remaining, cost, i = q, 0.0, 0
        while remaining > 0:
            take = min(d, remaining)
            cost += take * (start + side * i * st)
            remaining -= take
            i += 1
        vwap = cost / q
        # impact: price moves by levels consumed, partly persistent
        self.disp[t] += side * (q / d) * st * 0.6
        return vwap

    # ---------- limits ----------
    def gross_net(self, pos):
        g = abs(pos["BULL"]) + abs(pos["BEAR"]) + 2 * abs(pos["RITC"])
        n = pos["BULL"] + pos["BEAR"] + 2 * pos["RITC"]
        return g, n

    def within_limits(self, pos):
        g, n = self.gross_net(pos)
        return g <= self.gross_limit and abs(n) <= self.net_limit

    def _record(self):
        if self.pos_lag > 0:
            now = time.time()
            self.pos_hist.append((now, dict(self.pos)))
            while len(self.pos_hist) > 2 and self.pos_hist[1][0] <= now - self.pos_lag:
                self.pos_hist.pop(0)

    def reported_pos(self):
        """Positions as the API shows them: pos_lag seconds old."""
        if self.pos_lag <= 0 or not self.pos_hist:
            return self.pos
        cutoff = time.time() - self.pos_lag
        seen = None
        for t, p in self.pos_hist:
            if t <= cutoff:
                seen = p
        return seen if seen is not None else self.pos_hist[0][1]

    def _apply_fill(self, t, side, q, px, fee_per_share):
        """side +1 buy, -1 sell. Cash: RITC settles in USD, others in CAD."""
        if self.pos_lag > 0 and not self.pos_hist:
            self.pos_hist.append((0.0, dict(self.pos)))
        self.pos[t] += side * q
        if t == "USD":
            self.pos["CAD"] -= side * q * px
            self._record()
            return
        ccy = "USD" if t == "RITC" else "CAD"
        self.pos[ccy] -= side * q * px
        self.pos[ccy] -= fee_per_share * q
        self.fees += fee_per_share * q * (self.F["USD"] if ccy == "USD" else 1.0)
        self.last[t] = px
        self._record()

    # ---------- simulation ----------
    def step(self):
        with self.lock:
            if self.status != "ACTIVE":
                return
            self.tick += 1
            r = self.rng
            for t in ("BULL", "BEAR"):
                self.F[t] = max(1.0, self.F[t] + r.gauss(0, self.vol[t]))
            self.F["USD"] *= (1 + r.gauss(0, self.vol["USD"]))
            for t in self.disp:
                self.disp[t] *= 0.75
            # ETF shocks
            self.shock *= self.shock_decay
            if r.random() < 0.04:
                self.shock += r.choice([-1, 1]) * r.uniform(0.08, 0.30) * self.shock_mult
            self._crowd_trades()
            self._fill_limit_orders()
            self._finish_conversions()
            self._tenders()
            if self.tick >= self.ticks:
                self._close_out()

    def _fill_limit_orders(self):
        r = self.rng
        for oid in list(self.open_orders):
            o = self.open_orders[oid]
            t = o["ticker"]
            bid, ask = self.best(t)
            side = 1 if o["action"] == "BUY" else -1
            rem = o["quantity"] - o["quantity_filled"]
            # marketable after price move -> fill now
            if (side > 0 and o["price"] >= ask) or (side < 0 and o["price"] <= bid):
                q = rem
            elif (side > 0 and o["price"] >= bid) or (side < 0 and o["price"] <= ask):
                q = min(rem, int(r.uniform(0, self.flow[t]))) if r.random() < 0.6 else 0
            else:
                q = 0
            if q > 0:
                tmp = dict(self.pos)
                tmp[t] += side * q
                if t in EQUITIES and not self.within_limits(tmp):
                    continue
                self._apply_fill(t, side, q, o["price"], -REBATE_LMT if t in EQUITIES else 0.0)
                o["quantity_filled"] += q
                if o["quantity_filled"] >= o["quantity"]:
                    o["status"] = "TRANSACTED"
                    del self.open_orders[oid]

    def _tenders(self):
        r = self.rng
        self.tenders = [x for x in self.tenders if x["expires"] > self.tick]
        if self.tick >= self.next_tender_tick and self.tick < self.ticks - 15:
            self.next_tender_tick = self.tick + (r.randint(22, 40) if self.real else r.randint(15, 30))
            self.tender_id += 1
            action = r.choice(["BUY", "SELL"])
            qty = r.randrange(self.tender_min, self.tender_max + 1, 1000)   # real server: 72k-98k
            fair = self.ritc_fair()
            # positive = good for us; sometimes a trap
            if self.real:      # real tenders: drawn from the logged ones, all fixed-price, 6 ticks to answer
                gift = r.choice(REAL_TENDER_EDGES) / self.F["USD"] + r.gauss(0, 0.02)
                fixed = True
            else:
                gift = r.gauss(self.gift_mean, 0.10)
                fixed = r.random() < 0.75
            px = round(fair - gift if action == "BUY" else fair + gift, 2)
            tdr = {"tender_id": self.tender_id, "period": 1, "tick": self.tick, "expires": self.tick + (6 if self.real else 12),
                   "caption": f"Institution wants you to {action} {qty} RITC", "quantity": qty,
                   "ticker": "RITC", "action": action, "is_fixed_bid": fixed,
                   "price": px if fixed else None}
            tdr["_reserve"] = px
            self.tenders.append(tdr)
            self._crowd_takes(action, qty, gift)

    def _crowd_takes(self, action, qty, gift):
        """Competing teams accept this tender too (most of them if it's good) and hedge it over 6 ticks:
        70% through BULL+BEAR, 30% back into RITC."""
        if not self.crowd:
            return
        cr = self.crowd_rng
        takers = sum(1 for _ in range(self.crowd) if cr.random() < (0.7 if gift > 0.05 else 0.2))
        if not takers:
            return
        shares = takers * qty
        side = 1 if action == "BUY" else -1          # they now hold side*shares RITC; hedge = opposite flow
        ticks = 6
        per = {"BULL": -side * 0.7 * shares / ticks, "BEAR": -side * 0.7 * shares / ticks,
               "RITC": -side * 0.3 * shares / ticks}
        self.crowd_flow.append([ticks, per])

    def _crowd_trades(self):
        """Apply the competitors' hedge flow: it moves prices like our own market orders do."""
        for f in self.crowd_flow:
            f[0] -= 1
            for t, sh in f[1].items():
                d = self.depth[t]
                self.disp[t] += (sh / d) * self.level_step[t] * 0.6 * self.crowd_impact
        self.crowd_flow = [f for f in self.crowd_flow if f[0] > 0]

    def accept_tender(self, tid, price):
        with self.lock:
            tdr = next((x for x in self.tenders if x["tender_id"] == tid), None)
            if not tdr:
                return 404, {"code": "NOT_FOUND", "message": "No such tender"}
            side = 1 if tdr["action"] == "BUY" else -1
            if tdr["is_fixed_bid"]:
                px = tdr["price"]
            else:
                if price is None:
                    return 400, {"code": "INVALID", "message": "Price required for auction tender"}
                px = float(price)
                ok = px <= tdr["_reserve"] if side > 0 else px >= tdr["_reserve"]
                self.tenders.remove(tdr)
                if not ok:
                    return 200, {"success": False}
            tmp = dict(self.pos)
            tmp["RITC"] += side * tdr["quantity"]
            if not self.within_limits(tmp):
                return 400, {"code": "INVALID", "message": "Accepting would exceed limits"}
            self._apply_fill("RITC", side, tdr["quantity"], px, 0.0)
            if tdr in self.tenders:
                self.tenders.remove(tdr)
            self.tender_log.append((tdr["tick"], tdr["action"], tdr["quantity"], px, round(self.ritc_fair(), 3)))
            return 200, {"success": True}

    def decline_tender(self, tid):
        with self.lock:
            self.tenders = [x for x in self.tenders if x["tender_id"] != tid]
            return 200, {"success": True}

    def convert(self, kind, qty):
        """One converter use (10,000), like clicking it in the RIT Client."""
        with self.lock:
            if kind not in self.RECIPES:
                return 400, {"code": "INVALID", "message": "kind must be redeem or create"}
            if qty != 10000:
                return 400, {"code": "INVALID", "message": "One use converts exactly 10,000"}
            c = self.conv[kind]
            if c["busy_until"] > self.tick:
                return 400, {"code": "INVALID", "message": "Converter is busy"}
            ins, outs = self.RECIPES[kind]
            for k, v in ins.items():
                self.pos[k] -= v
            self.fees += ins["USD"] * self.F["USD"]
            c["busy_until"], c["outputs"] = self.tick + self.CONVERT_TICKS, dict(outs)
            self._record()
            self.conversions += 1
            return 200, {"success": True, "kind": kind, "quantity": qty, "done_tick": c["busy_until"]}

    # ---------- converters (stand-in for the human click) ----------
    RECIPES = {
        "redeem": ({"RITC": 10000, "USD": 1500}, {"BULL": 10000, "BEAR": 10000}),
        "create": ({"BULL": 10000, "BEAR": 10000, "USD": 1500}, {"RITC": 10000}),
    }
    CONVERT_TICKS = 2

    LEASE_IDS = {"ETF-Redemption": (1, "redeem"), "ETF-Creation": (2, "create")}
    PARAM_INPUTS = {"redeem": {"RITC": 10000.0, "USD": 1500.0},
                    "create": {"BULL": 10000.0, "BEAR": 10000.0, "USD": 1500.0}}

    def leases_view(self):
        return [{"id": i, "ticker": t} for t, (i, _) in self.LEASE_IDS.items() if t in self.leased]

    def lease(self, ticker, params=None):
        """POST /leases?ticker=X leases it; with from1.. it also uses it straight away (refinery style)."""
        with self.lock:
            if ticker not in self.LEASE_IDS:
                return 400, {"code": "INVALID", "message": f"Unknown asset {ticker}"}
            self.leased.add(ticker)
        if params and "from1" in params:
            return self.use_lease(self.LEASE_IDS[ticker][0], params)
        return 200, {"id": self.LEASE_IDS[ticker][0], "ticker": ticker}

    def use_lease(self, lid, params):
        kind = next((k for t, (i, k) in self.LEASE_IDS.items() if i == lid and t in self.leased), None)
        if not kind:
            return 404, {"code": "NOT_FOUND", "message": "No such lease"}
        given = {params[f"from{i}"]: float(params.get(f"quantity{i}", 0)) for i in (1, 2, 3) if f"from{i}" in params}
        if given != self.PARAM_INPUTS[kind]:
            return 400, {"code": "INVALID", "message": f"Inputs must be {self.PARAM_INPUTS[kind]}"}
        return self.convert(kind, 10000)

    def _finish_conversions(self):
        for c in self.conv.values():
            if c["outputs"] and self.tick >= c["busy_until"]:
                for k, v in c["outputs"].items():
                    self.pos[k] += v
                c["outputs"] = None
                self._record()

    def _close_out(self):
        self.open_orders.clear()
        u = self.F["USD"]
        cad = self.pos["CAD"] + self.pos["BULL"] * self.F["BULL"] + self.pos["BEAR"] * self.F["BEAR"]
        usd = self.pos["USD"] + self.pos["RITC"] * self.ritc_fair()
        self.final_nlv = cad + usd * u
        self.status = "STOPPED"
        print(f"[mock] heat over. P&L={self.final_nlv:,.2f} CAD  (fees+converters {self.fees:,.2f}, "
              f"depth {self.depth}, tenders accepted {len(self.tender_log)}, conversions {self.conversions})", flush=True)

    def nlv(self):
        if self.status == "STOPPED":
            return self.final_nlv
        u = self.mid("USD")
        v = self.pos["CAD"] + self.pos["BULL"] * self.mid("BULL") + self.pos["BEAR"] * self.mid("BEAR")
        v += (self.pos["USD"] + self.pos["RITC"] * self.mid("RITC")) * u
        return v

    # ---------- orders ----------
    def order(self, t, action, qty, otype, price):
        with self.lock:
            if self.status != "ACTIVE":
                return 400, {"code": "INVALID", "message": "Case is not active"}
            if t not in ("BULL", "BEAR", "RITC", "USD"):
                return 400, {"code": "INVALID", "message": "Not tradeable"}
            if qty <= 0 or qty > MAX_ORDER[t]:
                return 400, {"code": "INVALID", "message": f"Quantity must be 1..{MAX_ORDER[t]}"}
            side = 1 if action == "BUY" else -1
            if t in EQUITIES:
                tmp = dict(self.pos)
                tmp[t] += side * qty
                if not self.within_limits(tmp):
                    return 400, {"code": "INVALID", "message": "Order would exceed trading limits"}
            oid = len(self.orders) + 1
            bid, ask = self.best(t)
            marketable = otype == "MARKET" or (side > 0 and price >= ask) or (side < 0 and price <= bid)
            if marketable:
                vwap = self._walk(t, side, qty)
                if otype == "LIMIT" and ((side > 0 and vwap > price) or (side < 0 and vwap < price)):
                    vwap = price
                self._apply_fill(t, side, qty, vwap, FEE_MKT if t in EQUITIES else 0.0)
                o = {"order_id": oid, "tick": self.tick, "ticker": t, "type": otype, "quantity": qty,
                     "action": action, "price": price, "quantity_filled": qty, "vwap": round(vwap, 4),
                     "status": "TRANSACTED"}
            else:
                o = {"order_id": oid, "tick": self.tick, "ticker": t, "type": "LIMIT", "quantity": qty,
                     "action": action, "price": price, "quantity_filled": 0, "vwap": None, "status": "OPEN"}
                self.open_orders[oid] = o
            self.orders.append(o)
            return 200, o

    def cancel(self, oid=None, ticker=None):
        with self.lock:
            ids = [oid] if oid else [k for k, o in self.open_orders.items() if ticker in (None, o["ticker"])]
            for k in ids:
                o = self.open_orders.pop(k, None)
                if o:
                    o["status"] = "CANCELLED"
            return 200, {"cancelled_order_ids": ids}

    def securities(self):
        out = []
        pos = self.reported_pos()
        for t in TICKERS:
            if t == "CAD":
                out.append({"ticker": "CAD", "position": round(pos["CAD"], 2), "bid": 1, "ask": 1, "last": 1,
                            "bid_size": 0, "ask_size": 0})
                continue
            bid, ask = self.best(t)
            out.append({"ticker": t, "position": round(pos[t], 2), "bid": bid, "ask": ask,
                        "last": round(self.last.get(t, self.mid(t)), 4), "bid_size": self.depth[t],
                        "ask_size": self.depth[t]})
        return out

    def limits(self):
        g, n = self.gross_net(self.reported_pos())
        return [{"name": "LIMIT-STOCK", "gross": g, "net": n, "gross_limit": self.gross_limit,
                 "net_limit": self.net_limit, "gross_fine": 0, "net_fine": 0}]


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

    def _q(self):
        u = urlparse(self.path)
        return u.path, {k: v[0] for k, v in parse_qs(u.query).items()}

    def _auth(self):
        if (self.headers.get("X-API-Key") or self.headers.get("X-API-key")) != "Rotman":
            self._send(401, {"code": "UNAUTHORIZED", "message": "Bad API key"})
            return False
        return True

    def do_GET(self):
        if not self._auth():
            return
        path, q = self._q()
        s = SIM
        with s.lock:
            if path == "/v1/case":
                return self._send(200, {"name": "ETF Arbitrage (mock)", "period": 1, "tick": s.tick,
                                        "ticks_per_period": s.ticks, "total_periods": 1, "status": s.status})
            if path == "/v1/trader":
                return self._send(200, {"trader_id": "mock", "nlv": round(s.nlv(), 2)})
            if path == "/v1/securities":
                secs = s.securities()
                if "ticker" in q:
                    secs = [x for x in secs if x["ticker"] == q["ticker"]]
                return self._send(200, secs)
            if path == "/v1/securities/book":
                return self._send(200, s.book(q.get("ticker", "RITC"), int(q.get("limit", 20))))
            if path == "/v1/tenders":
                return self._send(200, [{k: v for k, v in x.items() if not k.startswith("_")} for x in s.tenders])
            if path == "/v1/limits":
                return self._send(200, s.limits())
            if path.startswith("/v1/orders/"):
                oid = int(path.rsplit("/", 1)[1])
                for o in s.orders:
                    if o.get("order_id") == oid:
                        return self._send(200, o)
                return self._send(404, {"code": "NOT_FOUND", "message": "no such order"})
            if path == "/v1/orders":
                st = q.get("status", "OPEN")
                return self._send(200, [o for o in s.orders if o["status"] == st])
            if path == "/v1/news":
                return self._send(200, [])
            if path == "/v1/leases":
                return self._send(200, s.leases_view())
        self._send(404, {"code": "NOT_FOUND", "message": path})

    def do_POST(self):
        if not self._auth():
            return
        path, q = self._q()
        if path == "/v1/orders":
            price = float(q["price"]) if "price" in q else None
            otype = q.get("type", "MARKET").upper()
            if otype == "LIMIT" and price is None:
                return self._send(400, {"code": "INVALID", "message": "LIMIT needs price"})
            return self._send(*SIM.order(q.get("ticker"), q.get("action", "").upper(),
                                         int(float(q.get("quantity", 0))), otype, price))
        if path.startswith("/v1/tenders/"):
            return self._send(*SIM.accept_tender(int(path.rsplit("/", 1)[1]), q.get("price")))
        if path == "/v1/commands/cancel":
            return self._send(*SIM.cancel(ticker=q.get("ticker")))
        if path == "/v1/leases":
            return self._send(*SIM.lease(q.get("ticker", ""), q))
        if path.startswith("/v1/leases/"):
            return self._send(*SIM.use_lease(int(path.rsplit("/", 1)[1]), q))
        if path == "/v1/mock/convert":
            return self._send(*SIM.convert(q.get("kind", ""), int(q.get("quantity", 10000))))
        self._send(404, {"code": "NOT_FOUND", "message": path})

    def do_DELETE(self):
        if not self._auth():
            return
        path, q = self._q()
        if path.startswith("/v1/tenders/"):
            return self._send(*SIM.decline_tender(int(path.rsplit("/", 1)[1])))
        if path.startswith("/v1/orders/"):
            return self._send(*SIM.cancel(oid=int(path.rsplit("/", 1)[1])))
        self._send(404, {"code": "NOT_FOUND", "message": path})


def run_clock(args):
    global SIM
    seed = args.seed
    while True:
        SIM = Sim(seed, gross_limit=args.gross, net_limit=args.net, depth_mult=args.depth_mult,
                  vol_mult=args.vol_mult, flow_mult=args.flow_mult, gift_mean=args.gift_mean, shock_mult=args.shock_mult,
                  tender_min=args.tender_min, tender_max=args.tender_max, pos_lag=args.pos_lag,
                  crowd=args.crowd, crowd_impact=args.crowd_impact, real=args.real)
        print(f"[mock] new heat, seed={seed}", flush=True)
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
    p.add_argument("--tick", type=float, default=1.0)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--gross", type=int, default=300_000)
    p.add_argument("--net", type=int, default=200_000)
    p.add_argument("--tender-min", type=int, default=70_000, help="smallest tender (real server: ~72k)")
    p.add_argument("--tender-max", type=int, default=100_000, help="largest tender (real server: ~98k)")
    p.add_argument("--depth-mult", type=float, default=1.0, help="order book depth multiplier")
    p.add_argument("--vol-mult", type=float, default=1.0, help="price volatility multiplier")
    p.add_argument("--flow-mult", type=float, default=1.0, help="how fast resting limit orders get filled")
    p.add_argument("--gift-mean", type=float, default=0.10, help="average tender discount vs fair (USD/share)")
    p.add_argument("--shock-mult", type=float, default=1.0, help="size of RITC-vs-basket shocks")
    p.add_argument("--start-delay", type=float, default=2.0)
    p.add_argument("--pos-lag", type=float, default=0.0, help="seconds the reported positions lag real fills")
    p.add_argument("--crowd", type=int, default=0, help="number of competing teams taking and hedging the same tenders")
    p.add_argument("--crowd-impact", type=float, default=1.0, help="how hard their hedging moves prices")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--real", action="store_true",
                   help="closer to the real server: real book depth/spreads, tender prices drawn from the 62 logged "
                        "real tenders, all fixed-price, 6 ticks to answer. Still NOT for judging P&L: the book refills "
                        "instantly after every trade, so it scores the Nov 16 algo ~+55-80k/heat vs ~+6k for real")
    args = p.parse_args()
    global SIM
    SIM = Sim(args.seed, gross_limit=args.gross, net_limit=args.net)
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
    print(f"[mock] ETF mock on http://localhost:{args.port}/v1 ({args.tick}s per tick)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
