# FIE 6.7B — Abacus Production Baseline (authoritative Abacus variant)

**Document class:** Authoritative record of the accepted **Abacus-host** FIE Phase 6.7B Production variant (native/supervisord/SQLite), as accepted by the cutover, soak, and final acceptance work orders on 2026-10-04.

**Document status:** Published — documentation and minimal integration reconciliation only. This commit is not a release, rebuild, or redeploy of the FIE application.

**Companion record:** `docs/production/FIE_6_7B_PRODUCTION_BASELINE.md` in this repository describes the **other-host containerized (Docker + PostgreSQL) production variant** accepted separately on 2026-10-04. That topology is intentionally NOT normalized here: on the Abacus host no Docker daemon is reachable, and the accepted Abacus production is the native variant recorded below. Each document is authoritative for its own deployment variant; neither is a rebuild or redeploy of the other.

**Freeze date:** 2026-10-04

---

## 1. Release identity

| Item | Value |
|---|---|
| Authoritative release tag | `fie-6.7b-r4` (annotated tag object `c522d1043016a46a746d90bb94824591a0f98a7e`) |
| Authoritative release SHA (peeled) | `9c3be3a6c471b8309cd4e23f3e24c0d46035c242` |
| Tag peel == accepted SHA | VERIFIED (2026-10-04, fresh `git ls-remote` + local `git cat-file`) |
| Deployed runtime | release package installed from a pinned checkout at the accepted SHA |
| Schema version | `1` (`phase3b_initial_schema`) |
| Schema registry checksum (SQLite build) | `4765a3812dff58e2cfbdc8c37926d339852c6799a59c260676afe16a18d9fb0d` |
| Schema registry checksum (PostgreSQL build, same migration) | `a281dddeb5f1b1887a5efd0c4d0f43338c753e5b02c3f6a7d6c06033b7145888` |

The two schema registry checksums are **both correct by design** — the migration registry checksum differs by backend build (SQLite vs PostgreSQL). The Abacus production value is the SQLite build; the PostgreSQL value appears only in the containerized-variant companion record.

Accepted acceptance outcomes (prior work orders, 2026-10-04): Production cutover **PASS**; final soak **7/7 PASS** (full replacement window `2026-10-04T15:11:45Z → 15:41:46Z`); restart delta **0** (pid `1908278` continuous across the whole observation span); database integrity **PASS**; Hermes continuity **PASS**; auth boundary **PASS** (401 fail-closed / 405 verified live); secret scan **CLEAN**; blockers **none**.

## 2. Abacus host topology

| Property | Accepted value |
|---|---|
| Host | Abacus production host (abacusclaw persistent `/home/ubuntu`) |
| Deploy form | Native process under `supervisord` (no Docker daemon reachable on this host) |
| Service | supervisord program `fie-api-67b`, config `/etc/supervisor/conf.d/fie-api-67b.conf` |
| Runtime user | `ubuntu` (non-root, uid 1000) |
| Service runtime | `/home/ubuntu/fie-venv-67b/bin/python3 -m phase3.transport.http` (venv without pip; release package installed) |
| Job runtime (wrappers) | `/home/ubuntu/macro-venv/bin/python3` (yfinance 1.4.1 + PyYAML 6.0.3) |
| Production directory | `/home/ubuntu/macro-report` (canonical; must remain at this path post-cutover) |
| Bind | `127.0.0.1:8790` loopback only (8787 is occupied by hermes-runtime-bridge on this host) |
| Backend | SQLite (raw layer + Phase 3B store, local files) |
| Service env | `/home/ubuntu/fie-67b-upgrade/fie-service.env` (mode 0600; values sourced at process start) |
| Logs | `/var/log/fie_api_67b.log` / `/var/log/fie_api_67b.err.log` (structured JSON) |
| Boot record | `{"auth_mode":"token","backend":"sqlite","port":8790,"schema_version":"6.5"}` |

## 3. Database layout and preservation rules

