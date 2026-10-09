#!/bin/bash
# offhost-backup.sh — FIE 6.9D-R3 Gate B1: encrypted off-host backup pipeline
# WO: ABACUS_FIE_6_9D_R3_GATE_B_AUTOMATED_OFF_HOST_BACKUP_ACTIVATION.md
#
# Owner-authorized parameters (AUTHORIZE_R3_GATE_B1_EXECUTE):
#   destination  omarchy-a3 (physical host, separate failure domain, LUKS+btrfs at rest)
#   transport    native sshd port 2222 reached through pod tailscale socks5 1055
#                (tailnet port 22 is Tailscale-SSH identity auth, where authorized_keys
#                forced commands do NOT apply); dedicated fie-backup keypair; forced
#                commands = rrsync write-only + dest-side verify. NEVER sscomp admin.
#   schedule     daily 04:52 UTC by offhost-scheduler-loop.sh (dependency-gated) —
#                enabled only after B1.2 forced-command restriction verification
#   encryption   existing GPG public-key encrypt-to-recipient (no passphrase on the
#                backup path; private/passphrase only offline at restore; D0 custody untouched)
#   scope        all non-template databases + globals (peer auth via sudo -u postgres)
#   retention    7 daily / 4 weekly / 2 monthly — DRY-RUN ONLY this gate (proposals
#                are listed, nothing is ever deleted; real deletion needs separate authorization)
#   consistency  per-DB single-transaction pg_dump -Fc, sequential capture; cross-DB
#                point-in-time atomicity NOT claimed (disclosed in manifest)
#   fail closed  any preflight/encrypt/transfer/read-back failure => non-zero exit and a
#                Telegram alert; no destination artifact is treated as restorable
#
# Secret policy: no tokens in repo/log/scheduler; Telegram token sourced at runtime from
# /home/ubuntu/.hermes/.env and never echoed. Private GPG keys never appear in staging,
# output, logs, or this repository. ~/.gnupg and D0 key material are never touched.
#
# Usage:
#   offhost-backup.sh                    # one gated run + transfer + destination verify
#   offhost-backup.sh --dry-run-retention # retention proposal ONLY (lists; deletes nothing)
set -euo pipefail
umask 077

GPGHOME=/home/ubuntu/.gnupg-fie-backup
RECIPIENT_FP=CFF8A1D35039163ABD4D0C8FC7605C38F0CD5C38
STAGE=/home/ubuntu/fie-offhost-staging
OUT=/home/ubuntu/fie-offhost-out
LOGDIR=/home/ubuntu/fie-offhost-logs
A3_IP=100.105.45.20
A3_PORT=2222
KEY_TRANSFER=/home/ubuntu/.ssh/fie-backup-a3-transfer
KEY_VERIFY=/home/ubuntu/.ssh/fie-backup-a3-verify
HERMES_ENV=/home/ubuntu/.hermes/.env
TELEGRAM_CHAT_ID=5132341473
PROXY_CMD='nc -X 5 -x 127.0.0.1:1055 %h %p'

MODE=run
[ "${1:-}" = "--dry-run-retention" ] && MODE=dry-run-retention

RUN_ID=fie-offhost-$(date -u +%Y%m%dT%H%M%SZ)

log()  { echo "[offhost-backup $RUN_ID] $(date -u +%FT%TZ) $*"; }
fail() { echo "[offhost-backup $RUN_ID] FAIL: $*" >&2
         echo "FAIL $(date -u +%FT%TZ): $*" >> "$LOGDIR/last-offhost-status.txt"
         alert "FIE offhost backup FAIL $RUN_ID: $*" || true
         exit 1; }

# --- alert: token sourced at runtime, never echoed/logged/committed ---
alert() {
  local msg=$1
  [ "${OFFHOST_ALERT_ENABLE:-1}" = 1 ] || return 0
  [ -r "$HERMES_ENV" ] || return 0
  ( set -a; source "$HERMES_ENV"; command -v curl >/dev/null || return 0
    curl -s -m 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN:?}/sendMessage" \
      -d chat_id="$TELEGRAM_CHAT_ID" --data-urlencode "text=$msg" >/dev/null 2>&1 ) || return 0
  return 0
}

