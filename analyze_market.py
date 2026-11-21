"""
What are the OTHER teams trading? Reads a heat folder written by vol_algo (quotes.csv, tas.csv, fills.csv)
and subtracts our own fills from the market's trades.

    python analyze_market.py logs/vol/20261120_101500

Prints, per option and in total: the others' volume, which side they were on (a trade at the ask was a buy,
at the bid a sell), their running position (estimated from that), and when they traded, with the week starts
(t=76, 151, 226) called out. Without tas.csv (server has no time & sales) it falls back to the per-tick change
in market volume from quotes.csv, which gives volume and timing but not side.
"""
import csv
import os
import sys
from collections import defaultdict

BOUNDARIES = (76, 151, 226)


def load(folder, name):
    path = os.path.join(folder, f"{name}.csv")
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else "."
    quotes, tas, fills = load(folder, "quotes"), load(folder, "tas"), load(folder, "fills")
    if not quotes:
        sys.exit(f"{folder}: no quotes.csv (run a heat with the market log first)")
    q = {(int(r["tick"]), r["ticker"]): (num(r["bid"]), num(r["ask"]), num(r["volume"])) for r in quotes}
    ours = defaultdict(float)                       # (tick, ticker) -> our signed qty
    our_vol = defaultdict(float)                    # (tick, ticker) -> our volume
    for r in fills:
        k = (int(r["tick"]), r["ticker"])
        qty = float(r["quantity"])
        ours[k] += qty if r["action"] == "BUY" else -qty
        our_vol[k] += qty

    other_vol = defaultdict(float)                  # (tick, ticker) -> others' volume
    other_signed = defaultdict(float)               # (tick, ticker) -> others' signed qty (side from price)
    if tas:
        # remove our own trades: match each of our fills to a market trade (same ticker and size, within
        # +-2 ticks, same price when we know it); whatever is left was traded by someone else
        pool = defaultdict(list)
        for r in fills:
            pool[(r["ticker"], float(r["quantity"]))].append([int(r["tick"]), num(r["vwap"]), False])
        unmatched_ours = 0
        for r in tas:
            t, tk, px, qty = int(num(r["tick"]) or 0), r["ticker"], num(r["price"]), num(r["quantity"]) or 0
            cands = [c for c in pool.get((tk, qty), []) if not c[2] and abs(c[0] - t) <= 2
                     and (c[1] is None or px is None or abs(c[1] - px) < 1e-6)]
            if cands:
                min(cands, key=lambda c: abs(c[0] - t))[2] = True
                continue
            side = 0
            for qt in (t, t - 1):                   # a trade can print just after the quote moved
                bid, ask, _ = q.get((qt, tk), (None, None, None))
                if px is not None and bid is not None and ask is not None:
                    if abs(px - ask) < 0.005:
                        side = 1
                        break
                    if abs(px - bid) < 0.005:
                        side = -1
                        break
            other_vol[(t, tk)] += qty
            other_signed[(t, tk)] += side * qty
        unmatched_ours = sum(1 for v in pool.values() for c in v if not c[2])
        source = (f"time & sales minus our fills ({unmatched_ours} of our fills had no matching trade); "
                  f"side from trade price vs quote")
    else:
        prev = {}
        for (t, tk) in sorted(q):
            v = q[(t, tk)][2]
            if v is None:
                continue
            if tk in prev:
                other_vol[(t, tk)] = max(0.0, (v - prev[tk]) - our_vol.get((t, tk), 0))
            prev[tk] = v
        source = "market volume change per tick (no side: tas.csv missing)"

    print(f"source: {source}\n")
    tickers = sorted({k[1] for k in other_vol} | {k[1] for k in our_vol})
    print(f"{'ticker':<8}{'others vol':>12}{'others buys':>13}{'others sells':>14}{'our vol':>10}")
    tot_other = tot_ours = 0
    for tk in tickers:
        ov = sum(v for (t, x), v in other_vol.items() if x == tk)
        buys = sum(v for (t, x), v in other_signed.items() if x == tk and v > 0)
        sells = -sum(v for (t, x), v in other_signed.items() if x == tk and v < 0)
        uv = sum(v for (t, x), v in our_vol.items() if x == tk)
        tot_other += ov if tk != "RTM" else 0
        tot_ours += uv if tk != "RTM" else 0
        print(f"{tk:<8}{ov:>12,.0f}{buys:>13,.0f}{sells:>14,.0f}{uv:>10,.0f}")
    print(f"\noptions volume: others {tot_other:,.0f} contracts, ours {tot_ours:,.0f}")

    if other_signed:
        pos = defaultdict(float)
        peak_net = peak_gross = 0
        for t in sorted({k[0] for k in other_signed}):
            for tk in tickers:
                if tk != "RTM":
                    pos[tk] += other_signed.get((t, tk), 0)
            net = sum(pos.values())
            gross = sum(abs(v) for v in pos.values())
            peak_net, peak_gross = max(peak_net, abs(net)), max(peak_gross, gross)
        print(f"others' estimated option book: peak |net| {peak_net:,.0f}, peak gross {peak_gross:,.0f} contracts "
              f"(several other teams add up here)")

    print("\nwhen the others traded options (contracts per tick, ticks with any volume):")
    by_tick = defaultdict(float)
    for (t, tk), v in other_vol.items():
        if tk != "RTM" and v:
            by_tick[t] += v
    near = sum(v for t, v in by_tick.items() if any(b - 2 <= t <= b + 4 for b in BOUNDARIES))
    print(f"  within t-2..t+4 of a week start: {near:,.0f} of {sum(by_tick.values()):,.0f}")
    for t in sorted(by_tick):
        if by_tick[t] >= 50:
            mark = " <- week start" if any(b - 2 <= t <= b + 4 for b in BOUNDARIES) else ""
            detail = ", ".join(f"{tk[3:]}:{other_signed.get((t, tk), 0):+.0f}" if other_signed else f"{tk[3:]}:{other_vol[(t, tk)]:.0f}"
                               for tk in tickers if tk != "RTM" and other_vol.get((t, tk)))
            print(f"  t={t:>3}  {by_tick[t]:>6,.0f}  {detail}{mark}")


if __name__ == "__main__":
    main()
