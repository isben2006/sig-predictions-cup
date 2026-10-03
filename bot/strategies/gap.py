"""
Fair-value gap trading. Prices every contest contract against the real-money markets (Kalshi /
Polymarket mid = fair value), buys when the contest is cheap, sells when it converges or a take-profit
is reached. Sized with fractional Kelly under per-contract, per-race, near-certain and cash limits.
Called once per cycle from main.py (was cup_bot.py).
"""
import math, sys
from datetime import datetime

from bot.common import fair_value, fetch_external, load_mapping, race_key, round_tick


# Default profile: steady edge capture, aiming for a top 5-10% finish with low variance.
# Bet size = kelly x Kelly fraction of equity, then clipped by max_frac and max_race.
SAFE = dict(
    entry_edge=0.03,      # buy only if contest price is >= 3 points better than fair value
    exit_band=0.01,       # sell when the gap has shrunk to within 1 point of fair value
    take_profit=0.02,     # ...or as soon as it can be sold 2 points above what we paid: many gaps
                          # never close before the election, and realized profit + freed cash beat waiting
    kelly=0.25,           # quarter-Kelly: fair values are estimates; halves the swings, keeps most growth
    max_frac=0.10,        # never hold more than 10% of starting balance in one contract
    max_race=0.08,        # ...or 8% in one race (e.g. Dem YES + Rep NO on the same race)
    sure_level=0.90,      # "near-certain" contract: favorite priced >= 90% at fair value. These rarely
    sure_edge=0.05,       # converge before settlement (favorite-longshot bias), so they need a 5-pt edge
    max_sure=0.20,        # and at most 20% of starting balance in them in total
    max_spend=0.30,       # never spend more than 30% of cash in a single run
    min_cash=0.15,        # stop buying once cash is below 15% of starting balance (keeps room for
                          # arbs and new edges; sells still happen). The arb step may use this reserve.
    hold_edge=None,       # always exit once the gap closes, freeing cash for the next edge
    longshot=False,
    max_positions=None,
)
# --aggressive: fewer, bigger, held-to-settlement bets. Built for top-3-or-nothing payouts.
AGGRESSIVE = dict(
    entry_edge=0.03,
    exit_band=0.01,
    take_profit=None,
    kelly=1.0,
    max_frac=0.20,        # up to 20% of starting balance in one contract
    max_race=0.25,
    sure_level=0.90,
    sure_edge=0.03,
    max_sure=1.0,
    max_spend=0.50,
    min_cash=0.05,
    hold_edge=0.10,       # if bought >= 10 points below fair, hold to settlement instead of exiting
    longshot=True,        # also buy cheap contracts (<= 0.25) where fair is >= 1.5x the price
    max_positions=10,     # concentrate: at most 10 open mapped positions
)
LONGSHOT_MAX_PRICE = 0.25
LONGSHOT_MIN_RATIO = 1.5
LONGSHOT_MIN_EDGE = 0.02


def wants_entry(price, fair, cfg, sure=False):
    """Is buying at `price` (YES or NO) good enough, given that side's fair value?
    Near-certain contracts (`sure`) need the bigger sure_edge, since that money is tied up longer."""
    if price is None or price <= 0:
        return False
    if fair - price >= (cfg["sure_edge"] if sure else cfg["entry_edge"]):
        return True
    return (cfg["longshot"] and price <= LONGSHOT_MAX_PRICE
            and fair / price >= LONGSHOT_MIN_RATIO and fair - price >= LONGSHOT_MIN_EDGE)


def kelly_fraction(price, fair):
    """Share of bankroll that maximises long-run growth when buying a 0/1 contract at `price`
    that pays 1 with probability `fair`: (p - price) / (1 - price)."""
    return max(0.0, (fair - price) / (1 - price)) if price < 1 else 0.0


