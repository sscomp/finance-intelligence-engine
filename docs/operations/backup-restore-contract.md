# FIE Production Database Backup / Restore Contract (Phase 6.9B-R4)

Work order: `ABACUS_FIE_6_9B_R4_BACKUP_RESTORE_OBSERVABILITY_OPERATIONAL_RUNBOOK_AND_PRODUCTION_ACTIVATION_READINESS.md` (§4, §5, §6, §7).

This document is the operator-facing statement of the contracts that
`phase3/persistence/pg_backup.py` enforces. The code is the execution
authority; this document is the written authority.

---

## 1. Backup contract (§4)

### Command surface

```text
deploy/bin/fie-backup            # wrapper (env contract, 0600 file check)
python -m phase3.persistence.pg_backup backup --label <label> --dest <dir> [--db-target <url>]
```

Exit discipline: `0` = PASS, `2` = classified failure (machine-readable
JSON refusal on stderr), `78` = configuration refusal (fail-closed env
contract: `FIE_ENV_FILE` missing or not mode 0600, no resolvable source
target).

### Source

* Explicit `--db-target URL-form` (`postgres://` / `postgresql://`) wins;
  otherwise the R1 canonical env (`FIE_DB_TARGET_INTELLIGENCE`).
  No other default exists.
* Success is NEVER "command exit 0". A PASS report exists only when ALL
  of the following are proven:
  1. artifact exists and is non-empty;
  2. `pg_restore --list` accepts the artifact (consumability, BEFORE
     the PASS is reported);
  3. a SHA-256 checksum sidecar (`<artifact>.sha256`) was generated and
     matches the artifact bytes;
  4. the metadata sheet (`<artifact>.meta.json`) exists;
  5. the metadata records the expected database + schema identity:
     `source_backend`, `source_identity` (credential-free
     `postgres@host@port@db` text), `source_fingerprint` (16-hex),
     `source_production_shaped` (three-state);
  6. the applied schema version (`schema_version_applied`) and the
     per-table row counts (`table_row_counts`) are pinned at dump time;
  7. tooling is recorded: `pg_dump_version`, `compression`,
     `created_utc`, `label`.

### Artifact triple + write discipline

```text
<label>-<UTCstamp>.backup          the pg_dump custom-format artifact
<label>-<UTCstamp>.backup.meta.json
<label>-<UTCstamp>.backup.sha256
```

* The dump is written to `.{artifact}.backup.partial` and atomically
  renamed to the final name LAST — a consumer that sees the final name
  always sees a complete triple.
* Any failure deterministically removes the partial file and orphan
  siblings for that name. A backup that fails leaves no half-written
  artifacts.
* A same-named artifact already in the destination is REFUSED
  (`BACKUP_DESTINATION_INVALID`); existing bytes are never silently
  overwritten.

### Compression, destination, retention (policy, documented)

* Compression: pg_dump custom format (gzip-equivalent) — the default,
  recorded in the metadata `compression` field.
* Destination: explicit `--dest`. The production schedule (once the
  owner authorizes activation) writes into a host-local backup
  directory; the destination contract is: explicit, documented, and
  never a repo path consumed by the service.
* Retention: retention is EXPLICIT-PURGE-ONLY (repository `retention.py`
  doctrine). There is NO automatic deletion: every artifact triple in
  the destination stays until an operator removes it deliberately. The
  activation checklist freshness bound (`FIE_BACKUP_MAX_AGE_HOURS`,
  default 24) asserts that a RECENT valid backup exists; it never
  deletes.

### Failure semantics

| Code | Meaning |
|------|---------|
| `BACKUP_SOURCE_NOT_POSTGRES` | resolved source is not a PostgreSQL URL target |
| `BACKUP_SOURCE_UNREACHABLE` | source not openable / catalog pin unreadable |
| `BACKUP_DUMP_FAILED` | pg_dump failed, or produced an empty/consumability-refused artifact |
| `BACKUP_DESTINATION_INVALID` | missing/invalid dest, or same-named artifact exists |

Failures are machine-readable (`{result: REJECTED, code, detail}` on
stderr with secrets redacted), emit `BACKUP_FAILED` into the §9
operations log, and clean up partials.

### Secret handling

