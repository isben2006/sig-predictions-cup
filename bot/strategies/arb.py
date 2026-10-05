"""
Predictions Cup D + R arbitrage bot.

In a race exactly one of Democrat / Republican wins (barring a third party), so inside the contest:
  YES(D) + YES(R) always pays 1    -> buy both when their asks sum to < 1
  NO(D)  + NO(R)  always pays >= 1 -> buy both when their bids sum to > 1  (a 3rd-party win pays 2)
The profit is locked in when a pair is bought. A pair is sold early (both legs together) when selling
it now keeps at least half of that profit, which frees the cash for the next arb; otherwise it's held
to settlement.

YES+YES pairs are only bought where Kalshi gives candidates other than D and R <= 2% combined
(max_third_party), and never where Kalshi has no such data. NO+NO pairs need no such filter.
Both strategies share races: the share ledger (arb_races.json) records which shares belong to pairs,
and gap.py never sells them or buys the opposite side.

Called once per cycle from main.py (`main.py trade --arb`).
"""
import json, math, time

import requests

import os

from bot.common import (DATA_DIR, KALSHI_EVENT_URL, ORDER_TTL_SECONDS, TICK, _num, kalshi_event, load_mapping,
                        party_of, race_key)

ARB = dict(
    min_profit=0.01,      # lock in at least 1 point per pair (pair costs <= 0.99)
    max_third_party=0.02, # YES+YES only in races where Kalshi gives non-D/R candidates <= 2% combined
                          # (Oct 2026: SD and ID Senate looked like 5-pt arbs but had independents at ~7.5%).
                          # NO+NO needs no filter: a third-party win makes both NO legs pay.
    yes_third_frac=0.5,   # YES+YES loses everything if a third party wins, so it also needs that chance
                          # to be <= half the locked profit (1-pt pair -> third parties <= 0.5%)
    yes_both=True,        # YES+YES pays 0 if a third party wins; never used in races with a 3rd contract
    max_race=0.05,        # at most 5% of starting balance in one race's pair
    max_total=0.75,       # at most 75% of starting balance in arb pairs overall: the volume engine
    keep_cash=0.02,       # never spend the last 2% of starting balance: cash for completing one-sided
                          # fills (repairs aren't budget-limited). Raise it before election night.
    max_cash=0.50,        # never use more than 50% of current cash in one run
    unwind_keep=0.5,      # sell a pair early if that keeps >= 50% of its settlement profit
)
STATE_FILE = os.path.join(DATA_DIR, "arb_races.json")   # race -> {"side": "yes"|"no", "legs": {exchange_id: signed shares we own},
                                #          "unwind": {party: min sell price}} (only while a sale is part-done)
STATS_FILE = os.path.join(DATA_DIR, "arb_stats.json")   # recent locked-in profit + unfunded demand,
                                # read by gap.py to decide whether moving money into the arb pays
MAX_REPAIR_PAIR = 1.0025        # completing a broken pair may cost up to 1/4 point per pair: a tiny
                                # certain loss beats holding an unhedged leg (avg cost isn't exactly on tick)


_third_cache = {}                # race -> (fetched_at, chance or None)


def third_party_chance(race):
    """Kalshi's probability that neither the D nor the R candidate wins (sum of the other candidates'
    mid prices). None if Kalshi has no such event or doesn't list both a D and an R. Cached for an hour."""
    hit = _third_cache.get(race)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    chance, ev = None, kalshi_event(race)
    if ev:
        try:
            r = requests.get(KALSHI_EVENT_URL.format(ev), params={"with_nested_markets": "true"}, timeout=30)
            if r.ok:
                j = r.json()
                markets = j.get("markets") or j["event"].get("markets") or []
                if {"D", "R"} <= {m["ticker"].rsplit("-", 1)[-1] for m in markets}:
                    chance = 0.0
                    for m in markets:
                        if m["ticker"].rsplit("-", 1)[-1] not in ("D", "R"):
                            bid, ask = _num(m.get("yes_bid_dollars")) or 0, _num(m.get("yes_ask_dollars")) or 0
                            chance += (bid + ask) / 2 if ask else _num(m.get("last_price_dollars")) or 0
        except Exception as e:
            print(f"  ! Kalshi {ev}: {e}")
    _third_cache[race] = (time.time(), chance)
    return chance


