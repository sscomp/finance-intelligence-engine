#!/bin/bash
# FIE recovery-bundle restore drill — Phase 6.9D-R2 (repo deliverable).
#
#   deploy/scripts/restore-drill.sh <bundle-dir> <repo-root>
#
# Restores a 6.9D-R2 encrypted recovery bundle (two DBs + globals) into an
# ISOLATED ephemeral PostgreSQL cluster provisioned by the canonical
# provisioner (scripts/provision-test-postgres.sh). Fail-closed everywhere:
#
#   * refuses to run unless FIE_RESTORE_TARGET_DISPOSABLE=1 is exported;
#   * refuses any production-shaped target (job-owned cluster identity only);
#   * verifies bundle SHA256SUMS BEFORE any decryption;
#   * decrypts first, restores second — a corrupt artifact or wrong key
#     aborts before the target cluster exists (no mutation);
#   * the recovery key is injected by ENV VAR path (never argv), never echoed;
#   * decrypted material lives in a 0700 workdir that is destroyed at exit;
#   * evidence output is metadata-only (counts, hashes, role flags — no rows).
#
# Exit: 0 = all gates PASS; 78 = refused (config fail-closed); 1 = FAIL.
# Environment:
#   FIE_RESTORE_TARGET_DISPOSABLE=1  (required)
#   FIE_R2_RECOVERY_KEY_FILE=path    (required; 0600 recommended)
#   FIE_R2_DRILL_EVIDENCE_DIR=path   (optional; receipts copied there)
set -euo pipefail
umask 077

BUNDLE=${1:?usage: restore-drill.sh <bundle-dir> <repo-root>}
REPO=${2:?usage: restore-drill.sh <bundle-dir> <repo-root>}
TS=$(date -u +%Y%m%dT%H%M%SZ)
EVDIR="${FIE_R2_DRILL_EVIDENCE_DIR:-}"

die() { echo "DRILL-REFUSED: $*" >&2; exit 78; }
fail() { echo "DRILL-FAIL: $*" >&2; exit 1; }

# ---- gate 0: disposable declaration + identity guards ----------------------
[ "${FIE_RESTORE_TARGET_DISPOSABLE:-}" = "1" ] || die "FIE_RESTORE_TARGET_DISPOSABLE=1 not declared"
[ -n "${FIE_R2_RECOVERY_KEY_FILE:-}" ] || die "FIE_R2_RECOVERY_KEY_FILE (env) not set"
[ -f "$FIE_R2_RECOVERY_KEY_FILE" ] || die "key file missing"
stat -c '%a' "$FIE_R2_RECOVERY_KEY_FILE" | grep -qx '[4-7]00' || die "key file perms not restrictive"
[ -d "$BUNDLE" ] || die "bundle dir missing"
[ -d "$REPO" ] || die "repo root missing"
case "$(realpath "$BUNDLE")" in
  */postgresql-18/*|*/fie-6_9c-install/*|*/backups/20*) die "suspicious bundle location" ;;
esac

# ---- gate 1: bundle integrity BEFORE decryption ----------------------------
cd "$BUNDLE"
sha256sum -c SHA256SUMS >/dev/null || die "SHA256SUMS mismatch (corrupt bundle)"
T0=$(date -u +%s)
TS_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# ---- isolated workdir -------------------------------------------------------
WORK=$(mktemp -d "${TMPDIR:-/tmp}/fie-r2-drill-$TS-XXXXXX")
EVCOPY="true"
[ -n "$EVDIR" ] && { mkdir -m 700 -p "$EVDIR"; EVCOPY="cp -p"; }
cleanup() {
  if [ -n "$EVDIR" ]; then
    cp -p "$WORK/validation.json" "$WORK"/receipt-*.json "$EVDIR/" 2>/dev/null || true
  fi
  rm -rf -- "$WORK"
}
trap cleanup EXIT
chmod 700 "$WORK"

