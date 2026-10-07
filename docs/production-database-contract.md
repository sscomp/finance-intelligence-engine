# Production Database Contract — Identity, Privilege, Persistence (Phase 6.9B-R3)

Status: **active contract** (repository-tracked, WO Task B). This document
defines the production database identity, privilege, persistence,
migration, and rollback contracts the FIE runtime must satisfy. It is
the reference for `phase3/persistence/identity_gate.py`,
`phase3/persistence/migration_authority.py`, `phase3/persistence/schema_gate.py`,
and the R3 acceptance suite (`tests/phase3/test_69b_r3_db_identity_persistence.py`).

Scope: this contract is about *database* identity. Process/deployment
identity is the Phase 6.9B-R2 deployment artifact (`deploy/README.md`);
target resolution is the Phase 6.9B-R1 runtime configuration contract
(`phase3/runtime_contract.py`).

---

## 1. Identity model

Two database identities are defined for the production shape:

| Identity | Purpose | Attributes (PostgreSQL) | Owner of |
|---|---|---|---|
| **Migration authority** (`FIE_MIGRATION_ROLE`) | explicit schema migration/bootstrap only | LOGIN, normal role (non-superuser, no CREATEROLE/CREATEDB/REPLICATION/BYPASSRLS); typically DB + schema + object **OWNER** and therefore holds schema CREATE inside its own assets | database, application schema, all migrated objects, `schema_migrations` registry |
| **Runtime authority** (`FIE_RUNTIME_ROLE`) | the FIE application at request/ingest time; consumes migrated schema | LOGIN, normal role, **non-superuser**; MUST NOT have CREATEROLE, CREATEDB, REPLICATION, BYPASSRLS, superuser | nothing (explicit grants only) |

Hard prohibitions on the runtime identity (enforced unconditionally by
the identity gate, machine-readable code in parentheses):

- PostgreSQL superuser attribute (`RUNTIME_ROLE_SUPERUSER`)
- `CREATEROLE` (`RUNTIME_ROLE_CREATEROLE`)
- `CREATEDB` (`RUNTIME_ROLE_CREATEDB`)
- REPLICATION (`RUNTIME_ROLE_REPLICATION`)
- BYPASSRLS (`RUNTIME_ROLE_BYPASSRLS`) — RLS is not currently used
  anywhere in this codebase (no `ENABLE ROW LEVEL SECURITY` outside
  these docs); the check is enforced anyway so enabling RLS later
  cannot weaken the gate
- CREATE privilege on the application schema
  (`RUNTIME_OVERPRIVILEGED_SCHEMA`)
- ownership of any relation in the application schema
  (`RUNTIME_ROLE_OWNS_OBJECTS`)
- membership in any other database role (membership is the classic
  escalation path; the runtime role is a leaf — `RUNTIME_INHERITED_ROLE`)

The runtime identity is a **dedicated** role — it is never the
administrative/bootstrap identity, never `postgres`, never the OS
account, and never shared with other services.

There is no documented unavoidable technical requirement for the
runtime identity to own the database or schema. If a future phase
needs one, it must be proven in a dedicated review — this document's
default remains non-owner.

## 2. Ownership model

```
database            <- owner: migration authority role
application schema  <- owner: migration authority role (public schema in
                       the current repository convention: migrations create
                       objects unqualified -> created in the DSN-dependent
                       default schema; see §4)
all migrated tables <- owner: migration authority role
sequences           <- owned by their tables (IDENTITY columns)
functions/triggers  <- owner: migration authority role
```

The application schema is `public` in the current repository
convention (both schema twins create unqualified objects; the readiness
schema gate reads `current_schema()`/`information_schema` and works on
either shape). Operators MUST pin the expectation via
`FIE_EXPECTED_RUNTIME_SCHEMA` when the deployment uses a dedicated
schema (fail-closed when pin and reality disagree).

## 3. Minimum grants for the runtime identity

Derived from the actual DML inventory (Task A; repositories and
retention module). **Nothing else:**

- `CONNECT` on the target database
- `USAGE` on the application schema
- `SELECT` on `schema_migrations` (read-only — the registry is
  migration-registry material; the runtime never writes it; the identity
  gate and the schema gate read it)
- `SELECT`, `INSERT`, `UPDATE`, `DELETE` on the six application tables:
  `signal_log`, `score_snapshot`, `graph_nodes`, `graph_edges`,
  `adapter_run_log`, `ingestion_errors`
- `USAGE`, `SELECT` on sequences in the application schema if any
  exist in the deployment (the current twins use `IDENTITY` columns,
  which do not require a sequence grant; the grant is optional and
  only granted when present)
- `EXECUTE` on functions in the application schema (the append-only
  guard function is invoked by triggers; execution is what the tables'
  triggers need — granted to the runtime role explicitly even though
  PostgreSQL grants EXECUTE to PUBLIC by default, so a future
  revoke-PUBLIC hardening keeps the runtime working)

