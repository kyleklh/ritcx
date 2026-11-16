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
import json
import math
import os
import sys
import threading
import time
from datetime import datetime

import requests

from display import say, event, banner, set_log_file

# =========================== SETTINGS (tune between heats) ===========================
from config import API, API_KEY   # read from .env (see .env.example); defaults to localhost:9999 / Rotman
# The ETF case can run on its own RIT Client port/key: ETF_RIT_API / ETF_RIT_API_KEY in .env override the shared ones
API = os.environ.get("ETF_RIT_API", API)
API_KEY = os.environ.get("ETF_RIT_API_KEY", API_KEY)

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
# Shadow mode: the algo ESTIMATES how crowded the market is from what happens after each tender, and logs
# the decision it would have made with that estimate. It never trades on the estimate (it uses CROWD_SHARE).
SHADOW_START = 1.0            # the estimate starts from "alone" (practice) and moves with what it sees
SHADOW_OBS_TICKS = 5          # watch this many ticks after each tender
SHADOW_PUSH_REF = 0.10        # CAD/share basket push that we read as "others take half the book"
SHADOW_WEIGHT = 0.3           # how fast the estimate moves toward each new observation
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
# Converters: the case PDF says human-only, but the organizers' intro slide allows automating them.
# AUTO_CONVERT=1 (default): the algo leases and uses them through the RIT API (/leases). If the server
# refuses, it falls back to printing prompts for you to click. AUTO_CONVERT=0: prompts only.
AUTO_CONVERT = os.environ.get("AUTO_CONVERT", "1") == "1"
CONVERTER_TICKERS = {"redeem": "ETF-Redemption", "create": "ETF-Creation"}
REPROMPT_TICKS = 4                # manual mode: repeat the prompt if nothing happened after this many ticks

# The server can show a fill in /securities a moment AFTER the order returns. Until it does, the algo
# does not trade that ticker again (re-sending on a stale position caused huge USD flip-flops).
INFLIGHT_TIMEOUT = 1.5        # seconds; after this, trade the ticker again even if the fill never showed
DEFAULT_GROSS, DEFAULT_NET = 300_000, 200_000   # used if /limits is unavailable
STATUS_EVERY = 5              # print a status line every N ticks
LOOP_SLEEP = float(os.environ.get("LOOP_SLEEP", 0.15))
# Everything from a run goes to logs/ (send the whole folder after a session):
#   etf_<time>.txt    the terminal output
#   etf_<time>.csv    one line per tick: positions, limit usage, P&L
#   etf_<time>.jsonl  one line per tick: all quotes, top 10 book levels with trader ids (shows how many other
#                     teams are quoting), positions, limits; plus every market order the algo sent and its fill
_STAMP = f"{datetime.now():%Y%m%d_%H%M%S}"
LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, f"etf_{_STAMP}.csv")
TXT_FILE = os.path.join(LOG_DIR, f"etf_{_STAMP}.txt")
JSONL_FILE = os.path.join(LOG_DIR, f"etf_{_STAMP}.jsonl")
_jsonl = None


