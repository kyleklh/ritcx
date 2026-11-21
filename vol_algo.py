"""
RITCx Volatility Trading Case - trading algorithm.

Strategy
  1. Read the news to learn this week's realized vol and next week's forecast range.
  2. Fair vol for an option = sqrt(time-weighted average variance over its remaining life).
  3. Price every option with Black-Scholes at fair vol. Buy when ask is cheap, sell when bid is rich
     (after fees + a safety edge). Biggest vega (near ATM) gives the biggest edge, so it trades first.
  4. Delta-hedge with RTM whenever |delta| exceeds HEDGE_TRIGGER (well inside the 7000 limit).

Live commands (type in the terminal and press Enter while it runs):
  v 25        set THIS week's vol to 25%   (override if the news parser missed it)
  n 27 30     set NEXT week's forecast range to 27-30%
  p / r       pause / resume opening new option trades (hedging continues)
  h           force a delta hedge now
  s           print status
  q           stop the algorithm (positions stay as they are)

Each team member runs this on their own account (own API key/port in the RIT Client).
"""
import csv
import os
import math
import re
import sys
import threading
import time
from datetime import datetime

import requests
from concurrent.futures import ThreadPoolExecutor

import bs
from display import say, event, banner

# =========================== SETTINGS (tune between heats) ===========================
from config import API, API_KEY   # read from .env (see .env.example); defaults to localhost:9999 / Rotman

STRIKES = [48, 49, 50, 51, 52]
RISK_FREE = 0.0

OPT_FEE_PER_SHARE = 0.02      # $2 per contract / 100 shares
MIN_EDGE = 0.02               # extra $/share of mispricing required beyond fees (safety margin)
REVERSE_EDGE = float(os.environ.get("REVERSE_EDGE", 0.10))          # edge needed to flip the book's direction (long<->short): a flip pays the
                              # ~$3/contract round trip twice, so small vol wobbles shouldn't trigger it.
                              # The "direction" is remembered until the book has been flat for a while, so
                              # closing shorts and then opening longs at a small edge still counts as a flip.
CLIP = 100                    # contracts per order (max 100)
FAST_ENTRY = os.environ.get("FAST_ENTRY", "0") == "1"   # mock with week-start jumps, 12 seeds vs 4 orders: 2 orders
                              # -$16k, 3 and 10 no change, 20 with no pause +$1.2k +- 1.7k but fines doubled. Off.
MAX_ORDERS_PER_LOOP = int(os.environ.get("MAX_ORDERS", 20 if FAST_ENTRY else 4))   # option orders per loop. The market maker's vol jumps at the week
                              # start (t=76/151/226) and the extreme lasts 1-2 ticks; at 4 orders (400 contracts) per
                              # loop a -950 -> +950 flip took ~8 ticks live (heat 2, 16 Nov). RIT allows 50 orders/s.
MAX_NET_CONTRACTS = 950       # case limit 1000 net; keep a buffer
CASE_NET_LIMIT = 1000         # actual exchange limit; used to permit partial risk-reducing exits
MAX_GROSS_CONTRACTS = 2400    # case limit 2500 gross

HEDGE_TRIGGER = 2000          # re-hedge when |delta| > this (case limit 7000)
GAMMA_Z = float(os.environ.get("GAMMA_Z", 1.5))                # size the book so a GAMMA_Z-sigma one-tick move can't push delta past the limit.
                              # 1.5 beat 2.0 by ~$5k/heat on the mock (MM half-life 5-10); 1.0 and 0.75 added
                              # penalty (up to $5k in a heat) with no extra profit.
                              # Higher = safer/smaller. Gamma grows near expiry, so this shrinks size automatically.
HEDGE_TARGET = 0
RTM_MAX_ORDER = 10000
RTM_LIMIT = 49000             # case limit 50000

OPTION_DELTA_CAP = 25000      # max |delta| of the OPTIONS book alone. RTM limit (50k) must be able to hedge it,
                              # so this forces balanced books (e.g. short calls AND puts = straddle), not one-sided.
OPTION_DELTA_REBAL = 35000    # if price moves push |option delta| past this, cut the positions driving it
                              # (the normal logic then re-adds risk in delta-reducing strikes)
STOP_OPENING_TICKS_BEFORE_END = 25   # no new option risk this close to expiry
CLOSE_TICKS_BEFORE_END = 15   # in the last N ticks, close options near the money (gamma explodes there)
CLOSE_NEAR = 1.5              # ...only strikes within $1.50 of RTM. Far ITM/OTM ones are held to expiry
                              # (cash-settled for free; their delta is stable, so they are easy to hedge)
GAMMA_TRIM_ABOVE = 1.25       # cut gamma only when the book is 25% over the cap...
GAMMA_ADD_UP_TO = 0.90        # ...and add new risk only up to 90% of it (the gap stops buy/sell ping-pong)
GAMMA_EDGE_MAX = float(os.environ.get("GAMMA_EDGE_MAX", 1.5))   # a leg still worth more than GAMMA_KEEP_EDGE is
GAMMA_KEEP_EDGE = float(os.environ.get("GAMMA_KEEP_EDGE", 0.10))  # not cut for gamma until the book is past this many
                              # caps ($/share; 0.10 = $10/contract). The cap shrinks when a week's vol rises, which is
                              # exactly when the market maker's vol is far off: test 3 (18 Nov) sold 314 contracts at
                              # 22% vol to cut gamma, then vol went to 37%. Never adds risk; 0 turns it off.
GAMMA_EDGE_UNTIL_END = 30     # ...and only until this many ticks before the end (gamma explodes near expiry)
ARB_ON = os.environ.get("ARB", "1") == "1"   # strike-vs-strike engine (see arb_step); ARB=0 turns it off
ARB_MIN_BID = float(os.environ.get("ARB_MIN_BID", 0.02))   # skip strikes with an option bid at the 1-cent floor
ARB_MIN_DOLLARS = float(os.environ.get("ARB_MIN_DOLLARS", 10))   # enter when (rich - cheap strike vol) x vega >= this
                              # per contract. The market maker re-draws each strike's vol every tick (sd ~1.2 pts,
                              # spread across strikes median 2 pts, 90th pct 5 pts, 18 Nov test 8). Backtest on that
                              # heat's real quotes: $12-30 all profitable (+$50k..+$280k), $16 -> +$242k.
ARB_EXIT = float(os.environ.get("ARB_EXIT", 0.0))   # leave when the gap (vol pts) has closed to this...
ARB_MAX_HOLD = int(os.environ.get("ARB_MAX_HOLD", 40))   # ...or after this many ticks. Replay of 5 real heats
                              # with exact quotes vs the old $16/5: $10/40 +$31.8k per heat (4/5 better, worst -$2.2k);
                              # $16/20 +$13.8k (5/5); $12/20 +$24.6k but worst -$17k. Part of a strike's mispricing
                              # persists, and the top team holds its pairs ~20 ticks.