# --- retention dry-run: proposal ONLY; per owner constraint this gate never deletes ---
dry_retention() {
  echo "RETENTION DRY-RUN (policy 7 daily / 4 weekly / 2 monthly) — NOTHING DELETED (owner constraint)"
  echo "Local run history (from receipts; authoritative inventory lives on A3 under the"
  echo "restricted read-back path, listed there by the destination verify command):"
  ls -1 "$LOGDIR"/receipt-*.json 2>/dev/null | sed 's/^receipt-//; s/\.json$//' || true
  echo "Proposal only: keep newest 7 daily points + 4 weekly + 2 monthly; newest verified"
  echo "point is never a deletion candidate. Real pruning requires a separate owner authorization."
}

mkdir -p "$OUT" "$LOGDIR"; chmod 700 "$OUT" "$LOGDIR" 2>/dev/null || true

# --- lock: single instance; a second concurrent invocation fails closed ---
exec 9>> "$OUT/.lock"
flock -n 9 || { echo "[offhost-backup] lock held by another run — exit 78" >&2; exit 78; }
echo "$RUN_ID $(date -u +%FT%TZ) $$" > "$OUT/.lock.owner"

if [ "$MODE" = dry-run-retention ]; then dry_retention; exit 0; fi

SSH_BASE=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15
          -o "ProxyCommand=$PROXY_CMD")
ssh_a3() { # $1=key  $2=command string (becomes SSH_ORIGINAL_COMMAND under forced command)
  ssh "${SSH_BASE[@]}" -p "$A3_PORT" -i "$1" fie-backup@"$A3_IP" "$2"
}
RSH="ssh -p $A3_PORT -i $KEY_TRANSFER -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
     -o ConnectTimeout=15 -o ProxyCommand=\"$PROXY_CMD\""
rsync_a3() { rsync -az --perms --times -e "$RSH" "$@"; }

start=$(date -u +%FT%TZ)
log "=== start mode=$MODE"

# --- dependency-gated preflight (fail closed before touching anything) ---
mkdir -p "$STAGE"; chmod 700 "$STAGE"
sudo -u postgres psql -At -c "SELECT 1;" >/dev/null || fail "PostgreSQL not reachable (peer auth probe failed)"
# transport reachability + forced-command probe via the verify key (least-risk probe)
probe=$(ssh_a3 "$KEY_VERIFY" "" || echo __UNREACHABLE__)
echo "$probe" | grep -q "DEST-VERIFY-MODE" || fail "destination forced-command probe failed (transport path not ready): $probe"

# — explicit guard: production DBs are only ever read here (pg_dump is read-only);
#   no DDL/DML path exists in this script. Guarded against accidental scope drift:
if [ "${OFFHOST_ALLOW_DML:-0}" != 0 ] || [ "${OFFHOST_ALLOW_RESTORE:-0}" != 0 ]; then
  fail "DML/restore env flags present — this pipeline must never run mutations; refusing"
fi

# --- capture: all non-template DBs + globals (peer auth, no stored secrets) ---
dbs=$(sudo -u postgres psql -At -c \
  "SELECT datname FROM pg_database WHERE NOT datistemplate AND datallowconn ORDER BY 1;") \
  || fail "list databases failed"
[ -n "$dbs" ] || fail "no non-template databases"
SD="$STAGE/$RUN_ID"; rm -rf "$SD"; mkdir -m 700 -p "$SD"
for db in $dbs; do
  log "pg_dump -Fc $db"
  sudo -u postgres pg_dump -Fc "$db" > "$SD/$db.dump" || fail "pg_dump $db failed"
  [ -s "$SD/$db.dump" ] || fail "pg_dump $db produced empty file"
done
log "pg_dumpall --globals-only"
sudo -u postgres pg_dumpall --globals-only > "$SD/globals.sql" || fail "pg_dumpall globals failed"
[ -s "$SD/globals.sql" ] || fail "globals.sql empty"
pg_version=$(sudo -u postgres psql -At -c "SELECT version();" | awk '{print $1,$2,$3}')
snapshot=$(sudo -u postgres psql -At -c "SELECT pg_current_snapshot();")
db_list=$(printf '"%s",' $dbs | sed 's/,$//')

