"""
Market-making pilot. Quotes both sides of a few liquid contest contracts whose price agrees with the
real-money markets: a bid (buy YES) at the best bid and an ask (buy NO at 1 - ask, i.e. sell YES) at the
best ask, earning the spread when both get filled. Runs as its own process (`main.py mm`, systemd
sig-mm) on the realtime feed (bot/realtime.py), so quotes follow the book within a second.

Safety first:
- quotes only while the book's mid is within `band` of fair value, and never on the wrong side of fair
  (no bid at or above it, no ask at or below it);
- every quote is pulled when fair value moves `pull_move` since it was placed, while the feed is
  unhealthy, and on exit; inventory and money are capped;
- the main loop (gap + arb) stays out of these races entirely (excluded_races()), so the account's
  position in a market-making contract is the market maker's inventory;
- `mode="shadow"` places nothing: it simulates queue position and fills from the live trade tape, so the
  pilot can be judged before any money is at risk.
Prices are YES prices throughout; inventory is signed YES shares (+ long YES, - long NO).
"""
import asyncio, json, os, signal, time, uuid
from datetime import datetime, timedelta, timezone

from bot.common import DATA_DIR, TICK, fair_value, fetch_external, load_mapping, race_key

MM = dict(
    mode="shadow",            # "shadow" = simulate from the live feed, "live" = place orders, None = off
    markets=["983", "960"],   # Texas Senate R, Maine Senate R: balanced two-sided flow, no gap/arb holdings
    size=300,                 # shares per quote
    max_inventory=1000,       # |net YES shares| per contract: stop adding beyond this
    max_capital=5000,         # money in inventory + resting bids, all contracts together
    band=0.02,                # quote only while the book's mid is within 2 pts of fair value (each side
                              # still only on the right side of fair, so a 2-pt gap quotes one side only)
    pull_move=0.01,           # pull a contract's quotes when its fair value moves this much since quoting
    fair_every=5,             # seconds between Kalshi/Polymarket fair-value polls
    writes_per_min=10,        # API write budget (the trading loop keeps 18 of the account's 30)
    order_ttl=600,            # live quotes expire after 10 minutes if we stop refreshing them
    min_resize=0.25,          # re-quote for a size change only if it's more than 25%
)
STATS_FILE = os.path.join(DATA_DIR, "mm_stats.json")


def excluded_races():
    """Races the trading loop must leave alone while the market maker is on (empty when it's off)."""
    if not MM["mode"]:
        return set()
    m = load_mapping()
    return {race_key(m[ex]["market_title"]) for ex in MM["markets"] if ex in m}


def desired_quotes(book, fair, inv, cfg=MM):
    """{"bid": (price, qty) or None, "ask": (price, qty) or None} for one contract."""
    bids, asks = book.get("bids") or [], book.get("asks") or []
    if fair is None or not bids or not asks:
        return {"bid": None, "ask": None}
    bb, ba = bids[0]["price"], asks[0]["price"]
    if abs((bb + ba) / 2 - fair) > cfg["band"] + 1e-9:
        return {"bid": None, "ask": None}             # the contest disagrees with fair: that's gap territory
    wide = ba - bb > 2 * TICK + 1e-9                  # 3+ ticks: step inside on both sides, first in line
    bid = round(bb + TICK, 3) if wide else bb         # (at 2 ticks that would put our bid on our ask)
    ask = round(ba - TICK, 3) if wide else ba
    room_buy, room_sell = cfg["max_inventory"] - inv, cfg["max_inventory"] + inv
    return {"bid": (bid, min(cfg["size"], room_buy)) if bid < fair - 1e-9 and room_buy > 0 else None,
            "ask": (ask, min(cfg["size"], room_sell)) if ask > fair + 1e-9 and room_sell > 0 else None}


def capital_used(inv, fair, resting):
    """Money tied up: inventory (long YES costs ~fair, long NO ~1-fair) plus what resting bids would cost."""
    held = inv * fair if inv > 0 else -inv * (1 - fair)
    return held + sum(v[1] * (v[0] if kind == "bid" else 1 - v[0]) for kind, v in resting.items() if v)