ARB_SIZE = int(os.environ.get("ARB_SIZE", 700))   # contracts per leg (capped by the gross room left)
ARB_LAST_ENTRY = 30           # no new pairs in the last N ticks; all unwound before the t=285 close-out
FLY_ON = os.environ.get("FLY", "0") == "1"  # experimental; opt in with FLY=1
FLY_SIZE = int(os.environ.get("FLY_SIZE", "25"))  # wing contracts; middle leg is twice this size
FLY_MIN_CREDIT = float(os.environ.get("FLY_MIN_CREDIT", "0.15"))  # opening credit/share after option fees
FLY_EXIT_PROFIT = float(os.environ.get("FLY_EXIT_PROFIT", "0.05"))  # estimated round-trip profit/share
MIN_OPEN_QTY = 5              # don't open positions in clips smaller than this (closing is always allowed)
STATUS_EVERY = 5              # print a status line every N ticks
LOOP_SLEEP = float(os.environ.get("LOOP_SLEEP", 0.2))             # seconds between loops
LOG_ROOT = os.environ.get("VOL_LOG_DIR", os.path.join("logs", "vol"))   # one folder per heat inside this:
                              # vol_log.csv (per tick), news_raw.txt (every news item), console.txt (terminal output)
# =====================================================================================

CALLS = {k: f"RTM{k}C" for k in STRIKES}
PUTS = {k: f"RTM{k}P" for k in STRIKES}
OPTIONS = list(CALLS.values()) + list(PUTS.values())

s = requests.Session()
s.headers.update({"X-API-Key": API_KEY})


class State:
    def __init__(self):
        self.reset()

    def reset(self):
        self.vol_known = {}       # week -> realized vol announced for that week
        self.vol_est = {}         # week -> midpoint of forecast range
        self.last_news_id = 0
        self.paused = False
        self.quit = False
        self.force_hedge = False
        self.ticks = 300
        self.week_len = 75
        self.tpy = 3600
        self.last_tick = -1
        self.last_slow_warn = -100
        self.flush_tick = 0
        self.fill_acc = {}        # (side, ticker) -> contracts, printed once per tick
        self.book_dir = 0         # +1 long / -1 short: the direction the book last had, kept while flat
        self.hedge_acc = 0        # RTM shares bought(+)/sold(-) this tick
        self.news_file = os.devnull
        self.arb_pos = {}         # ticker -> contracts held by the strike engine (the main book sees the rest)
        self.arb = None           # open pair: dict(long_k, short_k, t0)
        self.limit_diag_pending = threading.Event()
        self.limit_diag_tick = -1


ST = State()


# ------------------------------- API helpers -------------------------------
def api_get(path, **params):
    for _ in range(5):
        try:
            r = s.get(f"{API}{path}", params=params, timeout=2)
            if r.status_code == 429:
                time.sleep(float(r.json().get("wait", 0.1)))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            say(f"[warn] GET {path} failed: {e}")
            time.sleep(0.2)
    return None


LIMIT_ORDERS = os.environ.get("LIMIT_ORDERS", "1") == "1"   # option trades on an edge go as limit orders at the
                              # price the algo saw. A burst of market orders that runs past the end of a tick fills at
                              # the NEXT tick's quotes: test 19 (20 Nov) lost $56k that way, every bad fill at exactly
                              # the next tick's price. Hedges and risk cuts stay market orders. LIMIT_ORDERS=0: off.


PARALLEL_ORDERS = os.environ.get("PARALLEL_ORDERS", "1") == "1"   # send a basket's orders at the same time instead
                              # of one after another: a 700-lot pair is 14+ orders (max 100 each), which one at a time
                              # can take most of a tick, so later orders land on the next tick's prices.
_POOL = ThreadPoolExecutor(max_workers=16)
_TLS = threading.local()


def _sess():
    """One HTTP session per thread (requests sessions are not safe to share across threads)."""
    if threading.current_thread() is threading.main_thread():
        return s
    if not hasattr(_TLS, "s"):
        _TLS.s = requests.Session()
        _TLS.s.headers.update({"X-API-Key": API_KEY})
    return _TLS.s


def place_many(orders):
    """[(ticker, action, qty, limit)] -> filled quantities, all sent at once when PARALLEL_ORDERS."""
    if not orders:
        return []
    if not PARALLEL_ORDERS or len(orders) == 1:
        return [place(*o) for o in orders]
    context = getattr(_TLS, "order_context", "unspecified")
    def submit(o):
        _TLS.order_context = context
        return place(*o)
    return list(_POOL.map(submit, orders))


def place(ticker, action, qty, limit=None):
    """Market order, or (limit given and LIMIT_ORDERS) a limit order at that price whose unfilled rest is cancelled
    at once, so it can only fill at the price the decision was made on."""
    _TLS.last_opt_net_rejection = False
    qty = int(qty)
    if qty <= 0:
        return 0
    use_limit = limit is not None and LIMIT_ORDERS
    params = {"ticker": ticker, "type": "LIMIT" if use_limit else "MARKET", "quantity": qty, "action": action}
    if use_limit:
        params["price"] = round(limit, 2)
    for _ in range(5):
        try:
            r = _sess().post(f"{API}/orders", params=params, timeout=2)
            if r.status_code == 429:
                time.sleep(float(r.json().get("wait", 0.1)))
                continue
            if not r.ok:
                context = getattr(_TLS, "order_context", "unspecified")
                say(f"[order rejected] t={ST.last_tick} {action} {qty} {ticker} [{context}]: {r.text[:1000]}")
                _TLS.last_opt_net_rejection = (ticker in OPTIONS and "net trading limits" in r.text.lower()
                                               and "limit-opt" in r.text.lower())
                if "trading limits" in r.text.lower():
                    ST.limit_diag_pending.set()
                return 0
            j = r.json()
            if use_limit:
                j = settle_limit(j, qty)
            if j.get("quantity_filled") is None:
                say("[RISK] order fill unknown; inspect orders and positions")
                return 0
            filled = int(j["quantity_filled"] or 0)
            MKT.fill(ticker, action, filled, j.get("vwap"))
            return filled
        except (requests.RequestException, ValueError) as e:
            say(f"[RISK] order outcome unknown ({e}); inspect orders and positions")
            return 0
    return 0


