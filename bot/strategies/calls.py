"""
Election night (Nov 3-4): trade the delay between a race being called in real life and the contest's
prices catching up. Runs as its own fast process (`main.py calls`, systemd sig-calls) and watches
Kalshi + Polymarket every few seconds; the contest is only touched when a race triggers.

When the real-money markets call a race for a party (or Kalshi records a result: its Senate/governor
markets settle early on a consensus of media projections), in one go per race:
  1. dead legs: sell the losing leg of every arb pair we hold there. It's worth 0, but the contest may
     still pay for it; the winning leg still pays 1 at settlement;
  2. defensive exit: sell any other shares we hold on the losing side;
  3. buy the winner with cash, at or below `max_buy`, paced over the night (`pace`).
Before a call, positions whose side the markets turn decisively against are sold while the contest still
pays more than fair value (`exit_level`).

Safety: both sources must agree for a while before anything happens (a race with only one live source
needs more), per-race and per-hour spending caps, phone alerts for every action, `mode="shadow"` logs
what it would do without trading, and live mode refuses to run next to the trading loop (`main.py trade`),
which is stopped on election night so only one process trades the account.
`python3 main.py calls --check` audits the contest -> Kalshi/Polymarket mapping (a wrong or inverted
mapping would buy the loser with full confidence).
"""
import json, os, signal, subprocess, time, traceback, uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from bot.common import (DATA_DIR, KALSHI_MARKETS_URL, POLY_URL, STATES, kalshi_market_mid, load_mapping,
                        party_of, poly_market_mid, race_key, round_tick)
from bot.reporting import notify
from bot.strategies import arb

ET = ZoneInfo("America/New_York")
ELECTION_DAY = datetime(2026, 11, 3, tzinfo=ET)
# When the last polls close in each state, in hours after midnight ET on election day (24 = midnight).
# No race counts as called before then, however high its price: safe races trade at 97-99% for weeks.
POLL_CLOSE = {
    "GA": 19, "IN": 19, "KY": 19, "SC": 19, "VT": 19, "VA": 19, "NC": 19.5, "OH": 19.5, "WV": 19.5,
    "AL": 20, "CT": 20, "DE": 20, "FL": 20, "IL": 20, "ME": 20, "MD": 20, "MA": 20, "MS": 20, "MO": 20,
    "NH": 20, "NJ": 20, "OK": 20, "PA": 20, "RI": 20, "TN": 20, "AR": 20.5,
    "AZ": 21, "CO": 21, "KS": 21, "LA": 21, "MI": 21, "MN": 21, "NM": 21, "NY": 21, "SD": 21, "TX": 21,
    "WI": 21, "WY": 21, "IA": 21, "NE": 21, "MT": 22, "NV": 22, "ND": 22, "UT": 22,
    "CA": 23, "ID": 23, "OR": 23, "WA": 23, "HI": 24, "AK": 25,
}
CALLS = dict(
    mode="live",               # "shadow" = log what it would do, "live" = trade, None = off
    test_polls_closed=False,   # shadow testing before the night: treat every state's polls as closed
    poll_s=3,                  # seconds between Kalshi/Polymarket polls
    stale_s=60,                # a source that hasn't answered for this long counts as down
    safe_level=0.95,           # a race whose favourite was >= 95% before the night is "safe"
    call_safe=(0.99, 10),      # safe race, polls closed: both sources >= 99% for the winner for 10 s
                               # (was 30 s; other bots trade the same calls, so the first seconds count)
    call_close=(0.98, 60),     # close race: >= 98% for 60 s (early returns swing)
    call_one=(0.995, 120),     # only one source answering: >= 99.5% for 2 minutes
                               # (a result recorded by Kalshi counts at once, unless Polymarket disagrees)
    max_buy=0.95,              # buy a called winner only at or below 0.95 (a 5+ point edge)
    min_dead=0.02,             # sell dead legs / lost shares down to this price (they're worth 0)
    race_cap=3000,             # cash per race
    hour_cap=6000,             # cash per rolling hour
    pace_until=21,             # before 9 pm ET (the safe East Coast wave) ...
    pace_frac=0.40,            # ... spend at most 40% of the night's cash ...
    pace_edge=0.15,            # ... unless the edge is >= 15 points (close races called early)
    keep_cash=1000,            # never spend the last 1,000
    exit_level=0.30,           # pre-call defensive exit: our side fell to <= 30% at fair value ...
    exit_hold=60,              # ... for 60 s (both sources), and the contest still pays >= fair + exit_margin
    exit_margin=0.05,
    order_ttl=300,             # unfilled rests of our limit orders expire after 5 minutes
    end_utc="2026-11-04T17:00:00+00:00",   # trading closes: stop
    skip_races=("U.S. Senate", "U.S. House"),   # chamber control: settled by hand, closed while pending
)
STATE_FILE = os.path.join(DATA_DIR, "calls_state{}.json")     # races acted on, money spent, race classes
ACTIONS_FILE = os.path.join(DATA_DIR, "calls_actions{}.jsonl")  # one line per action
                                                                # (shadow runs use "_shadow" files)


