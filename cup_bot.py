#!/usr/bin/env python3
"""
Predictions Cup gap-trading bot.

Compares Predictions Cup prices with Kalshi / Polymarket prices for the same
races, and buys when the contest is cheaper than the real-money "fair value"
(and sells when the gap has closed).

Commands
  python cup_bot.py markets      # download all contest markets into mapping.csv
  python cup_bot.py automap      # fill in Kalshi tickers / Polymarket slugs for every race it can match
  python cup_bot.py scan         # show opportunities, exits, constraint violations (no orders)
  python cup_bot.py trade        # same as scan, but actually places orders (asks to confirm)
  python cup_bot.py trade --loop 300   # repeat every 300 s
  python cup_bot.py positions    # show your current positions
  python cup_bot.py leaderboard  # top 10, your rank, and the top 5% / 10% cutoffs
  python cup_bot.py stats        # append a snapshot to stats.csv (done hourly while looping)
  add --aggressive to scan/trade for fewer, bigger, held-to-settlement bets
  add --max-orders N to place at most N orders per run (good for a first live test)

Running unattended (24/7), both strategies in one loop on every mapped race
  caffeinate -i nohup python3 cup_bot.py trade --arb --loop 60 --yes > /dev/null 2>&1 &
  tail -f cup_bot.log           # watch it; errors are logged and the loop keeps going
  pkill -f "cup_bot.py trade"   # stop it: finishes the current cycle, then exits (safe any time)

Setup
  pip install requests
  put SIG_API_KEY=your_key in a .env file next to this script
  (create the key in My Profile -> API Keys, scopes: read + trade)
"""
import argparse, csv, json, math, os, re, signal, sys, time, traceback, uuid
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------- settings
def load_dotenv(path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    """Read KEY=value lines from .env next to this script. Real environment variables win."""
    if os.path.exists(path):
        for line in open(path):
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


load_dotenv()
BASE_URL = os.environ.get("SIG_BASE_URL", "https://sig.thesuper.market/api/v1")
API_KEY = os.environ.get("SIG_API_KEY", "")
MAPPING_FILE = "mapping.csv"
STATS_FILE = "stats.csv"          # hourly leaderboard / equity snapshots (for pacing and Sharpe / drawdown)
STATS_COLUMNS = ["time_utc", "rank", "ranked", "pnl", "equity", "cash", "holdings",
                 "cutoff_top5", "cutoff_top10", "leader_pnl", "sharpe_api", "pnl_sell_now", "pnl_fair"]

MAX_SOURCE_DISAGREE = 0.08  # skip a race if Kalshi and Polymarket disagree by more than this
ORDER_TTL_SECONDS = 120     # unfilled limit orders expire after 2 minutes

# Default profile: steady edge capture, aiming for a top 5-10% finish with low variance.
# Bet size = kelly x Kelly fraction of equity, then clipped by max_frac and max_race.
SAFE = dict(
    entry_edge=0.03,      # buy only if contest price is >= 3 points better than fair value
    exit_band=0.01,       # sell when the gap has shrunk to within 1 point of fair value
    kelly=0.5,            # half-Kelly: fair values are estimates, so don't bet the full amount
    max_frac=0.10,        # never hold more than 10% of starting balance in one contract
    max_race=0.15,        # ...or 15% in one race (e.g. Dem YES + Rep NO on the same race)
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
    kelly=1.0,
    max_frac=0.20,        # up to 20% of starting balance in one contract
    max_race=0.25,
    max_spend=0.50,
    min_cash=0.05,
    hold_edge=0.10,       # if bought >= 10 points below fair, hold to settlement instead of exiting
    longshot=True,        # also buy cheap contracts (<= 0.25) where fair is >= 1.5x the price
    max_positions=10,     # concentrate: at most 10 open mapped positions
)
LONGSHOT_MAX_PRICE = 0.25
LONGSHOT_MIN_RATIO = 1.5
LONGSHOT_MIN_EDGE = 0.02
TICK = 0.005

KALSHI_MARKETS_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
KALSHI_EVENT_URL = "https://api.elections.kalshi.com/trade-api/v2/events/{}"
POLY_URL = "https://gamma-api.polymarket.com/markets"
POLY_SEARCH_URL = "https://gamma-api.polymarket.com/public-search"

STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
    "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY",
}


