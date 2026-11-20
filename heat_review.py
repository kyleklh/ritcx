"""Between heats: one screen on how the last heat went and whether to change ONE setting in .env for the next one.

    python heat_review.py                         # the last heat of the newest run in logs/
    python heat_review.py logs/etf_20261121_0930  # the last heat of that run
    python heat_review.py --heat 2                # heat 2 of the newest run instead of the last one

It only reads logs and suggests; nothing is changed. To apply a suggestion: q to quit the algo, edit .env,
start  python etf_algo.py  again before the next heat starts (the algo reads .env only when it starts).

What it checks (latest heat unless said otherwise):
  health         timeouts, slow loops, rejected orders, MM fills that never happened / orders it lost
  market making  net per share after hedging (mm_report.py), edge at fill, hedge cost, other teams quoting RITC
                 at or ahead of our quotes
  tenders        every accepted tender TODAY (all runs in logs/ with today's date): the edge the algo estimated vs
                 what it really made after hedging on the logged books (replay_etf_tenders.py)
  arb            how often the self-check raised the opening edge (other bots taking the gaps)

One suggestion at most, in this order of importance, and only on a clear signal (one heat is noisy):
  1. connection trouble (>= 20 timeouts or >= 15 slow loops)       -> ARB_BIG=0 (far fewer orders)
  2. MM losing: >= 20k clean shares and net < -0.02/sh             -> MM_EDGE +0.05 (MM_ON=0 if already 0.35)
     MM picked off: >= 20k clean shares and edge at fill < 0.15   -> MM_EDGE +0.05
  3. accepted tenders: >= 4 today, losing on average after hedging, and 0.08/sh+ worse than estimated
                                                                   -> TENDER_HEDGE_COST +0.05 at most (max 0.30)
  4. arb self-check raised the edge >= 3 times in the heat         -> ARB_BIG=0
  5. MM fine but starved: net >= +0.08/sh, < 15k shares filled, other teams ahead of our quotes on >= 30% of
     the ticks we quoted                                           -> MM_EDGE -0.02 (not below 0.22)
  otherwise: no change.
"""
import argparse
import ast
import glob
import io
import os
import re
import sys
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
with redirect_stdout(io.StringIO()):
    import etf_algo as A             # current settings, as .env sets them
    import mm_report as M
    import replay_etf_tenders as T


def newest_run(logdir):
    runs = sorted(glob.glob(os.path.join(logdir, "etf_*.jsonl")))
    return runs[-1][:-len(".jsonl")] if runs else None


def txt_heats(path):
    """The .txt of a run split into heats (same rule as the .jsonl: the tick drops sharply)."""
    heats, cur, last = [], [], None
    for line in open(path, encoding="utf-8", errors="replace"):
        m = re.match(r"t=\s*(\d+) \|", line)
        if m:
            t = int(m.group(1))
            if last is not None and t < last - M.HEAT_DROP:
                heats.append(cur)
                cur = []
            last = t
        cur.append(line)
    heats.append(cur)
    return heats


def tender_results(run):
    """Accepted tenders of one run: (heat, tick, qty, estimated edge, real edge after hedging) per share."""
    out = []
    tk_heats = T.split_heats(run + ".jsonl")
    for hi, (tk, lines) in enumerate(zip(tk_heats, txt_heats(run + ".txt")), 1):
        raw = {}
        for line in lines:
            m = re.search(r"\[tender raw\] (\{.*\})", line)
            if m:
                try:
                    t = ast.literal_eval(m.group(1))
                    raw[(t["action"], int(t["quantity"]), float(t["price"] or 0))] = t
                except (ValueError, SyntaxError, KeyError, TypeError):
                    pass
        for line in lines:
            m = re.search(r"t=\s*(\d+) \| TENDER\s*\| ACCEPT\s+(BUY|SELL)\s+([\d,.]+) RITC @ ([\d.]+)\s+edge ([+-][\d.]+)",
                          line)
            if not m:
                continue
            key = (m.group(2), int(float(m.group(3).replace(",", ""))), float(m.group(4)))
            t = raw.get(key)
            if not t:
                continue
            t0 = int(t["tick"]) + 1
            if t0 not in tk:
                continue
            r = tk[t0]
            sg = 1 if t["action"] == "SELL" else -1
            real_raw = (t["price"] * T.mid(r, "USD") - T.mid(r, "BULL") - T.mid(r, "BEAR")) * sg
            cost = T.hedge_cost(tk, t0, "BUY" if sg > 0 else "SELL", key[1], 0.03, 15)
            out.append({"heat": hi, "tick": t0 - 1, "qty": key[1], "side": key[0], "est": float(m.group(5)),
                        "real": real_raw - cost, "hedge": cost})
    return out