def jlog(rec):
    if _jsonl:
        _jsonl.write(json.dumps(rec, separators=(",", ":")) + "\n")
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
    shadow_share = SHADOW_START   # shadow estimate of CROWD_SHARE (logged only)
    shadow_obs = []       # tenders being watched: {"t0", "sgn", "basket0", "accepted", "est_cost"}
    basket_hist = {}      # tick -> basket mid (CAD)
    last_quotes = {}      # latest quotes, to price our own fills against the mid
    fill_costs = []       # (tick, ticker, shares, CAD/share cost vs mid incl. fee)
    ritc_plan = 0         # signed RITC still to trade back into the RITC book (part of a tender)
    plan_until = -1
    last_prompt_tick = -100
    last_slow_warn = -100
    flush_tick = 0
    arb_acc = {}          # side -> [shares, last edge], printed once per tick
    inflight = {}         # ticker -> {"base": position when first sent, "total": signed qty filled, "t": time}
    conv_busy_until = -1  # tick before which we don't start/prompt another conversion
    conv_prompts = 0
    auto_convert = AUTO_CONVERT
    leases = {}           # "redeem"/"create" -> lease id
    conv_hold_until = -1  # while a conversion runs, positions are in flux: don't trade the ETF tickers
    now_tick = -1


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
    if ST.now_tick < ST.conv_hold_until and any(t in ("RITC", "BULL", "BEAR") for t in tickers):
        return True       # a conversion is swapping RITC <-> BULL+BEAR right now
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
    jlog({"type": "order", "tick": ST.now_tick, "t": round(time.time(), 3), "ticker": ticker, "action": action,
          "qty": qty, "resp": {k: j.get(k) for k in ("order_id", "status", "quantity_filled", "vwap", "price")}})
    filled = int(j.get("quantity_filled") or 0) if j.get("status") == "TRANSACTED" else qty
    q_ = ST.last_quotes.get(ticker)
    if j.get("vwap") and q_ and q_[0] > 0 and ticker in ("BULL", "BEAR") and filled:
        mid = (q_[0] + q_[1]) / 2
        ST.fill_costs.append((ST.now_tick, ticker, filled,
                              (float(j["vwap"]) - mid if action == "BUY" else mid - float(j["vwap"])) + FEE))
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
    books, raw_books = {}, {}
    for t in ("BULL", "BEAR", "RITC"):
        b = get("/securities/book", ticker=t, limit=40)
        if not b:
            return None
        raw_books[t] = b
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
    return {"pos": pos, "q": quotes, "books": books, "raw_books": raw_books, "gross_limit": gl, "net_limit": nl,
            "limits_raw": lim}


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
def leg_value(levels, mid, q, sgn, share=None):
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
        share = CROWD_SHARE if share is None else share
        near = [(px, sz * share) for px, sz in levels if abs(px - best) <= HEDGE_SLIP + 1e-9]
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
        total += qm * (vwap + sgn * FEE) + later * sgn * REFILL_PENALTY / max(share, 0.1)
    return total / q


def blocks_to_convert(pos, gl, nl, d_ritc=0):
    """Hedged 10k blocks we must redeem/create so the FULLY hedged book fits under CONVERT_TARGET."""
    r = pos.get("RITC", 0) + d_ritc
    full = usage({"RITC": r, "BULL": -r, "BEAR": -r}, gl, nl)
    if full <= CONVERT_TARGET:
        return 0
    excess = (full - CONVERT_TARGET) * gl            # gross units over target (net is 0 when hedged)
    return math.ceil(excess / (4 * CONVERT_BLOCK))


def evaluate_tender(t, snap, share=None):
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
            stock = (leg_value(bk["BULL"]["bids"], mids["BULL"], qs, -1, share)
                     + leg_value(bk["BEAR"]["bids"], mids["BEAR"], qs, -1, share)) if qs else 0.0
            ritc = leg_value(bk["RITC"]["bids"], rmid, x, -1, share) * usd if x else 0.0
            value = (qs * stock + x * ritc) / q                       # CAD received per tender share
            breakeven_usd = (value - extra - TENDER_MIN_EDGE) / usd
            edge = value - extra - (px * usd if px else breakeven_usd * usd + TENDER_MIN_EDGE)
            bid = math.floor(breakeven_usd * 100) / 100
        else:              # we sold RITC: buy the basket for qs shares, buy x RITC back
            stock = (leg_value(bk["BULL"]["asks"], mids["BULL"], qs, 1, share)
                     + leg_value(bk["BEAR"]["asks"], mids["BEAR"], qs, 1, share)) if qs else 0.0
            ritc = leg_value(bk["RITC"]["asks"], rmid, x, 1, share) * usd if x else 0.0
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


def shadow_register(t, snap, why, edge):
    """Start watching the market after this tender (accepted or not) to estimate the crowd."""
    q = snap["q"]
    if not quotes_ok(q):
        return
    basket0 = sum(q["BULL"]) / 2 + sum(q["BEAR"]) / 2
    ST.shadow_obs.append({"t0": int(t.get("tick", ST.now_tick)), "sgn": 1 if t["action"] == "BUY" else -1,
                          "basket0": basket0, "accepted": why.startswith("ok") or why.startswith("auction"),
                          "qty": t["quantity"]})


