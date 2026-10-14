"""
Fair-value gap trading. Prices every contest contract against the real-money markets (Kalshi /
Polymarket mid = fair value), buys when the contest is cheap (5+ points), sells when it converges, or
earlier ("rotation") when the money would earn more in the arb. Sized with fractional Kelly under
per-contract, per-race, near-certain and cash limits.
Called once per cycle from main.py (was cup_bot.py).
"""
import math, re, sys
from datetime import datetime, timezone

from bot.common import fair_value, fetch_external, load_mapping, race_key, round_tick
from bot.strategies import mm


# Default profile: steady edge capture, aiming for a top 5-10% finish with low variance.
# Bet size = kelly x Kelly fraction of equity, then clipped by max_frac and max_race.
SAFE = dict(
    entry_edge=0.05,      # buy only if contest price is >= 5 points better than fair value: only the big
                          # edges; smaller ones are left so the cash goes to the arb (~2.5%/day, low risk)
    exit_band=0.01,       # sell when the gap has shrunk to within 1 point of fair value
    take_profit=None,     # e.g. 0.02 = also sell once 2 pts above cost. Off: gap trades are held for the
                          # full convergence (or settlement); the arb supplies the volume
    kelly=0.25,           # quarter-Kelly: fair values are estimates; halves the swings, keeps most growth
    max_frac=0.10,        # never hold more than 10% of starting balance in one contract
    max_race=0.08,        # ...or 8% in one race (e.g. Dem YES + Rep NO on the same race)
    sure_level=0.90,      # "near-certain" contract: favorite priced >= 90% at fair value. These rarely
    sure_edge=0.05,       # converge before settlement (favorite-longshot bias), so they need a 5-pt edge
    max_sure=0.20,        # and at most 20% of starting balance in them in total
    max_spend=0.30,       # never spend more than 30% of cash in a single run
    min_cash=0.02,        # stop buying once cash is below 2% of starting balance (sells still happen): kept
                          # for arb repairs. Raise to 0.07 when the market maker (mm.py) trades live, to
                          # leave it its 5,000 (Oct 11-14 it was at 0.07 while mm.py only ran in shadow).
                          # The gap step runs before the arb each cycle, so it gets first claim on cash.
    hold_edge=None,       # always exit once the gap closes, freeing cash for the next edge
    longshot=False,
    max_positions=None,
    # Rotation: sell a gap position early when its money earns more in the arb. Only ever at or above
    # what we paid, only while arbs are waiting for cash, at most one per cycle, and:
    rotate=True,
    rotate_haircut=0.5,       # holding must earn less per day than HALF the arb's measured daily return
                              # (extra money goes to the arb's thinner pairs, so it earns less than average)
    rotate_min_arb=0.005,     # and the arb must have measured >= 0.5%/day: if arbs slow down, no rotation
    rotate_min_days_left=7,   # never in the final week: near-certain positions are about to pay out
    rotate_min_age=7,         # competitive positions: only after 7 days held (share-weighted), and only if we
                              # wouldn't buy them today (edge at the ask below entry_edge). Tried 3 days on
                              # Oct 8-10: it moved $41k in two days, filled the arb's 75% cap, and the extra
                              # arb money then earned ~0.2%/day vs the ~$3k of fair-value edge given up
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
    rotate=False,
)
LONGSHOT_MAX_PRICE = 0.25
LONGSHOT_MIN_RATIO = 1.5
LONGSHOT_MIN_EDGE = 0.02


def wants_entry(price, fair, cfg, sure=False, bar=None, days_left=0):
    """Is buying at `price` (YES or NO) good enough, given that side's fair value?
    Near-certain contracts (`sure`) need sure_edge AND, while rotation is on (`bar` = rotation_bar()),
    must earn more per day held to settlement than the arb: they rarely converge early, so otherwise
    the rotation rule would sell them right back as soon as the price allowed."""
    if price is None or price <= 0:
        return False
    if sure and bar is not None and (fair - price) / price / days_left < bar:
        return False
    if fair - price >= (cfg["sure_edge"] if sure else cfg["entry_edge"]):
        return True
    return (cfg["longshot"] and price <= LONGSHOT_MAX_PRICE
            and fair / price >= LONGSHOT_MIN_RATIO and fair - price >= LONGSHOT_MIN_EDGE)


