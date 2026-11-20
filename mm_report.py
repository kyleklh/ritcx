"""Market-making report: did the RITC market making (MM) actually make money in a heat, after hedging?

    python mm_report.py                         # every run in logs/
    python mm_report.py logs/etf_20261121_*     # some runs (prefix, .txt or .jsonl all work)
    python mm_report.py --rebate 0.01           # if the limit rebate turns out to be 0.01, not 0.03
    python mm_report.py --csv mm_fills.csv      # also write one row per MM fill

For every MM fill (the "MM | filled ..." lines in etf_<time>.txt) it finds the BULL/BEAR hedge that followed in
etf_<time>.jsonl and prices it, all in CAD per RITC share:

    net = edge at fill + rebate on the RITC fill - hedge cost

  edge at fill  basket mid (BULL+BEAR) at the fill tick minus the RITC fill price x USD (sign: + = we got a good price)
  hedge cost    for each leg: (mid at the fill tick - hedge price) + fee, signed so + = cost. Measured from the mid
                at the FILL tick, so it includes any move while the hedge waited, not only the spread we crossed.
                Market hedges use the vwap the server returned. Passive hedge shares (MM_PASSIVE=1, or any position
                change no logged market order explains) use our own resting order's price from the logged book,
                minus the rebate. If our order isn't in the book (mock), the touch price is used.
  unhedged      shares still unhedged MAX_WAIT ticks after the fill are charged as if sold at the mid then + fee.

Hedge shares are matched to fills first-in first-out. A fill is marked "mixed" when something else traded the same
legs while its hedge was open (a tender, an arb pair, a conversion, the end-of-heat flatten); its numbers are then
less reliable, so the summary shows "clean" fills on their own as well. FX costs are ignored (tiny).

Decision rule printed at the end (on clean fills, all heats in the files given):
  net >= +0.03/sh  KEEP MM_ON=1        -0.02 .. +0.03  MARGINAL (keep only if the sample is small)
  net <  -0.02/sh  TURN OFF (MM_ON=0)  fewer than 20,000 shares: not enough fills to judge
No packages needed beyond the standard library.
"""
import argparse
import csv
import glob
import json
import os
import re
import sys

FEE = 0.02
MAX_WAIT = 15            # ticks after a fill before its unhedged rest is charged at mid + fee
LEGS = ("BULL", "BEAR")
HEAT_DROP = 50           # a tick this much lower than the last one = a new heat
FLATTEN_FROM = 280       # the algo flattens everything in the last 20 ticks of a 300-tick heat

MM_RE = re.compile(r"t=\s*(\d+) \| MM\s*\| filled (buy|sell) ([\d,]+) RITC @ ([\d.]+) \(([+-][\d.]+) USD vs fair\)")
EV_RE = re.compile(r"t=\s*(\d+) \| (TENDER|CONVERT|ARB|RITC)\s*\| (.*)")
ID_RE = re.compile(r"\[trader id\] (\S+)")


def runs_from_args(paths):
    if not paths:
        paths = ["logs"]
    out = []
    for p in paths:
        if os.path.isdir(p):
            out += sorted(glob.glob(os.path.join(p, "etf_*.jsonl")))
            continue
        for m in sorted(glob.glob(p + "*")) or [p]:
            base = m
            for ext in (".jsonl", ".txt", ".csv"):
                if base.endswith(ext):
                    base = base[:-len(ext)]
            if os.path.exists(base + ".jsonl") and base + ".jsonl" not in out:
                out.append(base + ".jsonl")
    return out


def split_heats(items, tick_of):
    """Split a chronological list into heats wherever the tick drops sharply."""
    heats, cur, last = [], [], None
    for x in items:
        t = tick_of(x)
        if last is not None and t < last - HEAT_DROP:
            heats.append(cur)
            cur = []
        cur.append(x)
        last = t
    if cur:
        heats.append(cur)
    return heats


def load_txt(path):
    fills, events, my_id = [], [], None
    lines = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = ID_RE.search(line)
            if m:
                my_id = m.group(1)
            m = MM_RE.search(line)
            if m:
                lines.append(("fill", int(m.group(1)), {"side": m.group(2), "qty": int(m.group(3).replace(",", "")),
                                                        "price": float(m.group(4)), "edge_log": float(m.group(5))}))
                continue
            m = EV_RE.search(line)
            if m:
                tag, msg = m.group(2), m.group(3)
                if tag == "TENDER" and not msg.startswith(("ACCEPT", "BID")):
                    continue
                if tag == "ARB" and "self-check" in msg:
                    continue
                lines.append(("event", int(m.group(1)), tag))
    heats = split_heats(lines, lambda x: x[1])
    out = []
    for h in heats:
        out.append({"fills": [dict(tick=t, **d) for k, t, d in h if k == "fill"],
                    "events": [(t, tag) for k, t, tag in h if k == "event"]})
    return out, my_id


