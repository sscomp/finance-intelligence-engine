# FIE Production Operational Runbook (Phase 6.9B-R4)

Work order: `ABACUS_FIE_6_9B_R4_BACKUP_RESTORE_OBSERVABILITY_OPERATIONAL_
RUNBOOK_AND_PRODUCTION_ACTIVATION_READINESS.md` §11 (ten required
sections) and §13 (rollback contract). Companion contract documents:

* `docs/production-database-contract.md` (R1 — configuration/identity)
* `docs/operations/backup-restore-contract.md` (R4 — backup/restore)
* `deploy/README.md` (R2 — supervisor lifecycle, staging/activation)

Authoritative machine surfaces:

* `deploy/bin/fie-backup`, `deploy/bin/fie-restore`,
  `deploy/bin/fie-activation-checklist`
* `deploy/bin/fie-start`, `deploy/bin/fie-preflight`, `deploy/bin/fie-stage`
* `python -m phase3.operations.activation` (§12 checklist)

Naming/authority: "Production" is the target declared by the R1
production contract set (`FIE_PRODUCTION_DB_CONTRACT` / default
contract files) — never inferred from success.

---

## 1. Startup

Sequence:

1. **preflight** — `deploy/bin/fie-preflight` (exit 78 fails the start;
   never bypass). Confirms the env contract file (0600), resolvable DB
   target, tooling.
2. **start** — `deploy/bin/fie-start` (supervisor: `phase3/service/...`
   via the staged conf; supervisor artifact `deploy/supervisor/fie-http.conf`).
3. **verify health** — `GET /healthz` (liveness ONLY; unauthenticated,
   fixed envelope `{"status":"alive","service":"fie-http"}`; liveness
   probe carries no backend facts by design).
4. **verify readiness** — `GET /readyz` (authenticated). READY requires
   ALL of: runtime configuration valid, database reachable, runtime
   identity acceptable (R3 identity gate), schema version compatible
   (R3 schema gate), required runtime dependencies ready.
5. **verify DB identity** — the identity gate verdict inside `/readyz`
   is the operator surface; for deeper inspection
   `python -m phase3.persistence.identity_gate` reports the sanitized
   identity + role attributes.
6. **verify schema compatibility** — schema gate verdict inside
   `/readyz`; `schema_current_version` in the readiness payload.

Failure at any step: startup is fail-closed — readiness answers 503
with a stable code (`SCHEMA_INCOMPATIBLE` / `DEPENDENCY_UNAVAILABLE` /
`IDENTITY_INCOMPATIBLE`) and the §9 operations log carries the mapped
event. Do NOT edit data to make a gate pass.

## 2. Normal Shutdown

* Graceful stop via supervisor (`supervisorctl stop fie-http`) or
  `SIGTERM`.
* Expected exit semantics: the transport logs `shutdown_signal` and
  `server_stopped` and emits the §9 `SHUTDOWN` operations event
  (bounded, deterministic order: stop accepting → close socket → close
  runtime store); exit code 0.
* Post-stop verification: `GET /healthz` fails (process gone);
  supervisor state `STOPPED`; `pg_stat_activity` still holds the
  database (the DB is NEVER stopped by service shutdown).

## 3. Restart

1. `supervisorctl restart fie-http` (supervisor artifact unchanged).
2. Re-run Startup steps 3–6 (health, readiness, identity, schema).
3. If readiness stays 503 after restart → treat as STARTUP FAILURE
   mode below; do not loop restarts (see §4 stop-retry rule).

## 4. Service Crash

* Supervisor behavior: `autorestart=true` with backoff (r2 contract);
  rapid-crash loops are observable via supervisor status `FATAL`.
* Restart verification: Startup steps 3–6.
* When to stop retrying: after supervisor reaches `FATAL` — stop
  supervisor retries; escalate. A crash loop that clears itself is
  NOT expected behavior.
* Evidence collection: preserve (see §10 Incident Evidence): the
  supervisor log window, the §9 operations JSON lines, the last
  `/readyz` verdict, the running commit SHA.

## 5. Database Unavailable

* Expected FIE behavior: liveness stays alive; readiness answers 503
  `DEPENDENCY_UNAVAILABLE`; the §9 log emits `DATABASE_UNAVAILABLE`.
  No data repair is attempted by the runtime.
* Readiness semantics: NOT-READY is CORRECT behavior. The service never
  answers ready against a degraded store.
* Recovery procedure: restore database connectivity (this is an
  infrastructure action, NOT a service action); re-verify `/readyz`.
  If the database needs a RESTORE, follow §8 restore and its
  authorization requirements.

## 6. Schema Incompatible

* Expected fail-closed behavior: readiness answers 503
  `SCHEMA_INCOMPATIBLE`; §9 log emits `SCHEMA_INCOMPATIBLE`; traffic
  should not be served beyond read-liveness.
* Migration authority requirement: schema changes go through the
  migration authority (`phase3/persistence/migration_authority.py`)
  under its OWN authority context, with the repository migration
  pipeline. An activation requires the schema compatibility gate to
  pass — never a gate edit.