class Sim:
    """Shadow fills for one contract: our virtual orders sit behind whatever was already at their price
    (cancellations ahead move us up; newcomers join behind), and fill from the real trade tape."""

    def __init__(self):
        self.quotes = {"bid": None, "ask": None}       # kind -> {"price", "qty", "ahead"}
        self.inv, self.cash, self.fills, self.bought, self.sold = 0, 0.0, 0, 0, 0

    def place(self, kind, want, book):
        if want is None:
            self.quotes[kind] = None
            return
        price, qty = want
        level = book["bids"] if kind == "bid" else book["asks"]
        ahead = next((l["quantity"] for l in level if abs(l["price"] - price) < 1e-9), 0)
        self.quotes[kind] = {"price": price, "qty": qty, "ahead": ahead}

    def on_book(self, book):
        for kind, q in self.quotes.items():
            if q:
                level = book["bids"] if kind == "bid" else book["asks"]
                q["ahead"] = min(q["ahead"], next((l["quantity"] for l in level if abs(l["price"] - q["price"]) < 1e-9), 0))

    def on_trade(self, price, qty):
        """Returns (kind, shares, price) if the trade would have filled us, else None."""
        for kind in ("bid", "ask"):
            q = self.quotes[kind]
            if not q or q["qty"] <= 0:
                continue
            through = price < q["price"] - 1e-9 if kind == "bid" else price > q["price"] + 1e-9
            at = abs(price - q["price"]) < 1e-9
            if not (through or at):
                continue
            if at:                                      # same price: the queue ahead of us trades first
                used = min(q["ahead"], qty)
                q["ahead"] -= used
                qty -= used
            n = min(q["qty"], qty)
            if n <= 0:
                return None
            q["qty"] -= n
            self.fills += 1
            if kind == "bid":
                self.inv += n; self.cash -= n * q["price"]; self.bought += n
            else:
                self.inv -= n; self.cash += n * q["price"]; self.sold += n
            return kind, n, q["price"]
        return None


class TokenBucket:
    def __init__(self, per_min):
        self.rate, self.tokens, self.t = per_min / 60, float(per_min), time.time()
        self.cap = float(per_min)

    def take(self, n=1):
        now = time.time()
        self.tokens = min(self.cap, self.tokens + (now - self.t) * self.rate)
        self.t = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