def run(cup, t, live, cfg, max_orders=0, locked=None):
    """One gap-trading pass. `locked` = {exchange_id: signed shares} owned by arb pairs (see arb.py):
    those shares are never sold here, and we never buy the opposite side of a locked contract
    (that would net against the pair instead of opening a position)."""
    locked = locked or {}
    slug, tid = t["slug"], t["id"]
    t = cup.tournament(slug)                       # refresh balance
    cash, start = t["myBalance"], t["initialBalance"]
    mapping = {k: v for k, v in load_mapping().items() if v.get("kalshi_ticker") or v.get("polymarket_slug")}
    if not mapping:
        sys.exit("No mapped contracts. Run `markets`, then fill in mapping.csv.")

    portfolio = cup.positions(slug)
    pos = {p["exchangeId"]: p for p in portfolio["positions"] if p["quantity"] and not p["settled"]}
    equity = cash + portfolio["summary"]["totalMarketValue"]     # Kelly bankroll
    quotes = cup.prices(tid, mapping.keys())
    ext = fetch_external(mapping.values())
    budget_run = max(0.0, min(cash * cfg["max_spend"], cash - start * cfg["min_cash"]))
    cap = start * cfg["max_frac"]
    race_cap = start * cfg["max_race"]
    actions, candidates = [], []
    sold_cost = {}                                 # exchange_id -> cost of the gap shares we're selling
    sure_cost = 0.0                                # gap money in near-certain contracts

    print(f"\n{datetime.now():%H:%M:%S}  cash {cash:,.0f}  equity {equity:,.0f} {t['currencyName']}"
          f"  |  {len(mapping)} mapped contracts")
    print(f"{'contract':52s} {'bid':>6s} {'ask':>6s} {'fair':>6s}  signal   (quiet contracts not shown)")
    quiet = 0
    for ex_id, row in mapping.items():
        q = quotes.get(ex_id)
        if not q:
            continue
        bid, ask = q.get("bestBid"), q.get("bestAsk")
        fair, detail = fair_value(row, ext)
        name = f"{row['market_title'][:40]} [{row['option'][:8]}]"
        if fair is None:
            print(f"{name:52s} {'':>6s} {'':>6s} {'':>6s}  skip ({detail})")
            continue
        p = pos.get(ex_id)
        total = p["quantity"] if p else 0          # + = YES shares, - = NO shares
        lock = locked.get(ex_id, 0)
        if lock * total <= 0:                      # ledger says a side we don't hold (stale ledger):
            lock = 0 if total == 0 or lock == 0 else total   # never let it invent opposite-side shares
        elif abs(lock) > abs(total):
            lock = total
        held = total - lock                        # the gap strategy's own shares
        held_cost = abs(p["costBasis"]) * abs(held) / abs(total) if total else 0
        paid = held_cost / abs(held) if held else 0  # per-share price paid for the side we hold
        sure = max(fair, 1 - fair) >= cfg["sure_level"]
        if sure:
            sure_cost += held_cost
        no_bid = 1 - ask if ask is not None else None  # what we can sell NO for
        no_ask = 1 - bid if bid is not None else None  # what we pay for NO
        signal = ""

        # exits first: has the gap closed (or flipped)?
        tp = cfg["take_profit"]
        if held > 0 and bid is not None and (bid >= fair - cfg["exit_band"] or (tp and bid >= paid + tp)):
            if cfg["hold_edge"] and fair - paid >= cfg["hold_edge"] and bid < fair + cfg["entry_edge"]:
                signal = f"HOLD to settlement (paid {paid:.3f}, fair {fair:.3f})"
            else:
                why = "converged" if bid >= fair - cfg["exit_band"] else f"take profit {bid - paid:+.3f}"
                signal = f"SELL {held} YES @ {bid:.3f} ({why})"
                actions.append((ex_id, "yes", "sell", held, bid))
                sold_cost[ex_id] = held_cost
                if sure:
                    sure_cost -= held_cost
        elif held < 0 and no_bid is not None and (no_bid >= (1 - fair) - cfg["exit_band"] or (tp and no_bid >= paid + tp)):
            if cfg["hold_edge"] and (1 - fair) - paid >= cfg["hold_edge"] and no_bid < (1 - fair) + cfg["entry_edge"]:
                signal = f"HOLD NO to settlement (paid {paid:.3f}, fair {1 - fair:.3f})"
            else:
                why = "converged" if no_bid >= (1 - fair) - cfg["exit_band"] else f"take profit {no_bid - paid:+.3f}"
                signal = f"SELL {-held} NO @ {no_bid:.3f} ({why})"
                actions.append((ex_id, "no", "sell", -held, no_bid))
                sold_cost[ex_id] = held_cost
                if sure:
                    sure_cost -= held_cost

        # entries: contest cheaper than fair value -- sized after the loop, best first
        elif held >= 0 and lock >= 0 and wants_entry(ask, fair, cfg, sure):
            candidates.append(((fair - ask) / ask, ex_id, "yes", ask, fair, held_cost, held != 0, name, sure))
            signal = f"edge YES {fair - ask:+.3f} ({(fair - ask) / ask:+.0%} return)"
        elif held <= 0 and lock <= 0 and wants_entry(no_ask, 1 - fair, cfg, sure):
            candidates.append((((1 - fair) - no_ask) / no_ask, ex_id, "no", no_ask, 1 - fair, held_cost, held != 0, name, sure))
            signal = f"edge NO {(1 - fair) - no_ask:+.3f} ({((1 - fair) - no_ask) / no_ask:+.0%} return)"

        if not signal:
            quiet += 1
            continue
        fmt = lambda x: f"{x:.3f}" if x is not None else "  -  "
        print(f"{name:52s} {fmt(bid):>6s} {fmt(ask):>6s} {fair:6.3f}  {signal}")
    print(f"({quiet} contracts priced within the bands: nothing to do)")

    # size entries: highest expected return per coin first, until budget / position limit runs out
    if candidates:
        open_count = sum(1 for k in pos if k in mapping) - sum(1 for a in actions if a[2] == "sell")
        race_cost = {}                               # money in each race (gap + arb shares, both parties)
        for k, p in pos.items():
            race = race_key(p["marketTitle"])
            race_cost[race] = race_cost.get(race, 0) + abs(p["costBasis"]) - sold_cost.get(k, 0)
        print(f"\nEntries (best expected return first; buying budget {budget_run:,.0f} after the "
              f"{cfg['min_cash']:.0%} cash reserve):")
        for ret, ex_id, side, price, fair_side, held_cost, already_held, name, sure in sorted(candidates, reverse=True):
            if cfg["max_positions"] and not already_held and open_count >= cfg["max_positions"]:
                print(f"  {name:52s} skip (already at {cfg['max_positions']} positions)")
                continue
            race = race_key(mapping[ex_id]["market_title"])
            k = kelly_fraction(price, fair_side)
            target = min(cfg["kelly"] * k * equity, cap)  # what we'd like to hold in this contract
            room = min(target - held_cost, race_cap - race_cost.get(race, 0), budget_run)
            if sure:
                room = min(room, start * cfg["max_sure"] - sure_cost)
            qty = math.floor(room / price) if room > 0 else 0
            if qty <= 0:
                print(f"  {name:52s} skip (already at Kelly size / race cap / near-certain cap / run budget)")
                continue
            print(f"  {name:52s} BUY {qty} {side.upper()} @ {price:.3f}  "
                  f"({ret:+.0%} expected, Kelly {k:.0%} x {cfg['kelly']:g})")
            actions.append((ex_id, side, "buy", qty, price))
            budget_run -= qty * price
            race_cost[race] = race_cost.get(race, 0) + qty * price
            if sure:
                sure_cost += qty * price
            if not already_held:
                open_count += 1

    # internal arbitrage the platform itself detects (e.g. Dem + Rep prices not summing to 1)
    try:
        viol = cup.violations(tid)
        if viol:
            print(f"\n{len(viol)} price-constraint violation(s) inside the contest:")
            for v in viol[:15]:
                print(f"  - {v['type']} off by {v['violationAmount']:.3f}: {v['reason'][:90]}")
                for s in v.get("suggestedCorrectiveTrades", []):
                    print(f"      suggest {s['action']} {s['outcomeSide']} on {s.get('marketTitle')} "
                          f"[{s.get('outcome')}] @ {s.get('currentPrice')}")
    except Exception as e:
        print(f"  (constraint check failed: {e})")

    if not actions:
        print("\nNo trades this round.")
        return
    if max_orders and len(actions) > max_orders:     # exits come first in `actions`, then best entries
        print(f"\n--max-orders {max_orders}: keeping the first {max_orders} of {len(actions)} trades")
        actions = actions[:max_orders]
    if not live:
        print(f"\nDry run: {len(actions)} trade(s) would be placed. Use `trade` to execute.")
        return
    for ex_id, side, action, qty, price in actions:
        try:
            r = cup.order(tid, ex_id, side, action, qty, price)
            print(f"  placed {action} {qty} {side} on {ex_id} @ {round_tick(price):.3f} "
                  f"-> filled {r.get('quantityTraded')}, resting={r.get('open')}")
        except Exception as e:
            print(f"  ! order failed on {ex_id}: {e}")
