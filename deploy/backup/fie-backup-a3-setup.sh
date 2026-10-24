#!/bin/bash
# fie-backup-a3-setup.sh — FIE 6.9D-R3 Gate B1: omarchy-a3 destination provisioning.
# Runs ON omarchy-a3 as ROOT (invoked by the owner: sudo bash /tmp/fie-backup-a3-setup.sh).
# Input files (pushed to A3 /tmp beforehand by the execution session):
#   /tmp/fie-backup-a3-transfer.pub   — pod-side transfer key public half
#   /tmp/fie-backup-a3-verify.pub     — pod-side verify key public half
#   /tmp/a3-dest-verify.sh            — destination-side verify script
# Everything below is idempotent and fail-closed (sshd -t failure => drop-in removed, exit 1).
set -euo pipefail

TS=$(date -u +%Y%m%dT%H%M%SZ)
LOG=/tmp/fie-backup-setup-$TS.log
log() { echo "[$TS] $*" | tee -a "$LOG"; }

PUB_TRANSFER=/tmp/fie-backup-a3-transfer.pub
PUB_VERIFY=/tmp/fie-backup-a3-verify.pub
DEST_VERIFY=/tmp/a3-dest-verify.sh
[ -f "$PUB_TRANSFER" ] && [ -f "$PUB_VERIFY" ] && [ -f "$DEST_VERIFY" ] \
  || { echo "missing input files in /tmp" | tee -a "$LOG"; exit 1; }

# --- 1. native sshd additional port 2222 (tailnet port 22 stays Tailscale-SSH; forced
#        commands only work under native sshd). Explicit "Port 22" preserves listener.
DROPIN=/etc/ssh/sshd_config.d/15-fie-backup-port.conf
cat > "$DROPIN" <<'EOF'
# FIE 6.9D-R3 Gate B1: additional native-sshd listener for fie-backup forced-command
# authentication. Port 22 remains default (and tailnet-22 is Tailscale-SSH identity
# auth, unaffected). 2222 is reachable ONLY over the tailnet/LAN — not exposed to the
# public internet (no port-forwarding in cloudflared/on LAN router for it).
Port 22
Port 2222
EOF
if ! sshd -t; then
  log "sshd config INVALID — removing drop-in (fail-closed)"; rm -f "$DROPIN"; sshd -t || true; exit 1
fi
systemctl reload sshd || systemctl restart sshd
log "sshd reloaded with ports 22+2222 (validation sshd -t PASS)"

# --- 2. dedicated least-privilege account (never sscomp) ---
if ! id fie-backup >/dev/null 2>&1; then
  useradd -r -s /usr/bin/nologin -d /home/fie-backup -m fie-backup
  log "user fie-backup created (system account, nologin)"
else
  log "user fie-backup exists — leaving as-is"
fi

# --- 3. restricted destination directory ---
install -d -m 0750 -o fie-backup -g fie-backup /srv/fie-backup
log "/srv/fie-backup configured (0750 fie-backup:fie-backup)"

# --- 4. install destination verify script (root-owned; never writable by fie-backup) ---
install -o root -g root -m 0755 "$DEST_VERIFY" /usr/local/bin/fie-backup-verify
log "verify script installed: /usr/local/bin/fie-backup-verify (root:root 0755)"

# --- 5. forced-command authorized_keys (restrict + nologin + write-only rrsync) ---
install -d -m 0700 -o fie-backup -g fie-backup /home/fie-backup/.ssh
AK=/home/fie-backup/.ssh/authorized_keys
pub_t=$(cat "$PUB_TRANSFER")
pub_v=$(cat "$PUB_VERIFY")
{
  printf 'restrict,no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty,command="/usr/bin/rrsync -wo -no-del /srv/fie-backup" %s\n' "$pub_t"
  printf 'restrict,no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty,command="/usr/local/bin/fie-backup-verify" %s\n' "$pub_v"
} > "$AK"
chown fie-backup:fie-backup "$AK"; chmod 0600 "$AK"
log "authorized_keys installed with 2 forced-command lines (rrsync write-only + dest-verify)"

# --- 6. summary ---
log "PROVISIONING COMPLETE runid=B1A3-$TS"