"""
RITCx Algorithmic ETF Arbitrage Case - trading algorithm.

Core idea: every RITC position is immediately HEDGED with the basket (BULL + BEAR), so we hold
"pairs" instead of naked RITC. Because the case closes out at the CORRECT price, a hedged pair
converges for free at the end. Profit comes from the price at which we enter the pair:

  1. TENDERS: accept only if (tender price vs. the basket we can actually trade, walking the book)
     beats fees + a safety edge. Auction tenders get a bid priced to keep that edge.
  2. HEDGE: keep BULL = BEAR = -RITC at all times, trading only liquidity within a slippage cap.
  3. ARB: when RITC vs basket is mispriced beyond costs, trade RITC (the hedge module adds the basket).
     Trades that shrink an existing pair need less edge, since they also free up limit capacity.
  4. FX: RITC settles in USD, so keep the USD cash position near 0 with the USD ticker.
  5. CONVERTERS: tenders (70k-100k RITC) are too big to hedge with stock alone, because a hedged
     share uses 4 units of gross (RITC counts 2x). So we accept the tender, hedge what fits, and
     redeem/create hedged 10k blocks: each block cancels RITC against BULL+BEAR and frees 40k of
     gross for the next part of the hedge. Done through the lease API when the server allows it,
     otherwise the algo prints "CONVERTER: REDEEM 1 x 10,000 NOW" for you to click.

Live commands (type + Enter):  p / r  pause / resume tenders+arb (hedging continues)
                               s      status        q  quit
"""
import csv
import math
import os
import sys
import threading
import time
from datetime import datetime

import requests

from display import say, event, banner

# =========================== SETTINGS (tune between heats) ===========================
from config import API, API_KEY   # read from .env (see .env.example); defaults to localhost:9999 / Rotman

FEE = 0.02                    # market order fee per share
MAX_EQ_ORDER = 10000
MAX_FX_ORDER = 2_500_000

# Tenders
TENDER_MIN_EDGE = 0.04        # CAD/share profit required after hedge cost + fees
TENDER_MIN_PROFIT = 500       # CAD total; skip tiny wins
UNHEDGED_PENALTY = 0.03       # CAD/share charged on the part we cannot hedge right away (waits for converters)
EVAL_SLICE = 20000            # walk the book for this many shares; assume patience for the rest
EXTRA_SLIP_BEYOND_SLICE = 0.01  # extra CAD/share assumed on shares beyond the slice
LIMIT_USAGE_MAX = 0.90        # arb keeps usage below this
LIMIT_TRANSIENT_MAX = 0.97    # max usage right after accepting a tender, before any hedge trades
HEDGE_USAGE_MAX = 0.96        # hedge orders never push gross usage above this (converters free the rest)

# Hedging: passive first (limit orders earn the rebate), market orders only for risk above URGENT_GAP
URGENT_GAP = 10000            # max unhedged shares per leg we tolerate; anything beyond is hedged at market
THIN_BOOK = 2500              # if fewer shares than this sit at the best price, the book is "thin":
THIN_GAP_MULT = 2.0           # ...tolerate URGENT_GAP x this before hitting it with market orders
HEDGE_SLIP = 0.03             # market hedges only take liquidity within this distance of the best price
HEDGE_MIN = 100               # ignore smaller imbalances
REBATE = 0.01

# Arbitrage
ARB_OPEN_EDGE = 0.03          # CAD/share net of 3 fees, to OPEN / grow pairs
ARB_CLOSE_EDGE = 0.00         # CAD/share net of fees, to CLOSE existing pairs (frees capacity)
ARB_CLIP = 5000
ARB_MAX_PAIRS = 20000         # max |RITC| the arb module builds on its own (keeps limit room for tenders)

# FX
FX_BAND = 20000               # re-hedge when |USD cash| > this

# Converters
CONVERT_BLOCK = 10000
CONVERT_FEE_USD = 1500            # per block
CONVERT_TICKS = 2                 # one use takes this many ticks; one use at a time
CONVERT_TARGET = 0.90             # convert hedged blocks until the fully hedged book fits under this usage
CONVERT_STOP_BEFORE_END = 5       # no new conversions in the last N ticks
AUTO_CONVERT = os.environ.get("AUTO_CONVERT", "1") == "1"   # use the lease API; falls back to prompts if it fails
MANUAL_REPROMPT_TICKS = 4         # manual mode: repeat the prompt if the position hasn't changed after this

