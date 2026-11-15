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
     gross for the next part of the hedge. The case rules say converters are HUMAN ONLY: the algo
     never calls a converter/lease endpoint on the real server. It prints
     ">>> CONVERTER: REDEEM 1 x 10,000 NOW" and you click it in the RIT Client Assets tab.

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
RITC_ROUTE_SPLITS = (0.0, 0.25, 0.5)   # fractions of a tender we may trade straight back into the RITC book
RITC_ROUTE_MAX = 40000        # never more than this via RITC (its book is thinner)
RITC_ROUTE_TICKS = 10         # whatever is left of that after this many ticks gets hedged with BULL+BEAR
CROWD_SHARE = float(os.environ.get("CROWD_SHARE", "0.5"))   # share of the visible book we expect to get.
                              # 0.5 = assume other teams take half (competition). 1.0 = alone
REFILL_PENALTY = 0.02         # CAD/share for hedge shares that must wait for the book to refill
EVAL_SLICE = 20000            # walk the book for this many shares; assume patience for the rest
EXTRA_SLIP_BEYOND_SLICE = 0.01  # extra CAD/share assumed on shares beyond the slice
LIMIT_USAGE_MAX = 0.90        # arb keeps usage below this
LIMIT_TRANSIENT_MAX = 0.97    # max usage right after accepting a tender, before any hedge trades
HEDGE_USAGE_MAX = 0.96        # hedge orders never push gross usage above this (converters free the rest)

# Hedging: passive first (limit orders earn the rebate), market orders only for risk above URGENT_GAP
URGENT_GAP = 5000             # max unhedged shares per leg we tolerate; anything beyond is hedged at market
STALL_TICKS = 5               # if a leg's gap hasn't closed passively after this many ticks, close it at market
HEDGE_SLIP = 0.03             # market hedges only take liquidity within this distance of the best price
HEDGE_MIN = 100               # ignore smaller imbalances
REBATE = 0.01

# Arbitrage
ARB_OPEN_EDGE = 0.03          # CAD/share net of 3 fees, to OPEN / grow pairs
ARB_CLOSE_EDGE = 0.00         # CAD/share net of fees, to CLOSE existing pairs (frees capacity)
ARB_CLIP = 5000
ARB_MAX_PAIRS = 20000         # max |RITC| the arb module builds on its own (keeps limit room for tenders)
ROOM_TARGET = 20000           # after a tender, unwind pairs above this |RITC| to make room for the next one...
ROOM_UNWIND_EDGE = -0.03      # ...even at a small loss: up to this CAD/share past fees (converting costs ~0.15)

# FX
FX_BAND = 20000               # re-hedge when |USD cash| > this

# Converters
CONVERT_BLOCK = 10000
CONVERT_FEE_USD = 1500            # per block
CONVERT_TICKS = 2                 # one use takes this many ticks; one use at a time
CONVERT_TARGET = 0.90             # convert hedged blocks until the fully hedged book fits under this usage
CONVERT_STOP_BEFORE_END = 5       # no new conversions in the last N ticks
# Converters are HUMAN ONLY in the competition (case rules): the algo only PRINTS a prompt.
# AUTO_CONVERT_MOCK=1 makes it "click" through the fake server's /mock/convert endpoint, which exists only
# in mock_rit_etf.py, and only when RIT_API points at localhost. It can never reach a real converter.
AUTO_CONVERT_MOCK = os.environ.get("AUTO_CONVERT_MOCK") == "1" and "localhost" in API
REPROMPT_TICKS = 4                # repeat the prompt if nothing happened after this many ticks

# The server can show a fill in /securities a moment AFTER the order returns. Until it does, the algo
# does not trade that ticker again (re-sending on a stale position caused huge USD flip-flops).
INFLIGHT_TIMEOUT = 1.5        # seconds; after this, trade the ticker again even if the fill never showed
DEFAULT_GROSS, DEFAULT_NET = 300_000, 200_000   # used if /limits is unavailable
STATUS_EVERY = 5              # print a status line every N ticks
LOOP_SLEEP = float(os.environ.get("LOOP_SLEEP", 0.15))
LOG_FILE = f"etf_log_{datetime.now():%Y%m%d_%H%M%S}.csv"
# =====================================================================================

