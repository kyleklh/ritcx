"""
RITCx Algorithmic ETF Arbitrage Case - trading algorithm.

Core idea: every RITC position is immediately HEDGED with the basket (BULL + BEAR), so we hold
"pairs" instead of naked RITC. Because the case closes out at the CORRECT price, a hedged pair
converges for free at the end. Profit comes from the price at which we enter the pair:

  1. TENDERS: accept only if (tender price vs. the basket we can actually trade, walking the book)
     beats fees + a safety edge. Auction tenders get a bid priced to keep that edge.
  2. HEDGE: keep BULL = BEAR = -RITC at all times. Right after a tender, hedge ALL of it at market at once
     (other teams hedge the same tender and move the basket within a few ticks).
  3. ARB: when RITC vs basket is mispriced beyond costs, trade RITC (the hedge module adds the basket).
     Trades that shrink an existing pair need less edge, since they also free up limit capacity.
  4. FX: RITC settles in USD, so keep the USD cash position near 0 with the USD ticker.
  6. ROOM + END: shrink pairs left from a tender (market, then converters) so the next tender fits, and
     flatten everything before the close-out.
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
TENDER_MIN_EDGE = float(os.environ.get("TENDER_MIN_EDGE", "0.05"))        # CAD/share profit required after hedge cost + fees
# What hedging a tender with BULL+BEAR really costs, CAD per share incl. both fees, vs the basket mid when the
# tender arrives. Measured by replaying all 32 tenders of the Nov 16 real heats against the logged books (take
# everything within 0.03 of best each tick): mean 0.18, range 0.09-0.33. Walking the visible book underestimated
# it (0.09-0.19); with this number, tenders 0.25+ over the basket were the profitable ones (~$98k over 4 heats).
# Across all 48 logged tenders (Nov 16 heats + 2 solo practices): 0.18 took 8 (all winners, +$153k), 0.15 took
# 11 (all winners, +$177k), 0.13 took 12 (+$180k), 0.10 took 13 incl. a loser. 0.15 keeps a margin over 0.13.
TENDER_HEDGE_COST = float(os.environ.get("TENDER_HEDGE_COST", "0.15"))
TENDER_MIN_PROFIT = 500       # CAD total; skip tiny wins
UNHEDGED_PENALTY = 0.03       # CAD/share charged on the part we cannot hedge right away (waits for converters)
RITC_ROUTE_SPLITS = (0.0,)     # fractions of a tender we may trade straight back into the RITC book (off: on the
                              # real books it cost up to 0.30/sh, and TENDER_HEDGE_COST is measured without it)
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
LIMIT_TRANSIENT_MAX = 1.00    # max usage right after accepting a tender, before any hedge trades. Was 0.97: a
                              # 98k tender alone is 196k of the 200k NET limit (RITC counts 2x), so every tender
                              # above ~97k was refused even when flat (practice Nov 16 t=116: a +$57k tender lost)
HEDGE_USAGE_MAX = 0.96        # hedge orders never push gross usage above this (converters free the rest)

# Hedging: passive first (limit orders earn the rebate), market orders only for risk above URGENT_GAP
URGENT_GAP = 5000             # max unhedged shares per leg we tolerate; anything beyond is hedged at market
STALL_TICKS = 5               # if a leg's gap hasn't closed passively after this many ticks, close it at market
HEDGE_SLIP = 0.03             # market hedges only take liquidity within this distance of the best price
# For FAST_TICKS after we take a tender, hedge ALL of it at market, no passive part: once per tick, everything
# within FAST_SLIP of that tick's best price, in 10k clips alternating BULL/BEAR. Replaying the real books, this
# patient version cost ~0.18/sh; walking to 0.15 at once cost ~0.26 (book levels are 0.03-0.08 apart, ~10k each).
FAST_TICKS = int(os.environ.get("FAST_TICKS", "12"))
FAST_SLIP = float(os.environ.get("FAST_SLIP", "0.03"))
HEDGE_MIN = 100               # ignore smaller imbalances
REBATE = 0.03                 # limit order rebate per share (case info sheet)

# Arbitrage
# Big arb (ARB_BIG=1, default): RITC vs BULL+BEAR gaps above 0.05/sh after fees show up on ~13% of ticks and close
# within 2-3 ticks. Replaying the 8 real heats' books: 60k pairs, 10k clips, open at 0.10 -> ~+12-16k/heat (+6k in
# the 4 competition heats with 0.03/sh extra slippage) vs ~+2-4k for the small arb (10k pairs, 5k clips, 0.03).
# All three legs are traded at once, and only as much as the visible books give at the edge. The gaps last <1 tick:
# acting a full tick late loses money in every version, so this relies on the loop being fast.
# ARB_BIG=0 in .env switches back to the small arb.
ARB_BIG = os.environ.get("ARB_BIG", "1") == "1"
ARB_OPEN_EDGE = 0.10 if ARB_BIG else 0.03   # CAD/share net of 3 fees, to OPEN / grow pairs
# Practice Nov 19 (3 heats, 75 three-leg trades): opens planned +0.116 realized +0.090; closes planned +0.041
# realized -0.017 (a close fires right as the gap flips back, and the flip is often gone before all legs fill).
# Replaying 11 real heats with that slippage: close at +0.06 -> +7.5k/heat vs +6.4k at 0.00 (old small arb +0.7k).
ARB_CLOSE_EDGE = 0.06 if ARB_BIG else 0.00   # CAD/share net of fees, to CLOSE existing pairs
ARB_CLIP = 10000 if ARB_BIG else 5000
ARB_STEP = 1000               # sizes tried when walking the books
# Self-check: after every opening trade, compare the edge we planned with what the fills gave. Other teams' bots race
# for the same gaps (practice, 5 traders: openings planned +0.11, realized -0.05). If the last ARB_GUARD_N openings
# realized less than ARB_GUARD_MIN on average, demand ARB_GUARD_STEP more edge (up to ARB_GUARD_MAX); give it back
# when they realize more than ARB_GUARD_OK.
ARB_GUARD_N = 4
ARB_GUARD_MIN = 0.02
ARB_GUARD_OK = 0.06
ARB_GUARD_STEP = 0.05
ARB_GUARD_MAX = 0.30
HOLD_MIN_EDGE = 0.15          # only tenders this good are worth paying to make room for (practice: two HOLDs at
                              # 0.09-0.11 cost ~2.2k of unwinding and the edge was gone 2 ticks later)
PENDING_SHARE = 0.6           # a good tender blocked by limits: spend up to this share of its edge unwinding pairs
PENDING_MAX_COST = 0.25       # ...but never more than this CAD/share per pair unwound (crossing 3 spreads + fees
                              # costs ~0.19 at a neutral premium; converters ~0.15 but 2 ticks per 10k block)
ARB_MAX_PAIRS = 60000 if ARB_BIG else 10000         # max |RITC| the arb module builds on its own (keeps limit room for tenders). Was 20k:
                              # 12.9k arb pairs + a 97k tender the same way = 102% of the net limit, so a tender worth
                              # ~+$16k was refused (practice t=75). With 10k, any tender up to 100k fits (2*110k-20k).
ROOM_TARGET = 10000           # after a tender, unwind pairs above this |RITC| to make room for the next one...
ROOM_UNWIND_EDGE = -0.05      # ...even at a small loss: up to this CAD/share past fees (converting costs ~0.15)
ROOM_CONVERT_AFTER = 10       # if the market hasn't shrunk them this many ticks after a tender...
ROOM_CONVERT_MIN_LEFT = 60    # ...but only while another tender can still come and be taken (they arrive every
                              # ~25-35 ticks; none are taken in the last TENDER_CUTOFF_TICKS). Practice: after a t=239
                              # tender, 4-5 room conversions (~$7k of fees) freed room no tender could use.
ROOM_CONVERT_ABOVE = 30000    # ...convert hedged 10k blocks while |RITC| is above this (frees room for the next tender;
                              # real heats: 4 tenders worth ~$44k declined on limits while we sat on old pairs)
# End of heat: the close-out marks RITC at its last price, which sits +-0.2-0.4 CAD off the basket, so pairs held
# into the close are a coin flip (heat 4: -7k in the last 5 ticks). Flatten instead.
NO_OPEN_TICKS = 30            # no new arb pairs in the last N ticks
FLATTEN_TICKS = 20            # in the last N ticks, unwind every pair at up to FLATTEN_EDGE...
FLATTEN_EDGE = -0.12
QUIET_TICKS = 2               # ...and in the last N ticks trade nothing but hedge gaps
TENDER_CUTOFF_TICKS = 35      # decline tenders this close to the end: no time to hedge and unwind them

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
    leases = {}           # unused (lease ids are looked up fresh)
    convert_failures = 0
    conv_hold_until = -1  # while a conversion runs, positions are in flux: don't trade the ETF tickers
    now_tick = -1
    fast_until = -1       # tick until which the hedge runs in fast mode (just took a tender)
    route_tick = -1
    leased = False
    pending = None        # a good tender we're making room for: {"t": tender, "deadline": tick}
    last_vwap = {}        # ticker -> vwap of our last market order
    arb_real = []         # realized edges of recent opening trades
    arb_extra = 0.0       # extra edge demanded by the self-check
    last_tender_tick = -100


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
    ST.last_vwap[ticker] = float(j["vwap"]) if j.get("vwap") else None
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
    """Expected CAD/share (incl. fees) to trade q shares of one leg: the mid plus half of TENDER_HEDGE_COST
    (measured on the real heats; see the setting). An empty book means something is broken: make it expensive."""
    if not levels:
        return mid + sgn * (FEE + 0.10)
    return mid + sgn * TENDER_HEDGE_COST / 2


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
    fast = tick <= ST.fast_until
    if fast and ST.route_tick == tick:
        return                                   # fast: once per tick, like the hedge
    ST.route_tick = tick
    avail = max(levels[0][1] if levels else 0, depth_within(levels, FAST_SLIP if fast else HEDGE_SLIP))
    side = "SELL" if ST.ritc_plan < 0 else "BUY"
    cur = snap["pos"].get("RITC", 0)
    for _ in range(4 if fast else 1):            # fast: several clips now, before the crowd takes the book
        qty = int(min(abs(ST.ritc_plan), MAX_EQ_ORDER, avail))
        if qty < HEDGE_MIN:
            return
        f = market("RITC", side, qty, cur)
        if not f:
            return
        cur += f if side == "BUY" else -f
        avail -= f
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


def _accept_fixed(t, snap, tick):
    r = _req("POST", f"/tenders/{t['tender_id']}")
    ok = r is not None and r.ok and r.json().get("success", True)
    if ok:
        q, act = t["quantity"], t["action"]
        note_fill("RITC", snap["pos"].get("RITC", 0), q if act == "BUY" else -q)
        plan_ritc_route(act, tick)
        ST.fast_until, ST.last_tender_tick = tick + FAST_TICKS, tick
    return ok


def pending_need(snap):
    """Same-direction pairs we must remove so the pending tender fits (gross 4*pairs + 2*tender <= limit)."""
    P = ST.pending
    if not P:
        return 0
    r = effective_pos(snap).get("RITC", 0)
    q = P["t"]["quantity"]
    if (P["t"]["action"] == "BUY") != (r > 0):
        return 0
    return max(0, int(abs(r) - (LIMIT_TRANSIENT_MAX * snap["gross_limit"] - 2 * q) / 4) + 2000)


def check_pending(snap, tick):
    """A good tender we couldn't fit: the arb unwinds same-direction pairs; accept as soon as it fits."""
    P = ST.pending
    if not P:
        return False
    t = P["t"]
    q, act = t["quantity"], t["action"]
    desc = f"{act:<4} {q:>6,} RITC @ {t.get('price')}"
    ST.last_detail = ""
    edge, _, why = evaluate_tender(t, snap)
    if why.startswith("ok") and not still_hedging(snap):
        ok = _accept_fixed(t, snap, tick)
        event(tick, "TENDER", f"ACCEPT  {desc}  edge {edge:+.3f}/sh = ${edge * q:,.0f}  (after making room)"
              + ("" if ok else "  (FAILED)"))
        ST.pending = None
        return ok
    if tick >= P["deadline"] or edge < TENDER_MIN_EDGE or ST.paused:
        _req("DELETE", f"/tenders/{t['tender_id']}")
        event(tick, "TENDER", f"decline {desc}  edge {edge:+.3f}/sh  (could not make room in time: {why})")
        ST.pending = None
    return False


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
        if snap.get("ticks", 300) - tick < TENDER_CUTOFF_TICKS and (why.startswith("ok") or why.startswith("auction")):
            why = f"too close to the end to hedge and unwind; edge was {edge:+.3f}/sh"
        shadow_register(t, snap, why, edge)
        q, act, px = t["quantity"], t["action"], t.get("price")
        desc = f"{act:<4} {q:>6,} RITC @ {px if px is not None else 'auction'}"
        if (why.startswith("limits") and t.get("is_fixed_bid", True) and not ST.pending
                and edge >= max(TENDER_MIN_EDGE, HOLD_MIN_EDGE) and edge * q >= TENDER_MIN_PROFIT
                and snap.get("ticks", 300) - tick >= TENDER_CUTOFF_TICKS
                and tick < int(t.get("expires", tick + 6)) - 1):
            ST.pending = {"t": t, "deadline": int(t.get("expires", tick + 6)) - 1}
            need = pending_need(snap)
            ST.pending["max_cost"] = min(PENDING_MAX_COST, PENDING_SHARE * edge * q / need) if need else 0.0
            event(tick, "TENDER", f"HOLD    {desc}  edge if it fits {edge:+.3f}/sh = ${edge * q:,.0f}: "
                                  f"unwinding pairs to make room ({why.split(';')[0]})")
            shadow_note(tick, t, s_edge, s_take, edge, why)
            continue
        if why.startswith("ok"):
            ok = _accept_fixed(t, snap, tick)
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
                ST.fast_until, ST.last_tender_tick = tick + FAST_TICKS, tick
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
    """Target BULL = BEAR = -RITC.
    Normal: gap beyond URGENT_GAP -> market now (within HEDGE_SLIP); the rest -> limit order at our best price.
    Fast (just took a tender, or the last FLATTEN_TICKS): the whole gap at market right away, in several clips
    per loop alternating BULL/BEAR, within FAST_SLIP. No resting orders: a resting bid that fills while we also
    buy at market is what overshot the hedge by ~10k in the real heats."""
    pos, bk, q = effective_pos(snap), snap["books"], snap["q"]
    target = -(pos.get("RITC", 0) + ST.ritc_plan)     # RITC still going back into its own book needs no hedge
    orders = open_orders() if orders is None else orders
    did = False
    fast = tick <= ST.fast_until or snap.get("ticks", 300) - tick <= FLATTEN_TICKS
    slip = FAST_SLIP if fast else HEDGE_SLIP
    gross = abs(pos.get("BULL", 0)) + abs(pos.get("BEAR", 0)) + 2 * abs(pos.get("RITC", 0))
    leg_room = max(0.0, (HEDGE_USAGE_MAX * snap["gross_limit"] - gross) / 2)   # gross we may add, per leg
    raw_gap = {l: target - pos.get(l, 0) for l in ("BULL", "BEAR")}
    plan = {}
    for leg in ("BULL", "BEAR"):
        if busy(leg):
            continue              # last fill not visible yet: wait rather than act on a stale position
        gap = int(round(target - pos.get(leg, 0)))
        p0 = pos.get(leg, 0)
        # only the part of the gap that moves this leg AWAY from zero adds gross: cap that part by the room
        toward_zero = min(abs(gap), abs(p0)) if p0 * gap < 0 else 0
        gap = int(math.copysign(min(abs(gap), toward_zero + leg_room), gap)) if gap else 0
        side = "BUY" if gap > 0 else "SELL"
        if abs(gap) >= HEDGE_MIN:
            since = ST.gap_since.setdefault(leg, tick)
        else:
            ST.gap_since.pop(leg, None)
            since = tick
        tolerance = 0 if fast or tick - since >= STALL_TICKS else URGENT_GAP
        urgent = abs(gap) - tolerance
        # keep the legs in step: don't let this leg get more than URGENT_GAP ahead of the other one
        other = raw_gap["BEAR" if leg == "BULL" else "BULL"]
        if not fast and other * gap > 0:
            urgent = min(urgent, abs(gap) - max(0.0, abs(other) - URGENT_GAP))
        mine = [o for o in orders if o["ticker"] == leg]
        if urgent >= HEDGE_MIN and mine:
            _req("POST", "/commands/cancel", ticker=leg)   # never market-buy on top of a resting bid
            mine = []
        levels = bk[leg]["asks"] if gap > 0 else bk[leg]["bids"]
        avail = max(levels[0][1] if levels else 0, depth_within(levels, slip))
        plan[leg] = {"gap": gap, "side": side, "urgent": max(0, urgent), "avail": avail, "mine": mine}
    # 1) market part. Fast: once per tick (the book refills between ticks; taking "within FAST_SLIP of the best"
    #    again in the same tick would walk down the book), alternating the legs, several clips each.
    for _ in range((5 if passive else 0) if fast else 1):
        sent = False
        for leg, P in plan.items():
            # keep the legs in step: never more than URGENT_GAP ahead of the other leg (it may be waiting on a fill)
            o = "BEAR" if leg == "BULL" else "BULL"
            o_gap = plan[o]["gap"] if o in plan else raw_gap[o]
            ahead_ok = abs(P["gap"]) - max(0.0, abs(o_gap) - URGENT_GAP) if o_gap * P["gap"] > 0 else abs(P["gap"])
            qty = min(P["urgent"], MAX_EQ_ORDER, P["avail"], ahead_ok)
            if qty < HEDGE_MIN:
                continue
            f = market(leg, P["side"], qty, pos.get(leg, 0))
            if f:
                sign = 1 if P["side"] == "BUY" else -1
                pos[leg] = pos.get(leg, 0) + sign * f
                P["gap"] -= sign * f
                P["urgent"] -= f
                P["avail"] -= f
                did = sent = True
        if not sent:
            break
    # 2) passive part: one resting order at the best bid (buy) / best ask (sell), normal mode only.
    #    Only once per tick: a resting order's fills can show in /securities late, and re-sizing
    #    it several times per tick on a stale position stacks orders and overshoots the hedge.
    for leg, P in plan.items():
        mine, gap, side = P["mine"], P["gap"], P["side"]
        if fast:
            if mine:
                _req("POST", "/commands/cancel", ticker=leg)
            continue
        if not passive:
            continue
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
def _vwap(levels, q):
    got, cost = 0, 0.0
    for px, sz in levels:
        k = min(sz, q - got)
        if k <= 0:
            break
        got += k
        cost += k * px
    return cost / got if got >= q else None


