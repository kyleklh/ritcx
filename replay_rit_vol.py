"""
Replay a REAL heat from vol_algo's own logs, so two versions of the algo can be compared on the
exact market you traded.

Why this works: the case says the market maker prices every option with Black-Scholes at ONE vol
(2-cent spread, unlimited depth, no price impact). vol_algo logs RTM's price (S) and that vol
(atm_iv, from the ATM call) every tick, which is enough to rebuild every option quote.
Orders, fees, limits, the delta penalty and settlement come from mock_rit_vol.py.

Inputs
  vol_log_*.csv       written by vol_algo.py (columns tick,S,fair_vol,atm_iv,...)
  news                news_raw_*.txt written by vol_algo.py, OR a saved console log
                      (the "t=  1 | NEWS | Week 1 vol is 26.0%" lines), OR nothing: then the news is
                      inferred from the fair_vol column (exact for logs from the current fair_vol code)

    python replay_rit_vol.py --csv vol_log_X.csv --news news_raw_X.txt --tick 0.2 --port 9990

Limits: one row per tick (moves inside a tick are lost); ticks before the algo joined are not
replayed (the heat starts where the log starts); assumes no volatility smile across strikes.
"""
import argparse
import ast
import csv
import re
import threading
import time

import mock_rit_vol as mock

RE_RAW = re.compile(r"^tick=(\d+) ticker=(.*) headline=(.*) body=(.*)$")
RE_CON_VOL = re.compile(r"t=\s*(\d+)\s*\|\s*NEWS\s*\|\s*Week (\d) vol is ([\d.]+)%")
RE_CON_FC = re.compile(r"t=\s*(\d+)\s*\|\s*NEWS\s*\|\s*Week (\d) vol forecast ([\d.]+)-([\d.]+)%")


def load_rows(path):
    """tick -> (S, iv). Missing ticks and iv=0 (not solvable) are forward-filled."""
    raw = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            t = int(r["tick"])
            raw[t] = (float(r["S"]), float(r["atm_iv"] or 0), float(r.get("nlv") or "nan"))
    first, last = min(raw), max(raw)
    rows, prev = {}, None
    for t in range(first, last + 1):
        S, iv, nlv = raw.get(t, (None, 0.0, float("nan")))
        if S is None:
            S = prev[0]
        if iv <= 0:
            iv = prev[1] if prev else 0.25
        rows[t] = (S, iv, nlv)
        prev = (S, iv)
    return rows, first, last


def load_news(path):
    """[(tick, ticker, headline, body)] from news_raw_*.txt or a console log."""
    items = []
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    for ln in lines:
        m = RE_RAW.match(ln)
        if m:
            items.append((int(m.group(1)), ast.literal_eval(m.group(2)),
                          ast.literal_eval(m.group(3)), ast.literal_eval(m.group(4))))
    if items:
        return items
    # console log: only the first heat in the file (a NEW HEAT banner starts the next one)
    for ln in lines:
        if "NEW HEAT" in ln and items:
            break
        m = RE_CON_VOL.search(ln)
        if m:
            t, w, v = int(m.group(1)), int(m.group(2)), m.group(3)
            items.append((t, f"Week {w}", f"Announcement {w - 1}",
                          f"The realized volatility of RTM for this week will be {float(v):g}%"))
            continue
        m = RE_CON_FC.search(ln)
        if m:
            t, lo, hi = int(m.group(1)), m.group(3), m.group(4)
            items.append((t, "", "News", f"The realized volatility of RTM for next week will be "
                                         f"between {float(lo):g}% and {float(hi):g}%"))
    return items