s = requests.Session()
s.headers.update({"X-API-Key": API_KEY})


class State:
    paused = False
    quit = False
    last_detail = ""
    seen_tenders = set()
    last_tick = -1
    acted_tick = -1
    gap_since = {}        # leg -> tick its hedge gap opened
    last_ritc_route = 0   # shares of the tender being evaluated that would go back into RITC
    ritc_plan = 0         # signed RITC still to trade back into the RITC book (part of a tender)
    plan_until = -1
    last_prompt_tick = -100
    last_slow_warn = -100
    flush_tick = 0
    arb_acc = {}          # side -> [shares, last edge], printed once per tick
    inflight = {}         # ticker -> {"base": position when first sent, "total": signed qty filled, "t": time}
    conv_busy_until = -1  # tick before which we don't prompt again
    conv_prompts = 0


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


def note_fill(ticker, cur, signed_qty):
    """Remember a fill until /securities shows it. cur = the position we believed before the order."""
    if not signed_qty:
        return
    f = ST.inflight.get(ticker)
    if f:
        f["total"] += signed_qty
        f["t"] = time.time()
    else:
        ST.inflight[ticker] = {"base": cur, "total": signed_qty, "t": time.time()}


def check_inflight(pos):
    """Clear a ticker once /securities shows our fill (or after the timeout). Never edits positions."""
    now = time.time()
    for tk, f in list(ST.inflight.items()):
        # cleared only when the server shows ALL our fills (orders in both directions can be pending at once)
        unseen = f["total"] - (pos.get(tk, 0) - f["base"])
        if abs(unseen) <= max(100, 0.02 * abs(f["total"])) or now - f["t"] > INFLIGHT_TIMEOUT:
            del ST.inflight[tk]


def busy(*tickers):
    return any(t in ST.inflight for t in tickers)


def market(ticker, action, qty, cur):
    """Market order. cur = current position (as the algo sees it), used to track the fill."""
    qty = int(qty)
    if qty <= 0:
        return 0
    r = _req("POST", "/orders", ticker=ticker, type="MARKET", quantity=qty, action=action)
    if r is None or not r.ok:
        say(f"[order rejected] {action} {qty} {ticker}: {r.text[:120] if r is not None else 'no response'}")
        return 0
    j = r.json()
    filled = int(j.get("quantity_filled") or 0) if j.get("status") == "TRANSACTED" else qty
    note_fill(ticker, cur, filled if action == "BUY" else -filled)
    return filled


# ------------------------------- market data -------------------------------
def snapshot():
    secs = get("/securities")
    if not secs:
        return None
    pos = {x["ticker"]: float(x.get("position", 0)) for x in secs}
    check_inflight(pos)
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


MAX_SANE_EDGE = 1.0           # CAD/share; anything bigger means a quote is broken, not an opportunity


def quotes_ok(q):
    """Every bid/ask we use is a real price (no zeros, no crossed books)."""
    for t in ("USD", "RITC", "BULL", "BEAR"):
        b, a = q.get(t, (0, 0))
        if b <= 0 or a <= 0 or a < b:
            return False
    return True


def usage(pos, gl, nl, d_ritc=0, d_bull=0, d_bear=0):
    b, e, r = pos.get("BULL", 0) + d_bull, pos.get("BEAR", 0) + d_bear, pos.get("RITC", 0) + d_ritc
    gross = abs(b) + abs(e) + 2 * abs(r)
    net = b + e + 2 * r
    return max(gross / gl, abs(net) / nl)


