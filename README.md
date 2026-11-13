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
| `test_etf_batch.sh` | Runs the ETF algo against many fast mock heats. |

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
   - Limit orders at the best price first (earning the $0.01 rebate).
   - Market orders only for the part of the gap beyond `URGENT_GAP`.
3. **Arb**: trades RITC when RITC vs the basket is mispriced beyond fees; the hedge adds the basket legs.
   - Building new pairs needs `ARB_OPEN_EDGE`. Closing existing pairs needs only `ARB_CLOSE_EDGE`.
   - Capped at `ARB_MAX_PAIRS`, and paused while a tender is being hedged or converted.
4. **FX**: keeps the USD cash position near 0.
6. **Late fills**: the real server can show a fill in `/securities` a moment after the order returns. After a
   market order the algo does not trade that ticker again until the fill shows (or `INFLIGHT_TIMEOUT` passes),
   and it resizes resting orders and the FX hedge once per tick. Without this, a slow server makes the hedges
   re-send and flip-flop (2.5M USD orders back and forth). Test it with `MOCK_ARGS="--pos-lag 0.15" ./test_etf_batch.sh`.
5. **Converters (human only)**: the case rules say the algo may NOT use the converters, so it never calls
   them. While the fully hedged book would not fit under `CONVERT_TARGET` usage, it prints
   `>>> CONVERTER: REDEEM 1 x 10,000 NOW` (long RITC) or `CREATE` (short RITC), and you click that converter
   in the RIT Client Assets tab. One click per prompt; each use takes 2 ticks.

### Operator during the heat

- Before the heat: lease ETF-Redemption and ETF-Creation in the Assets tab (free, lasts the whole heat).
- When a `>>> CONVERTER` prompt appears, click Use on that converter once. The prompt repeats every few ticks
  while more blocks are needed.
- Watch `limits=` and check that BULL and BEAR stay equal to -RITC.
- `p` / `r` to pause / resume tenders and arb (hedging continues). `s` for status, `q` to quit.

### Check once you have practice-server access

- [ ] How deep the books are and how fast passive orders fill (tune `URGENT_GAP`, `TENDER_MIN_EDGE`).
- [ ] Whether the USD position really is USD cash (the FX hedge assumes it is).
- [ ] Tender fields (`is_fixed_bid`, `price`, `action`) match what the algo expects.