def load_jsonl(path):
    recs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("type") in ("tick", "order", "convert") and "tick" in r:
                recs.append(r)
    return split_heats(recs, lambda r: r["tick"])


def mid(q, t):
    b, a = q.get(t, (0, 0))
    return (b + a) / 2 if b and a else None


def our_price(book, side, my_id):
    """Price of our own resting order on this side of a logged book level list [[price, qty, trader_id], ...]."""
    for lvl in book.get(side, []):
        if len(lvl) >= 3 and my_id and lvl[2] == my_id:
            return float(lvl[0])
    return None


def analyse_heat(jrecs, txt, my_id, rebate):
    ticks = {r["tick"]: r for r in jrecs if r["type"] == "tick"}
    if not ticks:
        return []
    last_tick = max(ticks)
    flat_from = max(FLATTEN_FROM, last_tick - 20) if last_tick >= 250 else 10 ** 9
    busy_ticks = set()                                   # ticks where something else traded the legs
    for t, tag in txt["events"]:
        busy_ticks.update(range(t, t + (8 if tag == "TENDER" else 3)))
    for r in jrecs:
        if r["type"] == "convert":
            busy_ticks.update(range(r["tick"], r["tick"] + 3))
    ritc_mkt_t = [r["t"] for r in jrecs if r["type"] == "order" and r.get("ticker") == "RITC"]

    # One item per MM fill, priced against the quotes of the tick where the algo logged it (as the algo does).
    res = []
    for i, f in enumerate(txt["fills"]):
        rt = ticks.get(f["tick"])
        q = (rt or {}).get("quotes") or {}
        ref = {leg: mid(q, leg) for leg in LEGS}
        if rt is None or None in ref.values():
            continue
        u = mid(q, "USD") or 1.0
        sgn = 1 if f["side"] == "buy" else -1        # bought RITC -> sell both legs
        item = {"heat_fill": i, "tick": f["tick"], "side": f["side"], "qty": f["qty"], "price": f["price"],
                "sgn": sgn, "usd": u, "edge": sgn * (ref["BULL"] + ref["BEAR"] - f["price"] * u),
                "edge_log": f["edge_log"], "ref_mid": ref, "hedge_cost": 0.0, "hedged_market": 0.0,
                "hedged_passive": 0.0, "unhedged": 0.0, "markout": {},
                "mixed": f["tick"] in busy_ticks or f["tick"] >= flat_from}
        for k in (5, 20):
            qk = (ticks.get(f["tick"] + k) or {}).get("quotes") or {}
            if mid(qk, "BULL") and mid(qk, "BEAR"):
                item["markout"][k] = sgn * (mid(qk, "BULL") + mid(qk, "BEAR") - f["price"] * (mid(qk, "USD") or u))
        res.append(item)
    res.sort(key=lambda x: x["tick"])
    open_needs = {leg: [] for leg in LEGS}   # FIFO of [fill_index, direction, remaining]
    nxt = 0                                  # next fill whose hedge window has not opened yet

    def allocate(leg, direction, qty, price_fn, how, tick):
        for need in open_needs[leg]:
            if qty <= 0:
                break
            i, d, rem = need
            if d != direction or rem <= 0:
                continue
            take = min(rem, qty)
            r_ = res[i]
            ref = r_["ref_mid"][leg]
            px, reb = price_fn()
            cost = ((ref - px) if direction < 0 else (px - ref))   # selling below / buying above the reference mid
            cost += FEE if how == "market" else -reb
            r_["hedge_cost"] += take * cost
            r_["hedged_" + how] += take
            if tick in busy_ticks or tick >= flat_from:
                r_["mixed"] = True
            need[2] -= take
            qty -= take
        for leg_ in LEGS:
            open_needs[leg_] = [n for n in open_needs[leg_] if n[2] > 0]

    # A tick record's position is the snapshot taken BEFORE that tick's orders, so the change between two tick
    # records is explained by the market orders logged with the earlier tick (by their tick field, not file order).
    mkt_by_tick = {}
    for r in jrecs:
        resp = r.get("resp") or {}
        if r["type"] == "order" and r.get("ticker") in LEGS and resp.get("status") == "TRANSACTED":
            n = float(resp.get("quantity_filled") or 0) * (1 if r["action"] == "BUY" else -1)
            d = mkt_by_tick.setdefault(r["tick"], {leg: 0.0 for leg in LEGS})
            d[r["ticker"]] += n
    known_id = my_id not in (None, "self", "mock")
    prev = None
    for r in jrecs:
        # The algo hedges as soon as the RITC position moves, which can be up to a tick BEFORE its "MM | filled"
        # line (mm() checks its orders once per tick). So a fill's hedge window opens one tick before that line.
        while nxt < len(res) and res[nxt]["tick"] - 1 <= r["tick"]:
            for leg in LEGS:
                open_needs[leg].append([nxt, -res[nxt]["sgn"], res[nxt]["qty"]])
            nxt += 1
        if r["type"] == "order":
            leg = r.get("ticker")
            resp = r.get("resp") or {}
            if leg not in LEGS or resp.get("status") != "TRANSACTED" or not resp.get("vwap"):
                continue
            n = float(resp.get("quantity_filled") or 0)
            if n <= 0:
                continue
            direction = 1 if r["action"] == "BUY" else -1
            if any(abs(r["t"] - t) < 0.25 for t in ritc_mkt_t):
                continue                                  # a leg of an arb pair / flatten, not an MM hedge
            vw = float(resp["vwap"])
            allocate(leg, direction, n, lambda vw=vw: (vw, 0.0), "market", r["tick"])
            continue
        if r["type"] != "tick":
            continue
        tick, q, pos = r["tick"], r.get("quotes") or {}, r.get("pos") or {}
        # position changes since the last tick record that no market order explains = passive fills
        if prev is not None:
            for leg in LEGS:
                mk = sum(mkt_by_tick.get(k, {}).get(leg, 0.0) for k in range(prev["tick"], tick))
                d = (pos.get(leg, 0) or 0) - (prev["pos"].get(leg, 0) or 0) - mk
                if abs(d) >= 100:
                    direction = 1 if d > 0 else -1
                    bk = (prev.get("books") or {}).get(leg, {})
                    side = "bids" if direction > 0 else "asks"
                    p0 = our_price(bk, side, my_id)
                    if p0 is None and known_id:
                        continue      # no resting order of ours on that side: a late position update, not a fill
                    if p0 is None:
                        pq = (prev.get("quotes") or {}).get(leg, (0, 0))
                        p0 = pq[0] if direction > 0 else pq[1]
                    allocate(leg, direction, abs(d), lambda p0=p0: (p0, rebate), "passive", prev["tick"])
        # needs past their deadline: charge the unhedged rest at the mid now + fee
        for leg in LEGS:
            for need in open_needs[leg]:
                i, d, rem = need
                if rem > 0 and tick - res[i]["tick"] > MAX_WAIT:
                    m = mid(q, leg)
                    ref = res[i]["ref_mid"][leg]
                    if m is not None:
                        res[i]["hedge_cost"] += rem * (((ref - m) if d < 0 else (m - ref)) + FEE)
                    res[i]["unhedged"] += rem
                    need[2] = 0
            open_needs[leg] = [n for n in open_needs[leg] if n[2] > 0]
        prev = r
    # whatever is still open at the end of the heat: charge at the last mid + fee
    lastq = ticks[last_tick].get("quotes") or {}
    for leg in LEGS:
        for i, d, rem in open_needs[leg]:
            m, ref = mid(lastq, leg), res[i]["ref_mid"][leg]
            if m is not None and rem > 0:
                res[i]["hedge_cost"] += rem * (((ref - m) if d < 0 else (m - ref)) + FEE)
                res[i]["unhedged"] += rem
    for x in res:
        x["rebate"] = rebate
        x["net"] = x["qty"] * (x["edge"] + rebate) - x["hedge_cost"]
    return res


