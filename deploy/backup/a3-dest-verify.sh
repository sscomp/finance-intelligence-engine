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

# Resolve the trusted base and require the selected directory to be its direct
# child. Reject links (including links that currently point inside the run): the
# transfer account must not redirect either the manifest or a ciphertext.
base=$(readlink -f -- "$BASE") || { printf 'DEST-VERIFY-REFUSED-BASE\n'; exit 1; }
run=$(readlink -f -- "$BASE/$orig") || { printf 'DEST-VERIFY-REFUSED-RUN\n'; exit 1; }
[ ! -L "$BASE/$orig" ] && [ "$run" = "$base/$orig" ] || {
  printf 'DEST-VERIFY-REFUSED-RUN\n'; exit 1; }
cd "$run" || { printf 'DEST-VERIFY-REFUSED-CD\n'; exit 1; }
[ -f SHA256SUMS ] && [ ! -L SHA256SUMS ] &&
  [ "$(readlink -f -- SHA256SUMS)" = "$run/SHA256SUMS" ] || {
    printf 'DEST-VERIFY-REFUSED-MANIFEST\n'; exit 1; }

fail=0
count=0
while read -r want name || [ -n "$want$name" ]; do
  # Match the producer's sha256sum format: a 64-hex digest and a safe
  # ciphertext basename. No paths, option-like names, or escaped names.
  if ! printf '%s\n' "$want" | grep -Eq '^[0-9a-f]{64}$' ||
     ! printf '%s\n' "$name" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9_.-]*\.enc$'; then
    fail=1; printf 'DEST-VERIFY-REFUSED-ENTRY\n'; continue
  fi
  if [ ! -f "$name" ] || [ -L "$name" ] ||
     [ "$(readlink -f -- "$name")" != "$run/$name" ]; then
    fail=1; printf 'DEST-VERIFY-REFUSED-ARTIFACT\n'; continue
  fi
  # Check sha256sum's own status, rather than masking it with a pipeline.
  sum=$(sha256sum -- "$name") || {
    fail=1; printf 'DEST-VERIFY-HASH-ERROR\n'; continue; }
  got=${sum%% *}
  printf '%s  %s\n' "$got" "$name"
  count=$((count + 1))
  [ "$got" = "$want" ] || fail=1
done < SHA256SUMS
[ "$count" -gt 0 ] || fail=1

if [ "$fail" = 0 ]; then printf 'DEST-VERIFY-OK:%s\n' "$orig"; else printf 'DEST-VERIFY-FAIL:%s\n' "$orig"; exit 1; fi