class MarketMaker:
    def __init__(self, cup, t, cfg=MM):
        from bot.realtime import Feed
        self.cup, self.cfg, self.tid, self.slug, self.start = cup, cfg, t["id"], t["slug"], t["initialBalance"]
        self.live = cfg["mode"] == "live"
        self.ex = [str(e) for e in cfg["markets"]]
        mapping = load_mapping()
        self.rows = {e: mapping[e] for e in self.ex}
        quotes = cup.prices(self.tid, self.ex)
        self.names = {e: f'{race_key(self.rows[e]["market_title"])} {self.rows[e]["market_title"].split()[2][0]}' for e in self.ex}
        self.fair, self.quoted_fair = {}, {}
        self.sims = {e: Sim() for e in self.ex}
        self.resting = {e: {"bid": None, "ask": None} for e in self.ex}   # live/shadow quotes we have out
        self.inv = {e: 0 for e in self.ex}                                 # live inventory (shadow uses Sim)
        self.cash_room = cfg["max_capital"]
        self.writes = TokenBucket(cfg["writes_per_min"])
        self.dirty, self.stop, self._need_positions = set(), False, False
        self.feed = Feed(cup, self.tid, {e: quotes[e]["marketId"] for e in self.ex}, on_change=self._on_book,
                         on_trades=self._on_trades, on_fills=self._on_fills)
        self.t0 = time.time()

    # ---- feed callbacks
    def _on_book(self, ex):
        if ex in self.feed.books:
            self.sims[ex].on_book(self.feed.books[ex])
        self.dirty.add(ex)

    def _on_trades(self, trades):
        if self.live:
            return
        for tr in trades:
            ex, price = str(tr["exchangeId"]), tr.get("price")
            if price is None:
                continue
            hit = self.sims[ex].on_trade(price, int(tr.get("quantity") or 0))
            if hit:
                kind, n, p = hit
                s = self.sims[ex]
                print(f"{time.strftime('%H:%M:%S')}  SHADOW FILL {self.names[ex]:34s} {'bought' if kind == 'bid' else 'sold'} "
                      f"{n} YES @ {p:.3f} (fair {self.fair.get(ex, 0):.3f})  inventory {s.inv:+d}")
                self.dirty.add(ex)

    def _on_fills(self, fills):
        for f in fills:
            ex = str(f["exchangeId"])
            print(f"{time.strftime('%H:%M:%S')}  FILL {self.names.get(ex, ex)}: {f.get('quantity')} @ {f.get('price')} "
                  f"(order {f.get('orderId')})")
            self.dirty.add(ex)
        self._need_positions = True

    # ---- helpers
    def inventory(self, ex):
        return self.inv[ex] if self.live else self.sims[ex].inv

    async def rest(self, fn, *args, **kw):
        async with self.feed.rest_lock:
            return await asyncio.to_thread(lambda: fn(*args, **kw))

    async def refresh_account(self):
        """Live: inventory = the account's position (only the market maker trades these contracts), and
        the money it may use = cash above the 2% floor, capped by max_capital."""
        t = await self.rest(self.cup.tournament, self.slug)
        pos = await self.rest(self.cup.positions, self.slug)
        held = {p["exchangeId"]: p["quantity"] for p in pos["positions"] if p["quantity"] and not p["settled"]}
        self.inv = {e: held.get(e, 0) for e in self.ex}
        self.cash_room = max(0.0, t["myBalance"] - 0.02 * self.start)

    async def cancel(self, ex):
        """Pull every order we have in ONE contract (never call cancel-all without an exchange)."""
        await self.rest(self.cup.req, "POST", "/orders/cancel-all", body={"exchangeId": ex, "tournamentId": self.tid})
        self.resting[ex] = {"bid": None, "ask": None}

    async def place(self, ex, want):
        """Bid = buy YES at the bid; ask = buy NO at 1 - ask (sells YES we hold). Only accepted quotes count."""
        exp = (datetime.now(timezone.utc) + timedelta(seconds=self.cfg["order_ttl"])).strftime("%Y-%m-%dT%H:%M:%SZ")
        kinds, orders = [], []
        for kind, side in (("bid", "yes"), ("ask", "no")):
            if want[kind]:
                price = want[kind][0] if kind == "bid" else round(1 - want[kind][0], 3)
                kinds.append(kind)
                orders.append({"exchangeId": ex, "side": side, "action": "buy", "quantity": int(want[kind][1]),
                               "price": price, "tournamentId": self.tid, "expirationDate": exp})
        placed = {"bid": None, "ask": None}
        if orders:
            res = await self.rest(self.cup.req, "POST", "/orders/batch",
                                  body={"idempotencyKey": str(uuid.uuid4()), "orders": orders})
            results = res.get("results") or []
            for i, kind in enumerate(kinds):
                r = next((x for x in results if x.get("index") == i), results[i] if i < len(results) else {})
                if int(r.get("status") or 200) < 400:
                    placed[kind] = want[kind]
                else:
                    print(f"  ! {self.names[ex]}: {kind} rejected: {json.dumps(r)[:300]}")
        self.resting[ex] = placed

    # ---- the quoting decision for one contract
    async def requote(self, ex):
        book, fair = self.feed.books.get(ex), self.fair.get(ex)
        have = self.resting[ex]
        if not self.feed.healthy or book is None or fair is None:
            want = {"bid": None, "ask": None}
        else:
            want = desired_quotes(book, fair, self.inventory(ex), self.cfg)
            # money cap: drop the side that adds exposure first if we're over it
            others = sum(capital_used(self.inventory(e), self.fair.get(e) or 0.5,
                                      {k: v for k, v in self.resting[e].items() if v}) for e in self.ex if e != ex)
            room = min(self.cfg["max_capital"], self.cash_room) - others
            if capital_used(self.inventory(ex), fair, {k: v for k, v in want.items() if v}) > room:
                side = "bid" if self.inventory(ex) >= 0 else "ask"
                want[side] = None
        same = all((want[k] is None) == (have[k] is None) and (want[k] is None or (
            abs(want[k][0] - have[k][0]) < 1e-9 and abs(want[k][1] - have[k][1]) <= self.cfg["min_resize"] * have[k][1]))
            for k in ("bid", "ask"))
        if same:
            return
        if not self.live:
            for k in ("bid", "ask"):
                if want[k] != have[k]:
                    self.sims[ex].place(k, want[k], book)
            self.resting[ex] = dict(want)
            self.quoted_fair[ex] = fair
            return
        out = have["bid"] or have["ask"]
        pulling = want["bid"] is None and want["ask"] is None
        if self.writes.take((1 if out else 0) + (0 if pulling else 1)):
            if out:
                await self.cancel(ex)
            if not pulling:
                await self.place(ex, want)
                self.quoted_fair[ex] = fair
        elif out and (fair is None or abs(fair - self.quoted_fair.get(ex, fair)) >= self.cfg["pull_move"] - 1e-9
                      or not self.feed.healthy) and self.writes.take(1):
            await self.cancel(ex)                    # short on budget: at least never leave stale quotes out
        else:
            self.dirty.add(ex)                       # try again once the budget refills

    # ---- loops
    async def fair_loop(self):
        while not self.stop:
            try:
                ext = await asyncio.to_thread(fetch_external, list(self.rows.values()))
                for ex, row in self.rows.items():
                    f = fair_value(row, ext)[0]
                    old = self.quoted_fair.get(ex)
                    self.fair[ex] = f
                    if f is None or (old is not None and abs(f - old) >= self.cfg["pull_move"] - 1e-9):
                        if self.resting[ex]["bid"] or self.resting[ex]["ask"]:
                            print(f"{time.strftime('%H:%M:%S')}  {self.names[ex]}: fair {old} -> {f}, pulling quotes")
                        self.dirty.add(ex)
                    elif f != old:
                        self.dirty.add(ex)
            except Exception as e:
                print(f"  ! fair value: {e!r}")
            await asyncio.sleep(self.cfg["fair_every"])

    async def quote_loop(self):
        last_acct = 0.0
        while not self.stop:
            await asyncio.sleep(0.5)
            try:
                if self.live and (time.time() - last_acct > 120 or getattr(self, "_need_positions", False)):
                    self._need_positions = False
                    await self.refresh_account()
                    last_acct = time.time()
                for ex in list(self.dirty):
                    self.dirty.discard(ex)
                    await self.requote(ex)
            except Exception as e:
                print(f"  ! quoting: {e!r}")
                await asyncio.sleep(5)

    async def report_loop(self):
        while not self.stop:
            await asyncio.sleep(300)
            self.report()

    def report(self):
        hours = (time.time() - self.t0) / 3600
        rows = {}
        for ex in self.ex:
            s, f = self.sims[ex], self.fair.get(ex) or 0.0
            pnl = s.cash + s.inv * f if not self.live else None
            rows[ex] = {"name": self.names[ex], "fair": f, "quotes": self.resting[ex], "inventory": self.inventory(ex),
                        "shadow_fills": s.fills, "bought": s.bought, "sold": s.sold,
                        "shadow_pnl_at_fair": round(pnl, 2) if pnl is not None else None}
            q = self.resting[ex]
            print(f"{time.strftime('%H:%M:%S')}  MM {self.names[ex]:34s} fair {f:.3f}  "
                  f"bid {q['bid'] or '-'}  ask {q['ask'] or '-'}  inv {self.inventory(ex):+d}"
                  + (f"  shadow: {s.fills} fills, bought {s.bought} sold {s.sold}, P&L at fair {pnl:+.2f}" if pnl is not None else ""))
        try:
            with open(STATS_FILE, "w") as fh:
                json.dump({"mode": self.cfg["mode"], "since": self.t0, "hours": round(hours, 2), "contracts": rows}, fh)
        except OSError:
            pass

    async def run(self):
        print(f"{time.strftime('%H:%M:%S')}  market maker ({self.cfg['mode']}) on: " + ", ".join(self.names.values()))
        if self.live:
            for ex in self.ex:
                await self.cancel(ex)                # start clean: nothing of ours resting
        tasks = [asyncio.create_task(c) for c in (self.feed.run(), self.fair_loop(), self.quote_loop(), self.report_loop())]
        loop = asyncio.get_running_loop()
        done = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, done.set)
        await done.wait()
        print(f"{time.strftime('%H:%M:%S')}  stopping market maker")
        self.stop = True
        if self.live:
            for ex in self.ex:
                try:
                    await self.cancel(ex)
                except Exception as e:
                    print(f"  ! cancel {ex} on exit: {e!r}")
        self.report()
        for tk in tasks:
            tk.cancel()
        await self.feed.close()
        print("Stopped.")


def main(cup, t):
    if not MM["mode"]:
        print("Market maker is off (MM['mode'] = None).")
        return
    asyncio.run(MarketMaker(cup, t).run())