def shadow_note(tick, t, s_edge, s_take, edge, why):
    take = why.startswith("ok") or why.startswith("auction")
    differs = " <- DIFFERENT" if s_take != take else ""
    event(tick, "SHADOW", f"with crowd estimate {ST.shadow_share:.2f}: edge {s_edge:+.3f}/sh -> "
                          f"{'accept' if s_take else 'decline'} (actual: {'accept' if take else 'decline'} "
                          f"at {CROWD_SHARE:.2f}){differs}")
    jlog({"type": "shadow_decision", "tick": tick, "tender_id": t.get("tender_id"), "shadow_share": ST.shadow_share,
          "shadow_edge": s_edge, "shadow_accept": s_take, "edge": edge, "accept": take, "crowd_share": CROWD_SHARE})


def shadow_update(tick):
    """After SHADOW_OBS_TICKS, turn what happened into an observed crowd share and move the estimate."""
    keep = []
    for ob in ST.shadow_obs:
        if tick < ob["t0"] + SHADOW_OBS_TICKS:
            keep.append(ob)
            continue
        path = [ST.basket_hist[k] for k in range(ob["t0"], ob["t0"] + SHADOW_OBS_TICKS + 1) if k in ST.basket_hist]
        if not path:
            continue
        # push = how far the basket moved against whoever hedges this tender (BUY tender -> hedgers sell basket)
        push = max(-ob["sgn"] * (b - ob["basket0"]) for b in path)
        push = max(0.0, push)
        s_push = 1.0 / (1.0 + push / SHADOW_PUSH_REF)
        # our own hedge cost in that window, if we traded (compare with an uncrowded ~0.03-0.05/sh)
        fills = [(sh, c) for (tk, _, sh, c) in ST.fill_costs if ob["t0"] <= tk <= ob["t0"] + SHADOW_OBS_TICKS]
        sh_tot = sum(sh for sh, _ in fills)
        cost = sum(sh * c for sh, c in fills) / sh_tot if sh_tot else None
        s_cost = min(1.0, 0.04 / cost) if cost and cost > 0 else None
        s_obs = min(s_push, s_cost) if s_cost is not None else s_push
        old = ST.shadow_share
        ST.shadow_share = min(1.0, max(0.2, (1 - SHADOW_WEIGHT) * old + SHADOW_WEIGHT * s_obs))
        event(tick, "CROWD", f"after tender @t={ob['t0']}: basket pushed {push:.3f} against hedgers"
                             + (f", our hedge cost {cost:.3f}/sh" if cost is not None else "")
                             + f" -> observed share {s_obs:.2f}, estimate {old:.2f} -> {ST.shadow_share:.2f}"
                             + " (shadow only)")
        jlog({"type": "shadow_obs", "tick": tick, "t0": ob["t0"], "accepted": ob["accepted"], "push": push,
              "our_cost": cost, "s_push": s_push, "s_cost": s_cost, "s_obs": s_obs, "estimate": ST.shadow_share})
    ST.shadow_obs = keep


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
        s_edge, _, s_why = evaluate_tender(t, snap, share=ST.shadow_share)      # shadow first (side effects)
        edge, bid, why = evaluate_tender(t, snap)
        s_take = s_why.startswith("ok") or s_why.startswith("auction")
        working = still_hedging(snap)
        if working and (why.startswith("ok") or why.startswith("auction")):
            why = f"still hedging the last tender ({working}); edge was {edge:+.3f}/sh"
        shadow_register(t, snap, why, edge)
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
            shadow_note(tick, t, s_edge, s_take, edge, why)
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
        shadow_note(tick, t, s_edge, s_take, edge, why)
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


def _lease_id(kind):
    """Lease the converter once per heat (free, lasts the whole case); reuse a lease you already hold."""
    if kind in ST.leases:
        return ST.leases[kind]
    ticker = CONVERTER_TICKERS[kind]
    for L in get("/leases") or []:
        if L.get("ticker") == ticker:
            ST.leases[kind] = L["id"]
            return L["id"]
    r = _req("POST", "/leases", ticker=ticker)
    if r is not None and r.ok and "id" in r.json():
        ST.leases[kind] = r.json()["id"]
        return ST.leases[kind]
    say(f"[converter] could not lease {ticker}: {r.text[:160] if r is not None else 'no response'}")
    return None


