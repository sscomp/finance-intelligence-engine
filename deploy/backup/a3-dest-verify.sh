#!/bin/sh
# a3-dest-verify.sh — installed on omarchy-a3, ROOT-OWNED, at /usr/local/bin/fie-backup-verify
# Forced-command verify script for the fie-backup account (forced command 2).
#
# Role (WO requirement: destination-side read-back integrity verification):
#   - re-computes sha256 of every ciphertext inside /srv/fie-backup/<RUN_ID>/ against
#     that run's SHA256SUMS file and prints the recomputed hash lines;
#   - refuses anything outside the run-id namespace (path-safety: no traversal, no abs paths).
#
# Invocation under forced command: the client's SSH command string arrives as
# SSH_ORIGINAL_COMMAND. We accept ONLY a run id of the exact shape fie-offhost-<UTC>
# (and empty = probe/list mode). Everything else is refused fail-closed.
#
# Never runs as a shell. Never touches anything outside /srv/fie-backup. No logging
# of file contents. Exit 0 only when all hashes match and the marker is printed.
set -u
BASE=/srv/fie-backup
orig=${SSH_ORIGINAL_COMMAND:-}

# allow only fie-offhost-YYYYMMDDTHHMMSSZ (or empty for probe mode)
if [ -n "$orig" ]; then
  echo "$orig" | grep -Eq '^fie-offhost-[0-9]{8}T[0-9]{6}Z$' || {
    printf 'DEST-VERIFY-REFUSED-BADARG\n'; exit 1; }
fi

if [ -z "$orig" ] || [ ! -d "$BASE/$orig" ]; then
  if [ -z "$orig" ]; then
    printf 'DEST-VERIFY-MODE ok\n'
    printf 'known-runs:\n'
    ls -1 "$BASE" 2>/dev/null | grep '^fie-offhost-[0-9]' || printf '(none)\n'
    exit 0
  fi
  printf 'DEST-VERIFY-MISSING %s\n' "$orig"; exit 1
fi

cd "$BASE/$orig" || { printf 'DEST-VERIFY-REFUSED-CD\n'; exit 1; }
[ -f SHA256SUMS ] || { printf 'DEST-VERIFY-MISSING %s/SHA256SUMS\n' "$orig"; exit 1; }

fail=0
while read -r want name; do
  case "$name" in
    *.enc) : ;;
    *) fail=1; printf 'DEST-VERIFY-REFUSED-NAME %s\n' "$name"; continue ;;
  esac
  if [ ! -f "$name" ]; then fail=1; printf 'DEST-VERIFY-MISSING %s\n' "$name"; continue; fi
  got=$(sha256sum "$name" | awk '{print $1}')
  printf '%s  %s\n' "$got" "$name"
  [ "$got" = "$want" ] || fail=1
done < SHA256SUMS

if [ "$fail" = 0 ]; then printf 'DEST-VERIFY-OK:%s\n' "$orig"; else printf 'DEST-VERIFY-FAIL:%s\n' "$orig"; exit 1; fi