DEFAULT_GROSS, DEFAULT_NET = 300_000, 100_000   # used if /limits is unavailable
STATUS_EVERY = 5              # print a status line every N ticks
LOOP_SLEEP = float(os.environ.get("LOOP_SLEEP", 0.15))
LOG_FILE = f"etf_log_{datetime.now():%Y%m%d_%H%M%S}.csv"
# =====================================================================================

s = requests.Session()
s.headers.update({"X-API-Key": API_KEY})


class State:
    paused = False
    quit = False
    seen_tenders = set()
    last_tick = -1
    last_prompt_tick = -100
    last_slow_warn = -100
    flush_tick = 0
    arb_acc = {}          # side -> [shares, last edge], printed once per tick
    auto_convert = AUTO_CONVERT
    leases = {}           # "redeem"/"create" -> lease id
    conv_busy_until = -1  # tick when the converter in use is free again
    conv_pending = {}     # outputs not yet delivered (only if the server takes inputs at the start)
    conv_done = 0


ST = State()


# ------------------------------- API helpers -------------------------------
def _req(method, path, **params):
    for _ in range(5):
        try:
            r = s.request(method, f"{API}{path}", params=params, timeout=2)
            if r.status_code == 429:
                try:
                    time.sleep(float(r.json().get("wait", 0.1)))
                except Exception:
                    time.sleep(0.1)
                continue
            return r
        except requests.RequestException as e:
            say(f"[warn] {method} {path}: {e}")
            time.sleep(0.2)
    return None


def get(path, **params):
    r = _req("GET", path, **params)
    return r.json() if r is not None and r.ok else None


def market(ticker, action, qty):
    qty = int(qty)
    if qty <= 0:
        return 0
    r = _req("POST", "/orders", ticker=ticker, type="MARKET", quantity=qty, action=action)
    if r is None or not r.ok:
        say(f"[order rejected] {action} {qty} {ticker}: {r.text[:120] if r is not None else 'no response'}")
        return 0
    return int(r.json().get("quantity_filled", qty))


# ------------------------------- market data -------------------------------
def snapshot():
    secs = get("/securities")
    if not secs:
        return None
    pos = {x["ticker"]: float(x.get("position", 0)) for x in secs}
    quotes = {x["ticker"]: (float(x.get("bid", 0) or 0), float(x.get("ask", 0) or 0)) for x in secs}
    books = {}
    for t in ("BULL", "BEAR", "RITC"):
        b = get("/securities/book", ticker=t, limit=40)
        if not b:
            return None
        books[t] = {"bids": [(float(l["price"]), int(l["quantity"]) - int(l.get("quantity_filled", 0)))
                             for l in b["bids"] if l.get("trader_id") != "self"],
                    "asks": [(float(l["price"]), int(l["quantity"]) - int(l.get("quantity_filled", 0)))
                             for l in b["asks"] if l.get("trader_id") != "self"]}
    lim = get("/limits")
    if lim:
        L = lim[0]
        gl, nl = float(L.get("gross_limit") or DEFAULT_GROSS), float(L.get("net_limit") or DEFAULT_NET)
    else:
        gl, nl = DEFAULT_GROSS, DEFAULT_NET
    return {"pos": pos, "q": quotes, "books": books, "gross_limit": gl, "net_limit": nl}


def snapshot_at(tick, ticks):
    snap = snapshot()
    if snap:
        snap["tick"], snap["ticks"] = tick, ticks
    return snap


def walk(levels, q, sgn, extra=EXTRA_SLIP_BEYOND_SLICE):
    """Average price to trade q shares through book levels (list of (price, qty)).
    Beyond EVAL_SLICE shares, assume the last walked price + extra (patient execution)."""
    if q <= 0 or not levels:
        return None
    # sgn: -1 when selling into bids (prices get worse going down), +1 when buying from asks
    target = min(q, EVAL_SLICE)
    got, cost, last = 0, 0.0, levels[0][0]
    for px, sz in levels:
        take = min(sz, target - got)
        if take <= 0:
            break
        cost += take * px
        got += take
        last = px
    if got < target:                     # book thinner than slice: penalize the rest
        cost += (target - got) * (last + sgn * 0.05)
        last = last + sgn * 0.05
    if q > target:
        cost += (q - target) * (last + sgn * extra)
    return cost / q