# ------------------------------- 1. tenders -------------------------------
def leg_value(levels, mid, q, sgn):
    """Expected CAD/share (incl. fees) to trade q shares of one leg the way hedge() does:
    - up to URGENT_GAP passively near mid (earning the rebate),
    - the rest with market orders that only take liquidity within HEDGE_SLIP of the best price. What is there
      now is priced from the book; the rest waits for the book to refill at about the same prices, plus
      REFILL_PENALTY per share for the price risk while waiting."""
    qm = max(0, q - URGENT_GAP)
    qp = q - qm
    total = qp * (mid + sgn * REBATE * -1)          # passive: ~mid, plus rebate
    if qm:
        if not levels:
            return mid + sgn * (FEE + 0.10)         # empty book: assume it's expensive
        best = levels[0][0]
        near = [(px, sz * CROWD_SHARE) for px, sz in levels if abs(px - best) <= HEDGE_SLIP + 1e-9]
        avail = sum(sz for _, sz in near)
        take = min(qm, avail)
        got, cost = 0, 0.0
        for px, sz in near:
            k = min(sz, take - got)
            if k <= 0:
                break
            cost += k * px
            got += k
        vwap = cost / got if got else best
        later = qm - take                            # waits for the book to refill
        # others hedging the same tender push the price: charge the refill wait more when crowded
        total += qm * (vwap + sgn * FEE) + later * sgn * REFILL_PENALTY / max(CROWD_SHARE, 0.1)
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
    if not quotes_ok(snap["q"]):
        return 0.0, None, "quotes missing or broken right now"
    bk, (ub, ua) = snap["books"], snap["q"]["USD"]
    mids = {k: sum(snap["q"][k]) / 2 for k in ("BULL", "BEAR")}
    usd = (ub + ua) / 2 or 1.0
    px = t.get("price")
    side = 1 if act == "BUY" else -1
    gl, nl = snap["gross_limit"], snap["net_limit"]
    pos = snap["pos"]
    # limits: we must be able to hold the tender before any hedge trades
    u_now = usage(pos, gl, nl, d_ritc=side * q)
    limit_block = None
    if u_now > LIMIT_TRANSIENT_MAX:
        limit_block = f"limits (usage would be {u_now:.0%} before hedging)"
    else:
        n_try = blocks_to_convert(pos, gl, nl, d_ritc=side * q)
        if n_try * CONVERT_BLOCK > abs(pos.get("RITC", 0) + side * q):
            limit_block = "limits (converters cannot make enough room)"
    if limit_block:
        pos = {}          # still value it, as if we were flat, so the log shows what the refusal cost
    # Two ways to get flat: hedge with BULL+BEAR (2 trades, 2 fees, maybe converters), or trade part of the
    # tender straight back into the RITC book (1 trade, 1 fee, no converters). Try a few splits, keep the best.
    rmid = sum(snap["q"]["RITC"]) / 2
    best = None
    for frac in RITC_ROUTE_SPLITS:
        x = int(min(frac * q, RITC_ROUTE_MAX))
        qs = q - x
        new_r = pos.get("RITC", 0) + side * (q - x)
        n_conv = blocks_to_convert(pos, gl, nl, d_ritc=side * (q - x))
        # shares of each leg we can hedge right away; the rest waits for converters
        g_now = abs(pos.get("BULL", 0)) + abs(pos.get("BEAR", 0)) + 2 * abs(new_r)
        hedge_now = max(0.0, min(qs, (HEDGE_USAGE_MAX * gl - g_now) / 2))
        waiting = qs - hedge_now
        extra = (n_conv * CONVERT_FEE_USD * usd) / q + UNHEDGED_PENALTY * waiting / q   # CAD per tender share
        if act == "BUY":   # we bought RITC: sell the basket for qs shares, sell x RITC back
            stock = (leg_value(bk["BULL"]["bids"], mids["BULL"], qs, -1)
                     + leg_value(bk["BEAR"]["bids"], mids["BEAR"], qs, -1)) if qs else 0.0
            ritc = leg_value(bk["RITC"]["bids"], rmid, x, -1) * usd if x else 0.0
            value = (qs * stock + x * ritc) / q                       # CAD received per tender share
            breakeven_usd = (value - extra - TENDER_MIN_EDGE) / usd
            edge = value - extra - (px * usd if px else breakeven_usd * usd + TENDER_MIN_EDGE)
            bid = math.floor(breakeven_usd * 100) / 100
        else:              # we sold RITC: buy the basket for qs shares, buy x RITC back
            stock = (leg_value(bk["BULL"]["asks"], mids["BULL"], qs, 1)
                     + leg_value(bk["BEAR"]["asks"], mids["BEAR"], qs, 1)) if qs else 0.0
            ritc = leg_value(bk["RITC"]["asks"], rmid, x, 1) * usd if x else 0.0
            value = (qs * stock + x * ritc) / q                       # CAD paid per tender share
            breakeven_usd = (value + extra + TENDER_MIN_EDGE) / usd
            edge = (px * usd if px else breakeven_usd * usd - TENDER_MIN_EDGE) - value - extra
            bid = math.ceil(breakeven_usd * 100) / 100
        if best is None or edge > best[0]:
            best = (edge, bid, x, n_conv, extra, value)
    edge, bid, x, n_conv, extra, basket = best
    ST.last_ritc_route = x
    note = f"{n_conv} conversion{'s' if n_conv != 1 else ''}" + (f", {x:,} back into RITC" if x else "")
    # breakdown for the log: where the edge goes (all CAD per share)
    mid_basket = mids["BULL"] + mids["BEAR"]
    leg = "bids" if act == "BUY" else "asks"
    top = lambda t: (bk[t][leg][0][1] if bk[t][leg] else 0)
    depth = lambda t: sum(sz for _, sz in bk[t][leg][:10])
    ST.last_detail = (f"tender {px * usd if px else float('nan'):.3f} CAD vs basket mid {mid_basket:.3f} | "
                      f"hedge cost {abs(mid_basket - basket):.3f} (incl. {x:,} via RITC) | converters+wait {extra:.3f} | "
                      f"BULL top/10lvl {top('BULL'):,}/{depth('BULL'):,}  BEAR {top('BEAR'):,}/{depth('BEAR'):,}")
    if limit_block:
        return edge, None, f"{limit_block}; edge if we had been flat {edge:+.3f}/sh = ${edge * q:,.0f}"
    if t.get("is_fixed_bid", True):
        if edge < TENDER_MIN_EDGE or edge * q < TENDER_MIN_PROFIT:
            return edge, None, f"edge too small, after {note}"
        return edge, None, f"ok, {note}"
    return edge, bid, f"auction, {note}"