def summarise(rows, label):
    n = sum(r["qty"] for r in rows)
    if not n:
        print(f"  {label:<14} no fills")
        return None
    edge = sum(r["qty"] * r["edge"] for r in rows) / n
    reb = sum(r["qty"] * r["rebate"] for r in rows) / n
    hc = sum(r["hedge_cost"] for r in rows) / n
    net = sum(r["net"] for r in rows)
    mk = sum(r["hedged_market"] for r in rows)
    pa = sum(r["hedged_passive"] for r in rows)
    un = sum(r["unhedged"] for r in rows)
    m5 = [r for r in rows if 5 in r["markout"]]
    mo5 = sum(r["qty"] * r["markout"][5] for r in m5) / max(1, sum(r["qty"] for r in m5)) if m5 else float("nan")
    print(f"  {label:<14} {len(rows):3d} fills {n:8,.0f} sh | edge {edge:+.3f} + rebate {reb:.3f} - hedge {hc:.3f}"
          f" = net {net / n:+.3f}/sh  ${net:+,.0f} | edge 5 ticks later {mo5:+.3f}"
          f" | hedge legs: market {mk:,.0f} passive {pa:,.0f} unhedged {un:,.0f}")
    return net / n, n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="log prefixes/files/folders (default: logs/)")
    ap.add_argument("--rebate", type=float, default=0.03, help="limit order rebate CAD/share (default 0.03)")
    ap.add_argument("--csv", help="write one row per MM fill to this file")
    ap.add_argument("--fills", action="store_true", help="print every fill")
    a = ap.parse_args()
    runs = runs_from_args(a.paths)
    if not runs:
        sys.exit("no logs found (looked for etf_*.jsonl)")
    allrows = []
    for jpath in runs:
        tpath = jpath[:-len(".jsonl")] + ".txt"
        if not os.path.exists(tpath):
            print(f"{jpath}: no matching .txt, skipped")
            continue
        theats, my_id = load_txt(tpath)
        jheats = load_jsonl(jpath)
        if len(theats) != len(jheats):
            print(f"{os.path.basename(jpath)}: {len(theats)} heats in .txt vs {len(jheats)} in .jsonl; matching by order")
        for h, (jh, th) in enumerate(zip(jheats, theats), 1):
            rows = analyse_heat(jh, th, my_id, a.rebate)
            name = f"{os.path.basename(jpath)[:-6]} heat {h}"
            for r in rows:
                r["run"] = name
            allrows += rows
            print(f"\n{name}" + ("" if rows else ": no MM fills"))
            if not rows:
                continue
            if a.fills:
                for r in rows:
                    print(f"    t={r['tick']:3d} {r['side']:<4} {r['qty']:7,.0f} @ {r['price']:.2f}  edge {r['edge']:+.3f}"
                          f" (log {r['edge_log']:+.3f})  hedge {r['hedge_cost'] / r['qty']:.3f}/sh  net ${r['net']:+,.0f}"
                          + ("  mixed" if r["mixed"] else "") + (f"  unhedged {r['unhedged']:,.0f}" if r["unhedged"] else ""))
            summarise(rows, "all fills")
            summarise([r for r in rows if not r["mixed"]], "clean fills")
    if not allrows:
        print("\nNo MM fills in these logs.")
        return
    print("\n=== ALL HEATS ===")
    summarise(allrows, "all fills")
    s = summarise([r for r in allrows if not r["mixed"]], "clean fills")
    if s is None:
        s = summarise(allrows, "(using all)")
    net, n = s
    if n < 20000:
        verdict = f"NOT ENOUGH DATA ({n:,.0f} clean shares, want 20,000+). Keep the current setting and collect more."
    elif net >= 0.03:
        verdict = f"KEEP MM_ON=1 (net {net:+.3f} CAD/sh on {n:,.0f} shares)"
    elif net >= -0.02:
        verdict = f"MARGINAL (net {net:+.3f} CAD/sh). Roughly break-even: fine to keep, not worth risk if heats go badly."
    else:
        verdict = f"TURN OFF: set MM_ON=0 in .env (net {net:+.3f} CAD/sh on {n:,.0f} shares)"
    print(f"\nVERDICT: {verdict}")
    if abs(a.rebate - 0.01) > 1e-9:
        print(f"(rebate assumed {a.rebate:.2f}; if the real rebate is 0.01, rerun with --rebate 0.01)")
    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["run", "tick", "side", "qty", "price", "usd", "edge", "edge_log", "rebate", "hedge_cost_per_sh",
                        "net", "hedged_market", "hedged_passive", "unhedged", "mixed", "markout5", "markout20"])
            for r in allrows:
                w.writerow([r["run"], r["tick"], r["side"], r["qty"], r["price"], round(r["usd"], 4), round(r["edge"], 4),
                            r["edge_log"], r["rebate"], round(r["hedge_cost"] / r["qty"], 4), round(r["net"], 2),
                            r["hedged_market"], r["hedged_passive"], r["unhedged"], r["mixed"],
                            r["markout"].get(5), r["markout"].get(20)])
        print(f"wrote {a.csv}")


if __name__ == "__main__":
    main()