def depth_within(levels, slip):
    if not levels:
        return 0
    best = levels[0][0]
    return sum(sz for px, sz in levels if abs(px - best) <= slip + 1e-9)


def usage(pos, gl, nl, d_ritc=0, d_bull=0, d_bear=0):
    b, e, r = pos.get("BULL", 0) + d_bull, pos.get("BEAR", 0) + d_bear, pos.get("RITC", 0) + d_ritc
    gross = abs(b) + abs(e) + 2 * abs(r)
    net = b + e + 2 * r
    return max(gross / gl, abs(net) / nl)


# ------------------------------- 1. tenders -------------------------------
def leg_value(levels, mid, q, sgn):
    """Expected CAD/share (incl. fees) to trade q shares of one leg the way hedge() does:
    the part beyond URGENT_GAP at market (walking the book), the rest passively near mid."""
    qm = max(0, q - URGENT_GAP)
    qp = q - qm
    total = qp * (mid + sgn * REBATE * -1)          # passive: ~mid, plus rebate
    if qm:
        total += qm * ((walk(levels, qm, sgn) or mid) + sgn * FEE)
    return total / q


def blocks_to_convert(pos, gl, nl, d_ritc=0):
    """Hedged 10k blocks we must redeem/create so the FULLY hedged book fits under CONVERT_TARGET."""
    r = pos.get("RITC", 0) + d_ritc
    full = usage({"RITC": r, "BULL": -r, "BEAR": -r}, gl, nl)
    if full <= CONVERT_TARGET:
        return 0
    excess = (full - CONVERT_TARGET) * gl            # gross units over target (net is 0 when hedged)
    return math.ceil(excess / (4 * CONVERT_BLOCK))


def evaluate_tender(t, snap):
    """Returns (edge_per_share_CAD, bid_price_for_auction_or_None, reason).
    Edge = tender price vs the basket we will sell/buy to hedge, minus converter fees for the blocks
    we must convert to fit the hedge in our limits, minus a penalty for the part that waits unhedged."""
    q, act = int(t["quantity"]), t["action"]
    bk, (ub, ua) = snap["books"], snap["q"]["USD"]
    mids = {k: sum(snap["q"][k]) / 2 for k in ("BULL", "BEAR")}
    usd = (ub + ua) / 2 or 1.0
    px = t.get("price")
    side = 1 if act == "BUY" else -1
    gl, nl = snap["gross_limit"], snap["net_limit"]
    pos = snap["pos"]
    # limits: we must be able to hold the tender before any hedge trades
    u_now = usage(pos, gl, nl, d_ritc=side * q)
    if u_now > LIMIT_TRANSIENT_MAX:
        return 0.0, None, f"limits (usage would be {u_now:.0%} before hedging)"
    n_conv = blocks_to_convert(pos, gl, nl, d_ritc=side * q)
    new_r = pos.get("RITC", 0) + side * q
    if n_conv * CONVERT_BLOCK > abs(new_r):
        return 0.0, None, "limits (converters cannot make enough room)"
    # shares of each leg we can hedge right away; the rest waits for converters
    g_now = abs(pos.get("BULL", 0)) + abs(pos.get("BEAR", 0)) + 2 * abs(new_r)
    hedge_now = max(0.0, min(q, (HEDGE_USAGE_MAX * gl - g_now) / 2))
    waiting = q - hedge_now
    extra = (n_conv * CONVERT_FEE_USD * usd) / q + UNHEDGED_PENALTY * waiting / q   # CAD per tender share
    if act == "BUY":      # we buy RITC, hedge by SELLING the basket (receive this per share)
        basket = leg_value(bk["BULL"]["bids"], mids["BULL"], q, -1) + leg_value(bk["BEAR"]["bids"], mids["BEAR"], q, -1)
        breakeven_usd = (basket - extra - TENDER_MIN_EDGE) / usd
        edge = basket - extra - (px * usd if px else breakeven_usd * usd + TENDER_MIN_EDGE)
        bid = math.floor(breakeven_usd * 100) / 100
    else:                 # we sell RITC, hedge by BUYING the basket (pay this per share)
        basket = leg_value(bk["BULL"]["asks"], mids["BULL"], q, 1) + leg_value(bk["BEAR"]["asks"], mids["BEAR"], q, 1)
        breakeven_usd = (basket + extra + TENDER_MIN_EDGE) / usd
        edge = (px * usd if px else breakeven_usd * usd - TENDER_MIN_EDGE) - basket - extra
        bid = math.ceil(breakeven_usd * 100) / 100
    note = f"{n_conv} conversion{'s' if n_conv != 1 else ''}"
    if t.get("is_fixed_bid", True):
        if edge < TENDER_MIN_EDGE or edge * q < TENDER_MIN_PROFIT:
            return edge, None, f"edge too small, after {note}"
        return edge, None, f"ok, {note}"
    return edge, bid, f"auction, {note}"