def still_hedging(snap):
    """Why we're not ready for another tender ('' if we are): RITC route or a big hedge gap still open."""
    if ST.ritc_plan:
        return f"{abs(ST.ritc_plan):,.0f} RITC still going back into its book"
    pos = snap["pos"]
    target = -(pos.get("RITC", 0) + ST.ritc_plan)
    gap = max(abs(target - pos.get(l, 0)) for l in ("BULL", "BEAR"))
    if gap > 2 * URGENT_GAP:
        return f"hedge gap {gap:,.0f}"
    return ""


def plan_ritc_route(act, tick):
    """After accepting: trade ST.last_ritc_route shares straight back into the RITC book."""
    x = ST.last_ritc_route
    if x:
        ST.ritc_plan += -x if act == "BUY" else x
        ST.plan_until = tick + RITC_ROUTE_TICKS


def ritc_route(snap, tick):
    """Work the RITC part of a tender back into the RITC book (market orders within HEDGE_SLIP).
    Anything left after RITC_ROUTE_TICKS is handed to the BULL+BEAR hedge instead."""
    if not ST.ritc_plan:
        return
    if tick > ST.plan_until:
        event(tick, "RITC", f"route timed out with {ST.ritc_plan:+,.0f} left: hedging that with BULL+BEAR")
        ST.ritc_plan = 0
        return
    if busy("RITC") or not quotes_ok(snap["q"]):
        return
    levels = snap["books"]["RITC"]["bids" if ST.ritc_plan < 0 else "asks"]
    avail = max(levels[0][1] if levels else 0, depth_within(levels, HEDGE_SLIP))
    qty = int(min(abs(ST.ritc_plan), MAX_EQ_ORDER, avail))
    if qty < HEDGE_MIN:
        return
    side = "SELL" if ST.ritc_plan < 0 else "BUY"
    f = market("RITC", side, qty, snap["pos"].get("RITC", 0))
    if f:
        ST.ritc_plan += f if side == "SELL" else -f
        a = ST.arb_acc.setdefault(f"TENDER via RITC book: {side.lower()} RITC", [0, 0])
        a[0] += f


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
        ST.last_detail = ""
        say(f"[tender raw] {t}")
        edge, bid, why = evaluate_tender(t, snap)
        working = still_hedging(snap)
        if working and (why.startswith("ok") or why.startswith("auction")):
            why = f"still hedging the last tender ({working}); edge was {edge:+.3f}/sh"
        q, act, px = t["quantity"], t["action"], t.get("price")
        desc = f"{act:<4} {q:>6,} RITC @ {px if px is not None else 'auction'}"
        if why.startswith("ok"):
            r = _req("POST", f"/tenders/{tid}")
            ok = r is not None and r.ok and r.json().get("success", True)
            if ok:
                note_fill("RITC", snap["pos"].get("RITC", 0), q if act == "BUY" else -q)
                plan_ritc_route(act, tick)
            event(tick, "TENDER", f"ACCEPT  {desc}  edge {edge:+.3f}/sh = ${edge * q:,.0f}  ({why[4:]})"
                  + ("" if ok else "  (FAILED)"))
            if ST.last_detail:
                event(tick, "  why", ST.last_detail)
            if ok:
                return True
        elif why.startswith("auction"):
            r = _req("POST", f"/tenders/{tid}", price=f"{bid:.2f}")
            ok = r is not None and r.ok and r.json().get("success", True)
            if ok:
                note_fill("RITC", snap["pos"].get("RITC", 0), q if act == "BUY" else -q)
                plan_ritc_route(act, tick)
            event(tick, "TENDER", f"BID     {desc}  our bid {bid:.2f}  -> {'WON' if ok else 'lost'}")
            if ok:
                return True
        else:
            _req("DELETE", f"/tenders/{tid}")
            event(tick, "TENDER", f"decline {desc}  edge {edge:+.3f}/sh  ({why})")
        if ST.last_detail:
            event(tick, "  why", ST.last_detail)
    return False


