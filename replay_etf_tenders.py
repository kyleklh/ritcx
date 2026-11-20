"""Replay every tender of a real ETF session against the order books it logged.

    python replay_etf_tenders.py logs/etf_20261116_173426        # the .jsonl and .txt of one run (no extension)
    python replay_etf_tenders.py logs/etf_20261116_173426 --slip 0.15

For each tender it prints:
  raw edge    tender price (in CAD) vs the BULL+BEAR mid when it arrived, per share, signed so + is good for us
  hedge cost  what hedging the whole tender with BULL+BEAR would have cost on the logged books: every tick, take
              everything within --slip of that tick's best price (both legs), fees included; whatever is still
              open after --ticks ticks is charged at that tick's mid + 0.10
  net         raw edge - hedge cost, per share and in $
  algo        what the CURRENT etf_algo.py would decide on that tender if we had been flat (limits ignored)

Caveats: the logged books already contain everyone's trading (ours too, for the tenders we took), but they do
not react to orders we did NOT send. Use it to calibrate TENDER_HEDGE_COST / TENDER_MIN_EDGE, not as a promise.
"""
import argparse
import ast
import json
import re

import etf_algo as A


def split_heats(path):
    heats, cur, last = [], [], -1
    for line in open(path, encoding="utf-8"):
        r = json.loads(line)
        if r.get("type") != "tick":
            continue
        if r["tick"] < last - 50:
            heats.append(cur)
            cur = []
        last = r["tick"]
        cur.append(r)
    heats.append(cur)
    return [{r["tick"]: r for r in h} for h in heats]


def snap_of(r, flat=True):
    books = {t: {s: [(p, q) for p, q, _ in r["books"][t][s]] for s in ("bids", "asks")} for t in ("BULL", "BEAR", "RITC")}
    pos = {k: (0.0 if flat else v) for k, v in r["pos"].items()}
    return {"pos": pos, "q": {k: tuple(v) for k, v in r["quotes"].items()}, "books": books,
            "gross_limit": A.DEFAULT_GROSS, "net_limit": A.DEFAULT_NET, "tick": r["tick"], "ticks": 300}


def mid(r, t):
    return sum(r["quotes"][t]) / 2


def hedge_cost(tk, t0, side, q, slip, ticks):
    """CAD/share (both legs, incl. fees) to buy (side='BUY') or sell q of BULL and of BEAR, vs the mids at t0."""
    sg = 1 if side == "BUY" else -1
    total = 0.0
    seen = [t for t in range(t0, t0 + ticks) if t in tk]
    for leg in ("BULL", "BEAR"):
        got, cost = 0.0, 0.0
        for t in seen:
            lv = tk[t]["books"][leg]["asks" if sg > 0 else "bids"]
            if not lv:
                continue
            best = lv[0][0]
            for px, sz, _ in lv:
                if abs(px - best) > slip + 1e-9 or got >= q:
                    break
                k = min(sz, q - got)
                got += k
                cost += k * px
        cost += (q - got) * (mid(tk[seen[-1]], leg) + sg * 0.10)
        total += sg * (cost / q - mid(tk[t0], leg)) + A.FEE
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run", help="log path without extension, e.g. logs/etf_20261116_173426")
    ap.add_argument("--slip", type=float, default=0.03)
    ap.add_argument("--ticks", type=int, default=15)
    a = ap.parse_args()
    heats = split_heats(a.run + ".jsonl")
    texts = open(a.run + ".txt", encoding="utf-8").read().split("NEW HEAT")
    print(f"{'heat':>4} {'tick':>4} {'side':4} {'qty':>7}  {'raw':>7} {'hedge':>7} {'net':>7} {'net $':>9} | algo")
    tot_all = tot_take = 0.0
    for hi, tk in enumerate(heats):
        if hi >= len(texts):
            break
        for m in re.finditer(r"\[tender raw\] (\{.*\})", texts[hi]):
            t = ast.literal_eval(m.group(1))
            t0 = int(t["tick"]) + 1
            if t0 not in tk or t.get("price") is None:
                continue
            r = tk[t0]
            sg = 1 if t["action"] == "SELL" else -1
            raw = (t["price"] * mid(r, "USD") - mid(r, "BULL") - mid(r, "BEAR")) * sg
            q = int(t["quantity"])
            c = hedge_cost(tk, t0, "BUY" if sg > 0 else "SELL", q, a.slip, a.ticks)
            A.ST.last_detail = ""
            e, _, why = A.evaluate_tender(t, snap_of(r))
            take = why.startswith("ok") or why.startswith("auction")
            net = (raw - c) * q
            tot_all += net
            tot_take += net if take else 0
            print(f"{hi + 1:4} {t0:4} {t['action']:4} {q:7,}  {raw:+.3f} {c:+.3f} {raw - c:+.3f} {net:+9,.0f} | "
                  f"{'TAKE' if take else 'pass'} {e:+.3f}")
    print(f"\nnet of the tenders the current algo takes: ${tot_take:,.0f}   (all tenders: ${tot_all:,.0f})")


if __name__ == "__main__":
    main()