def race_state(race):
    """'TX-15 House race' -> 'TX', 'Rhode Island Senate' -> 'RI'."""
    if race[:2].isupper() and race[2:3] == "-":
        return race[:2]
    for name, code in STATES.items():
        if race.startswith(name + " "):
            return code
    return None


def polls_closed(race, now, cfg):
    st = race_state(race)
    if cfg["test_polls_closed"]:
        return True
    return st in POLL_CLOSE and now >= ELECTION_DAY + timedelta(hours=POLL_CLOSE[st])


# ---------------------------------------------------------------- real-money sources
def _get(url, params, timeout=8):
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_kalshi(tickers):
    """{ticker: P(yes)}; a recorded result counts as 1 / 0. Raises if any request fails."""
    out = {}
    tickers = sorted(tickers)
    for i in range(0, len(tickers), 100):
        for m in _get(KALSHI_MARKETS_URL, {"tickers": ",".join(tickers[i:i + 100]), "limit": 1000}).get("markets", []):
            res = (m.get("result") or "").lower()
            out[m["ticker"]] = 1.0 if res == "yes" else 0.0 if res == "no" else kalshi_market_mid(m)
    return out


def fetch_poly(slugs):
    """{slug: P(yes)}; a closed market counts at its final outcome price. Raises if any request fails."""
    out = {}
    slugs = sorted(slugs)
    for i in range(0, len(slugs), 50):
        for m in _get(POLY_URL, [("slug", s) for s in slugs[i:i + 50]] + [("limit", 100)]):
            if m.get("closed"):
                prices = m.get("outcomePrices")
                prices = json.loads(prices) if isinstance(prices, str) else prices
                out[m["slug"]] = float(prices[0]) if prices else None
            else:
                out[m["slug"]] = poly_market_mid(m)
    return out


def races_from_mapping(mapping=None):
    """{race: {"D": row, "R": row}} for two-party races we can price (plus any third contract, unpriced)."""
    races = {}
    for ex, row in (mapping or load_mapping()).items():
        p = party_of(row["market_title"])
        key = "D" if p == "Democratic" else "R" if p == "Republican" else p
        races.setdefault(race_key(row["market_title"]), {})[key] = row
    return {r: v for r, v in races.items() if "D" in v and "R" in v}


class Sources:
    """Latest P(D wins) / P(R wins) per race and per source, with the time each source last answered."""

    def __init__(self, races):
        self.races = races
        self.k_tickers = {row["kalshi_ticker"].strip() for v in races.values() for row in (v["D"], v["R"])
                          if row.get("kalshi_ticker")}
        self.p_slugs = {row["polymarket_slug"].strip() for v in races.values() for row in (v["D"], v["R"])
                        if row.get("polymarket_slug")}
        self.k, self.p, self.k_at, self.p_at = {}, {}, 0.0, 0.0
        self.pool = ThreadPoolExecutor(2)

    def poll(self):
        fk, fp = self.pool.submit(fetch_kalshi, self.k_tickers), self.pool.submit(fetch_poly, self.p_slugs)
        for fut, name in ((fk, "kalshi"), (fp, "poly")):
            try:
                res = fut.result(timeout=20)
                if name == "kalshi":
                    self.k, self.k_at = res, time.time()
                else:
                    self.p, self.p_at = res, time.time()
            except Exception as e:
                print(f"  ! {name} poll failed: {e!r}"[:200])

    def probs(self, race, stale_s):
        """{"kalshi": (pD, pR) or None, "poly": ... } -- None when the source is down/stale or unmapped."""
        v, now, out = self.races[race], time.time(), {}
        for name, data, at, col in (("kalshi", self.k, self.k_at, "kalshi_ticker"),
                                    ("poly", self.p, self.p_at, "polymarket_slug")):
            keys = [(v[x].get(col) or "").strip() for x in ("D", "R")]
            vals = [data.get(k) if k else None for k in keys]
            out[name] = tuple(vals) if now - at <= stale_s and all(x is not None for x in vals) else None
        return out