def _parse_ts(s):
    """'2026-10-04T19:11:18.7351446+00:00' -> aware datetime (Python 3.9's fromisoformat wants exactly
    3 or 6 fraction digits, the API sends anywhere from 1 to 7, so pad/trim to 6)."""
    return datetime.fromisoformat(re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"),
                                         s.replace("Z", "+00:00")))


def held_days(position):
    """How long we've held a position: quantity-weighted age of its open lots, in days."""
    lots = position.get("lots") or []
    total = sum(l["quantity"] for l in lots)
    if not total:
        return None
    now = datetime.now(timezone.utc)
    return sum(l["quantity"] * (now - _parse_ts(l["openedAt"])).total_seconds() for l in lots) / total / 86400


def rotation_bar(cfg, arb_info, days_left):
    """Daily return a gap position must beat to keep its money out of the arb, or None while rotation
    is off: disabled, no arb data yet, final week, no arbs waiting for cash, or the arb earning too little."""
    if not (cfg.get("rotate") and arb_info) or days_left <= cfg["rotate_min_days_left"]:
        return None
    if arb_info["unfunded"] <= 0 or arb_info["rate"] < cfg["rotate_min_arb"]:
        return None
    return arb_info["rate"] * cfg["rotate_haircut"]


def rotation_reason(cfg, bar, position, held, paid, sell_px, buy_px, side_fair, sure, days_left):
    """Why this gap position should be sold to move its money into the arb, or None to keep it.
    `bar` = rotation_bar() (None = rotation off)."""
    if bar is None or not held or sell_px is None or sell_px < paid:
        return None                                # never at a loss
    hold_daily = (side_fair - sell_px) / sell_px / days_left    # what staying earns per day (to settlement)
    if hold_daily >= bar:
        return None
    if not sure:
        age = held_days(position)
        if age is None or age < cfg["rotate_min_age"]:
            return None                            # still has time to converge
        if buy_px is not None and side_fair - buy_px >= cfg["entry_edge"]:
            return None                            # we'd buy it today: still a top gap trade
    return f"holding earns {hold_daily:.2%}/day < arb {bar:.2%}/day"


def kelly_fraction(price, fair):
    """Share of bankroll that maximises long-run growth when buying a 0/1 contract at `price`
    that pays 1 with probability `fair`: (p - price) / (1 - price)."""
    return max(0.0, (fair - price) / (1 - price)) if price < 1 else 0.0