def settle_limit(j, qty):
    """A limit order that didn't fully fill at once: cancel the rest, then read back what actually filled."""
    if int(j.get("quantity_filled", 0) or 0) >= qty or j.get("status") == "TRANSACTED":
        return j
    oid = j.get("order_id")
    if oid is None:
        say("[RISK] unfilled limit order has no ID; inspect orders and positions")
        return j
    terminal = ("TRANSACTED", "CANCELLED", "CANCELED")
    try:
        cancel = _sess().delete(f"{API}/orders/{oid}", timeout=2)
        cancel_detail = f"{cancel.status_code} {cancel.text[:200]!r}"
    except requests.RequestException as e:
        # A timeout does not prove the cancel failed. Still query the order's state.
        cancel_detail = f"error {e}"
    try:
        r = _sess().get(f"{API}/orders/{oid}", timeout=2)
        lookup_detail = f"{r.status_code} {r.text[:200]!r}"
        if r.ok:
            result = r.json()
            if isinstance(result, dict) and result.get("status") in terminal and result.get("quantity_filled") is not None:
                return result
            if isinstance(result, dict) and result.get("status") == "OPEN":
                # DELETE can return 404 even though the subsequent lookup shows the order open.
                # The bulk-cancel endpoint supports cancelling this specific ID; verify afterward.
                bulk = _sess().post(f"{API}/commands/cancel", params={"ids": str(oid)}, timeout=2)
                lookup_detail += f"; bulk cancel {bulk.status_code} {bulk.text[:200]!r}"
                check = _sess().get(f"{API}/orders/{oid}", timeout=2)
                lookup_detail += f"; recheck {check.status_code} {check.text[:200]!r}"
                if check.ok:
                    result = check.json()
                    if isinstance(result, dict) and result.get("status") in terminal and result.get("quantity_filled") is not None:
                        return result
    except (requests.RequestException, ValueError) as e:
        lookup_detail = f"error {e}"
    # Some clients return an order through the collection but not the individual endpoint.
    # Only accept an exact ID and a terminal status; absence from a list proves nothing.
    try:
        history = _sess().get(f"{API}/orders", timeout=2)
        history_detail = str(history.status_code)
        if history.ok:
            rows = history.json()
            if isinstance(rows, list):
                for result in rows:
                    if (isinstance(result, dict) and str(result.get("order_id")) == str(oid)
                            and result.get("status") in terminal and result.get("quantity_filled") is not None):
                        return result
    except (requests.RequestException, ValueError) as e:
        history_detail = f"error {e}"
    say(f"[RISK] limit order {oid} outcome unknown: DELETE {cancel_detail}; GET {lookup_detail}; "
        f"order list {history_detail}; inspect open orders and positions; trading continues")
    return j


# ------------------------------- market log (read-only) -------------------------------
class MarketLog:
    """Background thread, own HTTP session, never trades. Per heat folder it writes
      quotes.csv  tick, ticker, bid, ask, last, volume       (every tick, all 11 tickers; volume = market total)
      tas.csv     tick, ticker, id, price, quantity          (every trade in the market, if the server has
                                                              /securities/tas; RIT lists all traders' trades)
      fills.csv   tick, ticker, action, quantity, vwap       (our own fills, to subtract from the market)
    analyze_market.py turns these into the other teams' volume, side and timing per strike."""
    TICKERS = ["RTM"] + [f"RTM{k}{c}" for k in STRIKES for c in "CP"]

    def __init__(self):
        self.lock = threading.Lock()
        self.files = {}
        self.tick = 0
        self.last_q_tick = -1
        self.after = {}
        self.tas_ok = True
        self.started = False

    def set_folder(self, folder):
        with self.lock:
            for f, _ in self.files.values():
                f.close()
            self.files = {}
            for name, cols in (("quotes", ["tick", "ticker", "bid", "ask", "last", "volume"]),
                               ("tas", ["tick", "ticker", "id", "price", "quantity"]),
                               ("fills", ["tick", "ticker", "action", "quantity", "vwap"])):
                f = open(os.path.join(folder, f"{name}.csv"), "w", newline="")
                w = csv.writer(f)
                w.writerow(cols)
                self.files[name] = (f, w)
            self.after = {}
            self.last_q_tick = -1
        if not self.started and os.environ.get("VOL_MARKET_LOG", "1") == "1":
            self.started = True
            threading.Thread(target=self._run, daemon=True).start()

    def _write(self, name, row):
        with self.lock:
            if name in self.files:
                self.files[name][1].writerow(row)

    def _flush(self):
        with self.lock:
            for f, _ in self.files.values():
                f.flush()

    def fill(self, ticker, action, qty, vwap):
        if qty:
            self._write("fills", [ST.last_tick, ticker, action, qty, vwap])

    def _run(self):
        ses = requests.Session()
        ses.headers.update({"X-API-Key": API_KEY})
        while not ST.quit:
            try:
                case = ses.get(f"{API}/case", timeout=2).json()
                self.tick = case.get("tick", 0)
                if case.get("status") == "ACTIVE" and self.tick != self.last_q_tick:
                    self.last_q_tick = self.tick
                    for x in ses.get(f"{API}/securities", timeout=2).json():
                        self._write("quotes", [self.tick, x.get("ticker"), x.get("bid"), x.get("ask"),
                                               x.get("last"), x.get("volume")])
                if self.tas_ok and case.get("status") == "ACTIVE":
                    for tk in self.TICKERS:
                        params = {"ticker": tk}
                        if tk in self.after:
                            params["after"] = self.after[tk]
                        r = ses.get(f"{API}/securities/tas", params=params, timeout=2)
                        if r.status_code in (404, 405, 501):
                            self.tas_ok = False
                            say("[market log] this server has no time & sales; logging quotes + volume only")
                            break
                        if not r.ok:
                            continue
                        rows = r.json()
                        rows = rows if isinstance(rows, list) else rows.get("data", [])
                        for t in sorted(rows, key=lambda t: t.get("id", 0)):
                            if t.get("id", 0) > self.after.get(tk, -1):
                                self._write("tas", [t.get("tick"), tk, t.get("id"), t.get("price"), t.get("quantity")])
                                self.after[tk] = t.get("id", 0)
                self._flush()
            except (requests.RequestException, ValueError):
                pass
            time.sleep(0.25)


MKT = MarketLog()


# ------------------------------- news & vol -------------------------------
NUM = r"(\d+(?:\.\d+)?)"
RE_RANGE = re.compile(NUM + r"\s*%?\s*(?:-|–|~|to|and)\s*" + NUM + r"\s*%", re.I)
RE_POINT = re.compile(r"(?:will be|is|of|at)\s*" + NUM + r"\s*%", re.I)
RE_WEEK = re.compile(r"week\s*(\d)", re.I)
RE_VOL_POINT = re.compile(r"volatil\w*(?:[^%\d]{0,60}?)(?:will be|is|of|at)\s*" + NUM + r"\s*%", re.I)