# ---- gate 2: decrypt (wrong key / corrupt cipher abort HERE) ----------------
export GNUPGHOME="$WORK/gnupg"
mkdir -m 700 "$GNUPGHOME"
gpg --batch --import "$FIE_R2_RECOVERY_KEY_FILE" >/dev/null 2>&1 || die "key import failed"
mkdir -m 700 "$WORK/dec"
for f in databases/fie_prod.dump.enc databases/fie_prod_raw.dump.enc globals-and-roles/globals.sql.enc; do
  out="$WORK/dec/$(basename "$f" .enc)"
  gpg --quiet --decrypt --output "$out" "$f" >/dev/null 2>"$WORK/gpg-err.log" \
    || die "decrypt failed for $f (corrupt ciphertext or wrong key) — no restore attempted"
done
echo "{\"step_utc\":\"$TS_UTC\",\"gates_passed\":[\"sha256sums\",\"decrypt\"]}" > "$WORK/receipt-steps-1-2.json"

# ---- gate 3: isolated cluster via canonical provisioner --------------------
for v in FIE_DB_PATH FIE_DB_TARGET_RAW FIE_DB_TARGET_INTELLIGENCE FIE_INTELLIGENCE_DB \
         FIE_DATABASE_URL FIE_TEST_PG_DSN FIE_DB_TARGETS; do unset "$v" 2>/dev/null || true; done
cd "$REPO"
# shellcheck disable=SC1091
. scripts/provision-test-postgres.sh
fie_test_pg_start
case "${FIE_TEST_PG_PORT:?provisioner did not set FIE_TEST_PG_PORT}" in
  5432|5433|8790|18720) die "job-owned port collides with a known production/test port" ;;
esac
case "$(realpath "${FIE_TEST_PG_JOB_DIR:?provisioner did not set FIE_TEST_PG_JOB_DIR}")" in
  /home/ubuntu/postgresql-18/*|/var/lib/postgresql/*) die "job dir overlaps production PGDATA" ;;
esac
echo "provisioner: port=${FIE_TEST_PG_PORT} (job-owned, loopback/socket-only)"

DSN_ROOT="$FIE_TEST_PG_DSN"        # provisioner synthetic bootstrap DB
DSN_DB() { echo "postgresql://${FIE_TEST_PG_USER}@/${1}?host=${FIE_TEST_PG_JOB_DIR}&port=${FIE_TEST_PG_PORT}"; }

# ---- gate 4: globals (roles) restore + target DBs ---------------------------
# PG 18 replay note (disclosed): the source dump carries GRANT role … GRANTED BY
# postgres, which PG 18 re-verifies against the GRANTED BY identity's admin
# option even when the executor is a superuser of a different bootstrap name.
# On a DR-host/ephemeral cluster whose bootstrap role is synthetic, this fails
# pre-mutation (target-only issue; production clusters replay fine). The drill
# preprocesses the DECRYPTED COPY in the isolated workdir: it drops the
# GRANTED BY clause so the grant is replayed by the bootstrap superuser
# (implicit admin) — semantically equivalent for a recovery target, and it
# touches nothing outside the isolated cluster.
sed -i 's/GRANTED BY [a-zA-Z_][a-zA-Z0-9_]*//' "$WORK/dec/globals.sql"
psql "$DSN_ROOT" -v ON_ERROR_STOP=1 -q -f "$WORK/dec/globals.sql" >/dev/null || fail "globals restore"
psql "$DSN_ROOT" -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE fie_prod OWNER fie_prod;" || fail "createdb fie_prod"
psql "$DSN_ROOT" -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE fie_prod_raw OWNER fie_prod;" || fail "createdb fie_prod_raw"
echo '{"globals_roles_db_create":"OK"}' > "$WORK/receipt-step4.json"

# ---- gate 5: both databases restored ----------------------------------------
for db in fie_prod fie_prod_raw; do
  pg_restore --exit-on-error -d "$(DSN_DB "$db")" "$WORK/dec/$db.dump" \
    >"$WORK/restore-$db.log" 2>&1 || fail "pg_restore $db (see restore-$db.log in evidence)"
done
RESTORE_DONE=$(date -u +%s)

