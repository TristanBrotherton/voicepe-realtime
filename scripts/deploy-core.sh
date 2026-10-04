#!/usr/bin/env bash
# Deploy the voice agent (openai_realtime_voice_agent/) to raawr core.
# Run by Rolle after a merge to main, from a checkout of that commit:
#
#   scripts/deploy-core.sh                 # deploy this checkout to core
#   scripts/deploy-core.sh --rollback      # put the previous version back
#   DEPLOY_HOST=root@10.10.0.118 scripts/deploy-core.sh
#
# What it knows about core (learnt the hard way, 2026-10-04):
# - /opt/raawr-rostagent/app is a SYMLINK to repo/openai_realtime_voice_agent,
#   which is not a git checkout. A backup must follow the link (cp -aL).
# - rsync must NOT --delete: the agent writes recordings/ in that directory.
# - Files arrive owned by the deployer; the service runs as raawr, so the
#   directory is chowned afterwards or the agent dies on "Permission denied".
# - /etc/raawr-rostagent.env holds the keys and the engine choice. Never touched.
#
# Health: the service is active, the agent listens on 127.0.0.1:8080, and no
# "Fatal error" since the restart. A failed health check rolls back on its own.
set -euo pipefail

HOST="${DEPLOY_HOST:-root@10.10.0.118}"
ROOT="/opt/raawr-rostagent"
DIR="$ROOT/repo/openai_realtime_voice_agent"
SRC="$(cd "$(dirname "$0")/.." && pwd)/openai_realtime_voice_agent"

remote() { ssh -o ConnectTimeout=10 "$HOST" "$@"; }

halsa() {
  # $1 = UTC time of the restart
  remote "since='$1'
    for i in \$(seq 1 30); do
      if journalctl -u raawr-rostagent --since \"\$since\" --no-pager | grep -q 'Fatal error'; then
        echo 'FATAL in the log'; exit 1; fi
      if systemctl is-active -q raawr-rostagent && ss -ltn | grep -q '127.0.0.1:8080 '; then
        echo \"healthy after \$((i*2)) s\"; exit 0; fi
      sleep 2
    done
    echo 'not healthy within 60 s'; exit 1"
}

omstart() {
  local t
  t="$(remote 'date -u +%T')"
  remote 'systemctl restart raawr-rostagent'
  halsa "$t"
}

if [[ "${1:-}" == "--rollback" ]]; then
  bak="$(remote "ls -dt $ROOT/app.bak-deploy-* 2>/dev/null | head -1")"
  [[ -n "$bak" ]] || { echo "no backup to roll back to" >&2; exit 1; }
  echo "rolling back to $bak"
  remote "rsync -a --exclude recordings '$bak/' '$DIR/' && chown -R raawr:raawr '$DIR'"
  omstart
  exit $?
fi

[[ -f "$SRC/config.yaml" ]] || { echo "run from a voicepe-realtime checkout ($SRC missing)" >&2; exit 1; }
ver="$(sed -n 's/^version: *"\(.*\)"/\1/p' "$SRC/config.yaml")"
commit="$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo '?')"
echo "deploying voice agent $ver ($commit) to $HOST"

lock="$(remote 'cat /root/PROVPLATS 2>/dev/null || true')"
if [[ -n "$lock" ]]; then
  echo "WARNING: provplats is held: $lock" >&2
  echo "(a merge deploy goes ahead; tell that track its test is overwritten)" >&2
fi

# Backup that follows the symlink; keep the three newest.
stamp="$(date -u +%Y%m%d%H%M%S)"
remote "cp -aL '$ROOT/app' '$ROOT/app.bak-deploy-$stamp' &&
  ls -dt $ROOT/app.bak-deploy-* | tail -n +4 | xargs -r rm -rf"

# Dependencies are not installed by this script. Say so if they changed.
if ! remote "cat '$DIR/poetry.lock'" 2>/dev/null | cmp -s - "$SRC/poetry.lock"; then
  echo "WARNING: poetry.lock differs from core's; update the venv by hand ($ROOT/venv)" >&2
fi

rsync -a --exclude .git --exclude tests --exclude __pycache__ --exclude recordings \
  "$SRC/" "$HOST:$DIR/"
remote "chown -R raawr:raawr '$DIR'"
remote "grep '^version' '$DIR/config.yaml'"

if ! omstart; then
  echo "health check failed - rolling back" >&2
  "$0" --rollback
  exit 1
fi
echo "done: $ver ($commit) live on core"