# ------------------------------- 2. hedge -------------------------------
def open_orders():
    return get("/orders", status="OPEN") or []


def hedge(snap, tick, orders=None, passive=True):
    """Target BULL = BEAR = -RITC. Gap beyond URGENT_GAP -> market now. Rest -> limit order at our best price."""
    pos, bk, q = effective_pos(snap), snap["books"], snap["q"]
    target = -(pos.get("RITC", 0) + ST.ritc_plan)     # RITC still going back into its own book needs no hedge
    orders = open_orders() if orders is None else orders
    did = False
    gross = abs(pos.get("BULL", 0)) + abs(pos.get("BEAR", 0)) + 2 * abs(pos.get("RITC", 0))
    leg_room = max(0.0, (HEDGE_USAGE_MAX * snap["gross_limit"] - gross) / 2)   # gross we may add, per leg
    raw_gap = {l: target - pos.get(l, 0) for l in ("BULL", "BEAR")}
    for leg in ("BULL", "BEAR"):
        if busy(leg, "RITC"):
            continue              # last fill not visible yet: wait rather than act on a stale position
        gap = int(round(target - pos.get(leg, 0)))
        p0 = pos.get(leg, 0)
        # only the part of the gap that moves this leg AWAY from zero adds gross: cap that part by the room
        toward_zero = min(abs(gap), abs(p0)) if p0 * gap < 0 else 0
        gap = int(math.copysign(min(abs(gap), toward_zero + leg_room), gap)) if gap else 0
        side = "BUY" if gap > 0 else "SELL"
        # 1) urgent part at market. A gap that the passive order hasn't filled for STALL_TICKS goes to
        #    market too (only liquidity within HEDGE_SLIP), so exposure never sits open for long.
        if abs(gap) >= HEDGE_MIN:
            since = ST.gap_since.setdefault(leg, tick)
        else:
            ST.gap_since.pop(leg, None)
            since = tick
        tolerance = 0 if tick - since >= STALL_TICKS else URGENT_GAP
        urgent = abs(gap) - tolerance
        # keep the legs in step: don't let this leg get more than URGENT_GAP ahead of the other one
        other = raw_gap["BEAR" if leg == "BULL" else "BULL"]
        if other * gap > 0:
            urgent = min(urgent, abs(gap) - max(0.0, abs(other) - URGENT_GAP))
        if urgent >= HEDGE_MIN:
            levels = bk[leg]["asks"] if gap > 0 else bk[leg]["bids"]
            avail = max(levels[0][1] if levels else 0, depth_within(levels, HEDGE_SLIP))
            f = market(leg, side, min(urgent, MAX_EQ_ORDER, avail), pos.get(leg, 0))
            if f:
                pos[leg] = pos.get(leg, 0) + (f if side == "BUY" else -f)
                gap += -f if side == "BUY" else f
                did = True
        # 2) passive part: one resting order at the best bid (buy) / best ask (sell).
        #    Only once per tick: a resting order's fills can show in /securities late, and re-sizing
        #    it several times per tick on a stale position stacks orders and overshoots the hedge.
        if not passive:
            continue
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
    if ST.paused or snap["tick"] < ST.conv_busy_until or busy("RITC", "BULL", "BEAR"):
        return False
    q, pos, bk = snap["q"], effective_pos(snap), snap["books"]
    if max(abs(pos.get(l, 0) + pos.get("RITC", 0)) for l in ("BULL", "BEAR")) > URGENT_GAP:
        return False                     # still hedging a tender: don't add more
    ub, ua = q["USD"]
    rb, ra = q["RITC"]
    bull_b, bull_a = q["BULL"]
    bear_b, bear_a = q["BEAR"]
    if not quotes_ok(q):
        return False
    sell_etf = rb * ub - (bull_a + bear_a) - 3 * FEE    # sell RITC, buy basket
    buy_etf = (bull_b + bear_b) - ra * ua - 3 * FEE     # buy RITC, sell basket
    if max(sell_etf, buy_etf) > MAX_SANE_EDGE:
        return False                     # a price is stale/missing (e.g. at tick 0): not a real edge
    r = pos.get("RITC", 0)
    gl, nl = snap["gross_limit"], snap["net_limit"]
    size = min(ARB_CLIP, bk["RITC"]["bids"][0][1] if bk["RITC"]["bids"] else 0,
               bk["BULL"]["asks"][0][1] if bk["BULL"]["asks"] else 0,
               bk["BEAR"]["asks"][0][1] if bk["BEAR"]["asks"] else 0)
    room_unwind = abs(r) > ROOM_TARGET   # holding big pairs from a tender: shrink them toward ROOM_TARGET
    if r > 0 and room_unwind and ROOM_UNWIND_EDGE < sell_etf <= ARB_CLOSE_EDGE:
        qty = min(size, r - ROOM_TARGET)
        if qty >= 100:
            f = market("RITC", "SELL", qty, r)
            pos["RITC"] = r - f
            if f:
                a = ST.arb_acc.setdefault("UNWIND for room: sell RITC / buy basket", [0, 0])
                a[0] += f
                a[1] = sell_etf
            return f > 0
    if r < 0 and room_unwind and ROOM_UNWIND_EDGE < buy_etf <= ARB_CLOSE_EDGE:
        size_b = min(ARB_CLIP, bk["RITC"]["asks"][0][1] if bk["RITC"]["asks"] else 0,
                     bk["BULL"]["bids"][0][1] if bk["BULL"]["bids"] else 0,
                     bk["BEAR"]["bids"][0][1] if bk["BEAR"]["bids"] else 0)
        qty = min(size_b, -r - ROOM_TARGET)
        if qty >= 100:
            f = market("RITC", "BUY", qty, r)
            pos["RITC"] = r + f
            if f:
                a = ST.arb_acc.setdefault("UNWIND for room: buy RITC / sell basket", [0, 0])
                a[0] += f
                a[1] = buy_etf
            return f > 0
    if sell_etf > (ARB_CLOSE_EDGE if r > 0 else ARB_OPEN_EDGE):
        qty = min(size, r) if r > 0 else min(size, ARB_MAX_PAIRS + r)
        if qty >= 100 and usage(pos, gl, nl, d_ritc=-qty, d_bull=qty, d_bear=qty) <= LIMIT_USAGE_MAX:
            f = market("RITC", "SELL", qty, r)
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
            f = market("RITC", "BUY", qty, r)
            pos["RITC"] = r + f
            if f:
                a = ST.arb_acc.setdefault("BUY RITC / sell basket", [0, 0])
                a[0] += f
                a[1] = buy_etf
            return f > 0
    return False