def _use_converter(kind):
    lid = _lease_id(kind)
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
    """Redeem/create one hedged 10k block at a time while the fully hedged book doesn't fit in our limits.
    Automatic through the lease API when allowed (AUTO_CONVERT); otherwise prompts you to click."""
    ticks_total = snap.get("ticks", 300)
    if tick < ST.conv_busy_until or tick > ticks_total - CONVERT_STOP_BEFORE_END:
        return
    if busy("RITC", "BULL", "BEAR"):
        return                                       # wait for fills/conversions to show first
    pos, gl, nl = snap["pos"], snap["gross_limit"], snap["net_limit"]
    need = blocks_to_convert(pos, gl, nl, d_ritc=ST.ritc_plan)
    if need == 0:
        return
    r = pos.get("RITC", 0) + ST.ritc_plan
    sgn = 1 if r > 0 else -1
    hedged = min(abs(r), max(0.0, -sgn * pos.get("BULL", 0)), max(0.0, -sgn * pos.get("BEAR", 0)))
    if hedged < CONVERT_BLOCK:
        return                                       # hedge the next block first
    kind = "redeem" if r > 0 else "create"
    ST.conv_prompts += 1
    if ST.auto_convert:
        if _use_converter(kind):
            ST.conv_busy_until = tick + CONVERT_TICKS
            ST.conv_hold_until = tick + CONVERT_TICKS + 1
            event(tick, "CONVERT", f"{kind.upper()} 1 x 10,000 (#{ST.conv_prompts}, fee ${CONVERT_FEE_USD:,} USD)"
                                   f"  [{need} block{'s' if need > 1 else ''} needed]")
            return
        ST.auto_convert = False
        banner("CONVERTER API NOT AVAILABLE: switching to prompts.\n"
               "Click ETF-Redemption / ETF-Creation in the RIT Client Assets tab when asked.")
    banner(f">>> CONVERTER: {kind.upper()} 1 x 10,000 NOW  (Assets tab -> {CONVERTER_TICKERS[kind]})"
           f"   [{need} block{'s' if need > 1 else ''} still needed]")
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
    global _jsonl
    os.makedirs(LOG_DIR, exist_ok=True)
    set_log_file(TXT_FILE)
    _jsonl = open(JSONL_FILE, "a", encoding="utf-8")
    say(f"[logging to {LOG_DIR}/etf_{_STAMP}.*]")
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
            ST.leases, ST.conv_hold_until, ST.auto_convert = {}, -1, AUTO_CONVERT
            ST.shadow_share, ST.shadow_obs, ST.basket_hist, ST.fill_costs = SHADOW_START, [], {}, []
        if tick != ST.flush_tick:
            for what, (qty, edge) in ST.arb_acc.items():
                event(ST.flush_tick, "ARB", f"{what}: {qty:,} shares, edge {edge:+.3f}/sh")
            ST.arb_acc = {}
            ST.flush_tick = tick
        ST.last_tick = tick
        ST.now_tick = tick
        loop_start = time.time()
        ticks = case.get("ticks_per_period", 300)
        snap = snapshot_at(tick, ticks)
        if not snap:
            time.sleep(LOOP_SLEEP)
            continue
        if handle_tenders(snap, tick):
            snap = snapshot_at(tick, ticks) or snap
        ST.last_quotes = snap["q"]
        if quotes_ok(snap["q"]) and tick not in ST.basket_hist:
            ST.basket_hist[tick] = sum(snap["q"]["BULL"]) / 2 + sum(snap["q"]["BEAR"]) / 2
            shadow_update(tick)
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
            lvl = lambda side: [[x.get("price"), x.get("quantity", 0) - x.get("quantity_filled", 0), x.get("trader_id")]
                                for x in side[:10]]
            traders = sorted({x.get("trader_id") for b in snap["raw_books"].values()
                              for x in b.get("bids", []) + b.get("asks", []) if x.get("trader_id")})
            jlog({"type": "tick", "tick": tick, "t": round(time.time(), 3), "pos": p, "quotes": snap["q"],
                  "books": {k: {"bids": lvl(b.get("bids", [])), "asks": lvl(b.get("asks", []))}
                            for k, b in snap["raw_books"].items()},
                  "traders_in_book": traders, "limits": snap["limits_raw"], "nlv": nlv,
                  "ritc_plan": ST.ritc_plan})
            _jsonl.flush()
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
