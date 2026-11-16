"""Analyse ETF algo logs (logs/etf_<time>.jsonl + .txt) to calibrate the strategy against real competition.

    python analyze_etf_logs.py                      # every run in logs/
    python analyze_etf_logs.py logs/etf_20261117_*.jsonl
    python analyze_etf_logs.py --csv out            # also write the tender event study to out_events.csv

It answers:
  1. Who else is in the books: distinct trader ids per tick, per ticker.
  2. How deep and wide the books are (top-of-book size, 10-level depth, spread).
  3. Does the crowd's price pressure STICK or BOUNCE BACK after a tender? For every tender, how BULL, BEAR, the
     basket and RITC move over the next 20 ticks, signed so that + means "moved against someone hedging it".
     A peak that fades by +20 ticks = bounce (wait / provide liquidity). A move that stays = sticks (hedge fast).
  4. What our market orders actually cost vs the mid at that tick, overall and right after tenders.
  5. What each accepted tender made in P&L over the next 30 ticks vs the edge the algo estimated.
No packages needed beyond the standard library.
"""
import argparse
import ast
import glob
import json
import os
import re
import statistics as st
import sys
from collections import Counter, defaultdict

OFFSETS = list(range(-5, 21))
TICKERS = ("BULL", "BEAR", "RITC")


def med(xs):
    xs = [x for x in xs if x is not None]
    return st.median(xs) if xs else float("nan")


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else float("nan")


def load(jsonl_path):
    ticks, orders = {}, []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("type") == "tick":
                ticks[r["tick"]] = r
            elif r.get("type") == "order":
                orders.append(r)
    txt_path = jsonl_path[:-len(".jsonl")] + ".txt"
    tenders = parse_tenders(txt_path) if os.path.exists(txt_path) else []
    return ticks, orders, tenders, txt_path