# ------------------------------- 4. FX -------------------------------
def fx_hedge(snap):
    if busy("USD", "RITC"):
        return            # the USD cash from those trades may not show yet: wait instead of guessing
    usd = snap["pos"].get("USD", 0)
    if abs(usd) <= FX_BAND:
        return
    qty = min(MAX_FX_ORDER, int(abs(usd)))
    market("USD", "SELL" if usd > 0 else "BUY", qty, usd)


# ------------------------------- 5. converters -------------------------------
def effective_pos(snap):
    return snap["pos"]


def converters(snap, tick):
    """Ask the HUMAN to redeem/create one hedged 10k block at a time while the fully hedged book
    doesn't fit in our limits. The algo itself never touches the converters (case rules)."""
    ticks_total = snap.get("ticks", 300)
    if tick < ST.conv_busy_until or tick > ticks_total - CONVERT_STOP_BEFORE_END:
        return
    pos, gl, nl = snap["pos"], snap["gross_limit"], snap["net_limit"]
    need = blocks_to_convert(pos, gl, nl, d_ritc=ST.ritc_plan)
    if need == 0:
        return
    r = pos.get("RITC", 0) + ST.ritc_plan
    sgn = 1 if r > 0 else -1
    hedged = min(abs(r), max(0.0, -sgn * pos.get("BULL", 0)), max(0.0, -sgn * pos.get("BEAR", 0)))
    if hedged < CONVERT_BLOCK:
        return                                       # hedge the next block first
    kind = "REDEEM" if r > 0 else "CREATE"
    ST.conv_prompts += 1
    banner(f">>> CONVERTER: {kind} 1 x 10,000 NOW  (Assets tab -> "
           f"ETF-{'Redemption' if kind == 'REDEEM' else 'Creation'})   [{need} block{'s' if need > 1 else ''} still needed]")
    if AUTO_CONVERT_MOCK:
        _req("POST", "/mock/convert", kind=kind.lower(), quantity=CONVERT_BLOCK)
    ST.conv_busy_until = tick + REPROMPT_TICKS


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
            ST.conv_busy_until, ST.conv_prompts, ST.inflight, ST.gap_since = -1, 0, {}, {}
            ST.ritc_plan, ST.plan_until = 0, -1
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
        ritc_route(snap, tick)
        converters(snap, tick)
        first_of_tick = tick != ST.acted_tick      # slow decisions (resting orders, FX) once per tick
        ST.acted_tick = tick
        hedge(snap, tick, passive=first_of_tick)
        if arb(snap, tick):
            hedge(snap, tick, passive=False)
        if first_of_tick:
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
