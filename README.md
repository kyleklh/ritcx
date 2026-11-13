# RITCx prep: Volatility Trading Case

| File | What it is |
|---|---|
| `vol_algo.py` | The trading algorithm. Settings are at the top. |
| `mock_rit_vol.py` | Local fake RIT server for practising without server access. |
| `bs.py` | Black-Scholes price, delta, gamma, vega, implied vol (no extra packages). |
| `test_batch.sh` | Runs the algo against many fast mock heats and prints each P&L. |

## Practise locally (no RIT access needed)

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

## On competition day

1. Open the RIT Client, log in, enable the API (check port and API key in the client).
2. Set `API` / `API_KEY` at the top of `vol_algo.py` if they differ from `localhost:9999` / `Rotman`.
3. Start `python vol_algo.py` before tick 1. Each teammate runs it on their own account.

Live commands (type + Enter):

- `v 25`: this week's vol is 25% (use if the news was not parsed)
- `n 27 30`: next week's range is 27 to 30%
- `p` / `r`: pause / resume new option trades (hedging continues)
- `h`: force a delta hedge
- `s`: show status
- `q`: quit

## What the algo does

1. Parses news for this week's vol and next week's range.
2. Fair vol = sqrt(time-weighted variance over the remaining life).
3. Buys options cheaper than fair (after fees + `MIN_EDGE`), sells rich ones. Max 950 net contracts.
4. Risk controls:
   - Delta hedged with RTM whenever |delta| > `HEDGE_TRIGGER`.
   - Option book delta capped (`OPTION_DELTA_CAP`) so 50k RTM can always hedge it.
   - Book gamma capped (`GAMMA_Z`) so a single tick move cannot breach the 7000 limit.
5. Closes all options in the last `CLOSE_TICKS_BEFORE_END` ticks.

## Check once you have practice-server access

- [ ] Exact wording of the vol news (the parser expects "will be 20%" and "between 27% and 30%").
- [ ] How fast the market maker's implied vol catches up (the mock assumes a half-life of 20 ticks).
- [ ] Whether the case is 300 ticks (the algo reads `ticks_per_period` automatically).