def load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (OSError, ValueError):
        return {}
    return {r: ({"side": s} if isinstance(s, str) else s) for r, s in state.items()}   # old format: "yes"


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)


def load_stats():
    try:
        with open(STATS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"since": time.time(), "fills": []}


def save_stats(stats):
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f)


def rotation_info(window_h=48, min_history_h=12, max_age_s=900):
    """What the arb has actually been earning, for gap.py's rotation rule:
    {"rate": profit locked in per day as a share of arb capital (last `window_h` hours),
     "unfunded": arbs skipped for lack of cash in the latest pass}.
    None until there are `min_history_h` hours of fresh data: then gap.py never rotates."""
    s = load_stats()
    now = time.time()
    if now - s.get("time", 0) > max_age_s or not s.get("capital"):
        return None
    span_h = min(window_h, (now - s["since"]) / 3600)
    if span_h < min_history_h:
        return None
    profit = sum(x for ts, x in s["fills"] if ts >= now - window_h * 3600)
    return {"rate": profit / s["capital"] / (span_h / 24), "unfunded": s.get("unfunded", 0)}


def ask_ladder(book, side):
    """[price, qty] levels we can BUY at, cheapest first. Buying NO = hitting a YES bid at 1 - bid."""
    if side == "yes":
        return [[l["price"], l["quantity"]] for l in book["asks"]]
    return [[round(1 - l["price"], 3), l["quantity"]] for l in book["bids"]]


def bid_ladder(book, side):
    """[price, qty] levels we can SELL at, best first. Selling NO = filling a YES ask at 1 - ask."""
    if side == "yes":
        return [[l["price"], l["quantity"]] for l in book["bids"]]
    return [[round(1 - l["price"], 3), l["quantity"]] for l in book["asks"]]


def pair_depth(a, b, limit, selling=False):
    """Walk two ladders (best level first) together while one more pair still costs <= limit
    (buying) or still sells for >= limit (selling). Returns (pairs, limit_a, limit_b, total).
    The limits are the worst levels used, so any fill at them or better keeps every pair within limit."""
    ok = (lambda s: s >= limit - 1e-9) if selling else (lambda s: s <= limit + 1e-9)
    a, b = [l[:] for l in a], [l[:] for l in b]
    i = j = 0
    qty, cost, lim_a, lim_b = 0, 0.0, None, None
    while i < len(a) and j < len(b) and ok(a[i][0] + b[j][0]):
        n = min(a[i][1], b[j][1])
        qty, cost = qty + n, cost + n * (a[i][0] + b[j][0])
        lim_a, lim_b = a[i][0], b[j][0]
        a[i][1] -= n
        b[j][1] -= n
        i += a[i][1] <= 0
        j += b[j][1] <= 0
    return int(qty), lim_a, lim_b, cost


def cancel_rest(cup, order):
    """Cancel whatever part of an order didn't fill immediately, so it can't fill later unhedged.
    Returns the exact number of shares the order filled (re-read after the cancel, since a fill can
    land between the order's response and the cancel)."""
    filled = int(order.get("quantityTraded") or 0)
    if order.get("open") and order.get("orderId"):
        try:
            cup.cancel(order["orderId"])
        except Exception as e:
            print(f"    ! cancel {order['orderId']} failed ({e}); it expires in {ORDER_TTL_SECONDS}s anyway")
        try:
            filled = int(cup.order_fills(order["orderId"])["totalQuantityFilled"])
        except Exception as e:
            print(f"    ! couldn't re-read fills of {order['orderId']} ({e}); using {filled}")
    return filled


def locked_shares():
    """{exchange_id: signed shares (+YES / -NO)} that belong to arb pairs. The gap strategy must
    neither sell these nor buy the opposite side of these contracts."""
    out = {}
    for race, st in load_state().items():
        if "legs" not in st:                         # fail safe: never let the gap step guess
            raise RuntimeError(f"arb state for {race} has no share ledger yet; run an arb pass first")
        for ex, q in st["legs"].items():
            out[ex] = out.get(ex, 0) + q
    return out


