#!/bin/bash
# skills_matning.sh: raawr US-098's measurement on core, ways a, b and c, one side instance each.
#
#   tools/skills_matning.sh [reps]     # run from /root/skills_matning (this dir copied there), Henrik's yes first
#
# The deployed app is copied and started as a side instance (ports 18080/18081, the stand-in "attrapp", its cloud
# minutes in the test ledger) with ROST_SKILLS empty (a), b, then c; tools/skills_matning.py asks each question
# `reps` times (default 2). Every light.* is saved before and each one whose on/off changed is put back after
# (tools/ljus_lage.py: s6/s7 switch the office window lamp, a wrong guess
# could switch another), the side instance stopped and PROVPLATS removed, also on failure. Results: ut-<way>.jsonl here.
# Never a question about a lock, the alarm, the garage, the TV or the living room (tools/skills_fragor.jsonl).
set -u
K=$(cd "$(dirname "$0")" && pwd)
REPS=${1:-2}
PY=/opt/raawr-rostagent/venv/bin/python
[ -e /root/PROVPLATS ] && { echo "PROVPLATS finns redan: avbryter"; exit 1; }
touch /root/PROVPLATS
aterstall() {
  systemctl stop prov-skills-a prov-skills-b prov-skills-c 2>/dev/null
  [ -s "$K/ljus-fore.json" ] && $PY "$K/ljus_lage.py" aterstall "$K/ljus-fore.json"
  rm -rf /root/PROVPLATS /root/provapp-skills
}
rm -f "$K/ljus-fore.json"  # never put back a snapshot from an earlier run
trap aterstall EXIT  # before sourcing: a failing source must not leave PROVPLATS behind (B, #90)
set -a; . /etc/raawr-rostagent.env; . /run/raawr-rostagent/secrets.env; set +a
$PY "$K/ljus_lage.py" spara "$K/ljus-fore.json" || exit 1
rm -rf /root/provapp-skills && cp -aL /opt/raawr-rostagent/app /root/provapp-skills
for V in ${VAGAR:-a b c}; do  # VAGAR="a" measures only today's way (b and c need comms US-089)
  S=$V; [ $V = a ] && S=""
  systemctl reset-failed "prov-skills-$V" 2>/dev/null
  systemd-run --unit=prov-skills-$V -p WorkingDirectory=/root/provapp-skills \
    -p EnvironmentFile=/etc/raawr-rostagent.env -p EnvironmentFile=/run/raawr-rostagent/secrets.env \
    /usr/bin/env -u PHASE_IDLE_DEBOUNCE_MS -u FOLLOW_UP_OPEN_DELAY_MS WEBSOCKET_PORT=18080 ANNOUNCE_PORT=18081 \
    ROST_SKILLS=$S "DEVICE_PROVIDERS=attrapp=gemini" $PY -m app.main >/dev/null 2>&1
  sleep 12
  $PY "$K/skills_matning.py" "$K/skills_fragor.jsonl" --vag $V --url ws://127.0.0.1:18080/ \
    --unit prov-skills-$V --reps "$REPS" --ut "$K/ut-$V.jsonl" | tail -1
  systemctl stop prov-skills-$V; sleep 2
done