# ---------------------------------------------------------------- the mapping audit
def check_mapping():
    """Print every race whose mapping looks wrong: Kalshi ticker for the wrong party, Polymarket slug naming
    the other party, D + R far from 1 within a source, or the two sources disagreeing."""
    races = races_from_mapping()
    src = Sources(races)
    src.poll()
    problems, ok = [], 0
    for race, v in sorted(races.items()):
        issues = []
        for side, word in (("D", "democrat"), ("R", "republican")):
            t = (v[side].get("kalshi_ticker") or "").strip()
            if t and not t.endswith("-" + side):
                issues.append(f"{side} Kalshi ticker {t} is for another party")
            s = (v[side].get("polymarket_slug") or "").strip()
            other = "republican" if side == "D" else "democrat"
            if s and (word not in s or other in s):
                issues.append(f"{side} Polymarket slug {s} doesn't name the {word}s")
            if v[side].get("invert"):
                issues.append(f"{side} is marked inverted")
        pr = src.probs(race, 1e9)
        for name, pp in pr.items():
            if pp and not 0.85 <= pp[0] + pp[1] <= 1.05:
                issues.append(f"{name}: D {pp[0]:.3f} + R {pp[1]:.3f} = {pp[0] + pp[1]:.3f}")
        if pr["kalshi"] and pr["poly"]:
            for i, side in enumerate("DR"):
                if abs(pr["kalshi"][i] - pr["poly"][i]) > 0.08:
                    issues.append(f"{side}: Kalshi {pr['kalshi'][i]:.3f} vs Polymarket {pr['poly'][i]:.3f}")
        if not pr["kalshi"] and not pr["poly"]:
            issues.append("no source prices both parties")
        if issues:
            problems.append((race, issues))
        else:
            ok += 1
    for race, issues in problems:
        print(f"{race}:")
        for i in issues:
            print(f"    {i}")
    print(f"\n{ok} of {len(races)} races look right; {len(problems)} flagged above.")
    return problems


# ---------------------------------------------------------------- the night
def trading_loop_running():
    """True if `main.py trade` runs on this machine: live mode must be the only process trading."""
    return subprocess.run(["pgrep", "-f", "main.py trade"], capture_output=True).returncode == 0


def fill_estimate(book, side, action, qty, limit):
    """Shadow fills: (shares, money) a limit order would get from a YES-normalised book right now.
    Selling NO = buying YES at 1 - price; buying NO = selling YES at 1 - price."""
    yes_buy = (side == "yes") == (action == "buy")         # do we take YES asks (True) or hit YES bids?
    levels = book.get("asks" if yes_buy else "bids") or []
    yes_limit = limit if side == "yes" else 1 - limit
    shares, money = 0, 0.0
    for lv in levels:
        p = lv["price"]
        if (yes_buy and p > yes_limit + 1e-9) or (not yes_buy and p < yes_limit - 1e-9) or shares >= qty:
            break
        n = min(qty - shares, int(lv["quantity"]))
        shares += n
        money += n * (p if side == "yes" else 1 - p)
    return shares, round(money, 2)


