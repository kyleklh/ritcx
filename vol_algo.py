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

import bs

# =========================== SETTINGS (tune between heats) ===========================
API = os.environ.get("RIT_API", "http://localhost:9999/v1")
API_KEY = "Rotman"

STRIKES = [48, 49, 50, 51, 52]
RISK_FREE = 0.0

OPT_FEE_PER_SHARE = 0.02      # $2 per contract / 100 shares
MIN_EDGE = 0.02               # extra $/share of mispricing required beyond fees (safety margin)
CLIP = 100                    # contracts per order (max 100)
MAX_ORDERS_PER_LOOP = 4       # how many option orders to fire per loop (fast entry after news)
MAX_NET_CONTRACTS = 950       # case limit 1000 net; keep a buffer
MAX_GROSS_CONTRACTS = 2400    # case limit 2500 gross

HEDGE_TRIGGER = 2000          # re-hedge when |delta| > this (case limit 7000)
GAMMA_Z = 3.0                 # size the book so a GAMMA_Z-sigma one-tick move can't push delta past the limit.
                              # Higher = safer/smaller. Gamma grows near expiry, so this shrinks size automatically.
HEDGE_TARGET = 0
RTM_MAX_ORDER = 10000
RTM_LIMIT = 49000             # case limit 50000

OPTION_DELTA_CAP = 25000      # max |delta| of the OPTIONS book alone. RTM limit (50k) must be able to hedge it,
                              # so this forces balanced books (e.g. short calls AND puts = straddle), not one-sided.
OPTION_DELTA_REBAL = 35000    # if price moves push |option delta| past this, cut the positions driving it
                              # (the normal logic then re-adds risk in delta-reducing strikes)
STOP_OPENING_TICKS_BEFORE_END = 25   # no new option risk this close to expiry
CLOSE_TICKS_BEFORE_END = 15   # close all options in the last N ticks (gamma explodes near expiry)
LOOP_SLEEP = float(os.environ.get("LOOP_SLEEP", 0.2))             # seconds between loops
LOG_FILE = f"vol_log_{datetime.now():%Y%m%d_%H%M%S}.csv"
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
            print(f"[warn] GET {path} failed: {e}")
            time.sleep(0.2)
    return None


def place(ticker, action, qty):
    qty = int(qty)
    if qty <= 0:
        return 0
    for _ in range(5):
        try:
            r = s.post(f"{API}/orders", params={"ticker": ticker, "type": "MARKET",
                                                "quantity": qty, "action": action}, timeout=2)
            if r.status_code == 429:
                time.sleep(float(r.json().get("wait", 0.1)))
                continue
            if not r.ok:
                print(f"[order rejected] {action} {qty} {ticker}: {r.text[:120]}")
                return 0
            return int(r.json().get("quantity_filled", qty))
        except requests.RequestException as e:
            print(f"[warn] order failed: {e}")
            time.sleep(0.2)
    return 0


# ------------------------------- news & vol -------------------------------
NUM = r"(\d+(?:\.\d+)?)"
RE_RANGE = re.compile(NUM + r"\s*%?\s*(?:-|–|~|to|and)\s*" + NUM + r"\s*%", re.I)
RE_POINT = re.compile(r"will be\s*" + NUM + r"\s*%", re.I)


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
        text = f"{n.get('headline', '')} {n.get('body', '')}"
        w = week_of(max(1, n.get("tick", 1)))
        low = text.lower()
        m_range = RE_RANGE.search(text)
        m_point = RE_POINT.search(text)
        if "next week" in low and m_range:
            lo, hi = float(m_range.group(1)), float(m_range.group(2))
            ST.vol_est[w + 1] = (lo + hi) / 200
            print(f"\n[NEWS t={n.get('tick')}] next week (wk {w + 1}) range {lo}-{hi}% -> est {ST.vol_est[w + 1]:.3f}")
        elif m_point and "volatil" in low:
            target = w + 1 if "next week" in low else w
            ST.vol_known[target] = float(m_point.group(1)) / 100
            print(f"\n[NEWS t={n.get('tick')}] week {target} vol = {ST.vol_known[target]:.3f}")
        else:
            print(f"\n[NEWS t={n.get('tick')}] (not parsed) {text[:140]}")