**Prohibited grants** (must be absent; their presence is the R3
overprivilege detection suite's failure condition):

- CREATE on the application schema (schema/DDL authority)
- ALL/OPTIONS grants that imply CREATE; ownership of any object
- INSERT/UPDATE/DELETE on `schema_migrations` (registry is not runtime
  data; a runtime able to rewrite its own registry can erase migration
  history)
- any ROLE-administrative privilege (`CREATEROLE`), any attribute from
  §1's prohibition list, any inherited role membership
- TRUNCATE (not in the application vocabulary; DELETE-by-retention is
  the sanctioned erasure path)

The privilege verification suite pins the full grant surface
(`information_schema.role_table_grants` scan) so any extra grant on the
runtime identity is a test failure — least privilege is a pinned
invariant, not a spot check.

## 4. Migration / runtime authority separation

```
migration authority  -> python -m phase3.cli init-db  (explicit operator
                        command; PHASE3B_ENABLED-gated; R1 contract)
                     -> repository bootstrap/backfill/seed scripts
runtime authority    -> HTTP service (transport.open → open_store +
                        DefaultIntelligenceService — query-only, no DDL)
                     -> pipeline/persist arcs — consume the schema,
                        never create/alter it
```

**Code-path guards (Phase 6.9B-R3, `phase3/persistence/migration_authority.py`):**
implicit-DDL call sites consult
`auto_ddl_allowed(target)`:

- `phase3/api.py` `_open_store` — on a production-shaped target
  without authority the migration apply is REPLACED by the read-only
  schema compatibility gate (`check_schema_compatibility`); the
  runtime cannot even attempt DDL.
- `phase3/api.py` `_build_graph_store` — constructor
  `auto_migrate=True` collapses to `False` (fail-closed construction)
  on a production-shaped target without authority.
- `phase3/graph/sqlite_store.py` `SQLiteGraphStore.__init__` and
  `phase3/graph/pg_store.py` `PostgresGraphStore.__init__` —
  construction auto-migration refuses production-shaped targets without
  authority (raises `MigrationAuthorityRequired`,
  stable code `MIGRATION_AUTHORITY_REQUIRED`).
- `db.py` `init_db()` (invoked implicitly by every raw-layer `save_*`)
  — on a production-shaped target without authority there is NO DDL:
  the raw tables are proven present by a read-only probe; missing
  tables raise `RAW_SCHEMA_NOT_INITIALIZED` (fail closed, no silent
  repair).
- `phase3/bridge/seed_signals.py` — seed migration apply refuses
  production-shaped targets without authority.
- `phase3/backfill.py` `_ensure_table` — SQLite-only; the existing
  `assert_not_production` guard already refuses production-equivalent
  targets (single-owner DDL strings live in `db.py`).

The production-shaped detection uses the R1 production contract files
(`phase3/runtime_contract.load_production_identities`,
`FIE_PRODUCTION_DB_CONTRACT` env override):
a target whose identity fingerprint matches the contract files is
production-shaped. Unprovable production identity (clean-room clone
without the production layout) is treated as non-production by THIS
guard — the R1 rehearsal guards (`rehearsal_guard.assert_writable_target`,
`assert_rehearsal_target_safe`) stay the fail-closed net for write
funnels (unknown ≠ safe is enforced there).

**Structural guarantee**: even if every guard above were bypassed by a
bug, the production runtime role cannot CREATE/ALTER/DROP anything
(§3 prohibitions): the migration attempt dies with a PostgreSQL
insufficient-privilege error, and the registry write inside
`MigrationManager.apply` is denied too (§3 excludes registry writes
from runtime). The privilege-failure suite contains the exact cases.

## 5. Schema / version expectations and readiness behavior

The runtime readiness path (service `health` op →
`_readiness_schema_gate` → `_readiness_identity_gate`) proves, in
order, on every `/readyz`:

1. dependency counts readable (`DEPENDENCY_UNAVAILABLE` otherwise);
2. schema compatible with the registered migration twins — stable
   codes `REGISTRY_TABLE_MISSING`, `VERSION_RECORD_ABSENT`,
   `SCHEMA_STALE`, `SCHEMA_FUTURE_VERSION`, `SCHEMA_MALFORMED`,
   `SCHEMA_MIGRATION_FAILED`, `SCHEMA_CHECKSUM_MISMATCH`,
   `REQUIRED_OBJECTS_MISSING` → service code `SCHEMA_INCOMPATIBLE` (503);
3. runtime identity contract — codes `RUNTIME_ROLE_SUPERUSER`,
   `RUNTIME_ROLE_CREATEROLE`-family, `RUNTIME_OVERPRIVILEGED_SCHEMA`,
   `RUNTIME_ROLE_OWNS_OBJECTS`, `RUNTIME_INHERITED_ROLE`,
   `WRONG_DATABASE`, `WRONG_SCHEMA`, `WRONG_ROLE` → service code
   `IDENTITY_INCOMPATIBLE` (503).

Missing/incompatible schema NEVER triggers automatic mutation: failure
is explicit and machine-readable at the service taxonomy level. The
standalone deterministic runner is
`python -m phase3.persistence.identity_gate` (exit 0 PASS / 2
REJECTED-or-UNAVAILABLE / 78 missing-target-contract; the JSON verdict
carries catalog-derived names only).

Operator-pinned expectations (all optional; enforced only when set —
never guessed): `FIE_EXPECTED_RUNTIME_DB`, `FIE_EXPECTED_RUNTIME_SCHEMA`,
`FIE_EXPECTED_RUNTIME_ROLE`. A deployment whose PG roles differ per
environment pins all three in the operator 0600 env file
(`deploy/env/fie-runtime.env.example` schema).

**Enforcement scope (fail-closed, mirrors the migration-authority
guard)**: the readiness identity verdict is enforced when ANY of

- the connected target is provably production-shaped (R1 production
  contract fingerprint match);
- the deployment pinned runtime expectations (`FIE_EXPECTED_RUNTIME_*`)
  — a pinned deployment proves its identity wherever it runs;
- the operator explicitly opts in with `FIE_RUNTIME_IDENTITY_GATE=1`
  (staging/rehearsal hardening on any PostgreSQL target).

Disposable non-production PostgreSQL targets (test clusters) keep the
historical behavior, exactly as `migration_authority` treats an
unprovable production identity: NOT production-shaped. A PostgreSQL
store whose target DSN cannot be parsed is not provably
non-production and stays ENFORCED. Readiness therefore never answers
ready after a production-shaped least-privilege violation, while the
Phase 6.3–6.7B disposable-cluster parity legs remain meaningful.
The standalone runner `python -m phase3.persistence.identity_gate`
enforces the identity contract UNIVERSALLY (it is a deliberate
operator audit tool — invoked somewhere on purpose, it never silently
skips its verdict even on a non-production DSN); it is the readiness
wiring that applies the scope above.

## 6. Secret injection boundary

- Real credentials NEVER enter: source, `.env` examples, Supervisor
  templates, fixtures, tests, logs, reports, receipts, evidence.
- Secrets live ONLY in operator-managed 0600 env files (R2 deploy
  contract) or the isolated-test password file of a disposable
  provisioning tool; they reach PostgreSQL via DSN at process start.
- Repository artifacts carry variable NAMES and placeholders only.
- The runtime never logs its DSN (sanitized
  `database_spec_masked`; `sanitize_for_error` strips redact-able
  fragments); the identity gate reports catalog names only.
- Production credentials are not retrieved/printed by any R3
  acceptance path (the isolated suite provisions its own synthetic
  credentials).

## 7. Persistence contract

Backed by the isolated real-PostgreSQL acceptance suite (WO §3.5 steps
1–11, pytest fixture provisions its own cluster; teardown-safe): a
synthetic record written through the supported application write path
(repository writer with the RUNTIME identity) survives:

- FIE application-process stop/restart (service restart, store close/reopen);
- independent re-verification through a fresh read path
  (repository fetch + service read-back), without exposing the
  synthetic payload.

If PostgreSQL itself is restarted during any such drill, the drill must
show data survival whose ownership is unambiguous. R3's suite restarts
the APPLICATION, not the DB; a PostgreSQL restart drill belongs to the
phase-6.9B-R4 backup/restore readiness work (see §8).

## 8. Rollback / restore boundaries

Four distinct boundaries:

1. **Application/code rollback** — redeploy the previous FIE artifact
   (R2 deployment artifact layout; Supervisor points at the previous
   staged runtime). Schema stays as-is: migrations are versioned and
   the previous code consumes the same registry (schema twins are
   backward-compatible at v1).
2. **Configuration rollback** — revert the operator 0600 env file /
   staged artifact to the previous version (target DSNs, identity
   pins, authority flag). No database semantics change.
3. **Schema migration rollback** — migrations are **forward-only** by
   design (v1 only today; a broken migration is retried by
   `MigrationManager.apply` after fixing the cause; a registry failure
   row is the record). NO downgrade path is implemented or claimed.
   Reverting code to a version whose registered migration set is
   OLDER than the DB's works only when the newer migration did not
   change the older code's required objects — verified per-release, not
   guaranteed structurally. No schema downgrade is implemented.
4. **Data restore** — full-volume/database restore from backup into a
   REPLACEMENT instance, then repoint the operator env. The Production
   restore drill itself is NOT performed in R3 (WO §3.8); its
   prerequisites (this contract + the R3 fixture) are defined here.

## 9. Environment variables (contract surface)

| Variable | Meaning |
|---|---|
| `FIE_MIGRATION_AUTHORITY` | set `1` only for migration/bootstrap processes; absent for runtime |
| `FIE_EXPECTED_RUNTIME_DB` | pinned connected-database expectation (optional) |
| `FIE_EXPECTED_RUNTIME_SCHEMA` | pinned connected-schema expectation (optional) |
| `FIE_EXPECTED_RUNTIME_ROLE` | pinned connected-role expectation (optional) |

(Resolution roles `FIE_DB_TARGET_*` are the R1 contract and unchanged.)