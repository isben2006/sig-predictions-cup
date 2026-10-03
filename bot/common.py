"""
Shared plumbing for the Predictions Cup bots: settings from .env, the contest API client, the
contest-market -> Kalshi/Polymarket mapping file, and real-money "fair value" prices.
"""
import csv, json, os, re, sys, time, uuid
from datetime import datetime, timedelta, timezone

import requests


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # the project folder


def load_dotenv(path=os.path.join(ROOT, ".env")):
    """Read KEY=value lines from .env next to this script. Real environment variables win."""
    if os.path.exists(path):
        for line in open(path):
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


load_dotenv()


load_dotenv()
BASE_URL = os.environ.get("SIG_BASE_URL", "https://sig.thesuper.market/api/v1")
API_KEY = os.environ.get("SIG_API_KEY", "")
DATA_DIR = os.environ.get("SIG_DATA_DIR", os.path.join(ROOT, "data"))   # tests point this elsewhere
LOG_DIR = os.environ.get("SIG_LOG_DIR", os.path.join(ROOT, "logs"))
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
MAPPING_FILE = os.path.join(DATA_DIR, "mapping.csv")
MAX_SOURCE_DISAGREE = 0.08  # skip a race if Kalshi and Polymarket disagree by more than this
ORDER_TTL_SECONDS = 120     # unfilled limit orders expire after 2 minutes
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
                    body = r.json()
                    err = body.get("error", body) if isinstance(body, dict) else {"message": str(body)[:200]}
                except ValueError:
                    err = {"message": r.text[:200]}
                if not isinstance(err, dict):              # e.g. {"error": "Not found"}
                    err = {"message": str(err)[:200]}
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


def race_key(title):
    """'Will the Democratic Party win the Texas Senate?' -> 'Texas Senate', so both parties share a cap."""
    m = re.match(r"Will the \w+ Party win the (.+)\?", title)
    return m.group(1) if m else title


def party_of(title):
    m = re.match(r"Will the (\w+) Party win", title)
    return m.group(1) if m else None


# ---------------------------------------------------------------- logging
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
