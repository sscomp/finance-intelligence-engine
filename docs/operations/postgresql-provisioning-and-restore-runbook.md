# POSTGRESQL PROVISIONING AND RESTORE RUNBOOK — FIE 6.9D-R2

Operational runbook for provisioning PostgreSQL 18.4 for FIE and restoring a
6.9D-R2 encrypted recovery bundle. Anything touching the production cluster is
owner-gated; the restore path below is written for controlled/disposable
targets and was validated in the Gate B drill (2026-10-08).

---

## A. Provisioning

### A.1 External prerequisites

| item | requirement |
|---|---|
| PostgreSQL major | **18** (bundle dumps taken with PG 18.4 `pg_dump -Fc`) |
| binaries | `initdb`, `pg_ctl`, `psql`, `pg_restore`, `pg_dumpall` ≥ 18 |
| encoding | cluster default **UTF8** (production identity; drill provisioner pins UTF8) |
| locale | `C.UTF-8` or equivalent UTF-8 locale |

### A.2 Permanent cluster (manual init)

```bash
pg_createcluster 18 fie -- --encoding=UTF8 --locale=C.UTF-8   # Debian-style
# or generic:
initdb -D <PGDATA> --encoding=UTF8 --locale=C.UTF-8
pg_ctl -D <PGDATA> -l <log> start
```

- Listen/socket policy for the application runtime: loopback + local socket
  only (production parity; confirmed in preflight — the FIE service binds
  127.0.0.1 and the DB is never network-exposed).
- Create the application roles **only via the bundle's `globals.sql`** (next
  section) so roles/auth-membership/privileges match provenance. Do not
  hand-create `fie_prod`, `fie_prod_runtime`, or `fie_ro`.

### A.3 Isolated/ephemeral cluster (validation path)

```bash
. scripts/provision-test-postgres.sh && fie_test_pg_start
```

- Job-owned ephemeral PG 18.4 (random synthetic user/db, loopback + socket
  bind, random port), UTF8, fail-closed identity guard (exit 78) against
  production-flavored env. `fie_test_pg_stop <job-dir>` reaps only job-owned
  state.
- This is the path the Gate B drill used; it doubles as the drill fixture.

---

## B. Restore from a 6.9D-R2 recovery bundle

### B.0 Inputs

| input | source | gate |
|---|---|---|
| bundle dir `recovery-bundle-…` | off-host (disaster) or local mirror | verify `sha256sum -c SHA256SUMS` → all OK |
| recovery key | owner custodian (fingerprint `CFF8A1D35039163ABD4D0C8FC7605C38F0CD5C38`) | 0600 file via `FIE_R2_RECOVERY_KEY_FILE` (env-injected, never argv) |
| target | isolated/ephemeral or an owner-authorized target | **owner gate for anything production-shaped** |

### B.1 The one command (automated, fail-closed)

```bash
export FIE_RESTORE_TARGET_DISPOSABLE=1
export FIE_R2_RECOVERY_KEY_FILE=/secure/recovery-private-key.asc
deploy/scripts/restore-drill.sh <bundle-dir> <repo-root>
```

Order of operations inside (all fail-closed, exit 0/78/1):

1. **Integrity before anything** — `sha256sum -c SHA256SUMS`; mismatch aborts.
2. **Decrypt before restore** — GPG decrypt of `databases/*.dump.enc`,
   `globals-and-roles/globals.sql.enc` into a 0700 workdir; wrong key/corrupt
   ciphertext aborts with the target cluster not yet existing (no mutation).
3. **Provision isolated cluster** — canonical provisioner; refuses any
   production-shaped target (job-owned identity; port must avoid
   5432/5433/8790/18720; job dir must not overlap production PGDATA).
4. **Roles + DBs** — globals replayed into the cluster; `CREATE DATABASE
   fie_prod OWNER fie_prod` (+ `fie_prod_raw`). PG 18 `GRANTED BY` replay
   note below.
5. **Data restore** — `pg_restore --exit-on-error` for both DBs.
6. **Validation vs inventory** — per-table row counts, table owners,
   role super/login flags, and the `fie_prod_runtime` grants matrix compared
   against the bundle's `database-inventory.json`; `schema_migrations` ≥ 1.
   Leaves the cluster RUNNING for post-restore app smoke; caller owns
   teardown (`fie_test_pg_stop`).

### B.2 Manual equivalent (for a permanent, owner-authorized target)

```bash
gpg --decrypt -o /tmp-secure/globals.sql globals-and-roles/globals.sql.enc   # 0700 dir
psql -d "$DSN" -v ON_ERROR_STOP=1 -q -f globals.sql                          # roles
createdb --owner fie_prod fie_prod && createdb --owner fie_prod fie_prod_raw
gpg --decrypt -o /tmp-secure/fie_prod.dump databases/fie_prod.dump.enc
# NOTE: give the full per-DB URI as the ONLY -d value — 'psql/pg_restore
# "<uri>" -d <db>' misparses (userinfo leaks into user, host/port lost);
# embed the db name in the URI: postgresql://<user>@/fie_prod?host=<dir>&port=<port>
pg_restore --exit-on-error --dbname "$URI_FIE_PROD" /tmp-secure/fie_prod.dump   # no --clean against prod
```

- **Never** `--clean`/`--create` over a live production DB; restores to a
  running production cluster are an owner-gated, offline-decision procedure.
- Delete decrypted material after each step (`shred -u`).

### B.3 PG 18 `GRANTED BY` disclosure (read before manual restore)

The bundle's `globals.sql` carries `GRANT role … GRANTED BY postgres` (PG 18
dumps replay the grantor identity and re-verify its admin option). On an
ephemeral/DR cluster whose bootstrap role is synthetic, this fails
pre-mutation. The drill preprocesses the **decrypted copy in the isolated
workdir** (`sed 's/GRANTED BY <role>//'`) so the bootstrap superuser replays
the grants — semantically equivalent for a recovery target and target-only.
Production clusters (same `postgres` role name as source) replay unchanged.

### B.4 Post-restore smoke

```bash
curl http://127.0.0.1:<port>/healthz                                # 200
curl -H "Authorization: Bearer $FIE_AUTH_TOKEN" .../readyz          # 200
```

Fresh-clone runtime validated end-to-end (6.9D-R2 Gate B): clone →
`bootstrap --all` → restored DBs → smoke 200/200.

---

## C. Recurring backups (existing contract)

- Production daily scheduler: supervisord program
  `postgresql-backup-scheduler` → daily dumps of `fie_prod` + `fie_prod_raw`
  to `/home/ubuntu/postgresql-18/backups/YYYYMMDD/` (**unencrypted,
  same-pod** — see backup_design and OPEN_GAPS for the off-host gap).
- 6.9D-R2 adds the **encrypted off-host** path (one-shot): pg_dump → GPG
  cv25519 → Syncthing off-host → independent verification. Scheduler-based
  recurring off-host rotation is NOT yet authorized (OPEN_GAPS item 4).
- Export never touches the running cluster beyond logical reads
  (`pg_dump -Fc`, single transaction per DB); an export failure must never
  stop production nor overwrite the previous valid bundle.