# ---- gate 6: validation against bundle inventory ----------------------------
INV="$BUNDLE/database-inventory.json"
export FIE_TEST_PG_DSN
FIE_TEST_PG_DSN="$DSN_ROOT" python3 - "$INV" > "$WORK/validation.json" <<'PY'
import json, os, subprocess, sys
inv = json.load(open(sys.argv[1]))
raw = os.environ["FIE_TEST_PG_DSN"]
def db_uri(db):
    # psql 18 misparses the combo "psql <uri-positional> -d <db>" (userinfo leaks
    # into the user and host/port are lost), so the db name is embedded in the
    # URI instead. Fail loudly on any psql error — empty output must never
    # silently coerce into a "no rows" pass.
    head, qs = raw.split("?", 1)
    return head.rsplit("/", 1)[0] + "/" + db + "?" + qs
def q(sql, db):
    r = subprocess.run(["psql", "-d", db_uri(db), "-Atc", sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"psql failed rc={r.returncode}: {r.stderr.strip()[:200]}")
    return r.stdout.strip()
def counts(db):
    out = {}
    for t in q("select tablename from pg_tables where schemaname='public' order by 1", db).splitlines():
        out[t] = int(q(f"select count(*) from public.{t}", db))
    return out
def grants(db, role):
    out = subprocess.run(["psql", "-d", db_uri(db), "-Atc",
        f"select distinct privilege_type from information_schema.role_table_grants where grantee='{role}'"],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"psql failed rc={out.returncode}: {out.stderr.strip()[:200]}")
    return sorted(set(out.stdout.split()))
gates = {}
for db in ("fie_prod", "fie_prod_raw"):
    spec = inv[db]
    got_counts = counts(db)
    gates[f"tables:{db}"] = {"want": spec["tables"], "got": got_counts,
        "pass": got_counts == spec["tables"]}
    owner = q("select distinct tableowner from pg_tables where schemaname='public'", db)
    gates[f"owner:{db}"] = {"want": spec["owner_role"], "got": owner, "pass": owner == spec["owner_role"]}
# PG 18 changed psql boolean output from 't'/'f' to 'true'/'false' — normalize.
def sbool(x: str) -> bool:
    return x in ("t", "true")
flags = q("select rolname||'|'||rolsuper||'|'||rolcanlogin from pg_roles "
          "where rolname in ('fie_prod','fie_prod_runtime','fie_ro')", "fie_prod")
for line in flags.splitlines():
    name, rolsuper, canlogin = line.split("|")
    gates[f"role:{name}"] = {"raw": line, "super": sbool(rolsuper), "canlogin": sbool(canlogin),
        "pass": not sbool(rolsuper) and sbool(canlogin) == (name != "fie_ro")}
for db in ("fie_prod", "fie_prod_raw"):
    want = inv["expected_grants"].get("fie_prod_runtime", {}).get(db)
    got = grants(db, "fie_prod_runtime")
    gates[f"grants:{db}:fie_prod_runtime"] = {"want": want, "got": got,
        "pass": want is None or set(got) == set(want)}
m = int(q("select count(*) from public.schema_migrations", "fie_prod"))
gates["schema_migrations_present"] = {"want": ">=1", "got": m, "pass": m >= 1}
ok = all(v["pass"] for v in gates.values())
print(json.dumps({"all_pass": ok, "gates": gates}, indent=1))
raise SystemExit(0 if ok else 1)
PY
echo '{"validations":"PASS"}' > "$WORK/receipt-step6.json"

# ---- finalize ----------------------------------------------------------------
RTO=$((RESTORE_DONE - T0))
{
  echo "{\"drill_utc\":\"$TS_UTC\",\"rto_restore_seconds\":$RTO,"
  echo '"coverage":["fie_prod","fie_prod_raw"],"verdict":"PASS",'
  echo "\"ephemeral_job_dir\":\"$FIE_TEST_PG_JOB_DIR\"}"
} > "$WORK/receipt-drill.json"
echo "DRILL-PASS bundle=$BUNDLE restore_rto_seconds=$RTO"
echo "NOTE: ephemeral cluster $FIE_TEST_PG_JOB_DIR is left RUNNING for post-restore"
echo "      app smoke; env file: $FIE_TEST_PG_JOB_DIR/test_pg.env ; the caller MUST"
echo "      own teardown via 'source scripts/provision-test-postgres.sh; fie_test_pg_stop $FIE_TEST_PG_JOB_DIR'"