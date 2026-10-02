# Predictions Cup trading bots

Automated trading for the SIG Predictions Cup, a play-money prediction-market contest on the
2026 US midterm elections (~3,900 participants, Oct 1 – Nov 4 2026). Two strategies run in one
loop against the contest's REST API:

1. **Fair-value gap trading** (`cup_bot.py`): prices every contest contract against the
   real-money markets on Kalshi and Polymarket, buys when the contest is cheap, and sells when
   it converges.
2. **D + R arbitrage** (`arb_bot.py`): in a two-candidate race exactly one of Democrat and
   Republican wins, so buying both sides locks in a profit whenever the contest prices them
   inconsistently.

Goal: a top 5–10% finish with low variance, rather than a lottery-style shot at first place.

## Strategy

### Fair-value gap trading
- **Fair value** = average of the Kalshi and Polymarket mid prices. A contract is skipped if the
  two sources disagree by more than 8 points.
- **Entry**: buy YES when the contest ask is at least 3 points below fair value, or NO when the
  bid is at least 3 points above it.
- **Exit**: sell once the contest price is within 1 point of fair value, which frees the cash for
  the next mispricing.
- **Sizing**: half-Kelly, `f* = (p − price) / (1 − price)`, so bigger edges get bigger bets.
  Capped at 10% of bankroll per contract and 15% per race (both parties combined).
- **Cash reserve**: no new buys once cash falls below 15% of the starting balance.

### D + R arbitrage
- **NO + NO**: if `bid(D) + bid(R) > 1`, buy NO on both. One of them always pays 1, and a
  third-party win pays 2.
- **YES + YES**: if `ask(D) + ask(R) < 1`, buy YES on both. This pays 1 only if D or R wins, so it
  is only done where Kalshi prices all other candidates at ≤ 2%, *and* at ≤ half the locked
  profit. In practice the "best" YES+YES arbs (SD and ID Senate, 5 points) turned out to be
  races with ~7.5% independents.
- **Sizing from the order book**: the bot walks both order books together, so every pair costs
  ≤ 0.99.
- **Leg risk**: both legs go in one atomic multi-leg order. Unfilled remainders are cancelled
  immediately, and uneven fills are repaired next cycle (completing the pair at ≤ break-even).
- **Early exit**: a pair is sold early when that keeps ≥ 50% of its settlement profit, so the
  cash can be reused.

### Running both on the same races
The account only shows one net position per contract, so the strategies are coordinated
explicitly:
- The arb keeps a **share ledger** (`arb_races.json`) of exactly which shares belong to pairs.
  It is updated from exact fill counts, including fills that race a cancel.
- The gap strategy trades only the shares that aren't locked, and never buys the side opposite
  a locked leg. On this platform, opposite-side buys net against existing shares.
- Arb positions count toward the gap strategy's per-race caps.
- If the ledger can't be read, the gap step skips the cycle instead of guessing.

## Engineering notes
- **Bulk pricing**: all ~230 contracts are priced with ~3 requests per cycle (Kalshi `tickers=`,
  Polymarket multi-`slug`), not ~460.
- **`automap`**: maps contest races to Kalshi tickers by naming pattern (`SENATETX-26-D`,
  `HOUSETX15-26-R`, …) and to Polymarket by search. A Polymarket match is kept only if its price
  agrees with Kalshi. This covered 232 of 237 contracts.
- **Rate limits and outages**: requests are paced under 100 reads / 30 writes per minute, with
  backoff on 429/5xx responses. Orders carry idempotency keys, so retries can't double-place.
- **Unattended operation**: the loop survives errors and logs with timestamps. `SIGTERM` finishes
  the current cycle (orders + ledger) before exiting, so restarts are always safe.
- **Measurement**: an hourly snapshot (`stats.csv`) of rank, the top-5%/10% cutoffs, and P&L
  three ways:
  - **leaderboard**: valued at the contest's lagging trade-average prices
  - **sell-now**: everything sold at the current bids
  - **fair value**: valued at Kalshi/Polymarket prices

## Usage

```bash
pip install requests
echo "SIG_API_KEY=your_key" > .env            # contest API key (read + trade)

python3 cup_bot.py markets                     # download contest markets -> mapping.csv
python3 cup_bot.py automap                     # fill in Kalshi / Polymarket mappings
python3 cup_bot.py scan --arb                  # dry run: show what both strategies would do
python3 cup_bot.py trade --arb --loop 60       # trade, one cycle a minute
python3 cup_bot.py stats                       # append a snapshot to stats.csv
python3 cup_bot.py positions | leaderboard

pkill -f "cup_bot.py trade"                    # graceful stop
```

## Results
*Updated at the end of the contest (Nov 4 2026): final rank, P&L, Sharpe ratio and max drawdown,
from `stats.csv`.*