def pair_edge(snap, side, qty):
    """CAD/share after 3 fees for trading qty pairs at market, walking the visible books.
    side -1: sell RITC / buy BULL+BEAR.  side +1: buy RITC / sell BULL+BEAR."""
    bk, (ub, ua) = snap["books"], snap["q"]["USD"]
    if side < 0:
        rp, bp, ep = _vwap(bk["RITC"]["bids"], qty), _vwap(bk["BULL"]["asks"], qty), _vwap(bk["BEAR"]["asks"], qty)
        return None if None in (rp, bp, ep) else rp * ub - bp - ep - 3 * FEE
    rp, bp, ep = _vwap(bk["RITC"]["asks"], qty), _vwap(bk["BULL"]["bids"], qty), _vwap(bk["BEAR"]["bids"], qty)
    return None if None in (rp, bp, ep) else bp + ep - rp * ua - 3 * FEE


def pair_size(snap, side, thr, cap):
    """Largest size (ARB_STEP steps, up to cap) whose average edge is still >= thr. Returns (size, edge)."""
    best, best_e, n = 0, None, ARB_STEP
    while n <= cap:
        e = pair_edge(snap, side, n)
        if e is None or e < thr:
            break
        best, best_e, n = n, e, n + ARB_STEP
    return best, best_e