def parse_tenders(txt_path):
    """Tenders from the terminal log: the [tender raw] dict plus the decision line that follows it."""
    out, cur = [], None
    dec = re.compile(r"t=\s*(\d+) \| TENDER\s+\| (ACCEPT|decline|BID)\s.*?edge ([+-]?\d+\.\d+)/sh(.*)")
    with open(txt_path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("[tender raw]"):
                try:
                    cur = ast.literal_eval(line[len("[tender raw]"):].strip())
                except (ValueError, SyntaxError):
                    cur = None
                continue
            m = dec.search(line)
            if m and cur is not None:
                won = m.group(2) == "ACCEPT" or (m.group(2) == "BID" and "WON" in line)
                out.append({"tick": int(cur.get("tick", m.group(1))), "action": cur.get("action"),
                            "qty": float(cur.get("quantity", 0)), "price": cur.get("price"),
                            "accepted": won, "edge": float(m.group(3)), "why": m.group(4).strip()})
                cur = None
    return out


def mids(rec):
    q = rec["quotes"]
    m = {t: (q[t][0] + q[t][1]) / 2 for t in ("USD", "BULL", "BEAR", "RITC") if t in q and q[t][0] > 0 and q[t][1] > 0}
    if len(m) == 4:
        m["BASKET"] = m["BULL"] + m["BEAR"]
        m["RITC_CAD"] = m["RITC"] * m["USD"]
        m["GAP"] = m["RITC_CAD"] - m["BASKET"]          # + = RITC rich vs basket
    return m


def our_id(ticks):
    """Our trader id = the id that sits at the price of our own resting orders most often (best guess:
    the id present in the most ticks when we have open hedge gaps). Fallback: none."""
    c = Counter()
    for r in ticks.values():
        for b in r["books"].values():
            for side in ("bids", "asks"):
                for px, qty, tid in b[side]:
                    if tid:
                        c[tid] += 1
    return c


def section(title):
    print("\n" + title)
    print("-" * len(title))


def event_study(tenders, ticks, csv_prefix):
    rows, events = defaultdict(lambda: defaultdict(list)), []
    for td in tenders:
        t0 = td["tick"]
        if t0 not in ticks:
            continue
        m0 = mids(ticks[t0])
        if "BASKET" not in m0:
            continue
        sgn = 1 if td["action"] == "BUY" else -1       # tender BUY: we/they hold RITC long, hedge = sell basket
        for k in OFFSETS:
            r = ticks.get(t0 + k)
            if not r:
                continue
            m = mids(r)
            if "BASKET" not in m:
                continue
            adv = {"BASKET": -sgn * (m["BASKET"] - m0["BASKET"]),   # basket moving the hedgers' way = adverse
                   "BULL": -sgn * (m["BULL"] - m0["BULL"]),
                   "BEAR": -sgn * (m["BEAR"] - m0["BEAR"]),
                   "RITC": -sgn * (m["RITC_CAD"] - m0["RITC_CAD"]),  # those exiting via RITC push it the same way
                   "GAP": sgn * (m["GAP"] - m0["GAP"])}               # gap opening the arb way after the crowd
            for key, v in adv.items():
                rows[key][k].append(v)
            events.append({"tender_tick": t0, "action": td["action"], "qty": td["qty"], "accepted": td["accepted"],
                           "offset": k, **{f"adv_{key}": round(v, 4) for key, v in adv.items()}})
    if not rows:
        print("  No tenders with prices around them.")
    else:
        show = [0, 1, 2, 3, 5, 8, 10, 15, 20]
        print("  " + "series ".ljust(9) + "".join(f"{k:>8d}" for k in show) + "    peak  @tick  end/peak")
        for key in ("BASKET", "BULL", "BEAR", "RITC", "GAP"):
            line = [mean(rows[key][k]) for k in show]
            curve = [(k, mean(rows[key][k])) for k in range(0, 21) if rows[key][k]]
            pk, pv = max(curve, key=lambda x: x[1]) if curve else (0, float("nan"))
            end = mean(rows[key][20]) if rows[key][20] else float("nan")
            ratio = end / pv if pv and pv > 0 else float("nan")
            print("  " + key.ljust(9) + "".join(f"{v:8.3f}" for v in line) + f"  {pv:6.3f}  {pk:5d}  {ratio:8.2f}")
        bk = [(k, mean(rows["BASKET"][k])) for k in range(0, 21) if rows["BASKET"][k]]
        if bk:
            pk, pv = max(bk, key=lambda x: x[1])
            end = mean(rows["BASKET"][20]) if rows["BASKET"][20] else float("nan")
            if pv > 0.02 and end < 0.5 * pv:
                verdict = "BOUNCES: pressure fades. Waiting / taking the other side of the crowd should pay."
            elif pv > 0.02:
                verdict = "STICKS: pressure mostly stays. Hedge fast, right after accepting."
            else:
                verdict = "little pressure: the crowd isn't moving the basket much after tenders."
            print(f"  Verdict (basket): {verdict}")
        acc = [td for td in tenders if td["accepted"]]
        print(f"  Based on {len(tenders)} tenders ({len(acc)} accepted by us).")
        if csv_prefix and events:
            fn = f"{csv_prefix}_events.csv"
            with open(fn, "a", encoding="utf-8") as f:
                if f.tell() == 0:
                    f.write(",".join(events[0].keys()) + "\n")
                for e in events:
                    f.write(",".join(str(v) for v in e.values()) + "\n")
            print(f"  (event rows written to {fn})")



def analyse(path, csv_prefix=None):
    ticks, orders, tenders, txt = load(path)
    if not ticks:
        print(f"{path}: no tick records")
        return
    T = sorted(ticks)
    print("=" * 78)
    print(f"{os.path.basename(path)}   ticks {T[0]}-{T[-1]}   orders {len(orders)}   tenders {len(tenders)}")
    first, last = ticks[T[0]], ticks[T[-1]]
    print(f"P&L at last logged tick: {last.get('nlv')}")

    # 1. traders
    section("1. Traders in the books")
    per_ticker = {t: [] for t in TICKERS}
    ids = Counter()
    for r in ticks.values():
        for t in TICKERS:
            b = r["books"].get(t, {"bids": [], "asks": []})
            s = {tid for _, _, tid in b["bids"] + b["asks"] if tid}
            per_ticker[t].append(len(s))
            ids.update(s)
    if not ids:
        print("  The book shows no trader ids (anonymous book): can't count competitors this way.")
    else:
        for t in TICKERS:
            xs = per_ticker[t]
            print(f"  {t:5s} distinct ids in top 10 levels per tick: median {med(xs):.0f}, max {max(xs)}")
        print(f"  {len(ids)} different ids seen. Most frequent: "
              + ", ".join(f"{k} ({v} ticks)" for k, v in ids.most_common(8)))
        print("  (one of these is you: your own resting orders show up in the book)")

    # 2. book depth
    section("2. Book depth and spread (median over the heat)")
    for t in TICKERS:
        top, d10, spr = [], [], []
        for r in ticks.values():
            b = r["books"].get(t)
            q = r["quotes"].get(t)
            if not b or not q:
                continue
            for side in ("bids", "asks"):
                lv = b[side]
                if lv:
                    top.append(lv[0][1])
                    d10.append(sum(x[1] for x in lv))
            if q[0] > 0 and q[1] > 0:
                spr.append(q[1] - q[0])
        print(f"  {t:5s} top-of-book {med(top):>8,.0f}   10-level depth {med(d10):>9,.0f}   spread {med(spr):.3f}")

    # 3. tender event study
    section("3. Price pressure after tenders (does it stick or bounce?)")
    print("  + = moved AGAINST someone hedging the tender (e.g. BUY tender -> hedgers sell basket -> basket down).")
    print("  Columns: ticks after the tender. Average over tenders; CAD per share.")
    for label, subset in (("all tenders", tenders),
                          ("tenders WE DECLINED (the crowd alone, without our own hedging)",
                           [td for td in tenders if not td["accepted"]])):
        print(f"\n  [{label}]")
        event_study(subset, ticks, csv_prefix if label == "all tenders" else None)

    # 4. execution cost
    section("4. What our market orders cost (vs mid at that tick, CAD per share incl. 0.02 fee)")
    tender_ticks = [td["tick"] for td in tenders if td["accepted"]]
    by = defaultdict(list)
    for o in orders:
        resp = o.get("resp") or {}
        vw, t, tk = resp.get("vwap"), o.get("tick"), o["ticker"]
        if vw is None or t not in ticks or tk not in TICKERS:
            continue
        q = ticks[t]["quotes"].get(tk)
        if not q or q[0] <= 0:
            continue
        mid = (q[0] + q[1]) / 2
        cost = (vw - mid if o["action"] == "BUY" else mid - vw) + 0.02
        if tk == "RITC":
            cost *= mids(ticks[t]).get("USD", 1.0)
        near = any(0 <= t - x <= 10 for x in tender_ticks)
        by[(tk, "within 10 ticks of a tender" if near else "other times")].append((cost, resp.get("quantity_filled") or 0))
    if not by:
        print("  No filled market orders with prices.")
    for (tk, when), xs in sorted(by.items()):
        sh = sum(q for _, q in xs)
        w = sum(c * q for c, q in xs) / sh if sh else float("nan")
        print(f"  {tk:5s} {when:28s} {len(xs):4d} orders  {sh:>9,.0f} sh   avg cost {w:6.3f}/sh")

    # 5. accepted tenders: estimated vs P&L move
    section("5. Accepted tenders: estimated edge vs P&L change over the next 30 ticks")
    if not [td for td in tenders if td["accepted"]]:
        print("  None accepted.")
    for td in tenders:
        if not td["accepted"]:
            continue
        t0 = td["tick"]
        before = ticks.get(t0 - 1, ticks.get(t0, {})).get("nlv")
        after = None
        for k in range(30, 0, -1):
            if t0 + k in ticks:
                after = ticks[t0 + k].get("nlv")
                break
        est = td["edge"] * td["qty"]
        realised = (after - before) if isinstance(after, (int, float)) and isinstance(before, (int, float)) else None
        print(f"  t={t0:3d} {td['action']:4s} {td['qty']:>7,.0f} @ {td['price']}  estimated ${est:>9,.0f}   "
              f"P&L change {'n/a' if realised is None else f'${realised:,.0f}'}"
              "   (includes everything else that happened in those ticks)")

    # declines summary
    dec = [td for td in tenders if not td["accepted"]]
    if dec:
        section("Declined tenders")
        for td in dec:
            print(f"  t={td['tick']:3d} {td['action']:4s} {td['qty']:>7,.0f} @ {td['price']}  edge {td['edge']:+.3f}  {td['why'][:90]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", help="jsonl files (default: logs/etf_*.jsonl)")
    ap.add_argument("--csv", help="prefix for an events CSV (tender event study rows)")
    a = ap.parse_args()
    files = a.files or sorted(glob.glob(os.path.join("logs", "etf_*.jsonl")))
    files = [f for pat in files for f in (glob.glob(pat) or [pat])]
    if not files:
        sys.exit("No logs found (expected logs/etf_*.jsonl).")
    for f in files:
        analyse(f, a.csv)


if __name__ == "__main__":
    main()
