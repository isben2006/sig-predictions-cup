#!/usr/bin/env python3
"""
Predictions Cup trading bots: command line and main loop.

Each cycle runs fair-value gap trading (bot/strategies/gap.py) first, then the D+R arbitrage
(bot/strategies/arb.py, with --arb), then the hourly stats snapshot and notifications (bot/reporting.py).

Commands
  python3 main.py markets      # download all contest markets into mapping.csv
  python3 main.py automap      # fill in Kalshi tickers / Polymarket slugs for every race it can match
  python3 main.py scan         # show opportunities, exits, constraint violations (no orders)
  python3 main.py trade        # same as scan, but actually places orders (asks to confirm)
  python3 main.py trade --loop 300   # repeat every 300 s
  python3 main.py positions    # show your current positions
  python3 main.py leaderboard  # top 10, your rank, and the top 5% / 10% cutoffs
  python3 main.py stats        # append a snapshot to stats.csv (done hourly while looping)
  add --aggressive to scan/trade for fewer, bigger, held-to-settlement bets
  add --max-orders N to place at most N orders per run (good for a first live test)

Running unattended (24/7), both strategies in one loop on every mapped race
  caffeinate -i nohup python3 main.py trade --arb --loop 60 --yes > /dev/null 2>&1 &
  tail -f logs/bot.log          # watch it; errors are logged and the loop keeps going
  pkill -f "main.py trade"   # stop it: finishes the current cycle, then exits (safe any time)
  on a server it runs as a systemd service instead: see deploy/deploy.sh

Setup
  pip install requests
  put SIG_API_KEY=your_key (and optionally NTFY_TOPIC=...) in a .env file next to this script
  (create the key in My Profile -> API Keys, scopes: read + trade)
"""
import argparse, os, signal, sys, time, traceback
from datetime import datetime

from bot.common import API_KEY, LOG_DIR, MAPPING_FILE, Cup, Tee
from bot.markets import cmd_automap, cmd_markets
from bot.reporting import (RESTART_CMD, SUMMARY_HOUR, cmd_leaderboard, cmd_positions, daily_summary, log_stats,
                           notify, notify_gap_sales)
from bot.strategies import arb, gap


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
    ap.add_argument("--log", default=os.path.join(LOG_DIR, "bot.log"), help="log file for scan/trade output ('' to disable)")
    ap.add_argument("--out", default=MAPPING_FILE, help="automap: where to write the filled-in mapping")
    ap.add_argument("--arb", action="store_true",
                    help="each cycle, run the gap strategy, then the D+R arbitrage (bot/strategies/arb.py) on every race")
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

    cfg = dict(gap.AGGRESSIVE if a.aggressive else gap.SAFE)
    if a.max_frac is not None:
        cfg["max_frac"] = a.max_frac
    print("Profile: " + ("AGGRESSIVE" if a.aggressive else "safe") + "  " +
          ", ".join(f"{k}={v}" for k, v in cfg.items()))
    live = a.command == "trade"
    if live and not a.yes and input("This places real contest orders. Type YES to continue: ").strip() != "YES":
        sys.exit("Cancelled.")
    if a.arb:
        arb_cfg = dict(arb.ARB)
        print("Arb settings: " + ", ".join(f"{k}={v}" for k, v in arb_cfg.items()))
    stop = []                                      # pkill -> finish this cycle (orders + arb ledger), then exit
    signal.signal(signal.SIGTERM, lambda *_: (stop.append(1), print("\nStop requested: finishing this cycle.")))
    last_stats, failures, summary_day = 0.0, 0, None
    if live and a.loop:
        notify("Trading bot started", "The bot is running. You'll get a summary every day at "
               f"{SUMMARY_HOUR}:00. If a summary doesn't arrive, the bot has stopped (or its machine is down): "
               f"restart it by pasting this in Terminal: {RESTART_CMD}", priority="low", tags="white_check_mark")
    while not stop:
        if a.loop and time.time() - last_stats >= 3600:   # hourly snapshot while looping
            try:
                log_stats(cup, t)
            except Exception as e:
                print(f"  (stats snapshot failed: {e})")
            last_stats = time.time()
        try:
            # gap step first: the big edges get first claim on cash. It never touches arb pairs (the
            # ledger is saved after every arb fill), even without --arb: selling one leg would break a pair
            locked = arb.locked_shares() if os.path.exists(arb.STATE_FILE) else {}
            sold = gap.run(cup, t, live, cfg, a.max_orders, locked,
                           arb_info=arb.rotation_info() if a.arb else None)   # enables rotation into the arb
            if live:
                notify_gap_sales(sold)
            failures = 0
            if a.arb:                              # then the arb, with the cash that's left (minus its floor)
                try:
                    arb.run(cup, t, live, arb_cfg, shared=True)
                except Exception:
                    print(f"\n! arb step failed:\n{traceback.format_exc()}")
        except Exception:
            if not a.loop:
                raise
            print(f"\n! run failed, retrying in {a.loop}s:\n{traceback.format_exc()}")
            failures += 1
            if live and failures == 5:
                notify("Trading bot keeps failing", "The last 5 cycles failed (usually the contest website "
                       "is down; the bot keeps retrying by itself). If you still get this in an hour, paste "
                       "this message to Claude.", priority="high", tags="warning")
        if live and a.loop and datetime.now().hour == SUMMARY_HOUR and summary_day != datetime.now().date():
            summary_day = datetime.now().date()
            notify("Daily trading summary", daily_summary(), tags="chart_with_upwards_trend")
        if not a.loop:
            break
        for _ in range(a.loop):                    # sleep in 1 s steps so a stop request is quick
            if stop:
                break
            time.sleep(1)
    print("Stopped.")
    if live and a.loop:
        notify("Trading bot stopped", "The bot was asked to stop and finished cleanly. If you didn't do "
               f"this on purpose, restart it by pasting this in Terminal: {RESTART_CMD}", tags="stop_sign")


if __name__ == "__main__":
    main()
