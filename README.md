# RITCx prep

| File | What it is |
|---|---|
| `vol_algo.py` | The trading algorithm. Settings are at the top. |
| `mock_rit_vol.py` | Local fake RIT server for practising without server access. |
| `bs.py` | Black-Scholes price, delta, gamma, vega, implied vol (no extra packages). |
| `test_batch.sh` | Runs the vol algo against many fast mock heats and prints each P&L. |
| `etf_algo.py` | ETF arbitrage algorithm. Settings are at the top. |
| `mock_rit_etf.py` | Local fake RIT server for the ETF case (order books, tenders, shocks, FX, converters). |
| `convert.py` | MOCK ONLY: stands in for clicking the converters in the RIT Client (talks to the fake server only). |
| `mock_rit_etf.py --real` | Mock with real book depth/spreads and tenders drawn from the 62 real ones. Good for checking the code runs; NOT for judging P&L (its book refills instantly; it scores the Nov 16 algo at ~+55-80k/heat, real was ~+6k). Judge strategy changes with solo practice + `replay_etf_tenders.py`. |
| `test_etf_batch.sh` | Runs the ETF algo against many fast mock heats. |
| `replay_etf_tenders.py` | Replays every tender of a real session against its logged books: raw edge, what hedging really cost, and what the current algo would decide. Settings it calibrates (`TENDER_HEDGE_COST`, `TENDER_MIN_EDGE`, `FAST_SLIP`) can be set in `.env`. |
| `mm_report.py` | After a heat: what the RITC market making really made per share once its hedges are paid, and whether to keep `MM_ON`. |
| `analyze_etf_logs.py` | Reads `logs/` after a session: competitors in the books, book depth, whether price pressure after tenders sticks or bounces, what hedges cost, tender estimate vs result. |

## Volatility case

### Practise locally (no RIT access needed)

Terminal 1 (real speed, 1 tick per second):

```bash
python mock_rit_vol.py --loop
```

Terminal 2:

```bash
python vol_algo.py
```

Batch test, 8 heats at 5x speed (about 70 seconds):

```bash
./test_batch.sh 8 1
```

### Your API key (once per computer)

The RIT Client generates its own API key (click **API** at the bottom right). Put it in a file named `.env`
in this folder so you never have to edit the scripts:

```bash
copy .env.example .env
```

then open `.env` and set `RIT_API_KEY=` to the key the client shows. `.env` is ignored by git, so your
key stays on your computer. Each teammate does this with their own key. All scripts
(`vol_algo.py`, `etf_algo.py`, `speedtest.py`, `convert.py`) read it automatically.

### On competition day

