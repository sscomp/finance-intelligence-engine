#!/bin/bash
# offhost-scheduler-loop.sh — FIE 6.9D-R3 Gate B1: daily 04:52 UTC dependency-gated loop.
# Same pattern as the proven postgresql-18 backup-scheduler.sh (no second daemon design
# change): computes the next 04:52 UTC, idles until then, then fires one gated run of
# deploy/backup/offhost-backup.sh. Any preflight failure inside offhost-backup.sh is
# fail-closed (non-zero exit + alert); the loop keeps the next-night schedule.
#
# Owner constraints honored: existing PostgreSQL backup scheduler is NOT modified
# (separate supervisord program); Production services are never restarted; scope is
# read-only dumps; retention is DRY-RUN ONLY.
#
# Dependency gates before firing (with one retry after backoff):
#   1. PostgreSQL reachable (peer-auth probe)
#   2. destination forced-command path reachable (cheap restricted-key probe)
# If a gate still fails, the night is marked FAILED-NOT-TRANSFERRED in the status file
# and an alert is sent; nothing is half-written to the destination.
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
BACKUP="$HERE/offhost-backup.sh"
LOGDIR=/home/ubuntu/fie-offhost-logs
TARGET_H=4    # 04:52 UTC
TARGET_M=52
[ -x "$BACKUP" ] || { echo "[offhost-scheduler] missing $BACKUP" >&2; exit 1; }

next_target() { # prints epoch of next 04:52 UTC (strictly in the future)
  now=$(date -u +%s)
  t=$(date -u -d "today $TARGET_H:$TARGET_M" +%s 2>/dev/null || date -u -d "tomorrow $TARGET_H:$TARGET_M" +%s)
  [ "$t" -le "$now" ] && t=$(date -u -d "tomorrow $TARGET_H:$TARGET_M" +%s)
  echo "$t"
}

gate_postgres() {
  sudo -u postgres psql -At -c "SELECT 1;" >/dev/null 2>&1
}
gate_destination() { # no args: uses verify key; forced command answers DEST-VERIFY-MODE
  out=$(ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 \
    -o "ProxyCommand=nc -X 5 -x 127.0.0.1:1055 %h %p" -p 2222 \
    -i /home/ubuntu/.ssh/fie-backup-a3-verify fie-backup@100.105.45.20 "" 2>/dev/null) || return 1
  echo "$out" | grep -q "DEST-VERIFY-MODE"
}
gate_run() { "$1" || return 1; }
gate_pass() { # gate with one retry after 300s backoff; records outcome
  local name=$1 fn=$2
  if gate_run "$fn"; then echo "GATE $name PASS $(date -u +%FT%TZ)" >> "$LOGDIR/scheduler-gates.log"; return 0; fi
  echo "GATE $name FAIL-1 $(date -u +%FT%TZ) — retry after 300s" >> "$LOGDIR/scheduler-gates.log"
  sleep 300
  if gate_run "$fn"; then echo "GATE $name PASS-RETRY $(date -u +%FT%TZ)" >> "$LOGDIR/scheduler-gates.log"; return 0; fi
  echo "GATE $name FAIL-2 $(date -u +%FT%TZ) — giving up for tonight" >> "$LOGDIR/scheduler-gates.log"
  return 1
}

mkdir -p "$LOGDIR"
echo "[offhost-scheduler] started $(date -u +%FT%TZ) (target ${TARGET_H}:${TARGET_M}Z daily)" >> "$LOGDIR/scheduler-gates.log"

while :; do
  now=$(date -u +%s); t=$(next_target)
  [ "$t" -le "$now" ] && t=$(date -u -d "tomorrow ${TARGET_H}:${TARGET_M}" +%s)
  wait=$(( t - now ))
  echo "[offhost-scheduler] next run at $(date -u -d "@$t" +%FT%TZ) (sleep ${wait}s)" >> "$LOGDIR/scheduler-gates.log"
  sleep "$wait"

  if ! gate_pass "postgres" gate_postgres; then
    echo "FAILED-NOT-TRANSFERRED $(date -u +%FT%TZ): postgres gate down" >> "$LOGDIR/last-offhost-status.txt"
    # offhost-backup.sh is not started at all, so alert directly here (token stays in env file)
    bash -c 'source /home/ubuntu/.hermes/.env 2>/dev/null; curl -s -m 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" -d chat_id=5132341473 --data-urlencode "text=FIE offhost backup FAILED-NOT-TRANSFERRED $(date -u +%FT%TZ): postgres gate down" >/dev/null' || true
    continue
  fi
  if ! gate_pass "destination" gate_destination; then
    echo "FAILED-NOT-TRANSFERRED $(date -u +%FT%TZ): destination gate down (A3 unreachable / forced-command path)" >> "$LOGDIR/last-offhost-status.txt"
    bash -c 'source /home/ubuntu/.hermes/.env 2>/dev/null; curl -s -m 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" -d chat_id=5132341473 --data-urlencode "text=FIE offhost backup FAILED-NOT-TRANSFERRED $(date -u +%FT%TZ): destination unreachable" >/dev/null' || true
    continue
  fi
  "$BACKUP" >> "$LOGDIR/runs.log" 2>&1 || {
    echo "[offhost-scheduler] backup run failed rc=$? (fail-closed; alert already sent by pipeline)" >> "$LOGDIR/scheduler-gates.log"; }
done