* The runtime MUST NOT self-repair schema (no auto-DDL in the runtime
  path; R3 authority separation).

## 7. Backup

Exact operator procedure:

```text
FIE_ENV_FILE=<runtime env, 0600> deploy/bin/fie-backup --label scheduled --dest <backup dir>
```

* Artifact location: `<dest>/<label>-<UTCstamp>.backup` with
  `.meta.json` and `.sha256` companions (triple written atomically —
  seeing the final name guarantees a complete triple).
* Checksum: the sidecar is the authority; restore re-verifies it.
* Validation: `python -m phase3.operations.activation` re-validates the
  newest triple (companions, checksum, age ≤ `FIE_BACKUP_MAX_AGE_HOURS`,
  default 24h). Retention is explicit-purge-ONLY; nothing is ever
  auto-deleted (doc §1 Compression/Destination/Retention).

## 8. Restore

* **Restore authorization**: a restore is an owner-authorized recovery
  action, NOT a routine deployment step. It requires a stated target,
  proof the target is disposable or the production-recovery decision is
  recorded by the owner.
* **Target validation** (all fail-closed, before any mutation):
  artifact gates A/B/C (existence, metadata, checksum — checksum
  mismatch STOPS BEFORE mutation), target gates D/G (declared
  disposable + provably not the production contract + not coinciding
  with a production-shaped source), separate verify context required.
* **Restore**:
  `FIE_ENV_FILE=... deploy/bin/fie-restore <artifact> --disposable`
  (single transaction, owner-preserving, atomic; failure leaves the
  target unchanged).
* **Verification**: schema gate + pinned row counts + runtime identity
  gate, all over the verify context; then `/readyz` on the runtime.
* **Failure handling**: every refusal is machine-readable; a refused
  restore mutates nothing. `RESTORE_PERMISSION_DENIED` is terminal —
  there is no superuser fallback. Do not grant superuser to make a
  restore pass.

## 9. Rollback (application rollback ≠ database restore)

The rollback contract answers §13's eight questions:

1. **Application binary/code rollback point**: the deployed commit —
   pinned by `FIE_EXPECTED_COMMIT` on the activation checklist; the
   rollback point is the previous accepted baseline commit
   (`81205b9…` for R4; `git checkout <baseline>` + supervisor restart).
   R2's `FIE_ROLLBACK_*` staged artifacts exist for the deployment side.
2. **Configuration rollback**: revert the runtime env contract file
   (0600) to the previous values (it is a file; the operator keeps the
   previous version before every activation — this is a documented
   operator duty, not a tool action).
3. **Supervisor artifact rollback**: re-stage the previous supervisor
   conf via `deploy/bin/fie-stage` from the baseline commit and
   `supervisorctl reread/update`; the staged conf is disposable and
   regenerable from the repo — a rollback is never source-of-truth
   loss.
4. **DB schema backward compatibility**: the schema is versioned and
   additive so far (single applied version at R3 close); a code
   rollback one baseline back is schema-compatible when the baseline
   pins the same schema version. The activation checklist's
   `SCHEMA_VERSION_COMPATIBLE` gate proves the pair.
5. **Irreversible DB migrations**: as of this work order the applied
   migration set contains no irreversible step recorded; ANY future
   migration that is not reversible MUST be recorded as irreversible in
   `phase3/persistence/migrations` metadata and in this runbook before
   deployment.
6. **Is a restore the last resort?** YES — a database restore is a
   data-loss recovery action for a DESTROYED/compromised database, NOT
   a deployment rollback mechanism. "Rollback the deploy" NEVER means
   "restore the DB" (§13 verbatim).
7. **Rollback prerequisites**: before any rollback the operator must
   have: (a) a validated recent backup triple (checksum + meta),
   (b) the current commit SHA and env-file copy recorded as incident
   evidence, (c) supervisor state captured, (d) a stated reason. With
   no valid backup, database recovery has NO proven mechanism — which
   is exactly what the `BACKUP_RECENT_AND_VALID` activation gate
   prevents.
8. **Post-rollback readiness validation**: restart + full Startup
   verification (health, readiness, identity, schema) and
   `python -m phase3.operations.activation` reporting
   `PRODUCTION_ACTIVATION_READY=true` for the rollback commit.

## 10. Incident Evidence

An operator MUST preserve, per incident:

* timestamp (UTC) of first and last observation;
* commit SHA(s) involved (`git rev-parse HEAD`, `FIE_EXPECTED_COMMIT`);
* supervisor state (`supervisorctl status`, conf file path + checksum);
* process state (pid, exit code, supervisor log tail);
* health/readiness output (`/healthz`, `/readyz` payloads + status);
* sanitized logs (the §9 `fie.operations` JSON lines; secrets are
  redacted by construction — do not add raw DSNs);
* DB/schema fingerprint (the sanitized identity + fingerprint + applied
  schema version from a backup meta sheet or the identity gate report);
* relevant backup identifier (artifact name + `source_fingerprint` +
  checksum).

Everything above is sufficient to reconstruct the incident WITHOUT any
credential and WITHOUT contacting Production from the analysis host.