def handle_tenders(snap, tick):
    tenders = get("/tenders") or []
    for t in tenders:
        tid = t["tender_id"]
        if tid in ST.seen_tenders:
            continue
        ST.seen_tenders.add(tid)
        if ST.paused:
            event(tick, "TENDER", f"#{tid} ignored (paused)")
            continue
        edge, bid, why = evaluate_tender(t, snap)
        q, act, px = t["quantity"], t["action"], t.get("price")
        desc = f"{act:<4} {q:>6,} RITC @ {px if px is not None else 'auction'}"
        if why.startswith("ok"):
            r = _req("POST", f"/tenders/{tid}")
            ok = r is not None and r.ok and r.json().get("success", True)
            event(tick, "TENDER", f"ACCEPT  {desc}  edge {edge:+.3f}/sh = ${edge * q:,.0f}  ({why[4:]})"
                  + ("" if ok else "  (FAILED)"))
            if ok:
                return True
        elif why.startswith("auction"):
            r = _req("POST", f"/tenders/{tid}", price=f"{bid:.2f}")
            ok = r is not None and r.ok and r.json().get("success", True)
            event(tick, "TENDER", f"BID     {desc}  our bid {bid:.2f}  -> {'WON' if ok else 'lost'}")
            if ok:
                return True
        else:
            _req("DELETE", f"/tenders/{tid}")
            event(tick, "TENDER", f"decline {desc}  edge {edge:+.3f}/sh  ({why})")
    return False


# ------------------------------- 2. hedge -------------------------------
def open_orders():
    return get("/orders", status="OPEN") or []


def hedge(snap, tick, orders=None):
    """Target BULL = BEAR = -RITC. Gap beyond URGENT_GAP -> market now. Rest -> limit order at our best price."""
    pos, bk, q = effective_pos(snap), snap["books"], snap["q"]
    target = -pos.get("RITC", 0)
    orders = open_orders() if orders is None else orders
    did = False
    gross = abs(pos.get("BULL", 0)) + abs(pos.get("BEAR", 0)) + 2 * abs(pos.get("RITC", 0))
    leg_room = max(0.0, (HEDGE_USAGE_MAX * snap["gross_limit"] - gross) / 2)   # gross we may add, per leg
    for leg in ("BULL", "BEAR"):
        gap = int(round(target - pos.get(leg, 0)))
        p0 = pos.get(leg, 0)
        # only the part of the gap that moves this leg AWAY from zero adds gross: cap that part by the room
        toward_zero = min(abs(gap), abs(p0)) if p0 * gap < 0 else 0
        gap = int(math.copysign(min(abs(gap), toward_zero + leg_room), gap)) if gap else 0
        side = "BUY" if gap > 0 else "SELL"
        # 1) urgent part at market (a thin book gets more patience: market orders there cost a lot)
        top = (bk[leg]["asks"] if gap > 0 else bk[leg]["bids"])
        top_size = top[0][1] if top else 0
        tolerance = URGENT_GAP * (THIN_GAP_MULT if top_size < THIN_BOOK else 1.0)
        urgent = abs(gap) - tolerance
        if urgent >= HEDGE_MIN:
            levels = bk[leg]["asks"] if gap > 0 else bk[leg]["bids"]
            avail = max(levels[0][1] if levels else 0, depth_within(levels, HEDGE_SLIP))
            f = market(leg, side, min(urgent, MAX_EQ_ORDER, avail))
            if f:
                pos[leg] = pos.get(leg, 0) + (f if side == "BUY" else -f)
                gap += -f if side == "BUY" else f
                did = True
        # 2) passive part: one resting order at the best bid (buy) / best ask (sell)
        mine = [o for o in orders if o["ticker"] == leg]
        want_qty = abs(gap) if abs(gap) >= HEDGE_MIN else 0
        bid, ask = q[leg]
        want_px = bid if side == "BUY" else ask
        resting = sum(o["quantity"] - o.get("quantity_filled", 0) for o in mine)
        ok = (want_qty == 0 and not mine) or (
            mine and all(o["action"] == side and abs(o["price"] - want_px) < 1e-6 for o in mine)
            and abs(resting - want_qty) <= max(HEDGE_MIN, 0.2 * want_qty))
        if ok:
            continue
        if mine:
            _req("POST", "/commands/cancel", ticker=leg)
        left = want_qty
        while left > 0:
            qty = min(MAX_EQ_ORDER, left)
            r = _req("POST", "/orders", ticker=leg, type="LIMIT", quantity=qty, action=side, price=f"{want_px:.2f}")
            if r is None or not r.ok:
                break
            left -= qty
    return did


