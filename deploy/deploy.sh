#!/usr/bin/env bash
# Run the bot on a Linux server (Ubuntu 24.04) from this Mac.
#
#   deploy/deploy.sh setup    first time only: user, firewall, Python, code + .env + data, systemd unit
#   deploy/deploy.sh push     send code changes (never data/, logs/ or .env) and restart if code changed
#   deploy/deploy.sh pull     copy the server's data/ and logs/ back here (the server's copy is the live one)
#   deploy/deploy.sh logs     follow the bot's output
#   deploy/deploy.sh status   service state and the latest stats line
#   deploy/deploy.sh start | stop | restart
#   deploy/deploy.sh mm-logs | mm-start | mm-stop | mm-restart   the market maker (sig-mm, bot/strategies/mm.py)
#
# The server comes from SIG_SERVER (e.g. bot@1.2.3.4), in the environment or in .env.
# The SSH key comes from SIG_SSH_KEY (default ~/.ssh/sig-bot).
set -euo pipefail
cd "$(dirname "$0")/.."

SIG_SERVER=${SIG_SERVER:-$(sed -n 's/^SIG_SERVER=//p' .env 2>/dev/null | tr -d "'\"")}
SIG_SSH_KEY=${SIG_SSH_KEY:-$HOME/.ssh/sig-bot}
[ -n "$SIG_SERVER" ] || { echo "Set SIG_SERVER=bot@<ip> in .env first."; exit 1; }
DIR=sig-predictions-cup
SSH=(ssh -i "$SIG_SSH_KEY" -o IdentitiesOnly=yes)
RESTART_HINT="SIG_RESTART_CMD=cd $PWD && deploy/deploy.sh restart"   # what the server's notifications tell you to run
RSYNC=(rsync -az -e "ssh -i $SIG_SSH_KEY -o IdentitiesOnly=yes" --exclude __pycache__ --exclude .DS_Store)

push_code() {   # prints the files that changed
    "${RSYNC[@]}" --itemize-changes main.py requirements.txt README.md "$SIG_SERVER:$DIR/" | awk '$1 != "created" && $NF != "./" {print $NF}'
    "${RSYNC[@]}" --itemize-changes --delete bot/ "$SIG_SERVER:$DIR/bot/" | awk '$1 != "created" && $NF != "./" {print "bot/" $NF}'
    "${RSYNC[@]}" --itemize-changes --delete deploy/ "$SIG_SERVER:$DIR/deploy/" | awk '$1 != "created" && $NF != "./" {print "deploy/" $NF}'
}

case "${1:-}" in
setup)
    "${SSH[@]}" "root@${SIG_SERVER#*@}" 'set -e
        id bot >/dev/null 2>&1 || adduser --disabled-password --gecos "" bot
        usermod -aG sudo bot
        echo "bot ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/bot && chmod 440 /etc/sudoers.d/bot
        rsync --archive --chown=bot:bot /root/.ssh /home/bot
        ufw allow OpenSSH >/dev/null && ufw --force enable
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq && apt-get -y -qq upgrade && apt-get install -y -qq python3-venv
        timedatectl set-timezone America/New_York'
    push_code >/dev/null
    "${RSYNC[@]}" .env data logs "$SIG_SERVER:$DIR/"       # the only time local state goes up
    "${SSH[@]}" "$SIG_SERVER" "cd $DIR && python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
        grep -q ^SIG_RESTART_CMD= .env || echo '$RESTART_HINT' >> .env
        sudo cp deploy/sig-bot.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable sig-bot"
    echo "Ready. Stop the bot on this Mac (pkill -f 'main.py trade'), then: deploy/deploy.sh start"
    ;;
push)
    changed=$(push_code)
    echo "${changed:-no changes}"
    for unit in sig-bot sig-mm; do
        if grep -q "^deploy/$unit.service" <<<"$changed"; then
            "${SSH[@]}" "$SIG_SERVER" "sudo cp $DIR/deploy/$unit.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable $unit"
        fi
    done
    if grep -qE '^(main\.py|requirements\.txt|bot/.*\.py|deploy/sig-(bot|mm)\.service)$' <<<"$changed"; then
        "${SSH[@]}" "$SIG_SERVER" "cd $DIR && .venv/bin/pip install -q -r requirements.txt && sudo systemctl restart sig-bot && systemctl is-active sig-bot
            if systemctl is-enabled --quiet sig-mm 2>/dev/null; then sudo systemctl restart sig-mm && echo -n 'mm: ' && systemctl is-active sig-mm; fi"
    fi
    ;;
pull)
    "${RSYNC[@]}" "$SIG_SERVER:$DIR/data" "$SIG_SERVER:$DIR/logs" ./
    tail -1 data/stats.csv
    ;;
logs)    "${SSH[@]}" -t "$SIG_SERVER" journalctl -u sig-bot -f -o cat ;;
status)  "${SSH[@]}" "$SIG_SERVER" "systemctl status sig-bot --no-pager | head -5; grep STATS $DIR/logs/bot.log | tail -1" ;;
start|stop|restart) "${SSH[@]}" "$SIG_SERVER" "sudo systemctl $1 sig-bot && systemctl is-active sig-bot || true" ;;
mm-logs) "${SSH[@]}" -t "$SIG_SERVER" journalctl -u sig-mm -f -o cat ;;
mm-start|mm-stop|mm-restart) "${SSH[@]}" "$SIG_SERVER" "sudo systemctl ${1#mm-} sig-mm && systemctl is-active sig-mm || true" ;;
*)  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
