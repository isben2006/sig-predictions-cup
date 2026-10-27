# Predictions Cup trading bots

Automated trading for the SIG Predictions Cup, a play-money prediction-market contest on the
2026 US midterm elections (~3,900 participants, Oct 1 – Nov 4 2026). Two strategies run in one
loop against the contest's REST API:

1. **Fair-value gap trading** (`bot/strategies/gap.py`): prices every contest contract against the
   real-money markets on Kalshi and Polymarket, buys when the contest is cheap, and sells when
   it converges.
2. **D + R arbitrage** (`bot/strategies/arb.py`): in a two-candidate race exactly one of Democrat and
   Republican wins, so buying both sides locks in a profit whenever the contest prices them
   inconsistently.

Goal: a top 5–10% finish with low variance, rather than a lottery-style shot at first place.

## Strategy

### Fair-value gap trading
- **Fair value** = average of the Kalshi and Polymarket mid prices. A contract is skipped if the
  two sources disagree by more than 8 points.
- **Entry**: buy YES when the contest ask is at least 5 points below fair value, or NO when the
  bid is at least 5 points above it. Only big edges: smaller ones are left so the cash goes to the
  arb, which earned ~2.5% a day on its capital at low risk.
- **Exit**: sell once the contest price is within 1 point of fair value, which frees the cash for
  the next mispricing.
- **Sizing**: quarter-Kelly, `f* = (p − price) / (1 − price)`, so bigger edges get bigger bets.
  Capped at 10% of bankroll per contract and 8% per race (both parties combined).
- **Near-certain contracts** (favorite ≥ 90%) rarely converge before settlement (the
  favorite-longshot bias), so they are capped at 20% of bankroll in total.
- **Holding**: positions are held for the full convergence; the ones that never converge are
  held to settlement. The gap step runs first each cycle, so these big edges get first claim on cash.
- **Rotation**: a position is sold early when its money would earn more in the arb: only at or
  above cost, only while arbs are waiting for cash, only if holding earns less per day than half
  the arb's *measured* daily return (and the arb measures ≥ 0.5%/day), never in the final week, at
  most one per cycle. Competitive positions additionally need 7+ days held (share-weighted) and an
  edge below the entry threshold (i.e. we wouldn't buy them today). The sale's cash is kept for the
  arb, so the gap step can't re-spend it.

### D + R arbitrage
- **NO + NO**: if `bid(D) + bid(R) > 1`, buy NO on both. One of them always pays 1, and a
  third-party win pays 2, so these are traded in every race, independents or not.
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
- **Capital**: the arb is the volume engine. It may use up to 75% of the bankroll; the gap step
  still gets first claim on cash each cycle, and 2% stays free for repairing one-sided fills.

### Market making (pilot, realtime)
A separate process (`main.py mm`, systemd `sig-mm`) quotes both sides of a few liquid contracts whose
contest price agrees with Kalshi/Polymarket, earning the spread when both sides fill. It is chosen from
data: 48 hours of the contest's trade tape showed most edge "around fair" was one-sided mispricing (the
gap trade), while ~680k shares/day traded two-sided in 71 contracts priced near fair.
- **Realtime**: order books and our fills arrive over the contest's WebSocket feed (one-second batches,
  versioned books); quotes follow the book within a second instead of once a cycle. Books are re-read over
  REST after any missed batch, reconnect or token refresh, as the feed is best-effort.
- **Quoting**: join the best bid / best ask (step inside spreads of 3+ ticks), only while the book's mid is
  within 2 points of fair value, and never on the wrong side of fair. Quotes are pulled when fair moves a
  point, when the feed is unhealthy, and on exit.
- **Risk**: ±1,000 shares per contract, $5,000 of capital (the trading loop's cash floor goes from 2% to 7%
  while it trades live), its own 10-writes/min share of the account's API budget; the trading loop stays
  out of its races.
- **Shadow mode first**: it simulates queue position (orders already at our price trade first) and fills
  from the live trade tape, to measure the edge before any money is at risk.
- **Outcome**: the shadow results (Oct 11-13) didn't beat leaving the money in the arb, so it never went
  live; the shadow run was switched off on Oct 27 to give election night the API budget.

### Election night (calls)
Races are called within hours of polls closing, but every contest market stays open until trading ends
(Nov 4, noon ET) and settles at 0/1 later. A separate fast process (`main.py calls`, systemd `sig-calls`)
trades that delay:
- **Signal**: Kalshi and Polymarket are polled every 3 seconds (all ~230 contracts in ~1 s). A race counts as
  called only after all of its state's polls have closed, and when both sources hold the winner at ≥ 99%
  for 30 s (safe races; safe races trade at 97-99% for weeks), ≥ 98% for 60 s (close races), ≥ 99.5% for
  2 minutes with one source down, or at once when Kalshi records a result (its markets settle early on a
  consensus of media projections).
- **Dead legs**: in a called race one leg of every arb pair is now worth 0, but the contest still pays for
  it; it is sold, and the other leg still pays 1 at settlement.
