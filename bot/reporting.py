"""
Reporting: positions / leaderboard printouts, the hourly stats.csv snapshot (rank, cutoffs, P&L three
ways, volume), and plain-language phone notifications (ntfy.sh) incl. the daily summary.
"""
import csv, math, os
from datetime import datetime, timedelta, timezone

import requests

from bot.common import DATA_DIR, LOG_DIR, fair_value, fetch_external, load_mapping

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")     # phone notifications via ntfy.sh (optional)
SUMMARY_HOUR = 9                                   # local hour for the daily summary
RESTART_CMD = os.environ.get("SIG_RESTART_CMD") or (   # shown in start/stop notifications; set in .env on a server
    "cd ~/Desktop/resume_projects/sig-predictions-cup && caffeinate -i nohup python3 main.py "
    "trade --arb --loop 60 --yes --tournament midterm-elections > /dev/null 2>&1 &")
LOG_FILE = os.path.join(LOG_DIR, "bot.log")
STATS_FILE = os.path.join(DATA_DIR, "stats.csv")   # hourly leaderboard / equity snapshots (for pacing and Sharpe / drawdown)
STATS_COLUMNS = ["time_utc", "rank", "ranked", "pnl", "equity", "cash", "holdings",
                 "cutoff_top5", "cutoff_top10", "leader_pnl", "sharpe_api", "pnl_sell_now", "pnl_fair",
                 "volume", "volume_top5_median"]



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
    me = []
    if lb["myRank"]:                                 # ranks can be tied: look around it for our own player id
        my_id = cup.req("GET", "/account")["id"]
        around = cup.leaderboard(slug, limit=20, offset=max(0, lb["myRank"] - 10))["leaderboard"]
        me = [e for e in around if e["profileId"] == my_id]
    row["volume"] = round(me[0]["volume"]) if me else ""
    top5 = cup.leaderboard(slug, limit=min(100, max(1, math.floor(n * 0.05))))["leaderboard"]   # turnover benchmark
    row["volume_top5_median"] = round(sorted(e["volume"] for e in top5)[len(top5) // 2]) if top5 else ""
    new_file = not os.path.exists(STATS_FILE)
    with open(STATS_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=STATS_COLUMNS)
        if new_file:
            w.writeheader()
        w.writerow(row)
    print(f"\nSTATS  rank {row['rank']} of {n}  P&L {row['pnl']:+,.0f} (sell now {row['pnl_sell_now']:+,.0f}, "
          f"at fair value {row['pnl_fair']:+,.0f})  |  top 10% {row['cutoff_top10']:+,.0f}"
          f"  top 5% {row['cutoff_top5']:+,.0f}  |  invested {row['holdings']:,.0f}, cash {row['cash']:,.0f}")


# ---------------------------------------------------------------- notifications
def notify(title, message, priority="default", tags=""):
    """Push a plain-language message to your phone (ntfy app, topic NTFY_TOPIC in .env). Never raises."""
    print(f"\nNOTIFY [{title}] {message}")
    if not NTFY_TOPIC:
        return
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"), timeout=15,
                      headers={"Title": title, "Priority": priority, "Tags": tags})
    except Exception as e:
        print(f"  (notification failed: {e})")


def notify_gap_sales(sold):
    """One plain-language phone message per gap position sold (sales reported by gap.run)."""
    for x in sold:
        result = "profit" if x["profit"] >= 0 else "loss"
        if x["why"].startswith("rotation"):
            why = ("its money earns more in the arbitrage right now ("
                   + x["why"].split(": ", 1)[1].replace("holding earns", "holding it earned") + ")")
        elif x["why"] == "converged":
            why = ("it reached its fair value, so the edge is collected" if x["profit"] >= 0 else
                   "it reached its fair value, but news had moved that value below what we paid")
        else:
            why = x["why"]
        msg = (f"Sold {x['shares']:,} {x['side']} of \"{x['title']}\" at {x['price']:.3f} "
               f"(bought at {x['paid']:.3f}): {x['profit']:+,.0f} {result}. Why: {why}.")
        if x["waiting"]:
            msg += f" {x['waiting']:,} more shares are offered and should sell over the next minutes."
        notify("Gap position sold", msg + " Nothing to do.", tags="moneybag" if x["profit"] >= 0 else "small_red_triangle_down")


def _log_lines_since(hours, path=LOG_FILE):
    """Log lines from the last `hours` hours (lines start with 'YYYY-MM-DD HH:MM:SS')."""
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(path, errors="replace") as f:
            return [l for l in f if l[:19] >= cutoff and l[:4].isdigit()]
    except OSError:
        return []


def daily_summary():
    """The last 24 hours in plain words, from stats.csv and the log. Flags anything unusual."""
    try:
        rows = list(csv.DictReader(open(STATS_FILE)))
    except OSError:
        rows = []
    if not rows:
        return "No data yet."
    now = rows[-1]
    day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
    then = next((r for r in rows if r["time_utc"] >= day_ago), rows[0])
    num = lambda r, k: float(r[k]) if r.get(k) not in (None, "") else None
    lines = _log_lines_since(24)
    sells = sum("placed sell" in l for l in lines)
    pair_sales = sum("sold D" in l for l in lines)
    failures = sum("run failed" in l or "arb step failed" in l for l in lines)
    unhedged = {l.split("!")[1].split(":")[0].strip() for l in lines if "unhedged" in l}

    pnl, cut10, cut5 = num(now, "pnl"), num(now, "cutoff_top10"), num(now, "cutoff_top5")
    msg = [f"P&L {pnl:+,.0f} (rank {now['rank']} of {now['ranked']}). "
           f"Top 10% needs {cut10:+,.0f}, top 5% needs {cut5:+,.0f}."]
    if num(then, "pnl") is not None and then is not now:
        msg.append(f"Last 24h: P&L {pnl - num(then, 'pnl'):+,.0f}.")
    msg.append(f"{sells} positions sold, {pair_sales} arbitrage pairs sold.")
    if num(now, "pnl_fair") is not None:
        msg.append(f"Value at Kalshi/Polymarket prices: {num(now, 'pnl_fair'):+,.0f}.")

    unusual = []
    f_now, f_then = num(now, "pnl_fair"), num(then, "pnl_fair")
    if f_now is not None and f_then is not None and f_now - f_then < -2000:
        unusual.append(f"value at Kalshi/Polymarket prices fell {f_now - f_then:+,.0f} (news moved against "
                       f"your positions)")
    if failures >= 10:
        unusual.append(f"{failures} failed cycles")
    if unhedged:
        unusual.append("arbitrage legs waiting for a hedge in " + ", ".join(sorted(unhedged)))
    if unusual:
        msg.append("UNUSUAL: " + "; ".join(unusual) + ". Paste this message to Claude to decide what to do.")
    else:
        msg.append("Everything normal, nothing to do.")
    return " ".join(msg)