class Night:
    def __init__(self, cup, t, cfg=CALLS):
        self.cup, self.t, self.tid, self.cfg = cup, t, t["id"], cfg
        self.live = cfg["mode"] == "live"
        sfx = "" if self.live else "_shadow"
        self.state_file, self.actions_file = STATE_FILE.format(sfx), ACTIONS_FILE.format(sfx)
        self.races = {r: v for r, v in races_from_mapping().items() if r not in cfg["skip_races"]}
        self.ex = {r: {p: v[p]["exchange_id"] for p in "DR"} for r, v in self.races.items()}
        self.src = Sources(self.races)
        self.state = self._load()
        self.since = {}                       # (kind, race, party) -> when the condition started holding
        self.pos, self.ledger, self.cash, self.pos_at = {}, {}, 0.0, 0.0

    # ---- state
    def _load(self):
        try:
            with open(self.state_file) as f:
                s = json.load(f)
        except (OSError, ValueError):
            s = {}
        for k in ("class", "acted", "exited", "virtual"):
            s.setdefault(k, {})               # virtual = shadow fills applied to positions
        s.setdefault("spent", [])             # [[unix time, money, race], ...] for the caps
        return s

    def save(self):
        with open(self.state_file + ".tmp", "w") as f:
            json.dump(self.state, f, indent=1)
        os.replace(self.state_file + ".tmp", self.state_file)

    def record(self, rec):
        rec = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "mode": self.cfg["mode"], **rec}
        with open(self.actions_file, "a") as f:
            f.write(json.dumps(rec) + "\n")

    # ---- account
    def refresh(self):
        res = self.cup.positions(self.t["slug"])
        self.pos = {p["exchangeId"]: p["quantity"] for p in res["positions"] if p["quantity"] and not p["settled"]}
        for ex, d in self.state["virtual"].items():
            self.pos[ex] = self.pos.get(ex, 0) + d
        self.ledger = {ex: q for st in arb.load_state().values() for ex, q in (st.get("legs") or {}).items()}
        self.cash = self.cup.tournament(self.t["slug"])["myBalance"]
        self.pos_at = time.time()
        if "night_cash" not in self.state:
            self.state["night_cash"] = max(0.0, self.cash - self.cfg["keep_cash"])
            self.save()

    def free(self, ex):
        """Our shares in a contract outside arb pairs (signed: + YES, - NO)."""
        q, lock = self.pos.get(ex, 0), self.ledger.get(ex, 0)
        if lock * q <= 0:
            lock = 0
        elif abs(lock) > abs(q):
            lock = q
        return q - lock

    def spent(self, race=None, within=None):
        now = time.time()
        return sum(a for at, a, r in self.state["spent"]
                   if (race is None or r == race) and (within is None or now - at < within))

    def budget(self, race, edge, now):
        """Cash we may put into one called race now: race and hourly caps, and before 9 pm ET at most
        pace_frac of the night's cash unless the edge is big (better trades come later in the night)."""
        c, total = self.cfg, self.spent()
        avail = self.cash - c["keep_cash"] if self.live else self.state["night_cash"] - total
        room = min(avail, c["race_cap"] - self.spent(race), c["hour_cap"] - self.spent(within=3600))
        if now < ELECTION_DAY + timedelta(hours=c["pace_until"]) and edge < c["pace_edge"]:
            room = min(room, c["pace_frac"] * self.state["night_cash"] - total)
        return max(0.0, room)

    # ---- signals
    def held(self, key, cond, secs):
        if not cond:
            self.since.pop(key, None)
            return False
        return time.time() - self.since.setdefault(key, time.time()) >= secs

    def classify(self, race, pr):
        """"safe" / "close", fixed the first time the race is seen (before the night, in practice)."""
        c = self.state["class"].get(race)
        if c is None:
            favs = [max(pp) for pp in pr.values() if pp]
            if not favs:
                return None
            c = self.state["class"][race] = "safe" if min(favs) >= self.cfg["safe_level"] else "close"
        return c

    def check_call(self, race, pr, now):
        """"D" / "R" once the race counts as called for that party, else None."""
        cls = self.classify(race, pr)
        if not polls_closed(race, now, self.cfg):
            return None
        k, p = pr["kalshi"], pr["poly"]
        for i, party in enumerate("DR"):
            if k and k[i] == 1.0 and (not p or p[i] >= 0.90):
                return party                              # Kalshi has recorded the result
            if k and p:
                level, secs = self.cfg["call_safe" if cls == "safe" else "call_close"]
                cond = k[i] >= level and p[i] >= level
            else:
                level, secs = self.cfg["call_one"]
                cond = bool(k or p) and (k or p)[i] >= level
            if self.held(("call", race, party), cond, secs):
                return party
        return None

    def check_exit(self, race, pr):
        """[(exchange, signed shares, fair of our side)]: shares outside pairs that the markets have
        turned decisively against (both sources <= exit_level for exit_hold; one source: half the level)."""
        out = []
        for i, party in enumerate("DR"):
            ex = self.ex[race][party]
            q = self.free(ex)
            fairs = [pp[i] if q > 0 else 1 - pp[i] for pp in pr.values() if pp] if q else []
            if not fairs:
                self.since.pop(("exit", race, party), None)
                continue
            level = self.cfg["exit_level"] if len(fairs) == 2 else self.cfg["exit_level"] / 2
            if self.held(("exit", race, party), max(fairs) <= level, self.cfg["exit_hold"]):
                out.append((ex, q, sum(fairs) / len(fairs)))
        return out

    # ---- orders
    def send(self, legs):
        """legs = [(exchange, side, action, qty, limit price)] -> one /orders/batch write (live) or
        estimated fills from the order books (shadow). Returns [(leg, shares filled, money)]."""
        out = []
        if not self.live:
            for leg in legs:
                book = self.cup.orderbook(self.tid, leg[0], 50)
                n, money = fill_estimate(book, *leg[1:])
                out.append((leg, n, money))
                sign = 1 if (leg[1] == "yes") == (leg[2] == "buy") else -1
                self.state["virtual"][leg[0]] = self.state["virtual"].get(leg[0], 0) + sign * n
                self.pos[leg[0]] = self.pos.get(leg[0], 0) + sign * n
            return out
        exp = (datetime.now(timezone.utc) + timedelta(seconds=self.cfg["order_ttl"])).strftime("%Y-%m-%dT%H:%M:%SZ")
        before = dict(self.pos)
        res = self.cup.req("POST", "/orders/batch", body={"idempotencyKey": str(uuid.uuid4()), "orders": [
            {"exchangeId": ex, "side": side, "action": action, "quantity": int(qty), "price": round_tick(px),
             "tournamentId": self.tid, "expirationDate": exp} for ex, side, action, qty, px in legs]})
        results = res.get("results") or []
        cash0 = self.cash
        self.refresh()
        for i, leg in enumerate(legs):
            r = next((x for x in results if x.get("index") == i), {})
            if not r.get("ok", True):
                print(f"    ! rejected: {json.dumps(r)[:300]}")
            n = abs(before.get(leg[0], 0) - self.pos.get(leg[0], 0))
            out.append((leg, min(n, leg[3]), None))
        if len(legs) == 1 and out:                       # one order per write -> the cash change is its money
            out[0] = (out[0][0], out[0][1], round(abs(self.cash - cash0), 2))
        return out

    def unpair(self, race, dead_sold):
        """After selling dead legs, shrink the race's pairs in the arb ledger (so the trading loop, if ever
        restarted, doesn't 'repair' the pair by buying the dead leg back)."""
        st = arb.load_state()
        legs = (st.get(race) or {}).get("legs")
        if not legs or dead_sold <= 0:
            return
        k = min(min(abs(q) for q in legs.values()), dead_sold)
        for ex in legs:
            legs[ex] += -k if legs[ex] > 0 else k
        if all(q == 0 for q in legs.values()):
            del st[race]
        arb.save_state(st)

    # ---- actions
    def act_call(self, race, winner, quotes, now):
        c, loser = self.cfg, "R" if winner == "D" else "D"
        w, l = self.ex[race][winner], self.ex[race][loser]
        # 1 + 2: everything on the losing side -- YES on the loser, NO on the winner. That's the dead leg
        # of a pair (NO+NO: NO on the winner; YES+YES: YES on the loser) plus any of our own shares there
        sells = [(ex, side, "sell", n, c["min_dead"]) for ex, side, n in
                 ((l, "yes", self.pos.get(l, 0)), (w, "no", -self.pos.get(w, 0))) if n > 0]
        lines, gained, dead_sold = [], 0.0, 0
        for leg, n, money in (self.send(sells) if sells else []):
            lines.append(f"sold {n:,} {leg[1].upper()} on the {'loser' if leg[0] == l else 'winner'}"
                         + (f" for {money:,.0f}" if money is not None else ""))
            gained += money or 0
            if self.ledger.get(leg[0], 0):              # this contract holds a pair leg: the pair is broken
                dead_sold += n
        if self.live and dead_sold:
            self.unpair(race, dead_sold)
            self.ledger = {ex: q for st in arb.load_state().values() for ex, q in (st.get("legs") or {}).items()}
        # 3: buy the winner, whichever is cheaper: YES on the winner or NO on the loser
        qw, ql = quotes.get(w, {}), quotes.get(l, {})
        choices = [(p, ex, side) for p, ex, side in
                   ((qw.get("bestAsk"), w, "yes"), (1 - ql["bestBid"] if ql.get("bestBid") else None, l, "no")) if p]
        if choices:
            price, ex, side = min(choices)
            room = self.budget(race, 1 - price, now) if price <= c["max_buy"] else 0
            qty = int(room / c["max_buy"])
            if qty > 0:
                for leg, n, money in self.send([(ex, side, "buy", qty, c["max_buy"])]):
                    if n:
                        cost = money if money is not None else n * price
                        self.state["spent"].append([time.time(), round(cost, 2), race])
                        lines.append(f"bought {n:,} {side.upper()} on {'winner' if ex == w else 'loser'} for {cost:,.0f}"
                                     f" ({n - cost:+,.0f} at settlement)")
            elif price > c["max_buy"]:
                lines.append(f"winner already at {price:.3f}: no buy")
            else:
                lines.append(f"no cash room (race/hour/pace caps) at edge {1 - price:.3f}")
        return lines, gained

    def act_exit(self, race, items, quotes):
        lines = []
        for ex, q, fair in items:
            qq = quotes.get(ex, {})
            best = qq.get("bestBid") if q > 0 else (1 - qq["bestAsk"] if qq.get("bestAsk") else None)
            if best is None or best < fair + self.cfg["exit_margin"]:
                lines.append(f"markets turned against {abs(q):,} shares (fair {fair:.2f}) but the contest pays "
                             f"{best if best is not None else 0:.3f}: held")
                continue
            floor = max(0.01, round_tick(fair + 0.02))   # never below what the shares are worth
            for leg, n, money in self.send([(ex, "yes" if q > 0 else "no", "sell", abs(q), floor)]):
                lines.append(f"defensive exit: sold {n:,} of {abs(q):,} {leg[1].upper()} at >= {floor:.3f}"
                             + (f" for {money:,.0f}" if money is not None else "") + f" (fair {fair:.2f})")
        return lines

    # ---- one poll
    def step(self):
        now = datetime.now(timezone.utc)
        self.src.poll()
        if time.time() - self.pos_at > 60:
            self.refresh()
        calls, exits = [], []
        for race in self.races:
            pr = self.src.probs(race, self.cfg["stale_s"])
            acted = self.state["acted"].get(race)
            if acted:                                   # revisit every 5 minutes: more cash room, unfilled rests
                if time.time() - acted["last"] < 300:
                    continue
                i = "DR".index(acted["winner"])
                vals = [pp[i] for pp in pr.values() if pp]
                if vals and min(vals) < 0.90:          # the markets walked the call back: hands off
                    if not acted.get("doubt"):
                        acted["doubt"] = True
                        self.alert(f"{race}: call in doubt", f"Markets dropped {acted['winner']} to {min(vals):.2f} "
                                   "after the bot acted on a call. It stopped trading this race; paste this to Claude.")
                    continue
                calls.append((race, acted["winner"]))
                continue
            winner = self.check_call(race, pr, now)
            if winner:
                calls.append((race, winner))
            elif time.time() - self.state["exited"].get(race, 0) >= 300:
                items = self.check_exit(race, pr)
                if items:
                    exits.append((race, items))
        if not calls and not exits:
            return
        ids = sorted({self.ex[r][p] for r, _ in calls for p in "DR"} | {ex for _, items in exits for ex, _, _ in items})
        quotes = self.cup.prices(self.tid, ids)
        self.refresh()

        def cheapest(c):                                # price of the winner: best edges go first
            race, winner = c
            qw, ql = quotes.get(self.ex[race][winner], {}), quotes.get(self.ex[race]["R" if winner == "D" else "D"], {})
            return min(qw.get("bestAsk") or 1, 1 - (ql.get("bestBid") or 0))

        for race, winner in sorted(calls, key=cheapest):
            first = race not in self.state["acted"]
            lines, gained = self.act_call(race, winner, quotes, now)
            self.state["acted"].setdefault(race, {"winner": winner, "at": now.isoformat(timespec="seconds")})["last"] = time.time()
            self.save()
            traded = any(x.startswith(("sold", "bought")) for x in lines)
            if first or traded:
                head = f"{race}: called for {winner}" + ("" if first else " (revisit)")
                self.report(head, lines, race=race, winner=winner, first=first)
        for race, items in exits:
            lines = self.act_exit(race, items, quotes)
            self.state["exited"][race] = time.time()
            self.save()
            self.report(f"{race}: markets turned against us", lines, race=race, exit=True)

    def report(self, head, lines, **rec):
        print(f"{datetime.now():%H:%M:%S}  {'CALL' if not rec.get('exit') else 'EXIT'}  {head}"
              + ("".join(f"\n      {x}" for x in lines) if lines else "  (nothing to trade)"))
        self.record({"head": head, "lines": lines, **rec})
        if self.live and lines:
            self.alert(head, "; ".join(lines))

    def alert(self, title, msg):
        print(f"  ALERT {title}: {msg}")
        if self.live:
            notify(title, msg, priority="high", tags="ballot_box")

    def summary(self):
        k_ok, p_ok = time.time() - self.src.k_at < self.cfg["stale_s"], time.time() - self.src.p_at < self.cfg["stale_s"]
        return (f"Kalshi {'ok' if k_ok else 'DOWN'}, Polymarket {'ok' if p_ok else 'DOWN'}  |  "
                f"{len(self.state['acted'])} race(s) called and traded, spent {self.spent():,.0f} of "
                f"{self.state.get('night_cash', 0):,.0f}  |  cash {self.cash:,.0f}")


