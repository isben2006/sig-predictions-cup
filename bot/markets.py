"""
Setup: download the contest's markets into mapping.csv (`markets`), then match every race to its
Kalshi / Polymarket twin (`automap`). Run via main.py.
"""
import csv, re

import requests

from bot.common import (MAPPING_FILE, MAP_COLUMNS, MAX_SOURCE_DISAGREE, POLY_SEARCH_URL, STATES, fetch_external,
                    kalshi_event, load_mapping, party_of, race_key)


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