| Store | Path | Role | Preservation rule |
|---|---|---|---|
| Raw historical DB | `/home/ubuntu/macro-report/macro_history.db` (72/400/400 rows at acceptance) | raw macro/stock/institutional history | **Authoritative historical data; never DROP/TRUNCATE/re-init/replace; schema unchanged since pre-6.7B** |
| Phase 3B intelligence store | `/home/ubuntu/macro-report/metadata/intelligence_store.db` (WAL mode; 546 signals at acceptance) | persistent derived intelligence | created once via the accepted mechanism; derived data may be re-seeded idempotently from the raw layer |

Both stores are local SQLite files on Abacus; both are **host-local artifacts and excluded from Git** (`.gitignore` pattern `*.db`, plus live WAL/SHM sidecars). Git carries the schema/migration code and this document, not the databases.

The `metadata/reports/artifacts_will_fail -> /dev/null` sentinel and the per-run JSON/artifact outputs under `metadata/reports/`/`logs/` are also host-local operational artifacts, deliberately not committed.

## 4. Schema / migration / bootstrap mechanism

The **only** accepted migration/bootstrap mechanism is, run from a checkout at the accepted release SHA:

```bash
PHASE3B_ENABLED=1 python3 -m phase3.cli init-db --force --db-path <store>
```

No hand-written DDL, no manual table edits. `check_schema_compatibility` is fail-closed: a missing/mismatched registry row stops the service; that is a STOP-and-rollback condition, never a manual patch situation. The running application never creates or alters schema objects on open.

## 5. Authentication boundary

| Interaction | Accepted behavior |
|---|---|
| `GET/HEAD /healthz` | Public operational liveness probe (credential-free, fixed minimal envelope; no bypass primitive) |
| Protected routes, no/wrong credential | `401` — fail closed (verified live in production: `readyz`, `v1/intelligence/latest`) |
| Protected routes, valid bearer token | Authorized behavior (200; principal `service-consumer`) |
| Wrong method on a read path | `405` (verified live: POST → 405) |
| `readyz` payload | `backend_kind=sqlite`, `schema_current_version=1`, counts — no secret material |

The token credential lives only in the 0600 env file above (recorded by fingerprint only, e.g. `bb6aad083644…`), is never echoed to logs/tickets/evidence, and is not documented by value anywhere in this repository.

## 6. Network boundary