`pg_dump` receives credentials ONLY via subprocess environment
(`PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE`). Credentials never appear
in argv, logs, JSON reports, or metadata. Reported identity is always
the sanitized text + fingerprint.

---

## 2. Restore contract (§5)

### Command surface

```text
deploy/bin/fie-restore            # wrapper
python -m phase3.persistence.pg_backup restore <artifact> [--db-target <url>] [--verify-dsn <url>] --disposable
```

Environment contract: `FIE_RESTORE_TARGET_DSN` (the mutation/owner
context), `FIE_RESTORE_VERIFY_DSN` (a SEPARATE verification context —
the restore mutation context can never certify its own result),
`FIE_RESTORE_TARGET_DISPOSABLE=1` (or `--disposable`).

### Default-reject doctrine (§5 rule 1 — verbatim authority)

The restore default is default-reject: any target that is not provably
disposable is refused. Restore REFUSES any target that is not PROVABLY
disposable. The target
must be (a) explicitly declared disposable AND (b) provably absent from
the R1 production contract set — AND the contract set must be READABLE:
when the production contracts cannot be read from this environment the
target cannot be proven disposable and the restore refuses
(`RESTORE_TARGET_NOT_DISPOSABLE`). There is NO implicit Production DSN
fallback; there is NO silent target/source production coincidence —
when the artifact's recorded source was production-shaped and the
target's fingerprint equals the recorded source fingerprint the restore
refuses (`RESTORE_TARGET_COINCIDES_WITH_SOURCE`).

### Gate order (each before any target contact where marked)

| Gate | Code | Contact-free? |
|------|------|--------------|
| A artifact missing | `BACKUP_NOT_FOUND` | yes |
| B artifact zero-byte / no metadata / unconsumable | `BACKUP_INVALID` | yes |
| C sidecar checksum mismatch | `CHECKSUM_MISMATCH` | yes (STOPS BEFORE mutation) |
| D unsafe/undeclared/unprovable target | `RESTORE_TARGET_NOT_DISPOSABLE` | yes |
| G target coincides with production-shaped source | `RESTORE_TARGET_COINCIDES_WITH_SOURCE` | yes |
| E target unreachable | `RESTORE_TARGET_UNREACHABLE` | (first contact, read-only) |
| H pg_restore failure | `RESTORE_FAILED` / `RESTORE_PERMISSION_DENIED` | mutation, atomic |
| I/J schema/data/identity verify | `RESTORE_SCHEMA_INCOMPATIBLE` / `RESTORE_DATA_MISMATCH` / `RESTORE_IDENTITY_INCOMPATIBLE` | verify context |

### Restore semantics

* `pg_restore --single-transaction --clean --if-exists --exit-on-error`
  either fully lands or fully rolls back (no partial state, no silent
  success). Owner-roles are preserved (no `--no-owner`): the runtime
  must NEVER own the objects; a missing owner role is a classified
  refusal, never a fallback. No superuser fallback exists in any path.
* Post-restore verification runs over the SEPARATE verify context and
  gates: schema compatibility, pinned row counts vs the metadata sheet,
  and the runtime identity gate. PASS requires all three.
* Restored database remains bound by the R3 privilege boundaries
  (prohibited DDL / registry writes stay migration-authority-only).

### Required drill chain (§5) — the drill PASS receipt

seed isolated DB → backup → destroy/recreate isolated restore target →
restore → schema verification → data verification → runtime identity
verification → FIE readiness verification. Each drill proves
`NO_PRODUCTION_CONTACT`, `NO_UNRELATED_MUTATION`, `NO_SILENT_SUCCESS`
(see Evidence 05–11).

---

## 3. RPO / RTO baseline (§7)

Measured lab baselines live in Evidence 12 (`_rpo_rto_measurement`).
They are a LAB BASELINE measured on disposable clusters on this host
class — NOT an SLA. The record separates:

* measured baseline (seconds, per drill);
* proposed target (engineering suggestion, owner may adjust);
* actual SLA: **UNKNOWN** (no SLA is asserted anywhere in this work
  order — fabricating one is forbidden).

RPO: the backup cadence is manual/tool-driven at activation readiness
time; the measured artifact pin carries `created_utc` so an operator
reading a restore point can state exactly how much data the restore
would return.