# ------------------------------- 3. arbitrage -------------------------------
def arb(snap, tick):
    if ST.paused or snap["tick"] < ST.conv_busy_until:
        return False
    q, pos, bk = snap["q"], effective_pos(snap), snap["books"]
    if max(abs(pos.get(l, 0) + pos.get("RITC", 0)) for l in ("BULL", "BEAR")) > URGENT_GAP:
        return False                     # still hedging a tender: don't add more
    ub, ua = q["USD"]
    rb, ra = q["RITC"]
    bull_b, bull_a = q["BULL"]
    bear_b, bear_a = q["BEAR"]
    sell_etf = rb * ub - (bull_a + bear_a) - 3 * FEE    # sell RITC, buy basket
    buy_etf = (bull_b + bear_b) - ra * ua - 3 * FEE     # buy RITC, sell basket
    r = pos.get("RITC", 0)
    gl, nl = snap["gross_limit"], snap["net_limit"]
    size = min(ARB_CLIP, bk["RITC"]["bids"][0][1] if bk["RITC"]["bids"] else 0,
               bk["BULL"]["asks"][0][1] if bk["BULL"]["asks"] else 0,
               bk["BEAR"]["asks"][0][1] if bk["BEAR"]["asks"] else 0)
    if sell_etf > (ARB_CLOSE_EDGE if r > 0 else ARB_OPEN_EDGE):
        qty = min(size, r) if r > 0 else min(size, ARB_MAX_PAIRS + r)
        if qty >= 100 and usage(pos, gl, nl, d_ritc=-qty, d_bull=qty, d_bear=qty) <= LIMIT_USAGE_MAX:
            f = market("RITC", "SELL", qty)
            pos["RITC"] = r - f
            if f:
                a = ST.arb_acc.setdefault("SELL RITC / buy basket", [0, 0])
                a[0] += f
                a[1] = sell_etf
            return f > 0
    size = min(ARB_CLIP, bk["RITC"]["asks"][0][1] if bk["RITC"]["asks"] else 0,
               bk["BULL"]["bids"][0][1] if bk["BULL"]["bids"] else 0,
               bk["BEAR"]["bids"][0][1] if bk["BEAR"]["bids"] else 0)
    if buy_etf > (ARB_CLOSE_EDGE if r < 0 else ARB_OPEN_EDGE):
        qty = min(size, -r) if r < 0 else min(size, ARB_MAX_PAIRS - r)
        if qty >= 100 and usage(pos, gl, nl, d_ritc=qty, d_bull=-qty, d_bear=-qty) <= LIMIT_USAGE_MAX:
            f = market("RITC", "BUY", qty)
            pos["RITC"] = r + f
            if f:
                a = ST.arb_acc.setdefault("BUY RITC / sell basket", [0, 0])
                a[0] += f
                a[1] = buy_etf
            return f > 0
    return False