Loopback-only: FIE binds `127.0.0.1:8790`; no public interface, no published external port. PostgreSQL (unrelated to this variant's FIE serving) remains loopback-only on 5432. Firewalls/DNS/TLS and the hermes-runtime-bridge binding on 8787 are outside this baseline's change scope.

## 7. Hermes execution flow (production continuity)

- Hermes agent cron jobs invoke the wrappers directly: `bash /home/ubuntu/macro-report/run.sh` (daily 晨報, cron `30 8 * * 1-6`), `run_weekly.sh` (每週一 週報, `0 8 * * 1`), `run_monthly.sh` (每月 12 日 月報, `0 8 12 * *`) — all `enabled=true, state=scheduled` at baseline capture.
- The invoking Hermes agent shells do **not** activate any venv; the wrappers therefore pin `PYTHON_BIN` to the provisioned job venv themselves (§9).
- Each wrapper run: fetches raw data into `macro_history.db` (read-only), seeds signals into the persistent intelligence store through the release's idempotent upsert path, exports intelligence report artifacts, and emits the report. The daily HTTP API then serves the accumulated intelligence.
- Cron expressions, jobs list, and Hermes services (gateway/dashboard/runtime-bridge at 8642/9119/8787) are production state; this baseline commit changes none of them.

## 8. Wrapper responsibilities

| Wrapper | Responsibility |
|---|---|
| `run.sh` | Daily FIE pipeline (raw fetch → seed → score → export) |
| `run_weekly.sh` | Industry/weekly pipeline, same seams |
| `run_monthly.sh` | Company/台灣50 monthly pipeline (44+ constituents), same seams |

Both production integration points inside every wrapper are env-var contracts with accepted Abacus defaults:

```bash
PYTHON_BIN="${FIE_PYTHON:-/home/ubuntu/macro-venv/bin/python3}"
SEED_DB="${FIE_INTELLIGENCE_DB:-/home/ubuntu/macro-report/metadata/intelligence_store.db}"
```

The release defaults (`python3` from an active env; a per-run `/tmp` intelligence DB) remain available by exporting `FIE_PYTHON` / `FIE_INTELLIGENCE_DB`; the hard-coded defaults above are the accepted Abacus production behavior (decision D5). `/home/ubuntu/macro-report` as the production directory is itself canonical for this host and is already hard-coded across the Hermes cron prompts.

## 9. D1–D5 accepted architecture decisions (EXPLAINED DRIFT — not to be normalized)

| Decision | Content |
|---|---|
| D1 | Native supervisord runtime instead of Docker — no reachable Docker daemon on Abacus (container topology belongs to the other-host variant) |
| D2 | FIE bind `127.0.0.1:8790` — the default 8787 is occupied by hermes-runtime-bridge on this host |
| D3 | SQLite production backend rather than PostgreSQL — no `fie_prod` on Abacus; SQLite is a first-class supported backend (ADR-017); backend-specific schema registry checksum noted in §1 |
| D4 | Persistent Phase 3B intelligence store replaces the legacy per-run `/tmp` intelligence DB — created via the accepted `init-db` mechanism only (§4) |
| D5 | Wrapper rewiring — `PYTHON_BIN` pinned to the provisioned job venv and `SEED_DB` pointed at the persistent store (release defaults assumed an active venv + temp DB); exact diff preserved as the cutover evidence `09_wrapper_seed_rewire.diff` |

These were resolved/explicitly accepted by the operator before the cutover gate; they are explained, verified drift versus the containerized reference topology, and no future commit should silently "normalize" them.

## 10. Health / readiness semantics

- `GET /healthz` — public liveness; answers 200 with a fixed minimal envelope (`schema_version: 6.5`, `kind: healthz`, `status: alive`) independent of the database and the authenticator; the ONE public probe surface (ADR-018).
- `GET /readyz` — authenticated readiness; exercises the persistence seam; healthy shape: `status=ok`, `backend_kind=sqlite`, `schema_current_version=1`; fail-closed 401 without credential, `503`-family classification on dependency/schema conditions per the release machine contract (ADR-016/019).
- Restart-loop signal: `server_started` events in the err log exceed deliberate restarts.

## 11. Backup / restore expectations

- Verified encrypted pre-upgrade backup: `/home/ubuntu/fie-67b-upgrade/backups/20261004T125100Z-macro_history-FULL.enc` (AES256 gpg-symmetric; mode 0600; external SHA256 recorded outside the archive; passphrase in a separate 0600 file, never printed).
- Method: SQLite online-backup API → tar.gz → gpg symmetric. Future cadence: before every schema/`init-db` action and weekly, same method, digest recorded outside the archive.
- Restore is exercised only in an isolated directory with logical-content verification before anything replaces production files.
- Recovery payloads, passphrases, and credentials live outside Git by design.

## 12. Rollback model

Rollback remains READY (accepted plan, evidence `07_rollback_plan.md`): stop the service, `git checkout legacy-pre-6-7b-cutover` (local tag @ `102bab4`, fast-forwardable lineage from master, no history rewrite), restore the raw-layer DB from the encrypted backup after verified logical-content hash, revert the three wrapper rewires (or point `FIE_INTELLIGENCE_DB` at a temp path), then relaunch and validate. Rolling back loses no historical data: the 6.7B cutover wrote no new raw-layer rows beyond one in-band idempotent daily refresh, and created only a NEW store file (archive, not merge).

## 13. Operational logs and observability

- `/var/log/fie_api_67b.log` / `.err.log`: structured JSON (`FIE_LOG_LEVEL=INFO`); annotated 401 probe lines from acceptance gates are deliberate and documented in acceptance evidence.
- Pipeline run logs: `/home/ubuntu/macro-report/logs/` (per-day JSON); artifacts under `/home/ubuntu/macro-report/metadata/reports/`.
- Any change to log destinations/levels is out of this baseline's scope.

## 14. Secret-handling rules

- Never print/store: `FIE_AUTH_TOKEN` (value; fingerprint only), database/backup credentials, passphrases, private keys, Telegram/bot credentials, DSNs with embedded credentials, or env-file values.
- Evidence and documentation may carry: variable names, file paths, mode/ownership, and non-reversible fingerprints/digests.
- Git carries no secret values in any file introduced by the baseline commits.

## 15. Known non-blocking observations (still applicable, owner decisions pending)

- Full regression skips are justified, never fake-passing: PostgreSQL-parity backend tests (84 at final acceptance; psycopg extra not installed; deployment targets SQLite per D3) and path-conditional skips. Canonical runner: `PYTHONPATH=. FIE_HTTP_PORT=18720 python -m unittest discover -s tests --top-level-dir=.` — `FIE_HTTP_PORT=18720` is required on Abacus because the default transport test port collides with hermes-runtime-bridge's 8787, and 18791 collides with openclaw (test-infra matter, not an app defect).
- `Macro_History.db` (128 KB sibling of the canonical `macro_history.db`) is a stale legacy artifact, referenced by no current code path; left untouched, host-local.
- The repo-side `taiwan50_config.json` list order may momentarily differ from the host file (order carries no semantics); constituent membership is reconciled (2026 Q3: +6446 藥華藥 / −3661 世芯-KY, effective 2026-09-21).
- Quarterly constituent updates arrive via the Hermes 季度成分股更新提醒 job (quarterly reminder; manual update + verification path as accepted).

## 16. Scope statement

**Phase 6.8 is NOT authorized by this baseline or its publishing commit.** Any future production change (application, schema, credentials, topology, Hermes schedules, Phase 6.8) requires an independent governance decision / work order with its own verification gates. This document, its publishing commit, and the wrapper/config reconciliation it carries do not execute, test-open, or authorize any such change.
## 17. Post-baseline amendment — Phase 6.8A runtime DB-target contract (2026-10-05)

The database-target selection semantics described above (D5 fallback
default, `FIE_DB_PATH` / `FIE_INTELLIGENCE_DB` wrapper contract, and the
rehearsal guard) are NORMALIZED by WO ABACUS_FIE_6_8A_RUNTIME_CONTRACT_
NORMALIZATION: explicit backend classes, role-scoped targets
(`FIE_DB_TARGET_RAW` / `FIE_DB_TARGET_INTELLIGENCE` / `FIE_DB_TARGET_TEST`
/ `FIE_DB_TARGET_ROLLBACK`), ONE canonical resolver
(`phase3/runtime_contract.py`), authoritative production identity read
ONLY from the production contract files, and fail-closed behavior on
missing/ambiguous/contradictory/malformed targets. The legacy env-file
variables remain valid aliases. The `FIE_SERVICE_ENV=production`
declaration is now mandatory for accepted cron runs, and an ambient test
declaration survives wrapper-env sourcing (ambient > env file > unset).
Sanitized contract documentation: `ABACUS_FIE_6_8A_RUNTIME_CONTRACT.md`
(repo root of the 6.8A WO). No credentials appear in any documentation.

## 18. Post-baseline amendment — SQLite→PostgreSQL controlled production cutover (2026-10-05, SUPERSEDES §9 D3 AS CURRENT BACKEND)

D3 (§9) records the accepted AT-FREEZE (2026-10-04) backend choice. On
2026-10-05 the **SQLite→PostgreSQL controlled production cutover was executed and
accepted** — production backend is now **PostgreSQL** (authoritative production
datastore), per the cutover / final-acceptance work orders and the wrapper PG
env stanza (commit `35c0b3e`). D3 is therefore a **historical record of the
pre-cutover freeze**, not the current production backend statement.

Consequently:

- **SQLite role = explicit ROLLBACK / RECOVERY SOURCE ONLY.** The SQLite-era
  stores (`macro_history.db`, `intelligence_store.db`) and encrypted backups are
  preserved untouched; operating on them requires an explicit rollback contract
  (canonical `FIE_DB_TARGET_ROLLBACK` / wrapper env removal path, 6.7B baseline
  §12). SQLite retirement, deletion, or silent re-selection is NOT authorized.
- Current-state narrative: `docs/architecture/postgresql-production-architecture.md`.
- The literal wrapper defaults described in §8 remain in the wrappers as the
  documented rollback path (effective only when the wrapper-pg env file is
  removed); they are NOT the production default.
- No schema change, data mutation, credential rotation, deployment, restart,
  tag creation accompanies this documentation amendment (Phase 6.8B publication
  WO), and no further "normalization" of the D1–D5 freeze-time decisions is
  authorized by this amendment beyond the recorded backend supersession.