- **Called winners**: bought with cash at ≤ 0.95. Cash is paced: before 9 pm ET at most 40% of it goes to
  the early safe-race wave unless the edge is ≥ 15 points; $3,000 per race, $6,000 per hour.
- **Defensive exits**: shares outside pairs that the markets turn decisively against (≤ 30% for a minute)
  are sold while the contest still pays at least 5 points more than they are worth.
- **Safety**: one batched order per race (no order-book read first; the limit price caps what we pay),
  a phone alert for every action, the race is left alone if the markets walk a call back, and live mode
  refuses to run (and pauses) while the trading loop is running, so one process trades the account.
- **Tested** offline against a fake contest, then in shadow mode for a week on live prices with every
  state's polls treated as closed (`--test`).

### Running both on the same races
The account only shows one net position per contract, so the strategies are coordinated
explicitly:
- The arb keeps a **share ledger** (`data/arb_races.json`) of exactly which shares belong to pairs.
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
- **Notifications**: plain-language phone messages (start, stop, repeated failures, a daily summary
  that flags anything unusual) via ntfy.sh.
- **Measurement**: an hourly snapshot (`stats.csv`) of rank, the top-5%/10% cutoffs, and P&L
  three ways:
  - **leaderboard**: valued at the contest's lagging trade-average prices
  - **sell-now**: everything sold at the current bids
  - **fair value**: valued at Kalshi/Polymarket prices

## Code layout

```
main.py                    command line + main loop (the only file you run)
bot/
  common.py                contest API client, mapping file, Kalshi/Polymarket fair values
  markets.py               setup: download contest markets, auto-map them to Kalshi/Polymarket
  reporting.py             stats.csv snapshots, P&L views, phone notifications, daily summary
  strategies/
    gap.py                 fair-value gap trading (entry/exit rules, Kelly sizing, risk limits)
    arb.py                 D + R arbitrage (pair logic, share ledger, repair, early exit)
    mm.py                  market-making pilot (quoting rules, shadow fill simulation, live quotes)
    calls.py               election night: trade races called in real life before the contest catches up
  realtime.py              WebSocket feed client: live order books + our fills, with REST resync
deploy/
  deploy.sh                set up, update and monitor the server from the laptop
  sig-bot.service          systemd unit for the trading loop
  sig-mm.service           systemd unit for the market maker
  sig-calls.service        systemd unit for the election-night bot
data/                      mapping.csv, stats.csv, arb_races.json (live arb ledger, not in git)
logs/                      bot.log (not in git)
```

Each cycle `main.py trade --arb` runs the gap step, then the arb step, then (hourly) the stats snapshot
and notifications. Paths are anchored to the project folder, so the bot runs from any directory; tests
point `SIG_DATA_DIR` / `SIG_LOG_DIR` at a scratch folder so they can't touch the live ledger.

## Usage

```bash
pip install -r requirements.txt
echo "SIG_API_KEY=your_key" > .env            # contest API key (read + trade)
echo "NTFY_TOPIC=some-private-name" >> .env    # optional: phone notifications via the ntfy app

python3 main.py markets                     # download contest markets -> data/mapping.csv
python3 main.py automap                     # fill in Kalshi / Polymarket mappings
python3 main.py scan --arb                  # dry run: show what both strategies would do
python3 main.py trade --arb --loop 60       # trade, one cycle a minute
python3 main.py stats                       # append a snapshot to data/stats.csv
python3 main.py positions | leaderboard

pkill -f "main.py trade"                    # graceful stop
```

## Deployment
The bot runs 24/7 on a DigitalOcean droplet (Ubuntu 24.04, 1 vCPU / 2 GB) in **San Francisco
(SFO3)**. The contest API is served from Vercel's `sfo1` region (its `x-vercel-id` header), so a
West Coast server skips a cross-country hop on every request: ~170 ms per call from SFO3 vs
~255 ms from Boston and ~330 ms from Europe. That matters little for a one-minute loop but more
for market making, where stale quotes get picked off.

It runs as a systemd service (`deploy/sig-bot.service`) that starts on boot and restarts on a
crash. Stopping sends `SIGTERM`, which finishes the current cycle first. The server's `data/` is
the live copy, so code deploys never send local state.

```bash
echo "SIG_SERVER=bot@<ip>" >> .env      # server address (SSH key: ~/.ssh/sig-bot)
deploy/deploy.sh setup                  # first time: user, firewall, Python, code + data, service
deploy/deploy.sh start                  # after stopping the bot on the laptop
deploy/deploy.sh push                   # deploy code changes (restarts only if code changed)
deploy/deploy.sh pull                   # bring the server's data/ and logs/ back for analysis
deploy/deploy.sh logs | status | stop | restart
```

## Results
*Updated at the end of the contest (Nov 4 2026): final rank, P&L, Sharpe ratio and max drawdown,
from `stats.csv`.*