def run(cup, t, live, cfg, max_orders=0, locked=None, arb_info=None, reserved=0.0):
    """One gap-trading pass. `locked` = {exchange_id: signed shares} owned by arb pairs (see arb.py):
    those shares are never sold here, and we never buy the opposite side of a locked contract
    (that would net against the pair instead of opening a position). `arb_info` = the arb's measured
    return and unfunded demand (arb.rotation_info()), for the rotation rule; None disables rotation.
    `reserved` = cash the arb freed and keeps for itself (arb.reserved_cash()): never spent here."""
    locked = locked or {}
    slug, tid = t["slug"], t["id"]
    t = cup.tournament(slug)                       # refresh balance
    cash, start = t["myBalance"], t["initialBalance"]
    mm_races = mm.excluded_races()                 # the market maker's races: hands off
    mapping = {k: v for k, v in load_mapping().items() if (v.get("kalshi_ticker") or v.get("polymarket_slug"))
               and race_key(v["market_title"]) not in mm_races}
    if not mapping:
        sys.exit("No mapped contracts. Run `markets`, then fill in mapping.csv.")

    portfolio = cup.positions(slug)
    pos = {p["exchangeId"]: p for p in portfolio["positions"] if p["quantity"] and not p["settled"]}
    equity = cash + portfolio["summary"]["totalMarketValue"]     # Kelly bankroll
    quotes = cup.prices(tid, mapping.keys())
    ext = fetch_external(mapping.values())
    budget_run = max(0.0, min(cash * cfg["max_spend"], cash - start * cfg["min_cash"] - reserved))
    cap = start * cfg["max_frac"]
    race_cap = start * cfg["max_race"]
    actions, candidates = [], []
    sold_cost = {}                                 # exchange_id -> cost of the gap shares we're selling
    sale_info = {}                                 # exchange_id -> (title, paid, reason), for reporting sales
    sure_cost = 0.0                                # gap money in near-certain contracts
    days_left = ((_parse_ts(t["endDate"]) - datetime.now(timezone.utc)).total_seconds() / 86400
                 if t.get("endDate") else 0)       # unknown end date -> 0 -> no rotation
    rotated = False                                # at most one rotation per cycle
    bar = rotation_bar(cfg, arb_info, days_left)   # arb's daily return to beat (None = rotation off)

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
        rotate_why = None if rotated or not held else rotation_reason(
            cfg, bar, p, held, paid, bid if held > 0 else no_bid, ask if held > 0 else no_ask,
            fair if held > 0 else 1 - fair, sure, days_left)

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
                sale_info[ex_id] = (row["market_title"], paid, why)
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
                sale_info[ex_id] = (row["market_title"], paid, why)
                if sure:
                    sure_cost -= held_cost

        # rotation: its money earns more in the arb (only at or above cost; see rotation_reason)
        elif rotate_why:
            side, qty, px = ("yes", held, bid) if held > 0 else ("no", -held, no_bid)
            signal = f"ROTATE: SELL {qty} {side.upper()} @ {px:.3f} ({rotate_why})"
            actions.append((ex_id, side, "sell", qty, px))
            sold_cost[ex_id] = held_cost
            sale_info[ex_id] = (row["market_title"], paid, "rotation: " + rotate_why)
            if sure:
                sure_cost -= held_cost
            rotated = True

        # entries: contest cheaper than fair value -- sized after the loop, best first
        elif held >= 0 and lock >= 0 and wants_entry(ask, fair, cfg, sure, bar, days_left):
            candidates.append(((fair - ask) / ask, ex_id, "yes", ask, fair, held_cost, held != 0, name, sure))
            signal = f"edge YES {fair - ask:+.3f} ({(fair - ask) / ask:+.0%} return)"
        elif held <= 0 and lock <= 0 and wants_entry(no_ask, 1 - fair, cfg, sure, bar, days_left):
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
              f"{cfg['min_cash']:.0%} cash reserve" + (f" and {reserved:,.0f} kept for the arb" if reserved >= 1 else "")
              + "):")
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
        return []
    if max_orders and len(actions) > max_orders:     # exits come first in `actions`, then best entries
        print(f"\n--max-orders {max_orders}: keeping the first {max_orders} of {len(actions)} trades")
        actions = actions[:max_orders]
    if not live:
        print(f"\nDry run: {len(actions)} trade(s) would be placed. Use `trade` to execute.")
        return []
    sold = []                                      # what got sold this pass, for the caller to report
    for ex_id, side, action, qty, price in actions:
        try:
            r = cup.order(tid, ex_id, side, action, qty, price)
            print(f"  placed {action} {qty} {side} on {ex_id} @ {round_tick(price):.3f} "
                  f"-> filled {r.get('quantityTraded')}, resting={r.get('open')}")
            filled = int(r.get("quantityTraded") or 0)
            if action == "sell" and filled and ex_id in sale_info:
                title, paid, why = sale_info[ex_id]
                sold.append({"title": title, "side": side.upper(), "shares": filled, "price": round_tick(price),
                             "paid": paid, "profit": filled * (round_tick(price) - paid), "why": why,
                             "waiting": qty - filled if r.get("open") else 0})
        except Exception as e:
            print(f"  ! order failed on {ex_id}: {e}")
    return sold