def trade_pair(side, qty, pos, label, edge, snap=None):
    """All three legs at market, back to back. A leg that fills short is fixed by hedge()."""
    f = market("RITC", "SELL" if side < 0 else "BUY", qty, pos.get("RITC", 0))
    if not f:
        return 0
    rp = ST.last_vwap.get("RITC")
    pos["RITC"] = pos.get("RITC", 0) + (f if side > 0 else -f)
    leg_act = "BUY" if side < 0 else "SELL"
    vw = {}
    for leg in ("BULL", "BEAR"):
        g = market(leg, leg_act, f, pos.get(leg, 0))
        vw[leg] = ST.last_vwap.get(leg) if g else None
        pos[leg] = pos.get(leg, 0) + (g if leg_act == "BUY" else -g)
    if label == "ARB" and snap and rp and vw.get("BULL") and vw.get("BEAR"):
        ub, ua = snap["q"]["USD"]
        real = (rp * ub - vw["BULL"] - vw["BEAR"] if side < 0 else vw["BULL"] + vw["BEAR"] - rp * ua) - 3 * FEE
        ST.arb_real = (ST.arb_real + [real])[-ARB_GUARD_N:]
        if len(ST.arb_real) >= ARB_GUARD_N:
            avg = sum(ST.arb_real) / len(ST.arb_real)
            old = ST.arb_extra
            if avg < ARB_GUARD_MIN:
                ST.arb_extra = min(ARB_GUARD_MAX - ARB_OPEN_EDGE, ST.arb_extra + ARB_GUARD_STEP)
            elif avg > ARB_GUARD_OK:
                ST.arb_extra = max(0.0, ST.arb_extra - ARB_GUARD_STEP)
            if ST.arb_extra != old:
                ST.arb_real = []
                event(ST.now_tick, "ARB", f"self-check: last openings realized {avg:+.3f}/sh (planned ~{edge:+.3f}) -> "
                                          f"open edge now {ARB_OPEN_EDGE + ST.arb_extra:.2f}")
    a = ST.arb_acc.setdefault(label + (": sell RITC / buy basket" if side < 0 else ": buy RITC / sell basket"), [0, 0])
    a[0] += f
    a[1] = edge
    return f


