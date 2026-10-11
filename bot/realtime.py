"""
Live order books and our own fills from the contest's realtime feed, for the market maker
(strategies/mm.py). The feed is Supabase Realtime private broadcast channels, one batch per second:
  tournament:{id}:market:{market_id}   market_batch: full books of the exchanges it names + trades
  user:{profile_id}                    account_batch: our fills and order updates
It is best-effort, so books are re-read over REST on start, after a missed or oversized batch, after a
reconnect or token refresh, and whenever a book has been quiet for a minute. `healthy` is False until
that has happened; the market maker pulls its quotes while it is False.
"""
import asyncio, time

TOKEN_REFRESH_S = 2.5 * 3600   # tokens last 3 hours
QUIET_RESYNC_S = 60            # re-read a book over REST if the feed hasn't pushed it for this long
BAD_STATES = ("CHANNEL_ERROR", "TIMED_OUT", "CLOSED")


def _as_of(a):
    """Book version for 'is this push newer than what we hold': (sequence, at), or None."""
    return (a.get("sequence") or 0, a.get("at") or "") if a else None


class Feed:
    def __init__(self, cup, tid, exchanges, on_change, on_trades=None, on_fills=None, rest_lock=None):
        """exchanges = {exchange_id: market_id}. Callbacks: on_change(exchange_id) after its book changes,
        on_trades([{exchangeId, price, quantity, ...}]) for market trades, on_fills([...]) for ours.
        rest_lock serialises REST calls with the market maker (the client isn't thread-safe)."""
        self.cup, self.tid, self.exchanges = cup, tid, {str(k): str(v) for k, v in exchanges.items()}
        self.on_change, self.on_trades, self.on_fills = on_change, on_trades, on_fills
        self.rest_lock = rest_lock or asyncio.Lock()
        self.books = {}           # exchange_id -> {"bids": [...], "asks": [...], "asOf": ..., "t": last update}
        self.last_rev = {}        # topic -> last accepted revision
        self.healthy = False
        self._resync, self._dead, self.client = False, False, None

    async def rest(self, fn, *args):
        async with self.rest_lock:
            return await asyncio.to_thread(fn, *args)

    async def resync(self, exchanges=None):
        for ex in exchanges or list(self.exchanges):
            b = await self.rest(self.cup.orderbook, self.tid, ex, 50)
            self.books[ex] = {"bids": b.get("bids") or [], "asks": b.get("asks") or [],
                              "asOf": _as_of(b.get("asOf")), "t": time.time()}
            self.on_change(ex)

    # ---- message handlers (sync: called by the realtime client)
    def _apply_book(self, b):
        ex = str(b.get("exchangeId"))
        if ex not in self.exchanges:
            return
        held, new = self.books.get(ex), _as_of(b.get("asOf"))
        if held is None or held["asOf"] is None or (new is not None and new > held["asOf"]):
            self.books[ex] = {"bids": b.get("bids") or [], "asks": b.get("asks") or [], "asOf": new, "t": time.time()}
            self.on_change(ex)
        else:
            held["t"] = time.time()

    def _market_batch(self, topic, msg):
        p = msg.get("payload", msg)
        for b in p.get("books") or []:          # books apply even when the batch is a duplicate
            self._apply_book(b)
        d = p.get("delivery") or {}
        rev, prev, last = d.get("revision"), d.get("previousRevision"), self.last_rev.get(topic)
        if p.get("resyncRequired"):
            if last is None or (rev is not None and rev > last):
                self.last_rev[topic] = rev
            self._resync = True
            return
        if last is not None and rev is not None and rev <= last:
            return                              # duplicate
        if rev is not None:
            self.last_rev[topic] = rev
        trades = [t for t in p.get("trades") or [] if str(t.get("exchangeId")) in self.exchanges]
        if (last is not None and prev is not None and prev > last) or any(t.get("sequence") is None for t in trades):
            self._resync = True                 # missed a batch: trust REST, not this one
            return
        if trades and self.on_trades:
            self.on_trades(trades)

    def _account_batch(self, msg):
        p = msg.get("payload", msg)
        fills = [f for f in p.get("fills") or [] if str(f.get("exchangeId")) in self.exchanges]
        if fills and self.on_fills:
            self.on_fills(fills)

    def _status(self, status, err=None):
        if any(s in str(status) for s in BAD_STATES):
            print(f"  ! realtime channel {status} {err or ''}")
            self._dead = True

    # ---- connection
    async def _connect(self):
        from realtime import AsyncRealtimeClient       # only the market maker needs this package
        tok = await self.rest(self.cup.req, "POST", "/realtime/token")
        self.client = AsyncRealtimeClient(tok["supabaseUrl"].rstrip("/") + "/realtime/v1", tok["anonKey"])
        await self.client.connect()
        await self.client.set_auth(tok["token"])
        for market_id in sorted(set(self.exchanges.values())):
            topic = f"tournament:{self.tid}:market:{market_id}"
            ch = self.client.channel(topic, {"config": {"private": True}})
            ch.on_broadcast("market_batch", lambda msg, topic=topic: self._market_batch(topic, msg))
            await ch.subscribe(self._status)
        ch = self.client.channel(tok["channels"]["user"], {"config": {"private": True}})
        ch.on_broadcast("account_batch", self._account_batch)
        await ch.subscribe(self._status)
        self.last_rev, self._dead = {}, False

    async def close(self):
        if self.client:
            try:
                await self.client.close()
            except Exception:
                pass
            self.client = None

    async def run(self):
        """Connect, load books, keep them fresh; on any trouble mark unhealthy, reconnect and resync."""
        while True:
            try:
                await self._connect()
                await self.resync()
                self.healthy, t_token = True, time.time()
                print(f"{time.strftime('%H:%M:%S')}  realtime: subscribed to {len(set(self.exchanges.values()))} "
                      f"market(s) + our fills")
                while not self._dead:
                    await asyncio.sleep(1)
                    if self._resync:
                        self._resync = False
                        await self.resync()
                    quiet = [ex for ex in self.exchanges if time.time() - self.books[ex]["t"] > QUIET_RESYNC_S]
                    if quiet:
                        await self.resync(quiet)
                    if time.time() - t_token > TOKEN_REFRESH_S:
                        tok = await self.rest(self.cup.req, "POST", "/realtime/token")
                        await self.client.set_auth(tok["token"])
                        t_token = time.time()
                        await self.resync()
                raise ConnectionError("channel closed")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.healthy = False
                print(f"{time.strftime('%H:%M:%S')}  ! realtime: {e!r}; reconnecting in 5 s")
                await self.close()
                await asyncio.sleep(5)