# ------------------------------- 4. FX -------------------------------
def fx_hedge(snap):
    usd = snap["pos"].get("USD", 0)
    if abs(usd) <= FX_BAND:
        return
    qty = min(MAX_FX_ORDER, int(abs(usd)))
    market("USD", "SELL" if usd > 0 else "BUY", qty)


# ------------------------------- 5. converters -------------------------------
def effective_pos(snap):
    """Positions including converter outputs that are on their way (if the server takes inputs first)."""
    if not ST.conv_pending:
        return snap["pos"]
    p = dict(snap["pos"])
    for k, v in ST.conv_pending.items():
        p[k] = p.get(k, 0) + v
    return p


def _lease(kind):
    """Lease id for "redeem"/"create" (leased once per heat; the lease lasts the whole case)."""
    if kind in ST.leases:
        return ST.leases[kind]
    ticker = "ETF-Redemption" if kind == "redeem" else "ETF-Creation"
    for L in get("/leases") or []:
        if L.get("ticker") == ticker:
            ST.leases[kind] = L["id"]
            return L["id"]
    r = _req("POST", "/leases", ticker=ticker)
    if r is not None and r.ok and "id" in r.json():
        ST.leases[kind] = r.json()["id"]
        return ST.leases[kind]
    say(f"[converter] could not lease {ticker}: {r.text[:120] if r is not None else 'no response'}")
    return None


def _use_lease(kind):
    lid = _lease(kind)
    if lid is None:
        return False
    if kind == "redeem":
        params = {"from1": "RITC", "quantity1": CONVERT_BLOCK, "from2": "USD", "quantity2": CONVERT_FEE_USD}
    else:
        params = {"from1": "BULL", "quantity1": CONVERT_BLOCK, "from2": "BEAR", "quantity2": CONVERT_BLOCK,
                  "from3": "USD", "quantity3": CONVERT_FEE_USD}
    r = _req("POST", f"/leases/{lid}", **params)
    if r is None or not r.ok:
        say(f"[converter] use failed: {r.text[:160] if r is not None else 'no response'}")
        return False
    return True


def converters(snap, tick):
    """Redeem/create one hedged 10k block at a time while the fully hedged book doesn't fit in our limits."""
    ticks_total = snap.get("ticks", 300)
    if tick >= ST.conv_busy_until and ST.conv_pending:
        ST.conv_pending = {}                         # outputs have arrived
    if tick < ST.conv_busy_until or tick > ticks_total - CONVERT_STOP_BEFORE_END:
        return
    pos, gl, nl = effective_pos(snap), snap["gross_limit"], snap["net_limit"]
    if blocks_to_convert(pos, gl, nl) == 0:
        return
    r = pos.get("RITC", 0)
    sgn = 1 if r > 0 else -1
    hedged = min(abs(r), max(0.0, -sgn * pos.get("BULL", 0)), max(0.0, -sgn * pos.get("BEAR", 0)))
    if hedged < CONVERT_BLOCK:
        return                                       # hedge the next block first
    kind = "redeem" if r > 0 else "create"
    if ST.auto_convert:
        watch = "RITC" if kind == "redeem" else "BULL"          # an input of this conversion
        before = {x["ticker"]: float(x.get("position", 0)) for x in (get("/securities") or [])}.get(watch)
        if _use_lease(kind):
            ST.conv_done += 1
            ST.conv_busy_until = tick + CONVERT_TICKS
            after = {x["ticker"]: float(x.get("position", 0)) for x in (get("/securities") or [])}.get(watch)
            # If the inputs left already, the outputs are "pending" until the converter finishes.
            # (If the server swaps everything at the end instead, nothing is pending.)
            if before is not None and after is not None and abs(after - before) >= CONVERT_BLOCK * 0.99:
                ST.conv_pending = ({"BULL": CONVERT_BLOCK, "BEAR": CONVERT_BLOCK} if kind == "redeem"
                                   else {"RITC": CONVERT_BLOCK})
            event(tick, "CONVERT", f"{kind.upper()} 1 x 10,000 (#{ST.conv_done}, fee ${CONVERT_FEE_USD:,} USD)")
            return
        ST.auto_convert = False
        banner("CONVERTER API NOT AVAILABLE: switching to manual prompts.\n"
               "Click ETF-Redemption / ETF-Creation in the RIT Client Assets tab when asked.")
    banner(f">>> CONVERTER: {kind.upper()} 1 x 10,000 NOW  (Assets tab -> "
           f"ETF-{'Redemption' if kind == 'redeem' else 'Creation'})")
    ST.conv_busy_until = tick + MANUAL_REPROMPT_TICKS