def week_of(tick):
    return min(4, max(0, tick - 1) // ST.week_len + 1)


def parse_news():
    items = api_get("/news", since=ST.last_news_id)
    if not items:
        return
    for n in sorted(items, key=lambda x: x["news_id"]):
        if n["news_id"] <= ST.last_news_id:
            continue
        ST.last_news_id = n["news_id"]
        tick = n.get("tick", 0)
        ticker = str(n.get("ticker", "") or "")
        text = f"{n.get('headline', '')} {n.get('body', '')}"
        try:
            with open(ST.news_file, "a", encoding="utf-8") as f:
                f.write(f"tick={tick} ticker={ticker!r} headline={n.get('headline', '')!r} body={n.get('body', '')!r}\n")
        except OSError:
            pass
        # Which week does this item describe? The RIT 'ticker' field says "Week N" on announcements.
        # Otherwise fall back to the tick. (On the real server the week-N vol is announced at the START
        # of week N, e.g. "Week 2" at tick 74, so the ticker is the reliable source.)
        m_week = RE_WEEK.search(ticker) or RE_WEEK.search(n.get("headline", "") or "")
        w_tick = week_of(max(1, tick))
        w = int(m_week.group(1)) if m_week else w_tick
        low = text.lower()
        m_range = RE_RANGE.search(text)
        # single number: the % that follows the word "volatil" within a few words, so "risk free rate is 0%"
        # (which sits between a headline "volatility" and the body's "volatility is 39%") is skipped
        m_point = None
        for cand in RE_VOL_POINT.finditer(text):   # keep the LAST one: the announced vol ends these messages
            m_point = cand
        m_point = m_point or RE_POINT.search(text)
        if m_range and ("next week" in low or "forecast" in low or not m_week):
            # a range: a forecast for the week AFTER the current one
            lo, hi = float(m_range.group(1)), float(m_range.group(2))
            target = w_tick + 1
            ST.vol_est[target] = (lo + hi) / 200
            event(tick, "NEWS", f"Week {target} vol forecast {lo:g}-{hi:g}%  -> using {ST.vol_est[target]:.1%}")
        elif m_point and "volatil" in low:
            target = w + 1 if "next week" in low else w
            ST.vol_known[target] = float(m_point.group(1)) / 100
            event(tick, "NEWS", f"Week {target} vol is {ST.vol_known[target]:.1%}")
        elif "volatil" in low and "%" in text:
            event(n.get("tick", 0), "NEWS !!", f"NOT PARSED, type v/n yourself: {text[:110]}")
        else:
            event(n.get("tick", 0), "INFO", text[:110])


def vol_for_week(w):
    if w in ST.vol_known:
        return ST.vol_known[w]
    if w in ST.vol_est:
        return ST.vol_est[w]
    for prev in range(w - 1, 0, -1):          # fall back to most recent KNOWN vol. Tested falling back to the
        if prev in ST.vol_known:               # latest forecast instead: it traded earlier on the forecast and
            return ST.vol_known[prev]          # lost $1.5-6.9k/heat on the mock (more pre-boundary carry).
    return None


def fair_vol(tick):
    """sqrt of time-weighted variance over the options' remaining life."""
    var, tot = 0.0, 0
    for w in range(1, 5):
        start, end = (w - 1) * ST.week_len, w * ST.week_len
        rem = max(0, end - max(tick, start))
        if rem == 0:
            continue
        v = vol_for_week(w)
        if v is None:
            return None
        var += v * v * rem
        tot += rem
    return math.sqrt(var / tot) if tot else None


# ------------------------------- trading -------------------------------
def snapshot():
    secs = api_get("/securities")
    if not secs:
        return None
    return {x["ticker"]: x for x in secs}


def log_limit_diagnostics(tick):
    """Sample server limits once per tick after a rejection; never retry or block trading for long."""
    if not ST.limit_diag_pending.is_set():
        return
    ST.limit_diag_pending.clear()
    if ST.limit_diag_tick == tick:
        return
    ST.limit_diag_tick = tick
    try:
        response = s.get(f"{API}/limits", timeout=0.35)
        if response.ok:
            say(f"[limit snapshot] t={tick} after rejection: {response.text[:2000]}")
        else:
            say(f"[limit snapshot] t={tick} unavailable: HTTP {response.status_code}")
    except requests.RequestException as e:
        say(f"[limit snapshot] t={tick} unavailable: {e}")


def option_info(sec, S, T, fv):
    """Per-option fair price, edges, delta."""
    out = {}
    for k in STRIKES:
        for flag, tk in (("c", CALLS[k]), ("p", PUTS[k])):
            x = sec[tk]
            bid, ask = float(x["bid"] or 0), float(x["ask"] or 0)
            mid = (bid + ask) / 2
            iv = bs.implied_vol(flag, mid, S, k, T, RISK_FREE) if bid > 0 and ask > 0 else None
            fair = bs.price(flag, S, k, T, fv, RISK_FREE)
            d_vol = iv if iv else fv
            out[tk] = {
                "K": k, "flag": flag, "bid": bid, "ask": ask, "iv": iv, "fair": fair,
                # an empty side quotes 0: never treat that as a price (an ask of 0 looks like a huge buy edge)
                "buy_edge": fair - ask - OPT_FEE_PER_SHARE if ask > 0 else -1.0,
                "sell_edge": bid - fair - OPT_FEE_PER_SHARE if bid > 0 else -1.0,
                "delta": bs.delta(flag, S, k, T, d_vol, RISK_FREE),
                "gamma": bs.gamma(S, k, T, d_vol, RISK_FREE),
                "pos": int(x["position"]) - ST.arb_pos.get(tk, 0),   # main book's share
                "pos_total": int(x["position"]),
            }
    return out


def portfolio_delta(sec, info):
    d = int(sec["RTM"]["position"])
    for tk, o in info.items():
        d += o["pos_total"] * 100 * o["delta"]
    return d


def gamma_cap(S, vol_now):
    """Max |book gamma| (shares per $1) so a GAMMA_Z-sigma tick move stays inside the delta limit.
    Uses THIS week's vol: the next tick's move depends on current vol, not the average over the month."""
    move = GAMMA_Z * vol_now / math.sqrt(ST.tpy) * S
    return (7000 - HEDGE_TRIGGER) / move


def quick_hedge(tick, d):
    """Hedge immediately inside an option-trading burst so delta never sits outside the band."""
    need = int(round(HEDGE_TARGET - d))
    need = max(-RTM_LIMIT - ST.rtm_pos, min(RTM_LIMIT - ST.rtm_pos, need))
    side = "BUY" if need > 0 else "SELL"
    sg = 1 if side == "BUY" else -1
    clips = [("RTM", side, min(RTM_MAX_ORDER, abs(need) - i), None) for i in range(0, abs(need), RTM_MAX_ORDER)]
    _TLS.order_context = f"quick hedge, estimated delta={d:.0f}, RTM={ST.rtm_pos}"
    for filled in place_many(clips):
        ST.hedge_acc += sg * filled
        ST.rtm_pos += sg * filled
        d += sg * filled
    return d


def trade_options(info, tick, gcap):
    if ST.paused or tick >= ST.ticks - STOP_OPENING_TICKS_BEFORE_END:
        return False
    net = sum(o["pos_total"] for o in info.values())          # total net: the engine's legs count too, so
    gross = sum(abs(o["pos_total"]) for o in info.values())   # the main book leaves it room to unwind   # the gross limit counts the strike engine too
    # candidate trades ranked by $ edge per contract
    # Rank by edge, but trades that CLOSE an existing position go first: when the view flips,
    # buying back old shorts beats opening offsetting longs at a nearby strike (saves fees + limit room).
    cands = []
    net_now = sum(o["pos"] for o in info.values())
    if net_now > 50:
        ST.book_dir = 1
    elif net_now < -50:
        ST.book_dir = -1
    # (when flat, book_dir keeps its last value so a close-then-reopen the other way still counts as a flip)
    for tk, o in info.items():
        # opening against the book's remembered direction is a reversal: ask for more edge
        buy_need = REVERSE_EDGE if (ST.book_dir < 0 and o["pos"] >= 0) else MIN_EDGE
        sell_need = REVERSE_EDGE if (ST.book_dir > 0 and o["pos"] <= 0) else MIN_EDGE
        if o["buy_edge"] > buy_need:
            cands.append((o["pos"] < 0, o["buy_edge"], tk, "BUY"))
        if o["sell_edge"] > sell_need:
            cands.append((o["pos"] > 0, o["sell_edge"], tk, "SELL"))
    cands.sort(reverse=True)
    cands = [(edge, tk, side) for _, edge, tk, side in cands]
    traded = False
    n_orders = 0
    blocked_sides = set()  # a rejected direction cannot add option net until the next fresh loop
    fills = {}
    opt_delta = sum(o["pos"] * 100 * o["delta"] for o in info.values())
    book_g = sum(o["pos"] * 100 * o["gamma"] for o in info.values())
    gcap = gcap * GAMMA_ADD_UP_TO
    for edge, tk, side in cands:
        if side in blocked_sides:
            continue
        while n_orders < MAX_ORDERS_PER_LOOP:
            pos = info[tk]["pos"]
            if side == "BUY":
                room_net = MAX_NET_CONTRACTS - net
                total = info[tk]["pos_total"]
                room_gross = MAX_GROSS_CONTRACTS - gross + (2 * max(0, -total))
            else:
                room_net = MAX_NET_CONTRACTS + net
                total = info[tk]["pos_total"]
                room_gross = MAX_GROSS_CONTRACTS - gross + (2 * max(0, total))
            q = min(CLIP, room_net, room_gross)
            # keep the options book hedgeable: skip trades that push |option delta| past the cap
            d_per = (1 if side == "BUY" else -1) * 100 * info[tk]["delta"]
            if d_per and abs(opt_delta + d_per * q) > OPTION_DELTA_CAP and abs(opt_delta + d_per * q) > abs(opt_delta):
                q = min(q, int(max(0, (OPTION_DELTA_CAP - abs(opt_delta)) / abs(d_per))))
            g_per = (1 if side == "BUY" else -1) * 100 * info[tk]["gamma"]
            if g_per and abs(book_g + g_per * q) > gcap and abs(book_g + g_per * q) > abs(book_g):
                q = min(q, int(max(0, (gcap - abs(book_g)) / abs(g_per))))
            closing = (side == "BUY" and pos < 0) or (side == "SELL" and pos > 0)
            against_book = (side == "BUY" and ST.book_dir < 0) or (side == "SELL" and ST.book_dir > 0)
            if closing:
                q = min(q, abs(pos)) if q > 0 else 0
            elif against_book and edge <= REVERSE_EDGE:
                # this leg was admitted as a CLOSE at MIN_EDGE and is now flat: carrying on would open a
                # position against the book, which is a reversal and needs REVERSE_EDGE
                break
            if q <= 0 or (q < MIN_OPEN_QTY and not closing):
                break
            px = info[tk]["ask"] if side == "BUY" else info[tk]["bid"]     # the price the edge was computed on
            _TLS.order_context = f"main book, local net={net}, gross={gross}, edge={edge:.4f}"
            filled = place(tk, side, q, limit=px)
            n_orders += 1
            if filled <= 0:
                if getattr(_TLS, "last_opt_net_rejection", False):
                    blocked_sides.add(side)
                break
            sgn = 1 if side == "BUY" else -1
            # A fill in the opposite direction can reopen net-limit room during this loop.
            blocked_sides.discard("SELL" if side == "BUY" else "BUY")
            old = info[tk]["pos"]
            info[tk]["pos"] = old + sgn * filled
            old_total = info[tk]["pos_total"]
            info[tk]["pos_total"] = old_total + sgn * filled
            net += sgn * filled
            opt_delta += sgn * filled * 100 * info[tk]["delta"]
            book_g += sgn * filled * 100 * info[tk]["gamma"]
            gross += abs(old_total + sgn * filled) - abs(old_total)
            traded = True
            ST.run_delta += sgn * filled * 100 * info[tk]["delta"]
            if abs(ST.run_delta) > HEDGE_TRIGGER:
                ST.run_delta = quick_hedge(tick, ST.run_delta)
            k = (side, tk)
            fills[k] = (fills.get(k, (0, edge))[0] + filled, edge)
        if n_orders >= MAX_ORDERS_PER_LOOP:
            break
    for k, (qty, edge) in fills.items():
        prev = ST.fill_acc.get(k, (0, edge))[0]
        ST.fill_acc[k] = (prev + qty, edge)
    return traded


def strike_ivs(info):
    """Each strike's implied vol (call and put agree on the real server)."""
    out = {}
    for k in STRIKES:
        c, p = info[CALLS[k]]["iv"], info[PUTS[k]]["iv"]
        if c and p:
            out[k] = (c + p) / 2
    return out


def straddle(info, k, n):
    """Delta-neutral straddle of n contracts at strike k: calls n(1-dc), puts n*dc."""
    dc = info[CALLS[k]]["delta"]
    nc = int(round(n * (1 - dc)))
    return {CALLS[k]: nc, PUTS[k]: n - nc}


def arb_orders(info, tick, buys, sells, entry=False):
    """Trade two baskets in two waves of 100-lots sent at the same time: first every clip on the side that moves
    net toward zero, then the other side, capped at what the first wave filled. Net never goes beyond where it
    started (the main book may be at the net limit), and a pair takes 2 round trips instead of 14+."""
    net = sum(o["pos_total"] for o in info.values())
    gross = sum(abs(o["pos_total"]) for o in info.values())
    good = "SELL" if net > 0 else "BUY"                  # side that moves net toward zero
    baskets = {"BUY": buys, "SELL": sells}
    traded = False
    buffer = 0
    done = {"BUY": {}, "SELL": {}}
    for side in (good, "BUY" if good == "SELL" else "SELL"):
        cap = None if side == good or not any(n > 0 for n in baskets[good].values()) else buffer
        if not entry:
            # For a one-sided or partial unwind, never request more than the locally estimated
            # remaining exchange net capacity. A smaller close can succeed where a 100-lot is rejected.
            room = max(0, CASE_NET_LIMIT - net if side == "BUY" else CASE_NET_LIMIT + net)
            cap = room if cap is None else min(cap, room)
        clips = []
        for tk, n in baskets[side].items():
            # A missing quote is not an executable price. In particular, a zero bid used as a sell
            # limit could execute at an arbitrary lower price than the strategy intended.
            px = info[tk]["ask"] if side == "BUY" else info[tk]["bid"]
            if px <= 0:
                continue
            while n > 0:
                q = min(CLIP, n) if cap is None else min(CLIP, n, cap)
                if q <= 0:
                    break
                clips.append((tk, side, q, px))
                n -= q
                if cap is not None:
                    cap -= q
        sgn = 1 if side == "BUY" else -1
        _TLS.order_context = f"{'entry' if entry else 'exit'} basket {side}, starting net={net}, gross={gross}, buys={buys}, sells={sells}"
        for (tk, _, q, _), filled in zip(clips, place_many(clips)):
            if filled > 0:
                traded = True
                ST.arb_pos[tk] = ST.arb_pos.get(tk, 0) + sgn * filled
                info[tk]["pos_total"] += sgn * filled
                net += sgn * filled
                ST.run_delta += sgn * filled * 100 * info[tk]["delta"]
                done[side][tk] = done[side].get(tk, 0) + filled
                if side == good:
                    buffer += filled
    bad = "BUY" if good == "SELL" else "SELL"
    excess = sum(done[good].values()) - sum(done[bad].values())
    if entry and excess > 0:
        # second leg came up short (limit orders that missed): take back the unmatched part of the first leg at
        # market, so the engine never holds a lopsided pair it may not be able to unwind at the net limit
        back = "BUY" if good == "SELL" else "SELL"
        sgn = 1 if back == "BUY" else -1
        clips = []
        for tk, n in sorted(done[good].items(), key=lambda kv: -kv[1]):
            while n > 0 and excess > 0:
                q = min(CLIP, n, excess)
                clips.append((tk, back, q, None))
                n -= q
                excess -= q
        _TLS.order_context = f"basket imbalance rollback, starting net={net}, gross={gross}"
        for (tk, _, q, _), filled in zip(clips, place_many(clips)):
            if filled > 0:
                ST.arb_pos[tk] = ST.arb_pos.get(tk, 0) + sgn * filled
                info[tk]["pos_total"] += sgn * filled
                ST.run_delta += sgn * filled * 100 * info[tk]["delta"]
    if abs(ST.run_delta) > HEDGE_TRIGGER:                # one hedge for the whole basket
        ST.run_delta = quick_hedge(tick, ST.run_delta)
    return traded


def arb_step(info, tick, S, T):
    """Strike-vs-strike engine. The market maker re-draws each strike's vol every tick around a common level.
    Buy a delta-neutral straddle at the cheapest strike, sell one at the richest, both N contracts, so net
    contracts stay zero (the main book keeps the whole net limit; this uses gross room). Leave when the gap
    has closed, after ARB_MAX_HOLD ticks, or before the end-of-heat close-out."""
    if not ARB_ON:
        return False
    ivs = strike_ivs(info)
    traded = False
    end = ST.ticks - CLOSE_TICKS_BEFORE_END - 1
    if ST.arb and ST.arb.get("kind") == "fly":
        a = ST.arb
        left, middle, right = a["legs"]
        o1, o2, o3 = info[left], info[middle], info[right]
        # Closing pays two middle asks and sells the wings at their bids.
        quoted = o1["bid"] > 0 and o2["ask"] > 0 and o3["bid"] > 0
        close_cost = 2 * o2["ask"] - o1["bid"] - o3["bid"] if quoted else None
        profit = a["credit"] - close_cost - 8 * OPT_FEE_PER_SHARE if quoted else None
        if a.get("incomplete") or tick >= end or tick - a["t0"] >= ARB_MAX_HOLD or (profit is not None and profit >= FLY_EXIT_PROFIT):
            held = {tk: n for tk, n in ST.arb_pos.items() if n}
            traded = arb_orders(info, tick, {tk: -n for tk, n in held.items() if n < 0},
                                {tk: n for tk, n in held.items() if n > 0})
            if not any(ST.arb_pos.values()):
                event(tick, "FLY", f"closed butterfly after {tick - a['t0']} ticks")
                ST.arb = None
            else:
                a["incomplete"] = True  # keep trying; do not leave an uneven basket unattended
            return traded
        return False
    if ST.arb:
        a = ST.arb
        gap = (ivs[a["short_k"]] - ivs[a["long_k"]]) * 100 if a["short_k"] in ivs and a["long_k"] in ivs else None
        if (gap is not None and gap <= ARB_EXIT) or tick - a["t0"] >= ARB_MAX_HOLD or tick >= end:
            held = {tk: n for tk, n in ST.arb_pos.items() if n}
            traded = arb_orders(info, tick, {tk: -n for tk, n in held.items() if n < 0},
                                {tk: n for tk, n in held.items() if n > 0})
            gap_text = f"{gap:+.1f} pts" if gap is not None else "unavailable"
            event(tick, "ARB", f"out: {a['long_k']}/{a['short_k']} after {tick - a['t0']} ticks, gap now {gap_text}")
            if not any(ST.arb_pos.values()):
                ST.arb = None
            else:
                a["fails"] = 0 if traded else a.get("fails", 0) + 1
                if a["fails"] < 3:
                    return traded                        # something didn't fill: try again next loop
                left = ", ".join(f"{tk}:{n:+d}" for tk, n in ST.arb_pos.items() if n)
                event(tick, "ARB", f"could not unwind {left}; the main book takes them over")
                ST.arb_pos = {}
                ST.arb = None
    if ST.arb or ST.paused or tick >= ST.ticks - ARB_LAST_ENTRY or len(ivs) < 2:
        return traded
    if FLY_ON and FLY_SIZE > 0:
        candidates = []
        for tickers in (CALLS, PUTS):
            for low, width in ((48, 1), (49, 1), (50, 1), (48, 2)):
                left, middle, right = tickers[low], tickers[low + width], tickers[low + 2 * width]
                o1, o2, o3 = info[left], info[middle], info[right]
                if min(o1["ask"], o2["bid"], o3["ask"]) <= 0:
                    continue
                credit = 2 * o2["bid"] - o1["ask"] - o3["ask"]
                if credit - 4 * OPT_FEE_PER_SHARE >= FLY_MIN_CREDIT:
                    candidates.append((credit, left, middle, right))
        if candidates:
            credit, left, middle, right = max(candidates)
            net = sum(o["pos_total"] for o in info.values())
            gross = sum(abs(o["pos_total"]) for o in info.values())
            n = min(FLY_SIZE, CLIP // 2, max(0, (MAX_GROSS_CONTRACTS - gross) // 4),
                    max(0, (MAX_NET_CONTRACTS + abs(net)) // 2))
            if n > 0:
                traded = arb_orders(info, tick, {left: n, right: n}, {middle: 2 * n}, entry=True) or traded
                held = {tk: v for tk, v in ST.arb_pos.items() if v}
                if held:
                    actual = held.get(left, 0)
                    complete = actual > 0 and held.get(right, 0) == actual and held.get(middle, 0) == -2 * actual
                    ST.arb = {"kind": "fly", "legs": (left, middle, right), "t0": tick,
                              "credit": credit, "incomplete": not complete}
                    event(tick, "FLY", f"{left}/{middle}/{right}: {actual} wings, credit ${credit * 100:.0f} per butterfly"
                          + ("; incomplete fills, unwinding" if not complete else ""))
                    return traded
    # A strike whose call or put is bid at the 1-cent floor has a fake vol: the far option is worth ~0 and the
    # near one ~intrinsic, so a 1-cent rounding moves its vol by many points. Its "gap" is pure fees and spread.
    ivs = {k: v for k, v in ivs.items()
           if min(info[CALLS[k]]["bid"], info[PUTS[k]]["bid"]) >= ARB_MIN_BID - 1e-9}
    if len(ivs) < 2:
        return traded
    lk, sk = min(ivs, key=ivs.get), max(ivs, key=ivs.get)
    gap = (ivs[sk] - ivs[lk]) * 100
    vega = (bs.vega(S, lk, T, ivs[lk]) + bs.vega(S, sk, T, ivs[sk])) / 2
    if gap * vega < ARB_MIN_DOLLARS:
        return traded
    gross = sum(abs(o["pos_total"]) for o in info.values())
    n = min(ARB_SIZE, (MAX_GROSS_CONTRACTS - gross) // 2)
    if n < CLIP:
        return traded
    buys, sells = straddle(info, lk, n), straddle(info, sk, n)
    traded = arb_orders(info, tick, buys, sells, entry=True) or traded
    if not any(ST.arb_pos.values()):
        return traded                                    # nothing (net) filled: no pair to hold
    ST.arb = {"long_k": lk, "short_k": sk, "t0": tick}
    event(tick, "ARB", f"in: buy {lk} straddle, sell {sk} straddle, {n} each; gap {gap:.1f} pts "
                       f"(${gap * vega:,.0f}/contract)")
    return traded


def trim_gamma(info, tick, gcap):
    """As expiry nears, gamma per contract rises. Cut the highest-gamma legs when the book exceeds the cap.
    Legs still holding a big edge (selling them would cross > GAMMA_KEEP_EDGE of fair value) are kept until
    the book passes GAMMA_EDGE_MAX caps; the others are cut first, as before."""
    book_g = sum(o["pos"] * 100 * o["gamma"] for o in info.values())
    if abs(book_g) <= gcap * GAMMA_TRIM_ABOVE:
        return False
    sg = 1 if book_g > 0 else -1
    keep_on = GAMMA_EDGE_MAX > 0 and tick < ST.ticks - GAMMA_EDGE_UNTIL_END
    hard = gcap * max(GAMMA_EDGE_MAX, GAMMA_TRIM_ABOVE)

    def protected(o):
        # edge given up by closing this leg: long -> sell at bid, short -> buy at ask (both vs fair)
        crossed = (o["fair"] - o["bid"]) if o["pos"] > 0 else (o["ask"] - o["fair"])
        return keep_on and crossed > GAMMA_KEEP_EDGE

    # cut low-edge legs first, then (only past the hard cap) the protected ones
    legs = sorted(info.items(), key=lambda kv: (protected(kv[1]), -sg * kv[1]["pos"] * kv[1]["gamma"]))
    traded = False
    n = 0
    for tk, o in legs:
        if n >= MAX_ORDERS_PER_LOOP or abs(book_g) <= gcap:
            break
        if sg * o["pos"] * o["gamma"] <= 0:
            continue
        if protected(o):
            if abs(book_g) <= hard:
                continue
            excess = abs(book_g) - hard              # protected legs only come back to the hard cap
        else:
            excess = abs(book_g) - gcap
        q = min(CLIP, abs(o["pos"]), max(1, int(math.ceil(excess / (100 * o["gamma"])))))
        side = "SELL" if o["pos"] > 0 else "BUY"
        _TLS.order_context = f"gamma trim, local net={sum(x['pos_total'] for x in info.values())}"
        filled = place(tk, side, q)
        n += 1
        if filled <= 0:
            continue
        chg = -filled if side == "SELL" else filled
        o["pos"] += chg
        o["pos_total"] += chg
        book_g += chg * 100 * o["gamma"]
        ST.run_delta += chg * 100 * o["delta"]
        if abs(ST.run_delta) > HEDGE_TRIGGER:
            ST.run_delta = quick_hedge(tick, ST.run_delta)
        traded = True
        event(tick, "RISK", f"{side} {filled} {tk} to cut gamma (gamma {book_g:,.0f}, cap {gcap:,.0f})")
    return traded


def rebalance_option_delta(info, tick):
    """Gamma drift can push the option book's delta beyond what 50k RTM can hedge. Trim the worst legs."""
    opt_delta = sum(o["pos"] * 100 * o["delta"] for o in info.values())
    if abs(opt_delta) <= OPTION_DELTA_REBAL:
        return False
    sgn = 1 if opt_delta > 0 else -1
    # legs contributing most delta in the offending direction
    legs = sorted(info.items(), key=lambda kv: -sgn * kv[1]["pos"] * kv[1]["delta"])
    traded = False
    for tk, o in legs[:2]:
        contrib = o["pos"] * 100 * o["delta"]
        if sgn * contrib <= 0 or o["pos"] == 0:
            continue
        q = min(CLIP, abs(o["pos"]))
        side = "SELL" if o["pos"] > 0 else "BUY"
        _TLS.order_context = f"delta rebalance, local net={sum(x['pos_total'] for x in info.values())}"
        filled = place(tk, side, q)
        if filled > 0:
            chg = -filled if side == "SELL" else filled
            o["pos"] += chg
            o["pos_total"] += chg
            ST.run_delta += chg * 100 * o["delta"]
            ST.run_delta = quick_hedge(tick, ST.run_delta)
            traded = True
            event(tick, "RISK", f"{side} {filled} {tk} to cut option delta ({opt_delta:,.0f})")
    return traded


def close_options(info, tick, S):
    """Before expiry, unwind options NEAR THE MONEY (their delta can flip 0 <-> 1 in a tick).
    Far ITM/OTM options are held: they settle for free and their delta is stable.
    But the far ones must still be hedgeable: if their combined delta exceeds what RTM can absorb
    (RTM_LIMIT minus a buffer), close the biggest-delta legs too until it fits."""
    traded = False
    n = 0
    far = [(tk, o) for tk, o in info.items() if abs(o["K"] - S) > CLOSE_NEAR and o["pos"]]
    far_delta = sum(o["pos"] * 100 * o["delta"] for _, o in far)
    must_close = {tk for tk, o in info.items() if abs(o["K"] - S) <= CLOSE_NEAR}
    if abs(far_delta) > RTM_LIMIT - 5000:
        # close far legs in order of how much delta they carry, until the remainder is hedgeable
        for tk, o in sorted(far, key=lambda kv: -abs(kv[1]["pos"] * kv[1]["delta"])):
            if abs(far_delta) <= RTM_LIMIT - 5000:
                break
            must_close.add(tk)
            far_delta -= o["pos"] * 100 * o["delta"]
    order = sorted(info.items(), key=lambda kv: -abs(kv[1]["pos"] * kv[1]["delta"]))   # most delta first
    for tk, o in order:
        if tk not in must_close:
            continue
        while o["pos"] != 0 and n < MAX_ORDERS_PER_LOOP:
            q = min(CLIP, abs(o["pos"]))
            side = "SELL" if o["pos"] > 0 else "BUY"
            _TLS.order_context = f"expiry close, local net={sum(x['pos_total'] for x in info.values())}"
            filled = place(tk, side, q)
            n += 1
            if filled <= 0:
                break
            chg = -filled if side == "SELL" else filled
            o["pos"] += chg
            o["pos_total"] += chg
            ST.run_delta += chg * 100 * o["delta"]
            if abs(ST.run_delta) > HEDGE_TRIGGER:
                ST.run_delta = quick_hedge(tick, ST.run_delta)
            traded = True
        if n >= MAX_ORDERS_PER_LOOP:
            break
    if traded:
        event(tick, "CLOSE", f"closing options within ${CLOSE_NEAR} of RTM before expiry")
    return traded


def hedge(sec, info, tick, force=False):
    d = portfolio_delta(sec, info)
    if not force and abs(d) <= HEDGE_TRIGGER:
        return d
    need = int(round(HEDGE_TARGET - d))
    rtm = int(sec["RTM"]["position"])
    need = max(-RTM_LIMIT - rtm, min(RTM_LIMIT - rtm, need))
    side = "BUY" if need > 0 else "SELL"
    sg = 1 if side == "BUY" else -1
    clips = [("RTM", side, min(RTM_MAX_ORDER, abs(need) - i), None) for i in range(0, abs(need), RTM_MAX_ORDER)]
    _TLS.order_context = f"hedge, estimated delta={d:.0f}, RTM={rtm}"
    did = sg * sum(place_many(clips))
    d += did
    if did:
        ST.hedge_acc += did
    elif abs(d) > 7000:
        event(tick, "!! RISK", f"CANNOT HEDGE (no hedge fill or RTM limit), delta {d:+,.0f}")
    return d


def open_heat_logs(old=None):
    """Start a fresh folder for this heat: logs/vol/<date_time>/ with vol_log.csv, news_raw.txt, console.txt,
    and the market log (quotes.csv, tas.csv, fills.csv; see MarketLog)."""
    if old:
        old.close()
    folder = os.path.join(LOG_ROOT, f"{datetime.now():%Y%m%d_%H%M%S}")
    n = 1
    while os.path.exists(folder):
        n += 1
        folder = os.path.join(LOG_ROOT, f"{datetime.now():%Y%m%d_%H%M%S}_{n}")
    os.makedirs(folder)
    ST.news_file = os.path.join(folder, "news_raw.txt")
    import display
    if display._logf:
        display._logf.close()
    display.set_log_file(os.path.join(folder, "console.txt"))
    logf = open(os.path.join(folder, "vol_log.csv"), "w", newline="")
    log = csv.writer(logf)
    log.writerow(["tick", "S", "fair_vol", "atm_iv", "net_contracts", "delta", "nlv"])
    MKT.set_folder(folder)
    say(f"[log] this heat is saved in {folder}")
    return logf, log


def flush_tick(tick):
    """Print this tick's trades and hedges as one line each."""
    for (side, tk), (qty, edge) in sorted(ST.fill_acc.items()):
        event(tick, side, f"{qty:>4} x {tk:<7} edge ${edge * 100:,.0f}/contract")
    if ST.hedge_acc:
        event(tick, "HEDGE", f"{'bought' if ST.hedge_acc > 0 else 'sold'} {abs(ST.hedge_acc):,} RTM")
    ST.fill_acc = {}
    ST.hedge_acc = 0


# ------------------------------- console commands -------------------------------
def console():
    for line in sys.stdin:
        parts = line.strip().lower().split()
        if not parts:
            continue
        c = parts[0]
        try:
            tick = api_get("/case")["tick"]
            w = week_of(max(1, tick))
            if c == "v" and len(parts) >= 2:
                ST.vol_known[w] = float(parts[1]) / 100
                say(f"[override] week {w} vol = {ST.vol_known[w]:.3f}")
            elif c == "n" and len(parts) >= 3:
                ST.vol_est[w + 1] = (float(parts[1]) + float(parts[2])) / 200
                say(f"[override] week {w + 1} est = {ST.vol_est[w + 1]:.3f}")
            elif c == "p":
                ST.paused = True
                say("[paused] no new option trades; hedging continues")
            elif c == "r":
                ST.paused = False
                say("[resumed]")
            elif c == "h":
                ST.force_hedge = True
            elif c == "s":
                say(f"[status] known={ST.vol_known} est={ST.vol_est} paused={ST.paused}")
            elif c == "q":
                ST.quit = True
                return
        except Exception as e:
            say(f"[console error] {e}")


# ------------------------------- main loop -------------------------------
def main():
    threading.Thread(target=console, daemon=True).start()
    banner("VOL ALGO  |  waiting for the case to start\nCommands (type + Enter): v 25 | n 27 30 | p pause | r resume | h hedge | s status | q quit")
    logf = log = None                                   # opened when a heat is actually running
    last_print = -1
    while not ST.quit:
        case = api_get("/case")
        if not case or case["status"] != "ACTIVE":
            time.sleep(0.5)
            continue
        tick = case["tick"]
        if tick < ST.last_tick:                       # new heat started: reset memory
            banner("NEW HEAT")
            ST.reset()
            logf, log = open_heat_logs(logf)
        if logf is None:
            logf, log = open_heat_logs()
        if ST.last_tick == -1 and tick > 5:
            event(tick, "NOTE", f"joined mid-heat at tick {tick}; reading past news first")
        if tick != ST.flush_tick:
            flush_tick(ST.flush_tick)
            ST.flush_tick = tick
        ST.last_tick = tick
        ST.ticks = case.get("ticks_per_period", 300)
        ST.week_len = ST.ticks // 4
        ST.tpy = ST.ticks * 12
        if tick < 1 or tick >= ST.ticks:
            time.sleep(LOOP_SLEEP)
            continue

        loop_start = time.time()
        parse_news()
        sec = snapshot()
        if not sec:
            continue
        rb, ra = float(sec["RTM"]["bid"] or 0), float(sec["RTM"]["ask"] or 0)
        S = (rb + ra) / 2 if rb > 0 and ra > 0 else float(sec["RTM"].get("last") or 0)
        if S <= 0:                                    # no RTM quote yet (seen at t=1): nothing to price against
            time.sleep(LOOP_SLEEP)
            continue
        T = (ST.ticks - tick) / ST.tpy
        fv = fair_vol(tick)
        if fv is None:
            time.sleep(LOOP_SLEEP)
            continue

        info = option_info(sec, S, T, fv)
        ST.rtm_pos = int(sec["RTM"]["position"])
        ST.run_delta = portfolio_delta(sec, info)
        if arb_step(info, tick, S, T):
            sec = snapshot() or sec
            info = option_info(sec, S, T, fv)
            ST.run_delta = portfolio_delta(sec, info)
        if tick >= ST.ticks - CLOSE_TICKS_BEFORE_END:
            traded = close_options(info, tick, S)
        else:
            gcap = gamma_cap(S, vol_for_week(week_of(tick)) or fv)
            traded = trim_gamma(info, tick, gcap)
            traded = rebalance_option_delta(info, tick) or traded
            traded = trade_options(info, tick, gcap) or traded
        if traded:
            sec = snapshot() or sec
            info = option_info(sec, S, T, fv)
        d = hedge(sec, info, tick, force=traded or ST.force_hedge)
        ST.force_hedge = False
        log_limit_diagnostics(tick)

        if tick != last_print:
            last_print = tick
            atm = min(STRIKES, key=lambda k: abs(k - S))
            atm_iv = info[CALLS[atm]]["iv"]
            net = sum(o["pos"] for o in info.values())
            trader = api_get("/trader") or {}
            nlv = trader.get("nlv", float("nan"))
            log.writerow([tick, round(S, 2), round(fv, 4), round(atm_iv or 0, 4), net, round(d), nlv])
            logf.flush()
            if tick % STATUS_EVERY == 0:
                mkt = f"{atm_iv:5.1%}" if atm_iv else "  -- "
                event(tick, "status", f"RTM {S:6.2f}  vol {fv:5.1%} vs mkt {mkt}  net {net:+5d}  "
                                      f"delta {d:+7,.0f}  P&L {nlv:+9,.0f}")
        took = time.time() - loop_start
        if took > 1.0 and tick - ST.last_slow_warn >= 10:
            ST.last_slow_warn = tick
            event(tick, "SLOW !!", f"one loop took {took:.1f}s (should be <0.5s). API is slow: check the connection")
        if not (FAST_ENTRY and traded):          # after a trading loop, look again at once: edge may remain
            time.sleep(LOOP_SLEEP)
    if logf:
        logf.close()
    say("Stopped.")


if __name__ == "__main__":
    main()