def run(cup, t, live, cfg, max_pairs=0, shared=False):
    """One arbitrage pass. shared=False: stay out of every race mapped in mapping.csv (the gap bot
    runs separately). shared=True: trade every race; gap.py runs right after, using locked_shares()."""
    slug, tid = t["slug"], t["id"]
    t = cup.tournament(slug)                         # refresh balance
    cash, start = t["myBalance"], t["initialBalance"]
    rows = load_mapping()
    races = {}                                       # race -> {party: exchange_id}
    for ex_id, r in rows.items():
        if party_of(r["market_title"]):
            races.setdefault(race_key(r["market_title"]), {})[party_of(r["market_title"])] = ex_id
    gap_races = set() if shared else {race_key(r["market_title"]) for r in rows.values()
                                      if r.get("kalshi_ticker") or r.get("polymarket_slug")}
    pos = {p["exchangeId"]: p for p in cup.positions(slug)["positions"] if p["quantity"] and not p["settled"]}
    state = {r: s for r, s in load_state().items() if r in races}
    qty_of = lambda ex: pos[ex]["quantity"] if ex in pos else 0

    # the ledger: which shares are ours. Older state files have no "legs": back then the arb owned
    # every share in its races. Never claim more than the account actually holds on that side.
    for race, st in state.items():
        legs = st.setdefault("legs", {e: qty_of(e) for e in (races[race]["Democratic"], races[race]["Republican"])})
        for ex, q in legs.items():
            p = qty_of(ex)
            legs[ex] = min(q, max(p, 0)) if q > 0 else max(q, min(p, 0)) if q < 0 else 0

    def held(race, ex):
        return abs(state[race]["legs"].get(ex, 0))

    def leg_cost(race, ex):
        q = held(race, ex)
        return abs(pos[ex]["costBasis"]) * q / abs(pos[ex]["quantity"]) if q and ex in pos else 0

    def book(race, ex, side, filled, action):     # record a fill in the ledger
        sign = 1 if side == "yes" else -1
        legs = state[race]["legs"]
        legs[ex] = legs.get(ex, 0) + (sign if action == "buy" else -sign) * filled
        if live:
            save_state(state)                        # on disk at once, so the ledger is never stale

    def race_cost(race):                             # everything in the race, gap shares included
        return sum(abs(pos[e]["costBasis"]) for e in races[race].values() if e in pos)

    stats, unfunded, in_arbs = load_stats(), 0, 0.0
    try:                                             # save the ledger even if the pass fails midway
        in_arbs = sum(leg_cost(r, e) for r, st in state.items() for e in st["legs"])
        budget = min(cash * cfg["max_cash"], start * cfg["max_total"] - in_arbs, cash - start * cfg["keep_cash"])
        print(f"\n{time.strftime('%H:%M:%S')}  ARB  cash {cash:,.0f} {t['currencyName']}  |  "
              f"{in_arbs:,.0f} in {len(state)} pair(s)  |  budget this run {max(budget, 0):,.0f}")

        # 1. repair legs that filled unevenly (on a buy, or on an early sale)
        for race, st in list(state.items()):
            side, party_of_ex = st["side"], {v: k for k, v in races[race].items()}
            d, r = races[race]["Democratic"], races[race]["Republican"]
            qd, qr = held(race, d), held(race, r)
            if qd == qr:
                if qd == 0:
                    del state[race]                      # sold, settled, or never filled: forget it
                else:
                    st.pop("unwind", None)               # balanced again
                continue
            big, small, n = (d, r, qd - qr) if qd > qr else (r, d, qr - qd)
            # mid-sale: finish selling the leftover leg at (about) the price the sale was planned at
            if "unwind" in st:
                ladder = bid_ladder(cup.orderbook(tid, big), side)
                floor = st["unwind"][party_of_ex[big]] - TICK
                if ladder and ladder[0][0] >= floor - 1e-9:
                    print(f"  {race:28s} FINISH SALE: sell {int(n)} {side.upper()} on the leftover leg @ {ladder[0][0]:.3f}")
                    if live:
                        f = cancel_rest(cup, cup.order(tid, big, side, "sell", int(n), ladder[0][0]))
                        book(race, big, side, f, "sell")
                        print(f"    filled {f}")
                    continue
            # otherwise re-pair: buy the missing shares, as long as the pair still (about) breaks even
            paid = leg_cost(race, big) / held(race, big)
            ladder = ask_ladder(cup.orderbook(tid, small), side)
            if not ladder or paid + ladder[0][0] > MAX_REPAIR_PAIR:
                print(f"  ! {race}: {int(n)} {side.upper()} shares unhedged; missing leg costs "
                      f"{ladder[0][0] if ladder else '-'} vs {paid:.3f} paid -- waiting for a better price")
                continue
            print(f"  {race:28s} REPAIR: buy {int(n)} {side.upper()} on the missing leg @ {ladder[0][0]:.3f} "
                  f"(pair {paid + ladder[0][0]:.3f})")
            if live:
                f = cancel_rest(cup, cup.order(tid, small, side, "buy", int(n), ladder[0][0]))
                book(race, small, side, f, "buy")
                print(f"    filled {f}")

        # 2. sell pairs early when that keeps >= unwind_keep of the settlement profit, freeing the cash
        for race, st in list(state.items()):
            side = st["side"]
            d, r = races[race]["Democratic"], races[race]["Republican"]
            q = held(race, d)
            if q == 0 or q != held(race, r) or "unwind" in st:
                continue
            paid = (leg_cost(race, d) + leg_cost(race, r)) / q                  # cost per pair
            target = paid + max(cfg["unwind_keep"] * (1 - paid), TICK)         # sell only at or above this
            n, lim_d, lim_r, got = pair_depth(bid_ladder(cup.orderbook(tid, d), side),
                                              bid_ladder(cup.orderbook(tid, r), side), target, selling=True)
            if n <= 0:
                continue
            avg, n = got / n, min(n, q)                  # avg over the walked depth (an upper-bound estimate)
            print(f"  {race:28s} SELL {n} {side.upper()} pairs: D @>= {lim_d:.3f}, R @>= {lim_r:.3f}  "
                  f"(pair ~{avg:.3f} vs {paid:.3f} paid: {avg - paid:+.3f}/pair, {n * (avg - paid):+,.0f} now "
                  f"instead of {n * (1 - paid):+,.0f} at settlement)")
            if not live:
                continue
            try:
                st["unwind"] = {"Democratic": lim_d, "Republican": lim_r}       # lets step 1 finish a part-fill
                res = cup.multi_leg(tid, [(d, side, "sell", n, lim_d), (r, side, "sell", n, lim_r)])
                fd, fr = (cancel_rest(cup, o) for o in res)
                book(race, d, side, fd, "sell")
                book(race, r, side, fr, "sell")
                print(f"    sold D {fd} / R {fr}" + ("" if fd == fr else "  (uneven: leftover leg sold next run)"))
                if fd == fr:
                    st.pop("unwind")
            except Exception as e:
                st.pop("unwind", None)
                print(f"  ! sell failed on {race}: {e}")

        # 3. spot new pairs from the bulk quotes (cheap), then size them from the real order books
        quotes = cup.prices(tid, [e for p in races.values() for e in p.values()])
        opps = []
        for race, parties in races.items():
            if race in gap_races or not {"Democratic", "Republican"} <= set(parties):
                continue
            d, r = parties["Democratic"], parties["Republican"]
            st = state.get(race, {})
            if "unwind" in st:
                continue                                 # mid-sale: don't buy back in
            # shares in this race that aren't ours (the gap strategy's, or your own manual trades)
            other = {e: qty_of(e) - st.get("legs", {}).get(e, 0) for e in (d, r)}
            if not shared and any(other.values()):
                continue
            qd, qr = quotes.get(d, {}), quotes.get(r, {})
            for side, sign, edge in (("yes", 1, (1 - qd["bestAsk"] - qr["bestAsk"]) if qd.get("bestAsk") and qr.get("bestAsk") else None),
                                     ("no", -1, (qd["bestBid"] + qr["bestBid"] - 1) if qd.get("bestBid") and qr.get("bestBid") else None)):
                if edge is None or st.get("side", side) != side or (side == "yes" and not (cfg["yes_both"] and len(parties) == 2)):
                    continue
                if any(q * sign < 0 for q in other.values()):
                    continue                             # buying would net against the opposite-side shares there
                opps.append((edge, race, side))
        opps = sorted((o for o in opps if o[0] >= cfg["min_profit"] - 1e-9), reverse=True)
        if not opps:
            print(f"  no D + R pairs locking in >= {cfg['min_profit']:.3f} right now")

        placed = 0
        for i, (profit, race, side) in enumerate(opps):
            if max_pairs and placed >= max_pairs:
                print(f"  --max-pairs {max_pairs} reached")
                break
            if budget <= 0:
                print("  arb budget used up")
                unfunded += len(opps) - i                # waiting for money (gap.py may rotate cash here)
                break
            # third parties only threaten YES+YES (both legs lose); a third-party win pays NO+NO twice
            third = third_party_chance(race) if side == "yes" else None
            if side == "yes" and (third is None or third > cfg["max_third_party"]):
                why = "no Kalshi odds for third parties" if third is None else f"third parties at {third:.1%} on Kalshi"
                print(f"  {race:28s} {side.upper()}+{side.upper()} {profit:+.3f}: skip ({why})")
                continue
            d, r = races[race]["Democratic"], races[race]["Republican"]
            qty, lim_d, lim_r, cost = pair_depth(ask_ladder(cup.orderbook(tid, d), side),
                                                 ask_ladder(cup.orderbook(tid, r), side), 1 - cfg["min_profit"])
            if qty <= 0:
                print(f"  {race:28s} {side.upper()}+{side.upper()}: gap not there in the order book")
                continue
            avg = cost / qty
            if side == "yes" and third > cfg["yes_third_frac"] * (1 - avg):
                print(f"  {race:28s} YES+YES locks {1 - avg:+.3f}/pair but third parties are at {third:.1%}: "
                      f"expected value too thin, skip")
                continue
            race_room = start * cfg["max_race"] - race_cost(race)
            room = min(budget, race_room)
            qty = min(qty, math.floor(room / (lim_d + lim_r))) if room > 0 else 0
            if qty <= 0:
                print(f"  {race:28s} {side.upper()}+{side.upper()} {profit:+.3f}: "
                      + ("at race cap" if race_room < budget else "no arb budget left this run"))
                unfunded += race_room >= budget
                continue
            print(f"  {race:28s} BUY {qty} {side.upper()} on D @<= {lim_d:.3f} and R @<= {lim_r:.3f}  "
                  f"(pair ~{avg:.3f}: locks {1 - avg:+.3f}/pair, {qty * (1 - avg):+,.0f} total)")
            budget -= qty * (lim_d + lim_r)
            placed += 1
            if not live:
                continue
            try:
                res = cup.multi_leg(tid, [(d, side, "buy", qty, lim_d), (r, side, "buy", qty, lim_r)])
                fd, fr = (cancel_rest(cup, o) for o in res)
                if fd or fr:
                    state.setdefault(race, {"side": side, "legs": {d: 0, r: 0}})
                    book(race, d, side, fd, "buy")
                    book(race, r, side, fr, "buy")
                    stats["fills"].append([time.time(), min(fd, fr) * (1 - avg)])   # profit locked in
                print(f"    filled D {fd} / R {fr}" + ("" if fd == fr else "  (uneven: repaired next run)"))
            except Exception as e:
                print(f"  ! arb order failed on {race}: {e}")

        if not live and opps:
            print("\nDry run: no orders placed. Use `trade` to execute.")
    finally:
        if live:
            save_state(state)
            stats.update(time=time.time(), capital=in_arbs, unfunded=unfunded,
                         fills=[f for f in stats["fills"] if f[0] >= time.time() - 72 * 3600])
            save_stats(stats)