def vol_for_week(w):
    if w in ST.vol_known:
        return ST.vol_known[w]
    if w in ST.vol_est:
        return ST.vol_est[w]
    for prev in range(w - 1, 0, -1):          # fall back to most recent known vol
        if prev in ST.vol_known:
            return ST.vol_known[prev]
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


def option_info(sec, S, T, fv):
    """Per-option fair price, edges, delta."""
    out = {}
    for k in STRIKES:
        for flag, tk in (("c", CALLS[k]), ("p", PUTS[k])):
            x = sec[tk]
            bid, ask = float(x["bid"]), float(x["ask"])
            mid = (bid + ask) / 2
            iv = bs.implied_vol(flag, mid, S, k, T, RISK_FREE)
            fair = bs.price(flag, S, k, T, fv, RISK_FREE)
            d_vol = iv if iv else fv
            out[tk] = {
                "K": k, "flag": flag, "bid": bid, "ask": ask, "iv": iv, "fair": fair,
                "buy_edge": fair - ask - OPT_FEE_PER_SHARE,
                "sell_edge": bid - fair - OPT_FEE_PER_SHARE,
                "delta": bs.delta(flag, S, k, T, d_vol, RISK_FREE),
                "gamma": bs.gamma(S, k, T, d_vol, RISK_FREE),
                "pos": int(x["position"]),
            }
    return out


def portfolio_delta(sec, info):
    d = int(sec["RTM"]["position"])
    for tk, o in info.items():
        d += o["pos"] * 100 * o["delta"]
    return d


def gamma_cap(S, fv):
    """Max |book gamma| (shares per $1) so a GAMMA_Z-sigma tick move stays inside the delta limit."""
    move = GAMMA_Z * fv / math.sqrt(ST.tpy) * S
    return (7000 - HEDGE_TRIGGER) / move


def quick_hedge(tick, d):
    """Hedge immediately inside an option-trading burst so delta never sits outside the band."""
    need = int(round(HEDGE_TARGET - d))
    need = max(-RTM_LIMIT - ST.rtm_pos, min(RTM_LIMIT - ST.rtm_pos, need))
    while abs(need) >= 1:
        q = min(RTM_MAX_ORDER, abs(need))
        side = "BUY" if need > 0 else "SELL"
        filled = place("RTM", side, q)
        if filled <= 0:
            break
        sg = 1 if side == "BUY" else -1
        ST.rtm_pos += sg * filled
        d += sg * filled
        need -= sg * filled
    return d


def trade_options(info, tick, gcap):
    if ST.paused or tick >= ST.ticks - STOP_OPENING_TICKS_BEFORE_END:
        return False
    net = sum(o["pos"] for o in info.values())
    gross = sum(abs(o["pos"]) for o in info.values())
    # candidate trades ranked by $ edge per contract
    cands = []
    for tk, o in info.items():
        if o["buy_edge"] > MIN_EDGE:
            cands.append((o["buy_edge"], tk, "BUY"))
        if o["sell_edge"] > MIN_EDGE:
            cands.append((o["sell_edge"], tk, "SELL"))
    cands.sort(reverse=True)
    traded = False
    n_orders = 0
    opt_delta = sum(o["pos"] * 100 * o["delta"] for o in info.values())
    book_g = sum(o["pos"] * 100 * o["gamma"] for o in info.values())
    for edge, tk, side in cands:
        while n_orders < MAX_ORDERS_PER_LOOP:
            pos = info[tk]["pos"]
            if side == "BUY":
                room_net = MAX_NET_CONTRACTS - net
                room_gross = MAX_GROSS_CONTRACTS - gross if pos >= 0 else max(-pos, MAX_GROSS_CONTRACTS - gross)
            else:
                room_net = MAX_NET_CONTRACTS + net
                room_gross = MAX_GROSS_CONTRACTS - gross if pos <= 0 else max(pos, MAX_GROSS_CONTRACTS - gross)
            q = min(CLIP, room_net, room_gross)
            # keep the options book hedgeable: skip trades that push |option delta| past the cap
            d_per = (1 if side == "BUY" else -1) * 100 * info[tk]["delta"]
            if d_per and abs(opt_delta + d_per * q) > OPTION_DELTA_CAP and abs(opt_delta + d_per * q) > abs(opt_delta):
                q = min(q, int(max(0, (OPTION_DELTA_CAP - abs(opt_delta)) / abs(d_per))))
            g_per = (1 if side == "BUY" else -1) * 100 * info[tk]["gamma"]
            if g_per and abs(book_g + g_per * q) > gcap and abs(book_g + g_per * q) > abs(book_g):
                q = min(q, int(max(0, (gcap - abs(book_g)) / abs(g_per))))
            if q <= 0:
                break
            filled = place(tk, side, q)
            n_orders += 1
            if filled <= 0:
                break
            sgn = 1 if side == "BUY" else -1
            old = info[tk]["pos"]
            info[tk]["pos"] = old + sgn * filled
            net += sgn * filled
            opt_delta += sgn * filled * 100 * info[tk]["delta"]
            book_g += sgn * filled * 100 * info[tk]["gamma"]
            gross += abs(old + sgn * filled) - abs(old)
            traded = True
            ST.run_delta += sgn * filled * 100 * info[tk]["delta"]
            if abs(ST.run_delta) > HEDGE_TRIGGER:
                ST.run_delta = quick_hedge(tick, ST.run_delta)
            print(f"  t={tick} {side} {filled} {tk} edge ${edge * 100:.0f}/contract  (net {net}, gross {gross})")
        if n_orders >= MAX_ORDERS_PER_LOOP:
            break
    return traded


