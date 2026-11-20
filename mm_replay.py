"""Replay RITC market making on logged real books: which quote distance (and inventory skew) would have paid most?

    python mm_replay.py                          # every run in logs/
    python mm_replay.py logs/etf_20261120_*      # some runs
    python mm_replay.py --edges 0.15,0.2,0.25,0.3 --skews 0,0.05,0.1

For every heat in the logs it rebuilds, tick by tick, the RITC book WITHOUT our own orders (trader id from the
.txt), and simulates our two quotes:

    bid = fair - EDGE - SKEW * inv / MM_MAX        ask = fair + EDGE - SKEW * inv / MM_MAX      (USD)

  fair      (BULL mid + BEAR mid) / USD mid at the tick the quote is (re)placed
  inv       RITC we hold from market making (+ long). SKEW > 0 lowers both quotes when long, so the ask is
            likelier to fill next and the bid less likely: inventory turns over instead of piling up to MM_MAX.

Fill rule (snapshots are once per tick, so this is an approximation, checked below against real fills):
  a bid at b fills at the next snapshot if the other traders' best bid is now below b (the price traded down
  through our level) or their best ask is at or below b. Placed already crossing the book = instant fill at
  their price, paying the fee instead of earning the rebate. Each fill takes the whole quote (up to 10k).

Each fill is priced like mm_report.py, in CAD per RITC share:
  edge at fill (vs fair at the fill tick) + rebate - hedge, where hedge = walking the logged BULL and BEAR
  books for the fill size + 2 fees + HEDGE_DRIFT (the extra cost measured on real fills, default 0.03).
MM is off where the real algo stops it: the first tick, 8 ticks after a tender, around conversions, the last 30
ticks. Inventory left at the end pays --close-cost per share (the flatten pays up to 0.12; arb closes often
make money, so default 0.06).

The first line of output compares the replay at the real settings (0.25, no skew) with the real confirmed fills
(same matching as mm_report.py). If those are far apart, don't trust the other rows much.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mm_report as R   # noqa: E402  (log loading, heat splitting, real-fill confirmation)

FEE, REBATE = 0.02, 0.03
MM_SIZE, MM_MAX = 10000, 30000
NO_QUOTE_LAST = 30


def mid(q, t):
    return R.mid(q, t)


def others(levels, my_id):
    return [(float(p), float(n)) for p, n, *rest in levels if n and (not rest or rest[0] != my_id)]


def walk_cost(levels, qty):
    """CAD/share below (bids) or above (asks) the first level price paid to trade qty through these levels."""
    left, cost, first = qty, 0.0, None
    for p, n in levels:
        first = p if first is None else first
        take = min(left, n)
        cost += take * abs(p - first)
        left -= take
        if left <= 0:
            break
    if left > 0 and first is not None:
        cost += left * (abs(levels[-1][0] - first) + 0.02)   # beyond the logged levels: one more step
    return cost / qty if qty else 0.0


def hedge_cost(rec, sgn, qty, my_id, drift):
    """Bought RITC (sgn +1) -> sell BULL and BEAR at their bids, and vice versa. Cost vs mid, per RITC share."""
    q, bk = rec.get("quotes") or {}, rec.get("books") or {}
    c = 0.0
    for leg in ("BULL", "BEAR"):
        side = "bids" if sgn > 0 else "asks"
        lv = others((bk.get(leg) or {}).get(side, []), my_id)
        m = mid(q, leg)
        if not lv or m is None:
            return None
        c += abs(lv[0][0] - m) + walk_cost(lv, qty) + FEE
    return c + drift


def pauses(txt, jrecs, last_tick):
    off = {0, 1}
    for t, tag in txt["events"]:
        if tag == "TENDER":
            off.update(range(t, t + 9))
    for r in jrecs:
        if r["type"] == "convert":
            off.update(range(r["tick"] - 1, r["tick"] + 4))
    off.update(range(last_tick - NO_QUOTE_LAST, last_tick + 1))
    return off


def real_quoting(ticks, real_fills, my_id):
    """Ticks where the real algo had a RITC quote up on each side (or filled one within 2 ticks): the replay only
    quotes then, so it inherits every pause of the real algo (tenders, hedges, conversions, its inventory cap)."""
    up = {"BUY": set(), "SELL": set()}
    for k, r in ticks.items():
        bk = (r.get("books") or {}).get("RITC") or {}
        if any(len(l) >= 3 and l[2] == my_id for l in bk.get("bids", [])):
            up["BUY"].add(k)
        if any(len(l) >= 3 and l[2] == my_id for l in bk.get("asks", [])):
            up["SELL"].add(k)
    for f in real_fills:
        side = "BUY" if f["sgn"] > 0 else "SELL"
        up[side].update(range(f["tick"] - 2, f["tick"] + 1))
    return up


def simulate(ticks, off, my_id, edge, skew, drift, close_cost, up=None, size_frac=1.0):
    ks = sorted(ticks)
    inv, fills = 0, []
    quotes = {}                       # side -> (price, qty, placed_tick)
    for k0, k1 in zip(ks, ks[1:]):
        r0, r1 = ticks[k0], ticks[k1]
        q0 = r0.get("quotes") or {}
        u0 = mid(q0, "USD")
        b0, e0 = mid(q0, "BULL"), mid(q0, "BEAR")
        # 1) quotes resting since k0 fill at k1?
        bk1 = (r1.get("books") or {}).get("RITC") or {}
        ob, oa = others(bk1.get("bids", []), my_id), others(bk1.get("asks", []), my_id)
        q1 = r1.get("quotes") or {}
        u1 = mid(q1, "USD")
        fair1 = (mid(q1, "BULL") + mid(q1, "BEAR")) / u1 if u1 and mid(q1, "BULL") and mid(q1, "BEAR") else None
        for side, (px, n, _) in list(quotes.items()):
            if fair1 is None or not ob or not oa:
                continue
            hit = (ob[0][0] < px or oa[0][0] <= px) if side == "BUY" else (oa[0][0] > px or ob[0][0] >= px)
            if hit and n >= 100:
                sgn = 1 if side == "BUY" else -1
                hc = hedge_cost(r1, sgn, n, my_id, drift)
                if hc is None:
                    continue
                fills.append({"tick": k1, "sgn": sgn, "qty": n, "edge": sgn * (fair1 - px) * u1, "rebate": REBATE,
                              "hedge": hc})
                inv += sgn * n
                del quotes[side]
        # 2) (re)place quotes for the coming tick
        if k0 in off or not (u0 and b0 and e0):
            quotes = {}
            continue
        fair0 = (b0 + e0) / u0
        shift = skew * inv / MM_MAX
        want = {}
        if inv < MM_MAX:
            want["BUY"] = (int((fair0 - edge - shift) * 100) / 100, min(MM_SIZE, MM_MAX - inv))
        if inv > -MM_MAX:
            want["SELL"] = (-int(-(fair0 + edge - shift) * 100) / 100, min(MM_SIZE, MM_MAX + inv))
        bk0 = (r0.get("books") or {}).get("RITC") or {}
        ob0, oa0 = others(bk0.get("bids", []), my_id), others(bk0.get("asks", []), my_id)
        quotes = {}
        for side, (px, n) in want.items():
            sgn = 1 if side == "BUY" else -1
            if up is not None and k0 not in up[side]:
                continue
            n = max(100, int(round(n * size_frac / 100)) * 100)
            cross = (oa0 and oa0[0][0] <= px) if side == "BUY" else (ob0 and ob0[0][0] >= px)
            if cross:      # marketable when placed: fills at once at their price, pays the fee, no rebate
                p = oa0[0][0] if side == "BUY" else ob0[0][0]
                hc = hedge_cost(r0, sgn, n, my_id, drift)
                if hc is not None:
                    fills.append({"tick": k0, "sgn": sgn, "qty": n, "edge": sgn * (fair0 - p) * u0, "rebate": -FEE,
                                  "hedge": hc})
                    inv += sgn * n
                continue
            quotes[side] = (px, n, k0)
    gross = sum(f["qty"] * (f["edge"] + f["rebate"] - f["hedge"]) for f in fills)
    return {"fills": fills, "shares": sum(f["qty"] for f in fills), "left": inv,
            "net": gross - abs(inv) * close_cost}


def next_rec(h, tick):
    t = h["ticks"]
    return t.get(tick) or t[min(t, key=lambda k: abs(k - tick))]


def load_heats(paths):
    heats = []
    for jpath in R.runs_from_args(paths):
        tpath = jpath[:-len(".jsonl")] + ".txt"
        if not os.path.exists(tpath):
            continue
        theats, my_id = R.load_txt(tpath)
        jheats = R.load_jsonl(jpath)
        for h, (jh, th) in enumerate(zip(jheats, theats), 1):
            ticks = {r["tick"]: r for r in jh if r["type"] == "tick"}
            if len(ticks) < 50:
                continue
            heats.append({"name": f"{os.path.basename(jpath)[:-6]} heat {h}", "ticks": ticks, "txt": th,
                          "jrecs": jh, "my_id": my_id or "team21"})
    return heats


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--edges", default="0.15,0.18,0.20,0.22,0.25,0.28,0.30,0.35")
    ap.add_argument("--skews", default="0,0.03,0.06,0.10")
    ap.add_argument("--drift", type=float, default=None,
                    help="hedge cost beyond the book walk, CAD/sh (default: calibrated on the real fills)")
    ap.add_argument("--size-frac", type=float, default=None,
                    help="share of the quote each fill takes (default: real average fill / 10k)")
    ap.add_argument("--close-cost", type=float, default=0.06, help="CAD/sh for MM inventory left at the end")
    a = ap.parse_args()
    heats = load_heats(a.paths)
    if not heats:
        sys.exit("no logs found (looked for etf_*.jsonl with a matching .txt)")
    edges = [float(x) for x in a.edges.split(",")]
    skews = [float(x) for x in a.skews.split(",")]
    for h in heats:
        h["off"] = pauses(h["txt"], h["jrecs"], max(h["ticks"]))

    # real confirmed MM fills per heat (same matching as mm_report.py)
    real_all = []
    for h in heats:
        try:
            h["real"], _, _ = R.analyse_heat(h["jrecs"], h["txt"], h["my_id"], REBATE)
        except Exception:
            h["real"] = []
        h["up"] = real_quoting(h["ticks"], h["real"], h["my_id"])
        real_all += h["real"]
    if not real_all:
        sys.exit("no real MM fills in these logs to calibrate against")
    rshares = sum(f["qty"] for f in real_all)
    size_frac = a.size_frac or min(1.0, rshares / len(real_all) / MM_SIZE)
    # hedge drift: what real hedges cost beyond what walking the logged books predicts for the same fills
    clean = [f for f in real_all if not f["mixed"] and not f["unhedged"]]
    if a.drift is None:
        model = [hedge_cost(next_rec(h, f["tick"]), f["sgn"], f["qty"], h["my_id"], 0.0)
                 for h in heats for f in h["real"] if f in clean]
        model = [m for m in model if m is not None]
        meas = sum(f["hedge_cost"] for f in clean) / sum(f["qty"] for f in clean) if clean else 0.12
        drift = meas - (sum(model) / len(model) if model else 0.09)
        print(f"Real hedges cost {meas:.3f}/sh; the book walk predicts {meas - drift:.3f}; drift {drift:+.3f} added.")
    else:
        drift = a.drift
    print(f"Real fills averaged {rshares / len(real_all):,.0f} sh: each replay fill takes {size_frac:.0%} of the quote.")

    print(f"\n{len(heats)} heats. Calibration at the real settings (0.25, no skew) vs real confirmed MM fills:")
    tot_sim = tot_real = 0
    for h in heats:
        sim = simulate(h["ticks"], h["off"], h["my_id"], 0.25, 0.0, drift, a.close_cost, h["up"], size_frac)
        rt = {f["tick"] for f in h["real"]}
        st = {f["tick"] for f in sim["fills"]}
        near = sum(1 for t in st if any(abs(t - x) <= 2 for x in rt))
        caught = sum(1 for t in rt if any(abs(t - x) <= 2 for x in st))
        rs = sum(f["qty"] for f in h["real"])
        rnet = sum(f["qty"] * (f["edge"] + REBATE) - f["hedge_cost"] for f in h["real"])
        tot_sim += sim["shares"]
        tot_real += rs
        print(f"  {h['name']:<30} replay {len(st):3d} fills {sim['shares']:8,.0f} sh ${sim['net']:+8,.0f} | real {len(rt):3d}"
              f" fills {rs:8,.0f} sh ${rnet:+8,.0f} | replay fills near a real one {near}/{len(st)},"
              f" real fills the replay caught {caught}/{len(rt)}")
    ratio = tot_sim / tot_real if tot_real else float("nan")
    print(f"  total: replay {tot_sim:,.0f} sh vs real {tot_real:,.0f} sh (x{ratio:.2f})."
          " Near 1 = the fill rule is about right; far off = read the rows below as a ranking only.\n")

    print(f"{'edge':>5} {'skew':>5} | {'shares':>9} {'edge/sh':>8} {'hedge/sh':>9} {'left':>7} | "
          f"{'net $':>9} {'per heat':>9} {'worst heat':>10}")
    rows = []
    for e in edges:
        for sk in skews:
            res = [simulate(h["ticks"], h["off"], h["my_id"], e, sk, drift, a.close_cost, h["up"], size_frac)
                   for h in heats]
            sh = sum(r["shares"] for r in res)
            fl = [f for r in res for f in r["fills"]]
            ed = sum(f["qty"] * f["edge"] for f in fl) / sh if sh else 0
            hc = sum(f["qty"] * f["hedge"] for f in fl) / sh if sh else 0
            left = sum(abs(r["left"]) for r in res) / len(res)
            net = sum(r["net"] for r in res)
            worst = min(r["net"] for r in res)
            rows.append((net, e, sk))
            mark = "  <- real setting" if abs(e - 0.25) < 1e-9 and sk == 0 else ""
            print(f"{e:5.2f} {sk:5.2f} | {sh:9,.0f} {ed:+8.3f} {hc:9.3f} {left:7,.0f} | {net:+9,.0f} "
                  f"{net / len(res):+9,.0f} {worst:+10,.0f}{mark}")
    best = max(rows)
    base = next(r for r in rows if abs(r[1] - 0.25) < 1e-9 and r[2] == 0) if any(
        abs(r[1] - 0.25) < 1e-9 and r[2] == 0 for r in rows) else None
    print(f"\nBest in this replay: edge {best[1]:.2f}, skew {best[2]:.2f}: {best[0] / len(heats):+,.0f} per heat"
          + (f" vs {base[0] / len(heats):+,.0f} at the real setting." if base else "."))
    print("Only change MM_EDGE if the best row beats the real setting clearly AND in most heats, on 5+ heats of logs.")


if __name__ == "__main__":
    main()