def arb(snap, tick):
    if snap["tick"] < ST.conv_busy_until or busy("RITC", "BULL", "BEAR"):
        return False
    if ST.paused and not ST.pending:
        return False
    q, pos = snap["q"], effective_pos(snap)
    if max(abs(pos.get(l, 0) + pos.get("RITC", 0)) for l in ("BULL", "BEAR")) > URGENT_GAP:
        return False                     # still hedging a tender: don't add more
    if not quotes_ok(q):
        return False
    ub, ua = q["USD"]
    top_sell = q["RITC"][0] * ub - (q["BULL"][1] + q["BEAR"][1]) - 3 * FEE
    top_buy = (q["BULL"][0] + q["BEAR"][0]) - q["RITC"][1] * ua - 3 * FEE
    if max(top_sell, top_buy) > MAX_SANE_EDGE:
        return False                     # a price is stale/missing (e.g. at tick 0): not a real edge
    ticks_left = snap.get("ticks", 300) - tick
    if ticks_left <= QUIET_TICKS:
        return False
    flatten = ticks_left <= FLATTEN_TICKS
    can_open = ticks_left > NO_OPEN_TICKS and not ST.pending and not ST.paused
    r = pos.get("RITC", 0)
    gl, nl = snap["gross_limit"], snap["net_limit"]
    # 1) shrink existing pairs: at a profit always; at a small loss for room / the end / a pending tender
    if abs(r) >= 100:
        side = -1 if r > 0 else 1
        room_target = 0 if flatten else (ARB_MAX_PAIRS if ARB_BIG else ROOM_TARGET)
        cands = [(ARB_CLOSE_EDGE, abs(r), "CLOSE")]
        if abs(r) > room_target:
            cands.append((FLATTEN_EDGE if flatten else ROOM_UNWIND_EDGE, abs(r) - room_target,
                          "FLATTEN" if flatten else "UNWIND for room"))
        need = pending_need(snap)
        if need:
            cands.append((-ST.pending["max_cost"], need, "UNWIND for tender"))
        best = (0, None, "")
        for thr, cap, label in cands:
            n, e = pair_size(snap, side, thr, min(ARB_CLIP, cap))
            if n > best[0]:
                best = (n, e, label)
        if best[0] >= 100:
            return trade_pair(side, best[0], pos, best[2], best[1], snap) > 0
    # 2) open / grow pairs
    if not can_open:
        return False
    for side in (-1, 1):
        if r * side < 0:
            continue                     # that side shrinks pairs: handled above
        cap = min(ARB_CLIP, ARB_MAX_PAIRS - abs(r))
        if cap < 100:
            continue
        n, e = pair_size(snap, side, ARB_OPEN_EDGE + ST.arb_extra, cap)
        if n >= 100 and usage(pos, gl, nl, d_ritc=side * n, d_bull=-side * n, d_bear=-side * n) <= LIMIT_USAGE_MAX:
            return trade_pair(side, n, pos, "ARB", e, snap) > 0
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
    """Server positions plus our own market fills it doesn't show yet (so the hedge can act on the RITC part of a
    tender, and on RITC routed back into its book, without waiting for /securities to catch up)."""
    pos = dict(snap["pos"])
    for tk, f in ST.inflight.items():
        unseen = f["total"] - (pos.get(tk, 0) - f["base"])
        if unseen * f["total"] > 0:              # only fills in the direction we traded, never more than we sent
            pos[tk] = pos.get(tk, 0) + math.copysign(min(abs(unseen), abs(f["total"])), unseen)
    return pos