# --- encrypt to existing recipient (public-key only) ---
declare -A PL_SHA CT_SHA
for f in "$SD"/*.dump "$SD"/*.sql; do
  [ -f "$f" ] || continue
  log "encrypt $(basename "$f")"
  gpg --homedir "$GPGHOME" --batch --yes --trust-model always \
      -r "$RECIPIENT_FP" --output "$f.enc" --encrypt "$f" || fail "gpg encrypt $f"
  bn=$(basename "$f")
  PL_SHA[$bn]=$(sha256sum "$f" | awk '{print $1}')
  CT_SHA[$bn.enc]=$(sha256sum "$f.enc" | awk '{print $1}')
done

# --- manifest (no secrets) + SHA256SUMS, self-checked before any transfer ---
finish_local=$(date -u +%FT%TZ)
art_json=""
first=1
for name in $(printf '%s\n' "${!CT_SHA[@]}" | sort); do
  [ $first = 1 ] && first=0 || art_json="$art_json,"
  art_json="$art_json\"$name\":{\"plaintext_sha256\":\"${PL_SHA[${name%.enc}]}\",\"ciphertext_sha256\":\"${CT_SHA[$name]}\"}"
done
cat > "$SD/manifest.json" <<EOF
{
  "run_id": "$RUN_ID",
  "kind": "fie-offhost-encrypted",
  "start_utc": "$start",
  "finish_utc": "$finish_local",
  "consistency": "per-DB single-transaction pg_dump -Fc snapshots, sequential capture; cross-DB point-in-time NOT claimed; snapshot_at_first_dump=$snapshot",
  "pg_version": "$pg_version",
  "gpg_fingerprint": "$RECIPIENT_FP",
  "databases": [$db_list],
  "artifacts": {$art_json},
  "destination": {"host": "omarchy-a3", "ip": "$A3_IP", "port": "$A3_PORT", "path": "/srv/fie-backup/$RUN_ID", "account": "fie-backup (forced-command restricted)"},
  "retention": "7 daily / 4 weekly / 2 monthly — this gate DRY-RUN ONLY"
}
EOF
(cd "$SD" && for f in *.enc; do sha256sum "$f" >> "$SD/SHA256SUMS"; done)
(cd "$SD" && sha256sum -c SHA256SUMS >/dev/null) || fail "local SHA256SUMS self-check failed"

# --- transfer via forced-command rrsync (write-only; cannot read back through this key) ---
# B2 finding F1 fix: the send list must be EXPLICIT ciphertext-only. Sending the whole
# staging dir (as previously) also shipped the plaintext dumps/globals.sql to the
# destination (discovered 2026-10-09, see B2 evidence 05_remediation F1).
log "transfer ciphertext-only artifacts to fie-backup@omarchy-a3:/srv/fie-backup/$RUN_ID/"
rsync_a3 "$SD"/*.enc "$SD"/SHA256SUMS "$SD"/manifest.json \
  "fie-backup@$A3_IP:$RUN_ID/" || fail "rsync transfer failed"

# --- destination-side read-back verification (never trust the transfer exit code) ---
read_back=$(ssh_a3 "$KEY_VERIFY" "$RUN_ID") || fail "destination verify command failed"
mismatch=0
while read -r want name; do
  got=$(echo "$read_back" | awk -v n="$name" '$2==n {print $1; exit}')
  [ "$(printf '%s' "$got" | tr 'A-Z' 'a-z')" = "$want" ] || {
    echo "MISMATCH $name got=$got want=$want" >&2; mismatch=1; }
done < "$SD/SHA256SUMS"
[ "$mismatch" = 0 ] || fail "destination read-back hash mismatch — treat artifact as NOT restorable"
echo "$read_back" | grep -q "DEST-VERIFY-OK:$RUN_ID" || fail "destination verify script did not confirm success marker for $RUN_ID"

# --- receipt + status; staging plaintext/ciphertext removed after verified transfer ---
finish=$(date -u +%FT%TZ)
cat > "$LOGDIR/receipt-$RUN_ID.json" <<EOF
{
  "run_id": "$RUN_ID",
  "start_utc": "$start",
  "finish_utc": "$finish",
  "verified_destination_readback": true,
  "verify_method": "forced-command dest-verify key; sha256 re-computation on A3 inside /srv/fie-backup/$RUN_ID",
  "destination": "omarchy-a3:/srv/fie-backup/$RUN_ID",
  "databases": [$db_list],
  "ciphertext_sha256": {$(for name in $(printf '%s\n' "${!CT_SHA[@]}" | sort); do printf '"%s":"%s",' "$name" "${CT_SHA[$name]}"; done | sed 's/,$//')}
}
EOF
chmod 600 "$LOGDIR/receipt-$RUN_ID.json"
echo "PASS $finish: $RUN_ID verified off-host at omarchy-a3:/srv/fie-backup/$RUN_ID/" \
  > "$LOGDIR/last-offhost-status.txt"
rm -rf "$SD" && rmdir "$STAGE" 2>/dev/null || true
log "=== PASS run=$RUN_ID"
[ "${OFFHOST_SUCCESS_ALERT:-0}" = 1 ] && alert "FIE offhost backup OK $RUN_ID ($db_list + globals) verified off-host"
exit 0