def infer_news(path):
    """No news file? Recover it from the fair_vol column. vol_algo's fair vol is a fixed function of the
    announced vols and forecast midpoints, so each jump in it pins down one news item. Returns the same
    tuples as load_news, timed at the tick the algo first reacted (so the replay can only be later)."""
    import vol_algo as va
    fv = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            fv[int(r["tick"])] = float(r["fair_vol"])
    va.ST.reset()
    out, grid = [], [i / 2000 for i in range(20, 1800)]

    def err_for(t, kind, w, x):
        store = va.ST.vol_known if kind == "known" else va.ST.vol_est
        old = store.get(w)
        store[w] = x
        v = va.fair_vol(t)
        if old is None:
            del store[w]
        else:
            store[w] = old
        return abs(v - fv[t]) if v is not None else 9

    for t in sorted(fv):
        cur = va.fair_vol(t)
        if cur is not None and abs(cur - fv[t]) < 6e-4:
            continue
        near_boundary = t <= 2 or t % 75 >= 72 or t % 75 <= 1
        # a vol announced at t=74..76 is for the week starting at 76; week_of(t + 2) gets that right
        kinds = [("known", va.week_of(t + 2))] if near_boundary or cur is None else [("est", va.week_of(t) + 1)]
        best = min(((err_for(t, k, ww, x), k, ww, x) for k, ww in kinds if ww <= 4 for x in grid))
        e, kind, ww, x = best
        if e > 2e-3:
            print(f"[replay] warning: t={t} fair vol {fv[t]:.4f} not explained by one news item (off {e:.4f})")
        (va.ST.vol_known if kind == "known" else va.ST.vol_est)[ww] = x
        if kind == "known":
            out.append((t, f"Week {ww}", f"Announcement {ww - 1}",
                        f"The realized volatility of RTM for this week will be {x * 100:g}%"))
        else:
            out.append((t, "", "News", f"The realized volatility of RTM for next week will be between "
                                       f"{x * 100 - 2.5:g}% and {x * 100 + 2.5:g}%"))
    return out


class ReplaySim(mock.Sim):
    def __init__(self, tick_seconds, rows, first, last, news, jitter_seed=None, jitter=0.001):
        super().__init__(tick_seconds, seed=0)
        self.true_vols = ["replay"]
        # The algo is path-sensitive: a 1-cent quote change can flip a trade near a threshold and the
        # heat then plays out differently (about +-$5k). The logged vol already carries that much rounding
        # error, so repeats add this small noise (0.1 vol pts) to sample it instead of replaying one path.
        self.jit = mock.random.Random(jitter_seed) if jitter_seed is not None else None
        self.jitter = jitter
        self.rows, self.first, self.last = rows, first, last
        self.pending = sorted(news)
        self.tick = first - 1
        self.S, self.mm_vol = rows[first][0], rows[first][1]
        self._publish(upto=first - 1)            # news from before the join is already out

    def _publish(self, upto):
        while self.pending and self.pending[0][0] <= upto:
            t, ticker, head, body = self.pending.pop(0)
            self.news.append({"news_id": len(self.news) + 1, "period": 1, "tick": t,
                              "ticker": ticker, "headline": head, "body": body})

    def step(self):
        with self.lock:
            if self.status != "ACTIVE":
                return
            self.tick += 1
            if self.tick in self.rows:
                self.S, self.mm_vol = self.rows[self.tick][0], self.rows[self.tick][1]
                if self.jit:
                    self.mm_vol = max(0.01, self.mm_vol + self.jit.gauss(0, self.jitter))
            self._publish(upto=self.tick)
            d = abs(self.portfolio_delta())
            if d > mock.DELTA_LIMIT:
                self.penalty += (d - mock.DELTA_LIMIT) * mock.PENALTY_RATE
            if self.tick >= self.ticks:
                self._settle()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv", required=True)
    p.add_argument("--news", default=None, help="news_raw_*.txt or console log; omit to infer from fair_vol")
    p.add_argument("--port", type=int, default=9999)
    p.add_argument("--tick", type=float, default=1.0)
    p.add_argument("--start-delay", type=float, default=2.0)
    p.add_argument("--jitter-seed", type=int, default=None, help="add 0.1-vol-pt noise with this seed (see ReplaySim)")
    a = p.parse_args()
    rows, first, last = load_rows(a.csv)
    news = load_news(a.news) if a.news else infer_news(a.csv)
    live = rows[last][2]
    print(f"[replay] ticks {first}-{last}, {len(news)} news items, live P&L at t={last}: {live:,.0f}", flush=True)
    mock.SIM = ReplaySim(a.tick, rows, first, last, news, jitter_seed=a.jitter_seed)
    server = mock.ThreadingHTTPServer(("127.0.0.1", a.port), mock.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(a.start_delay)
    while mock.SIM.status == "ACTIVE":
        time.sleep(a.tick)
        mock.SIM.step()
    time.sleep(1)


if __name__ == "__main__":
    main()