# ------------------------------- console -------------------------------
def console():
    for line in sys.stdin:
        c = line.strip().lower()
        if c == "p":
            ST.paused = True
            say("[paused] no tenders/arb; hedging continues")
        elif c == "r":
            ST.paused = False
            say("[resumed]")
        elif c == "s":
            snap = snapshot()
            if snap:
                say(f"[status] pos={ {k: round(v) for k, v in snap['pos'].items()} } "
                      f"usage={usage(snap['pos'], snap['gross_limit'], snap['net_limit']):.0%}")
        elif c == "q":
            ST.quit = True
            return


# ------------------------------- main -------------------------------
def main():
    threading.Thread(target=console, daemon=True).start()
    banner("ETF ALGO  |  waiting for the case to start\nCommands (type + Enter): p pause | r resume | s status | q quit")
    logf = open(LOG_FILE, "w", newline="")
    log = csv.writer(logf)
    log.writerow(["tick", "BULL", "BEAR", "RITC", "USD", "usage", "nlv"])
    last_print = -1
    while not ST.quit:
        case = get("/case")
        if not case or case.get("status") != "ACTIVE":
            time.sleep(0.5)
            continue
        tick = case["tick"]
        if tick < ST.last_tick:
            banner("NEW HEAT")
            ST.seen_tenders = set()
            ST.last_prompt_tick = -100
            ST.leases, ST.conv_busy_until, ST.conv_pending, ST.conv_done = {}, -1, {}, 0
        if tick != ST.flush_tick:
            for what, (qty, edge) in ST.arb_acc.items():
                event(ST.flush_tick, "ARB", f"{what}: {qty:,} shares, edge {edge:+.3f}/sh")
            ST.arb_acc = {}
            ST.flush_tick = tick
        ST.last_tick = tick
        loop_start = time.time()
        ticks = case.get("ticks_per_period", 300)
        snap = snapshot_at(tick, ticks)
        if not snap:
            time.sleep(LOOP_SLEEP)
            continue
        if handle_tenders(snap, tick):
            snap = snapshot_at(tick, ticks) or snap
        converters(snap, tick)
        hedge(snap, tick)
        if arb(snap, tick):
            hedge(snap, tick)
        fx_hedge(snap)

        if tick != last_print:
            last_print = tick
            p = snap["pos"]
            u = usage(p, snap["gross_limit"], snap["net_limit"])
            nlv = (get("/trader") or {}).get("nlv", float("nan"))
            log.writerow([tick, p.get("BULL"), p.get("BEAR"), p.get("RITC"), round(p.get("USD", 0)), round(u, 3), nlv])
            logf.flush()
            if tick % STATUS_EVERY == 0:
                r_ = p.get("RITC", 0)
                hedged = abs(p.get("BULL", 0) + r_) < 200 and abs(p.get("BEAR", 0) + r_) < 200
                event(tick, "status", f"RITC {r_:+7,.0f}  BULL {p.get('BULL', 0):+7,.0f}  BEAR {p.get('BEAR', 0):+7,.0f}  "
                                      f"{'ok' if hedged else 'HEDGING'}  limits {u:3.0%}  P&L {nlv:+8,.0f}")
        took = time.time() - loop_start
        if took > 1.0 and tick - ST.last_slow_warn >= 10:
            ST.last_slow_warn = tick
            event(tick, "SLOW !!", f"one loop took {took:.1f}s (should be <0.5s). API is slow: check the connection")
        time.sleep(LOOP_SLEEP)
    logf.close()
    say("Stopped.")


if __name__ == "__main__":
    main()