def _lease_id(kind):
    """The id of our lease on this converter: looked up fresh each time (a stale id is what the server
    rejected as 'Invalid lease id'); leases it first if we don't hold one."""
    ticker = CONVERTER_TICKERS[kind]
    for L in get("/leases") or []:
        if L.get("ticker") == ticker:
            return L.get("id")
    r = _req("POST", "/leases", ticker=ticker)
    if r is not None and r.ok:
        try:
            return r.json().get("id")
        except ValueError:
            return None
    say(f"[converter] could not lease {ticker}: {r.text[:160] if r is not None else 'no response'}")
    return None


def _use_converter(kind):
    """Use the converter once (10,000). Tries, in order:
      1. POST /leases/{id}?from1=..                           (lease, then use the lease id: works on the real server)
      2. POST /leases?ticker=ETF-...&from1=..&quantity1=..   (by ticker: refused for REFINERY assets, kept as a fallback)"""
    ticker = CONVERTER_TICKERS[kind]
    if kind == "redeem":
        params = {"from1": "RITC", "quantity1": CONVERT_BLOCK, "from2": "USD", "quantity2": CONVERT_FEE_USD}
    else:
        params = {"from1": "BULL", "quantity1": CONVERT_BLOCK, "from2": "BEAR", "quantity2": CONVERT_BLOCK,
                  "from3": "USD", "quantity3": CONVERT_FEE_USD}
    lid = _lease_id(kind)
    err2 = "no lease"
    if lid is not None:
        r = _req("POST", f"/leases/{lid}", **params)
        if r is not None and r.ok:
            jlog({"type": "convert", "tick": ST.now_tick, "kind": kind, "how": f"lease id {lid}", "resp": r.text[:300]})
            return True
        err2 = r.text[:160] if r is not None else "no response"
    r = _req("POST", "/leases", ticker=ticker, **params)       # never worked on the real server; last resort
    if r is not None and r.ok:
        jlog({"type": "convert", "tick": ST.now_tick, "kind": kind, "how": "ticker", "resp": r.text[:300]})
        return True
    err1 = r.text[:160] if r is not None else "no response"
    say(f"[converter] {kind} failed. by lease id {lid}: {err2} | by ticker: {err1}")
    jlog({"type": "convert_failed", "tick": ST.now_tick, "kind": kind, "err_ticker": err1, "lease_id": lid, "err_id": err2})
    return False