def kalshi_event(race):
    """Contest race name -> Kalshi winner-event ticker, e.g. 'TX-15 House race' -> 'HOUSETX15-26'.
    Each event has one market per candidate; the D and R ones end in -D and -R."""
    if race in ("U.S. Senate", "U.S. House"):
        return "CONTROLS-2026" if race == "U.S. Senate" else "CONTROLH-2026"
    m = re.match(r"([A-Z]{2})-(\d+) House race$", race)
    if m:
        return f"HOUSE{m[1]}{int(m[2])}-26"
    m = re.match(r"(.+) (Senate|Governor)$", race)
    if m and m[1] in STATES:
        return ("SENATE" if m[2] == "Senate" else "GOVPARTY") + STATES[m[1]] + "-26"
    return None

MAP_COLUMNS = ["exchange_id", "market_id", "market_title", "option",
               "best_bid", "best_ask", "kalshi_ticker", "polymarket_slug", "invert", "notes"]


# ---------------------------------------------------------------- SIG API client
class Cup:
    """Thin client. Standard accounts get 100 reads + 30 writes per minute."""

    def __init__(self, key):
        if not key:
            sys.exit("Set SIG_API_KEY first (My Profile -> API Keys).")
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        self.last_read = 0.0
        self.last_write = 0.0

    def _wait(self, write):
        gap = 60 / 25 if write else 60 / 90       # stay safely under the limits
        last = self.last_write if write else self.last_read
        sleep = last + gap - time.time()
        if sleep > 0:
            time.sleep(sleep)
        if write:
            self.last_write = time.time()
        else:
            self.last_read = time.time()

    def req(self, method, path, params=None, body=None):
        write = method != "GET"
        for attempt in range(6):
            self._wait(write)
            r = self.s.request(method, BASE_URL + path, params=params,
                               data=json.dumps(body) if body is not None else None, timeout=180)
            if r.status_code in (429, 500, 502, 503, 504):
                wait = float(r.headers.get("Retry-After", 2 ** attempt))
                print(f"  [{r.status_code}] backing off {wait:.0f}s")
                time.sleep(wait)
                continue          # safe: orders reuse the same idempotency key
            if r.status_code >= 400:
                try:
                    err = r.json().get("error", {})
                except ValueError:
                    err = {"message": r.text[:200]}
                raise RuntimeError(f"{method} {path} -> {r.status_code} {err.get('code')}: {err.get('message')}")
            return r.json()
        raise RuntimeError(f"{method} {path} failed after retries")

    # --- helpers
    def tournament(self, slug=None):
        if slug:
            return self.req("GET", f"/tournaments/{slug}")
        ts = self.req("GET", "/tournaments", {"limit": 100})["data"]
        midterm = [t for t in ts if "midterm" in (t["name"] + t["slug"]).lower()]
        if len(midterm) == 1:
            return self.req("GET", f"/tournaments/{midterm[0]['slug']}")
        print("Couldn't pick the tournament automatically. Available:")
        for t in ts:
            print(f"  {t['slug']:40s} {t['name']}  ({t['status']})")
        sys.exit("Re-run with --tournament <slug>.")

    def all_markets(self, tid):
        out, cursor = [], None
        while True:
            p = {"limit": 100, "tournamentId": tid, "status": "open"}
            if cursor:
                p["cursor"] = cursor
            page = self.req("GET", "/markets", p)
            out += page["data"]
            if not page["pagination"]["hasMore"]:
                return out
            cursor = page["pagination"]["nextCursor"]

    def prices(self, tid, exchange_ids):
        quotes = {}
        ids = list(exchange_ids)
        for i in range(0, len(ids), 100):
            res = self.req("GET", "/exchanges/prices", {"ids": ",".join(ids[i:i + 100]), "tournamentId": tid})
            for q in res["data"]:
                quotes[q["exchangeId"]] = q
        return quotes

    def positions(self, slug):
        return self.req("GET", f"/tournaments/{slug}/portfolio/positions")

    def leaderboard(self, slug, limit=10, offset=0):
        return self.req("GET", f"/tournaments/{slug}/leaderboard",
                        {"limit": limit, "offset": offset, "period": "all"})

    def orderbook(self, tid, exchange_id, depth=20):
        """YES-normalized book: bids (desc) and asks (asc) as [{price, quantity}]."""
        return self.req("GET", f"/exchanges/{exchange_id}/orderbook", {"depth": depth, "tournamentId": tid})

    def multi_leg(self, tid, legs):
        """Place up to 10 orders atomically: all are placed or none. legs = [(exchange_id, side, action, qty, price)]"""
        exp = (datetime.now(timezone.utc) + timedelta(seconds=ORDER_TTL_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = {"idempotencyKey": str(uuid.uuid4()), "legs": [
            {"exchangeId": str(ex), "side": side, "action": action, "quantity": int(qty),
             "price": round_tick(price), "tournamentId": tid, "expirationDate": exp}
            for ex, side, action, qty, price in legs]}
        res = sorted(self.req("POST", "/orders/multi-leg", body=body)["results"], key=lambda r: r["index"])
        return [r["data"] for r in res]

    def cancel(self, order_id):
        return self.req("DELETE", f"/orders/{order_id}")

    def order_fills(self, order_id):
        """Includes totalQuantityFilled: the exact lifetime fill count of the order."""
        return self.req("GET", f"/orders/{order_id}/fills", {"limit": 1})

    def violations(self, tid):
        return self.req("GET", "/relationships/constraints",
                        {"tournamentId": tid, "violationsOnly": "true"})["data"]

    def order(self, tid, exchange_id, side, action, qty, price):
        body = {
            "idempotencyKey": str(uuid.uuid4()),
            "exchangeId": str(exchange_id), "side": side, "action": action,
            "quantity": int(qty), "price": round_tick(price), "tournamentId": tid,
            "expirationDate": (datetime.now(timezone.utc) + timedelta(seconds=ORDER_TTL_SECONDS))
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        return self.req("POST", "/orders", body=body)


def round_tick(p):
    return min(0.995, max(0.005, round(round(p / TICK) * TICK, 3)))


# ---------------------------------------------------------------- external fair values
def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def kalshi_market_mid(m):
    bid = _num(m.get("yes_bid_dollars")) or (_num(m.get("yes_bid")) or 0) / 100
    ask = _num(m.get("yes_ask_dollars")) or (_num(m.get("yes_ask")) or 0) / 100
    if bid and ask and ask > bid:
        return (bid + ask) / 2
    last = _num(m.get("last_price_dollars")) or (_num(m.get("last_price")) or 0) / 100
    return last or None


def poly_market_mid(m):
    bid, ask = _num(m.get("bestBid")), _num(m.get("bestAsk"))
    if bid and ask and ask > bid:
        return (bid + ask) / 2
    prices = m.get("outcomePrices")
    if isinstance(prices, str):
        prices = json.loads(prices)
    return _num(prices[0]) if prices else None


def fetch_external(rows):
    """Bulk-fetch every mapped Kalshi / Polymarket price once per cycle (a few requests instead of
    two per contract). Returns {("kalshi", ticker): mid, ("poly", slug): mid}."""
    out = {}
    tickers = sorted({r["kalshi_ticker"].strip() for r in rows if r.get("kalshi_ticker")})
    for i in range(0, len(tickers), 100):
        try:
            r = requests.get(KALSHI_MARKETS_URL, params={"tickers": ",".join(tickers[i:i + 100]), "limit": 1000},
                             timeout=30)
            r.raise_for_status()
            out.update({("kalshi", m["ticker"]): kalshi_market_mid(m) for m in r.json().get("markets", [])})
        except Exception as e:
            print(f"  ! Kalshi bulk fetch: {e}")
    slugs = sorted({r["polymarket_slug"].strip() for r in rows if r.get("polymarket_slug")})
    for i in range(0, len(slugs), 50):
        try:
            r = requests.get(POLY_URL, params=[("slug", s) for s in slugs[i:i + 50]] + [("limit", 100)], timeout=30)
            r.raise_for_status()
            out.update({("poly", m["slug"]): poly_market_mid(m) for m in r.json()})
        except Exception as e:
            print(f"  ! Polymarket bulk fetch: {e}")
    return out


def fair_value(row, ext):
    """Average of available real-money mids (from fetch_external). Returns (fair, detail) or (None, reason)."""
    vals = {}
    if row.get("kalshi_ticker"):
        vals["kalshi"] = ext.get(("kalshi", row["kalshi_ticker"].strip()))
    if row.get("polymarket_slug"):
        vals["poly"] = ext.get(("poly", row["polymarket_slug"].strip()))
    vals = {k: v for k, v in vals.items() if v}
    if not vals:
        return None, "no external price"
    if str(row.get("invert", "")).strip() in ("1", "yes", "true"):
        vals = {k: 1 - v for k, v in vals.items()}
    if len(vals) == 2 and abs(vals["kalshi"] - vals["poly"]) > MAX_SOURCE_DISAGREE:
        return None, f"sources disagree {vals}"
    return sum(vals.values()) / len(vals), vals


# ---------------------------------------------------------------- mapping file
def load_mapping():
    if not os.path.exists(MAPPING_FILE):
        return {}
    with open(MAPPING_FILE, newline="") as f:
        return {r["exchange_id"]: r for r in csv.DictReader(f)}


def cmd_markets(cup, t):
    markets = cup.all_markets(t["id"])
    old = load_mapping()
    rows = []
    for m in markets:
        for ex in m["exchanges"]:
            rows.append({"exchange_id": ex["id"], "market_id": m["id"],
                         "market_title": m["title"], "option": ex.get("option") or ""})
    quotes = cup.prices(t["id"], [r["exchange_id"] for r in rows])
    for r in rows:
        q = quotes.get(r["exchange_id"], {})
        r["best_bid"], r["best_ask"] = q.get("bestBid"), q.get("bestAsk")
        prev = old.get(r["exchange_id"], {})
        for k in ("kalshi_ticker", "polymarket_slug", "invert", "notes"):
            r[k] = prev.get(k, "")
    rows.sort(key=lambda r: r["market_title"])
    with open(MAPPING_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MAP_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} contracts to {MAPPING_FILE}. "
          f"Fill in kalshi_ticker and/or polymarket_slug for the races you want to trade.")


def party_of(title):
    m = re.match(r"Will the (\w+) Party win", title)
    return m.group(1) if m else None


def _poly_event_slugs(race):
    """Candidate Polymarket event slugs for a contest race (their naming isn't fully regular)."""
    if race == "U.S. Senate":
        return ["which-party-will-win-the-senate-in-2026"]
    if race == "U.S. House":
        return ["which-party-will-win-the-house-in-2026"]
    m = re.match(r"(.+) (Senate|Governor)$", race)
    if m and m[1] in STATES:
        s = m[1].lower().replace(" ", "-")
        if m[2] == "Senate":
            return [f"{s}-senate-election-winner", f"{s}-senate-election-winner-2026"]
        return [f"{s}-governor-winner-2026", f"{s}-governor-election-winner", f"{s}-governor-election-winner-2026"]
    return []


def _poly_search_events(race):
    """Fallback: Polymarket search, keeping only general-election winner events for this exact race."""
    m = re.match(r"([A-Z]{2})-(\d+) House race$", race)
    q = f"{m[1]}-{m[2]} House election winner" if m else f"{race} election winner"
    try:
        r = requests.get(POLY_SEARCH_URL, params={"q": q, "limit_per_type": 5, "events_status": "active"}, timeout=30)
        events = r.json().get("events") or []
    except Exception:
        return []
    want = (f"{m[1]}-{int(m[2]):02d}", f"{m[1]}-{int(m[2])}") if m else (race,)
    return [e for e in events if any(w.lower() in e["title"].lower() for w in want)
            and "winner" in e["title"].lower()
            and not re.search(r"county|primary|state senate|lieutenant|runoff|margin|nominee", e["title"], re.I)]


def poly_find(race):
    """{'Democratic': slug, 'Republican': slug} for this race on Polymarket, or {}."""
    events = []
    for s in _poly_event_slugs(race):
        try:
            events += requests.get("https://gamma-api.polymarket.com/events", params={"slug": s}, timeout=30).json()
        except Exception:
            pass
    events = events or _poly_search_events(race)
    for e in events:
        ms = [m for m in e.get("markets") or [] if not m.get("closed")]
        d = [m["slug"] for m in ms if "democrat" in m["slug"]]
        r = [m["slug"] for m in ms if "republican" in m["slug"]]
        if len(d) == 1 and len(r) == 1:
            return {"Democratic": d[0], "Republican": r[0]}
    return {}


def cmd_automap(cup, t, out=MAPPING_FILE):
    """Fill kalshi_ticker / polymarket_slug for every D and R contract we can match. Never overwrites
    a value you filled in by hand. Polymarket matches are kept only if they agree with Kalshi."""
    rows = load_mapping()
    races = {}
    for ex, r in rows.items():
        if party_of(r["market_title"]) in ("Democratic", "Republican"):
            races.setdefault(race_key(r["market_title"]), {})[party_of(r["market_title"])] = r
    # Kalshi: tickers follow a pattern; keep the ones that exist
    want = {}
    for race, parties in races.items():
        ev = kalshi_event(race)
        for party, r in parties.items():
            if ev and not r.get("kalshi_ticker"):
                want[r["exchange_id"]] = f"{ev}-{party[0]}"
    found = fetch_external([{"kalshi_ticker": tk} for tk in want.values()])
    for ex, tk in want.items():
        if found.get(("kalshi", tk)):
            rows[ex]["kalshi_ticker"] = tk
    # Polymarket: search per race, then sanity-check against Kalshi
    n_poly = 0
    for race, parties in sorted(races.items()):
        if all(r.get("polymarket_slug") for r in parties.values()):
            continue
        slugs = poly_find(race)
        if not slugs or set(slugs) != set(parties):
            continue
        ext = fetch_external([{"polymarket_slug": s} for s in slugs.values()] +
                             [{"kalshi_ticker": r["kalshi_ticker"]} for r in parties.values() if r.get("kalshi_ticker")])
        pm = {p: ext.get(("poly", s)) for p, s in slugs.items()}
        km = {p: ext.get(("kalshi", r.get("kalshi_ticker", ""))) for p, r in parties.items()}
        if not all(pm.values()):
            continue
        if all(km.values()):
            ok = all(abs(pm[p] - km[p]) <= MAX_SOURCE_DISAGREE for p in pm)
        else:
            ok = 0.9 <= sum(pm.values()) <= 1.1          # no Kalshi to compare with: at least sane
        if not ok:
            print(f"  {race}: Polymarket {pm} vs Kalshi {km} disagree -- not mapped")
            continue
        for p, s in slugs.items():
            if not parties[p].get("polymarket_slug"):
                parties[p]["polymarket_slug"] = s
        n_poly += 1
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MAP_COLUMNS)
        w.writeheader()
        w.writerows(sorted(rows.values(), key=lambda r: r["market_title"]))
    mapped = [r for r in rows.values() if r.get("kalshi_ticker") or r.get("polymarket_slug")]
    both = [r for r in mapped if r.get("kalshi_ticker") and r.get("polymarket_slug")]
    print(f"Wrote {out}: {len(mapped)} of {len(rows)} contracts mapped ({len(both)} with both sources, "
          f"{n_poly} races newly matched on Polymarket).")
    print("Unmapped races:", ", ".join(sorted({race_key(r['market_title']) for r in rows.values() if r not in mapped})))


# ---------------------------------------------------------------- the strategy
def wants_entry(price, fair, cfg):
    """Is buying at `price` (YES or NO) good enough, given that side's fair value?"""
    if price is None or price <= 0:
        return False
    if fair - price >= cfg["entry_edge"]:
        return True
    return (cfg["longshot"] and price <= LONGSHOT_MAX_PRICE
            and fair / price >= LONGSHOT_MIN_RATIO and fair - price >= LONGSHOT_MIN_EDGE)


def kelly_fraction(price, fair):
    """Share of bankroll that maximises long-run growth when buying a 0/1 contract at `price`
    that pays 1 with probability `fair`: (p - price) / (1 - price)."""
    return max(0.0, (fair - price) / (1 - price)) if price < 1 else 0.0


def race_key(title):
    """'Will the Democratic Party win the Texas Senate?' -> 'Texas Senate', so both parties share a cap."""
    m = re.match(r"Will the \w+ Party win the (.+)\?", title)
    return m.group(1) if m else title


def run(cup, t, live, cfg, max_orders=0, locked=None):
    """One gap-trading pass. `locked` = {exchange_id: signed shares} owned by arb pairs (see arb_bot):
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
        held = total - lock                        # the gap strategy's own shares
        held_cost = abs(p["costBasis"]) * abs(held) / abs(total) if total else 0
        paid = held_cost / abs(held) if held else 0  # per-share price paid for the side we hold
        no_bid = 1 - ask if ask is not None else None  # what we can sell NO for
        no_ask = 1 - bid if bid is not None else None  # what we pay for NO
        signal = ""

        # exits first: has the gap closed (or flipped)?
        if held > 0 and bid is not None and bid >= fair - cfg["exit_band"]:
            if cfg["hold_edge"] and fair - paid >= cfg["hold_edge"] and bid < fair + cfg["entry_edge"]:
                signal = f"HOLD to settlement (paid {paid:.3f}, fair {fair:.3f})"
            else:
                signal = f"SELL {held} YES @ {bid:.3f}"
                actions.append((ex_id, "yes", "sell", held, bid))
                sold_cost[ex_id] = held_cost
        elif held < 0 and no_bid is not None and no_bid >= (1 - fair) - cfg["exit_band"]:
            if cfg["hold_edge"] and (1 - fair) - paid >= cfg["hold_edge"] and no_bid < (1 - fair) + cfg["entry_edge"]:
                signal = f"HOLD NO to settlement (paid {paid:.3f}, fair {1 - fair:.3f})"
            else:
                signal = f"SELL {-held} NO @ {no_bid:.3f}"
                actions.append((ex_id, "no", "sell", -held, no_bid))
                sold_cost[ex_id] = held_cost

        # entries: contest cheaper than fair value -- sized after the loop, best first
        elif held >= 0 and lock >= 0 and wants_entry(ask, fair, cfg):
            candidates.append(((fair - ask) / ask, ex_id, "yes", ask, fair, held_cost, held != 0, name))
            signal = f"edge YES {fair - ask:+.3f} ({(fair - ask) / ask:+.0%} return)"
        elif held <= 0 and lock <= 0 and wants_entry(no_ask, 1 - fair, cfg):
            candidates.append((((1 - fair) - no_ask) / no_ask, ex_id, "no", no_ask, 1 - fair, held_cost, held != 0, name))
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
        for ret, ex_id, side, price, fair_side, held_cost, already_held, name in sorted(candidates, reverse=True):
            if cfg["max_positions"] and not already_held and open_count >= cfg["max_positions"]:
                print(f"  {name:52s} skip (already at {cfg['max_positions']} positions)")
                continue
            race = race_key(mapping[ex_id]["market_title"])
            k = kelly_fraction(price, fair_side)
            target = min(cfg["kelly"] * k * equity, cap)  # what we'd like to hold in this contract
            room = min(target - held_cost, race_cap - race_cost.get(race, 0), budget_run)
            qty = math.floor(room / price) if room > 0 else 0
            if qty <= 0:
                print(f"  {name:52s} skip (already at Kelly size / race cap / run budget)")
                continue
            print(f"  {name:52s} BUY {qty} {side.upper()} @ {price:.3f}  "
                  f"({ret:+.0%} expected, Kelly {k:.0%} x {cfg['kelly']:g})")
            actions.append((ex_id, side, "buy", qty, price))
            budget_run -= qty * price
            race_cost[race] = race_cost.get(race, 0) + qty * price
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


def cmd_positions(cup, t):
    res = cup.positions(t["slug"])
    for p in res["positions"]:
        if p["quantity"]:
            side = "YES" if p["quantity"] > 0 else "NO"
            print(f"{p['marketTitle'][:45]:45s} {side} {abs(p['quantity']):>7.0f} @ {p['avgCost']:.3f}"
                  f"  now {p['currentPrice'] or 0:.3f}  P&L {p['unrealizedPnl']:+,.0f}")
    s = res["summary"]
    print(f"Market value {s['totalMarketValue']:,.0f}  unrealized P&L {s['totalUnrealizedPnl']:+,.0f}")


def cmd_leaderboard(cup, t):
    lb = cup.leaderboard(t["slug"])
    for e in lb["leaderboard"]:
        print(f"  #{e['rank']:<4d} {e['username'] or '?':24s} P&L {e['pnl']:>+14,.0f}  trades {e['tradesCount']}")
    my_pnl = t["myBalance"] + cup.positions(t["slug"])["summary"]["totalMarketValue"] - t["initialBalance"]
    print(f"\nYou: rank {lb['myRank'] or 'unranked'} of {lb['total']}, P&L {my_pnl:+,.0f}"
          f" ({my_pnl / t['initialBalance']:+.1%})")
    for pct in (0.05, 0.10):                         # the targets: top 5% and top 10%
        rank = max(1, math.floor(lb["total"] * pct))
        e = cup.leaderboard(t["slug"], limit=1, offset=rank - 1)["leaderboard"]
        if e:
            need = e[0]["pnl"] - my_pnl
            print(f"Top {pct:.0%} cutoff (#{rank}): P&L {e[0]['pnl']:+,.0f}  ->  "
                  + (f"you need {need:+,.0f} more" if need > 0 else "you're in"))


def pnl_views(cup, t, portfolio):
    """P&L two more ways than the leaderboard's (which values holdings at lagging contest prices):
    sell_now = everything sold at the current bids; fair = valued at Kalshi/Polymarket fair value."""
    rows = load_mapping()
    pos = [p for p in portfolio["positions"] if p["quantity"] and not p["settled"]]
    ext = fetch_external([rows[p["exchangeId"]] for p in pos if p["exchangeId"] in rows])
    quotes = cup.prices(t["id"], [p["exchangeId"] for p in pos])
    bid_v = fair_v = 0.0
    for p in pos:
        n, yes, q = abs(p["quantity"]), p["quantity"] > 0, quotes.get(p["exchangeId"], {})
        bid = q.get("bestBid") if yes else (1 - q["bestAsk"] if q.get("bestAsk") else 0)
        fair = fair_value(rows[p["exchangeId"]], ext)[0] if p["exchangeId"] in rows else None
        fair = p["currentPrice"] if fair is None else fair
        bid_v += n * (bid or 0)
        fair_v += n * (fair if yes else 1 - fair)
    base = t["myBalance"] - t["initialBalance"]
    return round(base + bid_v, 2), round(base + fair_v, 2)


def log_stats(cup, t):
    """Append one snapshot to stats.csv: where you stand vs the top 5% / 10% cutoffs, and your equity
    (hourly equity is what Sharpe ratio and max drawdown get computed from)."""
    slug = t["slug"]
    t = cup.tournament(slug)
    lb = cup.leaderboard(slug, limit=1)
    n = lb["total"]

    def pnl_at(pct):
        e = cup.leaderboard(slug, limit=1, offset=max(1, math.floor(n * pct)) - 1)["leaderboard"]
        return e[0]["pnl"] if e else ""

    pnl = cup.req("GET", f"/tournaments/{slug}/portfolio/pnl", {"period": "all"})
    row = {"time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), "rank": lb["myRank"] or "",
           "ranked": n, "pnl": round(pnl["totalAccountValue"] - t["initialBalance"], 2),
           "equity": round(pnl["totalAccountValue"], 2), "cash": round(t["myBalance"], 2),
           "holdings": round(pnl["totalHoldingsValue"], 2), "cutoff_top5": pnl_at(0.05),
           "cutoff_top10": pnl_at(0.10), "leader_pnl": lb["leaderboard"][0]["pnl"] if lb["leaderboard"] else "",
           "sharpe_api": pnl.get("sharpe") if pnl.get("sharpe") is not None else ""}   # null early in the contest
    row["pnl_sell_now"], row["pnl_fair"] = pnl_views(cup, t, cup.positions(slug))
    new_file = not os.path.exists(STATS_FILE)
    with open(STATS_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=STATS_COLUMNS)
        if new_file:
            w.writeheader()
        w.writerow(row)
    print(f"\nSTATS  rank {row['rank']} of {n}  P&L {row['pnl']:+,.0f} (sell now {row['pnl_sell_now']:+,.0f}, "
          f"at fair value {row['pnl_fair']:+,.0f})  |  top 10% {row['cutoff_top10']:+,.0f}"
          f"  top 5% {row['cutoff_top5']:+,.0f}  |  invested {row['holdings']:,.0f}, cash {row['cash']:,.0f}")


# ---------------------------------------------------------------- main
class Tee:
    """Copy everything printed to a log file, with a timestamp on each line."""

    def __init__(self, path):
        self.term, self.log, self.at_line_start = sys.stdout, open(path, "a", buffering=1), True

    def write(self, s):
        self.term.write(s)
        for part in s.splitlines(keepends=True):
            if self.at_line_start and part.strip():
                self.log.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  ")
            self.log.write(part)
            self.at_line_start = part.endswith("\n")

    def flush(self):
        self.term.flush()
        self.log.flush()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["markets", "automap", "scan", "trade", "positions", "leaderboard", "stats"])
    ap.add_argument("--tournament", help="tournament slug (auto-detected if omitted)")
    ap.add_argument("--loop", type=int, default=0, help="repeat every N seconds")
    ap.add_argument("--aggressive", action="store_true",
                    help="bigger, concentrated, held-to-settlement bets (see AGGRESSIVE)")
    ap.add_argument("--max-orders", type=int, default=0, help="place at most N orders per run (0 = no limit)")
    ap.add_argument("--max-frac", type=float, help="override the per-contract cap, e.g. 0.01 for a small test")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt (for unattended runs)")
    ap.add_argument("--log", default="cup_bot.log", help="log file for scan/trade output ('' to disable)")
    ap.add_argument("--out", default=MAPPING_FILE, help="automap: where to write the filled-in mapping")
    ap.add_argument("--arb", action="store_true",
                    help="each cycle, run the D+R arbitrage (arb_bot.py) first on every race, then the gap strategy")
    a = ap.parse_args()
    if a.log and a.command in ("scan", "trade"):
        sys.stdout = sys.stderr = Tee(a.log)

    cup = Cup(API_KEY)
    t = cup.tournament(a.tournament)
    print(f"Tournament: {t['name']} ({t['slug']})")

    if a.command == "markets":
        return cmd_markets(cup, t)
    if a.command == "automap":
        return cmd_automap(cup, t, a.out)
    if a.command == "positions":
        return cmd_positions(cup, t)
    if a.command == "leaderboard":
        return cmd_leaderboard(cup, t)
    if a.command == "stats":
        return log_stats(cup, t)

    cfg = dict(AGGRESSIVE if a.aggressive else SAFE)
    if a.max_frac is not None:
        cfg["max_frac"] = a.max_frac
    print("Profile: " + ("AGGRESSIVE" if a.aggressive else "safe") + "  " +
          ", ".join(f"{k}={v}" for k, v in cfg.items()))
    live = a.command == "trade"
    if live and not a.yes and input("This places real contest orders. Type YES to continue: ").strip() != "YES":
        sys.exit("Cancelled.")
    if a.arb:
        import arb_bot                             # imported here: arb_bot imports this module
        arb_cfg = dict(arb_bot.ARB)
        print("Arb settings: " + ", ".join(f"{k}={v}" for k, v in arb_cfg.items()))
    stop = []                                      # pkill -> finish this cycle (orders + arb ledger), then exit
    signal.signal(signal.SIGTERM, lambda *_: (stop.append(1), print("\nStop requested: finishing this cycle.")))
    last_stats = 0.0
    while not stop:
        if a.loop and time.time() - last_stats >= 3600:   # hourly snapshot while looping
            try:
                log_stats(cup, t)
            except Exception as e:
                print(f"  (stats snapshot failed: {e})")
            last_stats = time.time()
        try:
            locked = {}
            if a.arb:                              # arb first; its pairs are then off-limits to the gap step
                try:
                    arb_bot.run(cup, t, live, arb_cfg, shared=True)
                except Exception:
                    print(f"\n! arb step failed:\n{traceback.format_exc()}")
                locked = arb_bot.locked_shares()
            run(cup, t, live, cfg, a.max_orders, locked)
        except Exception:
            if not a.loop:
                raise
            print(f"\n! run failed, retrying in {a.loop}s:\n{traceback.format_exc()}")
        if not a.loop:
            break
        for _ in range(a.loop):                    # sleep in 1 s steps so a stop request is quick
            if stop:
                break
            time.sleep(1)
    print("Stopped.")


if __name__ == "__main__":
    main()