def trim_gamma(info, tick, gcap):
    """As expiry nears, gamma per contract rises. Cut the highest-gamma legs when the book exceeds the cap."""
    book_g = sum(o["pos"] * 100 * o["gamma"] for o in info.values())
    if abs(book_g) <= gcap * 1.1:
        return False
    sg = 1 if book_g > 0 else -1
    traded = False
    n = 0
    for tk, o in sorted(info.items(), key=lambda kv: -sg * kv[1]["pos"] * kv[1]["gamma"]):
        if n >= MAX_ORDERS_PER_LOOP or abs(book_g) <= gcap:
            break
        if sg * o["pos"] * o["gamma"] <= 0:
            continue
        excess = abs(book_g) - gcap
        q = min(CLIP, abs(o["pos"]), max(1, int(math.ceil(excess / (100 * o["gamma"])))))
        side = "SELL" if o["pos"] > 0 else "BUY"
        filled = place(tk, side, q)
        n += 1
        if filled <= 0:
            continue
        chg = -filled if side == "SELL" else filled
        o["pos"] += chg
        book_g += chg * 100 * o["gamma"]
        ST.run_delta += chg * 100 * o["delta"]
        if abs(ST.run_delta) > HEDGE_TRIGGER:
            ST.run_delta = quick_hedge(tick, ST.run_delta)
        traded = True
        print(f"  t={tick} TRIM GAMMA: {side} {filled} {tk} (book gamma {book_g:,.0f}, cap {gcap:,.0f})")
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
        filled = place(tk, side, q)
        if filled > 0:
            o["pos"] += -filled if side == "SELL" else filled
            ST.run_delta += (-filled if side == "SELL" else filled) * 100 * o["delta"]
            ST.run_delta = quick_hedge(tick, ST.run_delta)
            traded = True
            print(f"  t={tick} REBALANCE option delta {opt_delta:,.0f}: {side} {filled} {tk}")
    return traded


def close_options(info, tick):
    """Unwind options before expiry, a few orders per loop. Returns True if anything traded."""
    traded = False
    n = 0
    for tk, o in sorted(info.items(), key=lambda kv: -abs(kv[1]["pos"])):
        while o["pos"] != 0 and n < MAX_ORDERS_PER_LOOP:
            q = min(CLIP, abs(o["pos"]))
            side = "SELL" if o["pos"] > 0 else "BUY"
            filled = place(tk, side, q)
            n += 1
            if filled <= 0:
                break
            o["pos"] += -filled if side == "SELL" else filled
            ST.run_delta += (-filled if side == "SELL" else filled) * 100 * o["delta"]
            if abs(ST.run_delta) > HEDGE_TRIGGER:
                ST.run_delta = quick_hedge(tick, ST.run_delta)
            traded = True
        if n >= MAX_ORDERS_PER_LOOP:
            break
    if traded:
        print(f"  t={tick} CLOSING options before expiry")
    return traded