1. Open the RIT Client, log in, enable the API.
2. Check `.env` has the key the client shows (and `RIT_API` if the port isn't 9999).
3. Start `python vol_algo.py` before tick 1. Each teammate runs it on their own account.

Live commands (type + Enter):

- `v 25`: this week's vol is 25% (use if the news was not parsed)
- `n 27 30`: next week's range is 27 to 30%
- `p` / `r`: pause / resume new option trades (hedging continues)
- `h`: force a delta hedge
- `s`: show status
- `q`: quit

### What the algo does

1. Parses news for this week's vol and next week's range.
2. Fair vol = sqrt(time-weighted variance over the remaining life).
3. Buys options cheaper than fair (after fees + `MIN_EDGE`), sells rich ones. Max 950 net contracts.
4. Risk controls:
   - Delta hedged with RTM whenever |delta| > `HEDGE_TRIGGER`.
   - Option book delta capped (`OPTION_DELTA_CAP`) so 50k RTM can always hedge it.
   - Book gamma capped (`GAMMA_Z`) so a single tick move cannot breach the 7000 limit.
5. Closes all options in the last `CLOSE_TICKS_BEFORE_END` ticks.

### Is a change better? Replay your real heats

Every heat leaves a `vol_log_*.csv` (and `news_raw_*.txt`) in the folder you ran from. Keep them: the
market maker prices all options at one vol, so RTM's price and that vol per tick rebuild every quote,
and two versions of the algo can be compared on the exact market you traded.

    mkdir -p logs/heatA          # one folder per heat: its vol_log_*.csv (+ news_raw_*.txt if you have it)
    python replay_compare.py logs/heatA logs/heatB --refs main WORKTREE --repeats 4

`--refs` are git refs (or `WORKTREE` for the files on disk). Look at the per-heat difference, not totals:
one heat's P&L swings about +-$5k on tiny quote changes, so judge on several heats. A log holding several
heats (tick drops back to 1) must be split first. Without a news file the news is inferred from `fair_vol`.

### Check once you have practice-server access

- [ ] Exact wording of the vol news (the parser expects "will be 20%" and "between 27% and 30%").
- [ ] How fast the market maker's implied vol catches up (the mock assumes a half-life of 20 ticks).
- [ ] Whether the case is 300 ticks (the algo reads `ticks_per_period` automatically).

## ETF Arbitrage case

### Practise locally

Terminal 1:

```bash
python mock_rit_etf.py --loop
```

Terminal 2:

```bash
python etf_algo.py
```

Batch test, 6 heats at 5x speed:

```bash
./test_etf_batch.sh 6 1
```

### What the algo does

1. **Tenders**: real tenders are 70k to 100k RITC. Fully hedged, one share uses 4 units of gross
   (RITC counts 2x), so a 90k tender needs 360k of gross: more than the 300k limit. The algo therefore:
   - accepts if it can hold the tender before hedging (usage under `LIMIT_TRANSIENT_MAX`),
   - hedges with BULL and BEAR as far as the gross limit allows (`HEDGE_USAGE_MAX`),
   - redeems/creates hedged 10k blocks to free room for the rest (see 5),
   - counts the converter fees ($1,500 USD per block) and a penalty for the part that waits unhedged
     (`UNHEDGED_PENALTY`) in the tender's edge. It accepts if the edge is at least `TENDER_MIN_EDGE` per share.
   - Auction tenders get a bid priced to keep that edge.
2. **Hedge**: keeps BULL = BEAR = -RITC, never pushing gross above `HEDGE_USAGE_MAX`.
   - For `FAST_TICKS` after taking a tender (and in the last `FLATTEN_TICKS`): the whole gap at market, once
     per tick, everything within `FAST_SLIP` (0.03) of that tick's best price, 10k clips alternating BULL/BEAR,
     no resting orders. Replaying the Nov 16 real books: this cost ~0.18/sh; walking to 0.15 at once cost ~0.26.
   - Tenders are valued with that measured cost (`TENDER_HEDGE_COST`, 0.15 CAD/sh incl. fees; measured 0.13-0.18) instead of a book
     walk: accept when tender vs basket mid beats it by `TENDER_MIN_EDGE` (so roughly 0.20-0.25+ over the basket).
   - Otherwise: limit orders at the best price first (earning the $0.01 rebate), market orders only for the
     part of the gap beyond `URGENT_GAP`.
   - A resting order on a leg is cancelled before market-trading it (a late-filling resting bid overshot the
     hedge by ~10k in the real heats).
3. **Arb**: trades RITC when RITC vs the basket is mispriced beyond fees; the hedge adds the basket legs.
   - Building new pairs needs `ARB_OPEN_EDGE`. Closing existing pairs needs only `ARB_CLOSE_EDGE`.
   - Capped at `ARB_MAX_PAIRS`, and paused while a tender is being hedged or converted.
4. **FX**: keeps the USD cash position near 0.
13. **Market making in RITC** (`MM_ON=1`, default; `MM_ON=0` in `.env` turns it off; `MM_EDGE` sets the distance):
   a resting RITC bid at fair-0.25 USD and ask at fair+0.25 (fair = (BULL+BEAR)/USD), 10k each, up to 30k RITC.
   RITC swings +-0.17 around fair every few ticks and snaps back, so the quotes get filled at the extremes for free
   (+0.03 rebate); each fill is hedged with BULL+BEAR at market. team08 does this in practice. Replaying 27 real
   heats' books (fill only if the bots' price moves 0.02 through ours, 0.03-0.05 hedge slippage): +13-19k/heat,
   24-27 of 27 heats positive. Real fill rates are the unknown: the log prints every `MM | filled` line with its
   edge vs fair. Quotes are pulled while a tender is being hedged/held, during conversions, and in the last 30 ticks.