def lease_converters(tick):
    """Lease both converters early in the heat: a lease made in the same tick as its first use was refused
    ('Invalid lease id 1' at t=144 in practice, fine from t=148 on). Leases are free, one per trader each."""
    if not AUTO_CONVERT or ST.leased or tick < 1:
        return
    ids = {k: _lease_id(k) for k in CONVERTER_TICKERS}
    ST.leased = all(v is not None for v in ids.values())
    say(f"[converter] leases: {ids}")


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
    r = pos.get("RITC", 0) + ST.ritc_plan
    if (not ARB_BIG and tick - ST.last_tender_tick >= ROOM_CONVERT_AFTER and tick > ST.fast_until
            and ticks_total - tick > ROOM_CONVERT_MIN_LEFT):
        # the market didn't shrink the tender's pairs: convert down toward ROOM_CONVERT_ABOVE so the next one fits
        need = max(need, math.ceil(max(0.0, abs(r) - ROOM_CONVERT_ABOVE) / CONVERT_BLOCK))
    p_need = pending_need(snap)
    if p_need >= CONVERT_BLOCK / 2:
        # help a pending tender fit, but only when the market can't unwind pairs at an acceptable cost right now:
        # a conversion locks the ETF tickers for 2-3 ticks, and the market does 10k in one loop
        side = -1 if r > 0 else 1
        n_mkt, _ = pair_size(snap, side, -ST.pending["max_cost"], min(ARB_CLIP, p_need))
        if n_mkt < 1000:
            need = max(need, math.ceil(p_need / CONVERT_BLOCK))
    if need == 0:
        return
    # Hedge first: a conversion locks the ETF tickers for ~3 ticks. While the hedge can still trade inside the
    # limits, let it (practice heat: 3 back-to-back conversions left an 85k tender ~60k unhedged for 10 ticks: -55k).
    pe = effective_pos(snap)
    tgt = -(pe.get("RITC", 0) + ST.ritc_plan)
    gap = max(abs(tgt - pe.get(l, 0)) for l in ("BULL", "BEAR"))
    gross_now = abs(pe.get("BULL", 0)) + abs(pe.get("BEAR", 0)) + 2 * abs(pe.get("RITC", 0))
    leg_room = max(0.0, (HEDGE_USAGE_MAX * gl - gross_now) / 2)
    if gap >= HEDGE_MIN and leg_room >= min(gap, CONVERT_BLOCK):
        return
    sgn = 1 if r > 0 else -1
    hedged = min(abs(r), max(0.0, -sgn * pos.get("BULL", 0)), max(0.0, -sgn * pos.get("BEAR", 0)))
    if hedged < CONVERT_BLOCK:
        return                                       # hedge the next block first
    kind = "redeem" if r > 0 else "create"
    ST.conv_prompts += 1
    if ST.auto_convert:
        if _use_converter(kind):
            # while a hedge gap is open, leave a couple of ticks between conversions for the hedge to trade
            # (a conversion locks the ETF tickers; back to back they starved the hedge for ~15 ticks on the mock)
            gap_open = any(abs(pos.get(l, 0) + r) >= HEDGE_MIN for l in ("BULL", "BEAR"))
            ST.conv_busy_until = tick + CONVERT_TICKS + (2 if gap_open else 0)
            ST.conv_hold_until = tick + CONVERT_TICKS + 1
            event(tick, "CONVERT", f"{kind.upper()} 1 x 10,000 (#{ST.conv_prompts}, fee ${CONVERT_FEE_USD:,} USD)"
                                   f"  [{need} block{'s' if need > 1 else ''} needed]")
            return
        ST.convert_failures += 1
        if ST.convert_failures >= 3:
            ST.auto_convert = False
            banner("CONVERTER API FAILED 3 TIMES: switching to prompts for this heat.\n"
                   "Click ETF-Redemption / ETF-Creation in the RIT Client Assets tab when asked.")
        else:
            say(f"[converter] API attempt {ST.convert_failures}/3 failed; click this one, will retry the API next time")
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
            ST.leases, ST.conv_hold_until, ST.auto_convert, ST.convert_failures = {}, -1, AUTO_CONVERT, 0
            ST.shadow_share, ST.shadow_obs, ST.basket_hist, ST.fill_costs = SHADOW_START, [], {}, []
            ST.fast_until, ST.last_tender_tick = -1, -100
            ST.leased = False
            ST.pending = None
            ST.arb_real, ST.arb_extra = [], 0.0
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
        took = check_pending(snap, tick) or handle_tenders(snap, tick)
        if took:
            snap = snapshot_at(tick, ticks) or snap
        ST.last_quotes = snap["q"]
        if quotes_ok(snap["q"]) and tick not in ST.basket_hist:
            ST.basket_hist[tick] = sum(snap["q"]["BULL"]) / 2 + sum(snap["q"]["BEAR"]) / 2
            shadow_update(tick)
        ritc_route(snap, tick)
        lease_converters(tick)
        converters(snap, tick)
        first_of_tick = tick != ST.acted_tick      # slow decisions (resting orders, FX) once per tick
        ST.acted_tick = tick
        hedge(snap, tick, passive=first_of_tick or took)   # a tender just taken: start its hedge now
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
                qq = snap["q"]
                fair = float("nan")
                if quotes_ok(qq) and nlv == nlv:
                    usd_m = sum(qq["USD"]) / 2
                    prem = sum(qq["RITC"]) / 2 - (sum(qq["BULL"]) / 2 + sum(qq["BEAR"]) / 2) / usd_m
                    fair = nlv - r_ * prem * usd_m     # RITC marked at the basket's value instead of its own price
                hedged = abs(p.get("BULL", 0) + r_) < 200 and abs(p.get("BEAR", 0) + r_) < 200
                event(tick, "status", f"RITC {r_:+7,.0f}  BULL {p.get('BULL', 0):+7,.0f}  BEAR {p.get('BEAR', 0):+7,.0f}  "
                                      f"{'ok' if hedged else 'HEDGING'}  limits {u:3.0%}  P&L {nlv:+8,.0f}  fair {fair:+8,.0f}")
        took = time.time() - loop_start
        if took > 1.0 and tick - ST.last_slow_warn >= 10:
            ST.last_slow_warn = tick
            event(tick, "SLOW !!", f"one loop took {took:.1f}s (should be <0.5s). API is slow: check the connection")
        time.sleep(LOOP_SLEEP)
    logf.close()
    say("Stopped.")


if __name__ == "__main__":
    main()