def main(cup, t, cfg=CALLS):
    if not cfg["mode"]:
        print("calls: off (CALLS['mode'] = None)")
        return
    if cfg["mode"] == "live" and trading_loop_running():
        raise SystemExit("calls: the trading loop (main.py trade) is running; stop it first (deploy.sh stop): "
                         "only one process may trade the account on election night.")
    night = Night(cup, t, cfg)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    end = datetime.fromisoformat(cfg["end_utc"])
    night.refresh()
    print(f"{datetime.now():%H:%M:%S}  calls: {cfg['mode']} mode on {len(night.races)} races"
          + ("  [TEST: every state's polls treated as closed]" if cfg["test_polls_closed"] else ""))
    print("Settings: " + ", ".join(f"{k}={v}" for k, v in cfg.items()))
    if night.live:
        notify("Election-night bot started", night.summary(), priority="high", tags="ballot_box")
    last_beat, last_guard, paused = 0.0, 0.0, False
    while not stop and datetime.now(timezone.utc) < end:
        t0 = time.time()
        if night.live and t0 - last_guard >= 60:      # a deploy or reboot must not bring the trading loop back
            last_guard, was = t0, paused
            paused = trading_loop_running()
            if paused and not was:
                night.alert("Election-night bot paused", "The trading loop (sig-bot) is running again. Stop it "
                            "(deploy/deploy.sh stop) and the election-night bot resumes by itself.")
        try:
            if not paused:
                night.step()
        except Exception:
            print(f"! step failed:\n{traceback.format_exc()}")
        if time.time() - last_beat >= 600:
            print(f"{datetime.now():%H:%M:%S}  {night.summary()}")
            last_beat = time.time()
        while not stop and time.time() - t0 < cfg["poll_s"]:
            time.sleep(0.2)
    print(f"{datetime.now():%H:%M:%S}  calls: stopped ({'trading closed' if not stop else 'stop requested'}). "
          + night.summary())
    if night.live:          # cancel our resting orders, one contract at a time (never cancel-all without one)
        for race in night.state["acted"]:
            for ex in night.ex.get(race, {}).values():
                try:
                    cup.req("POST", "/orders/cancel-all", body={"exchangeId": ex, "tournamentId": night.tid})
                except Exception as e:
                    print(f"  ! cancel on {ex} failed: {e}")
        notify("Election-night bot stopped", night.summary(), tags="stop_sign")