12. **Big arb** (`ARB_BIG=1`, default; `ARB_BIG=0` in `.env` = old small arb): RITC vs BULL+BEAR gaps above
   0.05/sh after fees appear on ~13% of ticks and close within 2-3 ticks. Up to 60k pairs, 10k per trade, opens at
   0.10/sh, all three legs at market at once, sized by walking the visible books. Replaying the 8 real heats' books:
   ~+12-16k/heat vs ~+2-4k for the small arb (+6k vs +1k in the competition heats with 0.03/sh extra slippage).
   It depends on speed: acting a full tick late loses money. When a good tender doesn't fit because of these pairs,
   the algo HOLDs it, unwinds just enough same-direction pairs (spending up to 60% of the tender's edge; converters
   when the market is too expensive) and accepts it within its 6 ticks.
9. **Limit room**: pairs left from a tender are unwound at up to `ROOM_UNWIND_EDGE` down to `ROOM_TARGET`; if
   they're still above `ROOM_CONVERT_ABOVE` `ROOM_CONVERT_AFTER` ticks after the tender, hedged 10k blocks are
   converted so the next tender fits (Nov 16: ~$44k of tenders declined on limits).
10. **End of heat**: no new arb pairs in the last `NO_OPEN_TICKS`, no tenders in the last `TENDER_CUTOFF_TICKS`,
   every pair unwound at up to `FLATTEN_EDGE` in the last `FLATTEN_TICKS`, nothing but hedging in the last
   `QUIET_TICKS`. The close-out marks RITC at its own last price, which sits +-0.2-0.4 off the basket.
11. **P&L swings**: the status line shows `fair`, the P&L with RITC marked at the basket's value. RIT's P&L
   swings by thousands on hedged pairs as RITC wobbles around the basket; `fair` is the number to watch.
7. **Two exit routes for tenders**: part of a tender (up to `RITC_ROUTE_MAX`) can go straight back into the RITC
   book (1 trade, 1 fee, no converters) instead of being hedged with BULL+BEAR. The tender valuation tries a few
   splits and keeps the best; whatever the RITC route hasn't done after `RITC_ROUTE_TICKS` is hedged with stocks.
   No new tender is accepted while the last one is still being worked.
8. **Competition**: `CROWD_SHARE` (default 0.5) assumes other teams take half of the visible book, so tenders are
   only accepted if they still pay when everyone hedges at once. Use `CROWD_SHARE=1.0` to value as if alone.
   Test with competitors on the mock: `MOCK_ARGS="--crowd 3" ./test_etf_batch.sh 8 201`.
6. **Late fills**: the real server can show a fill in `/securities` a moment after the order returns. After a
   market order the algo does not trade that ticker again until the fill shows (or `INFLIGHT_TIMEOUT` passes),
   and it resizes resting orders and the FX hedge once per tick. Without this, a slow server makes the hedges
   re-send and flip-flop (2.5M USD orders back and forth). Test it with `MOCK_ARGS="--pos-lag 0.15" ./test_etf_batch.sh`.
5. **Converters (automatic)**: the organizers confirmed the algo may use the converters through the API (the case PDF still says human-only). While the fully
   hedged book would not fit under `CONVERT_TARGET` usage, the algo leases ETF-Redemption / ETF-Creation and uses
   them one 10k block at a time through the RIT API (`CONVERT` lines in the log). If the server refuses, it prints
   `CONVERTER API NOT AVAILABLE` and switches to `>>> CONVERTER ... NOW` prompts for you to click.
   To always click yourself: `$env:AUTO_CONVERT="0"; python etf_algo.py`.

### Operator during the heat

- The algo converts by itself. If you see `CONVERTER API NOT AVAILABLE`, click Use on the converter named in each
  `>>> CONVERTER` prompt (10,000 per click); the prompt repeats every few ticks while more blocks are needed.
- Watch `limits=` and check that BULL and BEAR stay equal to -RITC.
- `p` / `r` to pause / resume tenders and arb (hedging continues). `s` for status, `q` to quit.

### Check once you have practice-server access

- [ ] How deep the books are and how fast passive orders fill (tune `URGENT_GAP`, `TENDER_MIN_EDGE`).
- [ ] Whether the USD position really is USD cash (the FX hedge assumes it is).
- [ ] Tender fields (`is_fixed_bid`, `price`, `action`) match what the algo expects.