def hedge(sec, info, tick, force=False):
    d = portfolio_delta(sec, info)
    if not force and abs(d) <= HEDGE_TRIGGER:
        return d
    need = int(round(HEDGE_TARGET - d))
    rtm = int(sec["RTM"]["position"])
    need = max(-RTM_LIMIT - rtm, min(RTM_LIMIT - rtm, need))
    did = False
    while abs(need) >= 1:
        q = min(RTM_MAX_ORDER, abs(need))
        side = "BUY" if need > 0 else "SELL"
        filled = place("RTM", side, q)
        if filled <= 0:
            break
        need -= filled if side == "BUY" else -filled
        d += filled if side == "BUY" else -filled
        did = True
    if did:
        print(f"  t={tick} HEDGE -> delta {d:,.0f}")
    elif abs(d) > 7000:
        print(f"  t={tick} !! CANNOT HEDGE (RTM limit), delta {d:,.0f}")
    return d


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
                print(f"[override] week {w} vol = {ST.vol_known[w]:.3f}")
            elif c == "n" and len(parts) >= 3:
                ST.vol_est[w + 1] = (float(parts[1]) + float(parts[2])) / 200
                print(f"[override] week {w + 1} est = {ST.vol_est[w + 1]:.3f}")
            elif c == "p":
                ST.paused = True
                print("[paused] no new option trades; hedging continues")
            elif c == "r":
                ST.paused = False
                print("[resumed]")
            elif c == "h":
                ST.force_hedge = True
            elif c == "s":
                print(f"[status] known={ST.vol_known} est={ST.vol_est} paused={ST.paused}")
            elif c == "q":
                ST.quit = True
                return
        except Exception as e:
            print(f"[console error] {e}")


# ------------------------------- main loop -------------------------------
def main():
    threading.Thread(target=console, daemon=True).start()
    print("Waiting for case to be ACTIVE...  (type v/n/p/r/h/s/q + Enter for commands)")
    logf = open(LOG_FILE, "w", newline="")
    log = csv.writer(logf)
    log.writerow(["tick", "S", "fair_vol", "atm_iv", "net_contracts", "delta", "nlv"])
    last_print = -1
    while not ST.quit:
        case = api_get("/case")
        if not case or case["status"] != "ACTIVE":
            time.sleep(0.5)
            continue
        tick = case["tick"]
        if tick < ST.last_tick:                       # new heat started: reset memory
            print("\n=== new heat detected, resetting state ===")
            ST.reset()
        ST.last_tick = tick
        ST.ticks = case.get("ticks_per_period", 300)
        ST.week_len = ST.ticks // 4
        ST.tpy = ST.ticks * 12
        if tick < 1 or tick >= ST.ticks:
            time.sleep(LOOP_SLEEP)
            continue

        parse_news()
        sec = snapshot()
        if not sec:
            continue
        S = (float(sec["RTM"]["bid"]) + float(sec["RTM"]["ask"])) / 2
        T = (ST.ticks - tick) / ST.tpy
        fv = fair_vol(tick)
        if fv is None:
            time.sleep(LOOP_SLEEP)
            continue

        info = option_info(sec, S, T, fv)
        ST.rtm_pos = int(sec["RTM"]["position"])
        ST.run_delta = portfolio_delta(sec, info)
        if tick >= ST.ticks - CLOSE_TICKS_BEFORE_END:
            traded = close_options(info, tick)
        else:
            gcap = gamma_cap(S, fv)
            traded = trim_gamma(info, tick, gcap)
            traded = rebalance_option_delta(info, tick) or traded
            traded = trade_options(info, tick, gcap) or traded
        if traded:
            sec = snapshot() or sec
            info = option_info(sec, S, T, fv)
        d = hedge(sec, info, tick, force=traded or ST.force_hedge)
        ST.force_hedge = False

        if tick != last_print:
            last_print = tick
            atm = min(STRIKES, key=lambda k: abs(k - S))
            atm_iv = info[CALLS[atm]]["iv"]
            net = sum(o["pos"] for o in info.values())
            trader = api_get("/trader") or {}
            nlv = trader.get("nlv", float("nan"))
            log.writerow([tick, round(S, 2), round(fv, 4), round(atm_iv or 0, 4), net, round(d), nlv])
            logf.flush()
            print(f"t={tick:3d} S={S:6.2f} fair={fv:.3f} atmIV={(atm_iv or 0):.3f} "
                  f"net={net:5d} delta={d:8,.0f} nlv={nlv:12,.2f}", end="\r")
        time.sleep(LOOP_SLEEP)
    logf.close()
    print("\nStopped.")


if __name__ == "__main__":
    main()