def competitors(jrecs, my_id):
    """Other teams (not ANON, not us) quoting RITC, and on how many of our quoting ticks one was at/ahead of us."""
    seen, ahead, quoting = {}, 0, 0
    for r in jrecs:
        if r["type"] != "tick":
            continue
        bk = (r.get("books") or {}).get("RITC") or {}
        bids, asks = bk.get("bids", []), bk.get("asks", [])
        ours_b = [l[0] for l in bids if len(l) >= 3 and l[2] == my_id]
        ours_a = [l[0] for l in asks if len(l) >= 3 and l[2] == my_id]
        for l in bids + asks:
            if len(l) >= 3 and l[2] not in (None, "ANON", my_id):
                seen[l[2]] = seen.get(l[2], 0) + 1
        if not ours_b and not ours_a:
            continue
        quoting += 1
        their_b = [l[0] for l in bids if len(l) >= 3 and l[2] not in (None, "ANON", my_id)]
        their_a = [l[0] for l in asks if len(l) >= 3 and l[2] not in (None, "ANON", my_id)]
        if (ours_b and their_b and max(their_b) >= max(ours_b)) or (ours_a and their_a and min(their_a) <= min(ours_a)):
            ahead += 1
    return seen, ahead, quoting


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", nargs="?", help="log path without extension (default: newest run in logs/)")
    ap.add_argument("--heat", type=int, help="heat number within the run (default: the last one)")
    ap.add_argument("--logs", default=os.path.join(HERE, "logs"))
    a = ap.parse_args()
    run = a.run[:-len(".jsonl")] if a.run and a.run.endswith(".jsonl") else a.run
    run = run or newest_run(a.logs)
    if not run or not os.path.exists(run + ".jsonl"):
        sys.exit("no run found (looked for logs/etf_*.jsonl)")
    theats, my_id = M.load_txt(run + ".txt")
    jheats = M.load_jsonl(run + ".jsonl")
    n = min(len(theats), len(jheats))
    hi = (a.heat or n) - 1
    if not 0 <= hi < n:
        sys.exit(f"{os.path.basename(run)} has {n} heats")
    jh, th, my_id = jheats[hi], theats[hi], my_id or "team21"
    tlines = txt_heats(run + ".txt")[hi] if hi < len(txt_heats(run + ".txt")) else []
    ticks = sorted(r["tick"] for r in jh if r["type"] == "tick")
    last = next((r for r in reversed(jh) if r["type"] == "tick"), {})
    print(f"=== {os.path.basename(run)} heat {hi + 1} of {n}  (ticks {ticks[0] if ticks else '?'}-{ticks[-1] if ticks else '?'})"
          f"  last NLV {last.get('nlv', float('nan')):+,.0f} ===")
    print(f"settings now: MM_ON={int(A.MM_ON)} MM_EDGE={A.MM_EDGE:.2f} ARB_BIG={int(A.ARB_BIG)} "
          f"TENDER_HEDGE_COST={A.TENDER_HEDGE_COST:.2f} TENDER_MIN_EDGE={A.TENDER_MIN_EDGE:.2f}\n")

    # health
    text = "".join(tlines)
    warns, slow = text.count("[warn]"), text.count("SLOW !!")
    rejected = text.count("[order rejected]")
    with redirect_stdout(io.StringIO()):
        fills, phantom, untracked = M.analyse_heat(jh, th, my_id, 0.03)
    print(f"health      timeouts {warns}, slow loops {slow}, rejected orders {rejected}, "
          f"phantom MM fills {len(phantom)}, lost MM orders filled {len(untracked)}")

    # market making
    clean = [f for f in fills if not f["mixed"]]
    cs = sum(f["qty"] for f in clean)
    rebate = 0.03
    if cs:
        edge = sum(f["qty"] * f["edge"] for f in clean) / cs
        hedge = sum(f["hedge_cost"] for f in clean) / cs
        net = edge + rebate - hedge
    else:
        edge = hedge = net = float("nan")
    allsh = sum(f["qty"] for f in fills)
    seen, ahead, quoting = competitors(jh, my_id)
    share_ahead = ahead / quoting if quoting else 0.0
    print(f"market mkg  {allsh:,.0f} sh filled ({cs:,.0f} clean): edge at fill {edge:+.3f}, hedge {hedge:.3f}, "
          f"net {net:+.3f}/sh = ${cs * net if cs else 0:+,.0f}")
    others = ", ".join(f"{k} ({v} ticks)" for k, v in sorted(seen.items(), key=lambda x: -x[1])[:6]) or "none"
    print(f"            other teams in the RITC book: {others}; at/ahead of our quotes on {ahead}/{quoting} ticks "
          f"({share_ahead:.0%})")

    # tenders: every run of the same day
    day = os.path.basename(run)[4:12]
    today = sorted(p[:-len(".jsonl")] for p in glob.glob(os.path.join(os.path.dirname(run) or ".", f"etf_{day}_*.jsonl")))
    today = [r_ for r_ in today if r_ <= run]           # this run and the ones before it, not later ones
    tres = []
    for r_ in today:
        try:
            with redirect_stdout(io.StringIO()):
                tres += [dict(x, run=os.path.basename(r_)) for x in tender_results(r_)
                         if r_ != run or x["heat"] <= hi + 1]
        except Exception as e:      # a half-written log must not stop the review
            print(f"            (tender replay failed on {os.path.basename(r_)}: {e})")
    print(f"tenders     accepted today: {len(tres)}")
    for x in tres:
        print(f"              {x['run'][4:]} heat {x['heat']} t={x['tick']:3d} {x['side']:<4} {x['qty']:,}: "
              f"estimated {x['est']:+.3f}, real {x['real']:+.3f}/sh (hedge cost {x['hedge']:.3f}) "
              f"= ${x['real'] * x['qty']:+,.0f}")
    gap = (sum(x["est"] - x["real"] for x in tres) / len(tres)) if tres else 0.0
    avg_real = (sum(x["real"] * x["qty"] for x in tres) / sum(x["qty"] for x in tres)) if tres else 0.0
    if tres:
        print(f"            on average: estimated {sum(x['est'] for x in tres) / len(tres):+.3f}, "
              f"real {avg_real:+.3f}/sh, total ${sum(x['real'] * x['qty'] for x in tres):+,.0f}")

    # arb
    selfchk = sum(1 for l in tlines if "self-check" in l and "->" in l)
    print(f"arb         self-check raised the opening edge {selfchk} times")

    # one suggestion
    print()
    if warns >= 20 or slow >= 15:
        s = ("ARB_BIG=0", f"connection trouble ({warns} timeouts, {slow} slow loops): the big arb sends the most orders")
    elif cs >= 20000 and net < -0.02:
        s = (("MM_ON=0", f"MM lost {net:+.3f}/sh even at the widest distance") if A.MM_EDGE >= 0.35 - 1e-9 else
             (f"MM_EDGE={min(0.35, A.MM_EDGE + 0.05):.2f}", f"MM lost {net:+.3f}/sh on {cs:,.0f} clean shares"
              + (f"; other teams sat at/ahead of our quotes {share_ahead:.0%} of the time" if share_ahead >= 0.3 else "")))
    elif cs >= 20000 and edge < 0.15:
        s = (f"MM_EDGE={min(0.35, A.MM_EDGE + 0.05):.2f}",
             f"edge at fill only {edge:+.3f}: quotes are being picked off when fair moves")
    elif len(tres) >= 4 and gap >= 0.08 and avg_real < 0 and A.TENDER_HEDGE_COST < 0.30 - 1e-9:
        s = (f"TENDER_HEDGE_COST={min(0.30, A.TENDER_HEDGE_COST + min(0.05, round(gap, 2))):.2f}",
             f"the {len(tres)} tenders taken today lost {avg_real:+.3f}/sh on average after hedging, "
             f"{gap:.3f}/sh worse than estimated")
    elif selfchk >= 3:
        s = ("ARB_BIG=0", f"arb self-check raised the edge {selfchk} times: other bots are taking the gaps")
    elif cs and net >= 0.08 and allsh < 15000 and share_ahead >= 0.30 and A.MM_EDGE > 0.22 + 1e-9:
        s = (f"MM_EDGE={max(0.22, A.MM_EDGE - 0.02):.2f}",
             f"MM pays ({net:+.3f}/sh) but few fills and other teams sit ahead of us {share_ahead:.0%} of the time")
    else:
        s = None
    if s:
        print(f"SUGGESTION: set {s[0]} in .env   ({s[1]})")
        print("            q to quit the algo, edit .env, start python etf_algo.py again before the next heat.")
    else:
        print("SUGGESTION: no change. Keep the algo running.")


if __name__ == "__main__":
    main()
