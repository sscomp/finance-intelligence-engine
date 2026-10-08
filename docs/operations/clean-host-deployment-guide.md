# CLEAN-HOST DEPLOYMENT GUIDE — FIE (Phase 6.9D-R2)

Purpose: install a working FIE runtime on a clean Linux host — **no `fie-venv-67b`, no Abacus-specific paths, no inherited host state** — from a fresh Git clone plus an encrypted recovery bundle. Validated end-to-end in the 6.9D-R2 Gate B drill (isolated ephemeral PostgreSQL 18.4).

---

## 0. What "clean host" needs (external prerequisites only)

| item | version / note |
|---|---|
| Linux x86_64 | glibc ≥ 2.31 (validated on Ubuntu-class) |
| System Python | ≥ 3.11 (venv + pip; the canonical resolver may add others) |
| Git | any recent |
| PostgreSQL server | **18.4** (same major line; `initdb`/`pg_ctl`/`psql`/`pg_restore`) — or use the repo's portable ephemeral distribution (18.4.0, Maven-pinned, sha-verified, no sudo) |
| GnuPG | 2.x (to decrypt a 6.9D-R2 recovery bundle) |
| Supervisor | optional — for service mode (template in `deploy/supervisor/fie-http.conf`) |

## 1. Fresh clone + bootstrap (validated)

```bash
git clone <repo-url-or-path> fie && cd fie
bash scripts/bootstrap.sh --all      # runtime + test + postgres extras
```

- Creates `<repo>/.venv` from `pyproject.toml` only (idempotent, second run verified no-op in drill).
- **Interpreter independence verified**: the clone venv references the system interpreter — it never touches `fie-venv-67b` (checked via `pyvenv.cfg`).
- Dependency lock: `requirements-frozen.txt` (pip freeze captured during the drill) accompanies this guide; `pip lock` was unavailable on the drill host so hash-pinned locking is an open gap (see OPEN_GAPS).

## 2. PostgreSQL provisioning

Preferred (isolated/validation): repo provisioner —
```bash
. scripts/provision-test-postgres.sh && fie_test_pg_start
```
Permanent host: PG 18.4 cluster per `POSTGRESQL_PROVISIONING_AND_RESTORE_RUNBOOK.md` §A.2–A.3; roles restored from the bundle's `globals.sql`; application runtime role `fie_prod_runtime` is a **non-superuser** login with the grant matrix recorded in `database-inventory.json`.

## 3. Environment contract

Copy `deploy/env/fie-runtime.env.example`, fill values, place at 0600:

```bash
install -o "$RUN_USER" -m 0600 /dev/null /etc/fie/fie-runtime.env   # then edit
```
Mandatory keys (canonical serve-plane profile): `FIE_SERVICE_ENV`, `FIE_PROJECT_ROOT`, `FIE_HTTP_HOST`(=127.0.0.1), `FIE_HTTP_PORT`, `FIE_AUTH_MODE=token`, `FIE_AUTH_TOKEN`, `FIE_DB_TARGET_RAW`, `FIE_DB_TARGET_INTELLIGENCE`. Every value MUST be `export`ed (non-export assignments are invisible to the child — Phase 6.7B lesson). **Secret injection: operator-owned 0600 file only; never commit, never echo, never bake into images.** Optional 6.9B-R4 keys (`FIE_BACKUP_DIR`, `FIE_BACKUP_MAX_AGE_HOURS`, restore context vars) per contract doc.

## 4. Service wiring (Supervisor)

`deploy/supervisor/fie-http.conf` is the portable template; `deploy/scripts/fie-stage` substitutes install-specific paths and emits the STAGED artifact (the only file that may land in a supervisor include dir). Lifecycle contract already tuned: `autorestart=unexpected`, `exitcodes=0,2`, `startsecs=5`, graceful TERM (30s), `stopasgroup/killasgroup`.

## 5. Data restore (disaster recovery)

Bring the databases up by restoring the encrypted recovery bundle (see runbook §B): checksums → decrypt (custodian key) → globals → `pg_restore` both DBs → validated by `deploy/scripts/restore-drill.sh`. Default-reject semantics apply on any production-shaped target.

## 6. Backup scheduler

Reference implementation (validated on Abacus): supervisord program `postgresql-backup-scheduler` running `backup-scheduler.sh` daily. Portable template and off-host sync guidance: `POSTGRESQL_PROVISIONING_AND_RESTORE_RUNBOOK.md` §E + OFF_HOST_RECOVERY_ATTESTATION conditions (no deletion protection in bare sync — enable versioning; verify off-host placement independently).

## 7. Smoke, upgrade, rollback, uninstall (validated chain)

```bash
FIE_AUTH_TOKEN=$(grep FIE_AUTH_TOKEN /etc/fie/fie-runtime.env | cut -d= -f2-)
curl http://127.0.0.1:<port>/healthz                                  # → 200 alive
curl -H "Authorization: Bearer $FIE_AUTH_TOKEN" http://127.0.0.1:<port>/readyz   # → 200
```
- **Upgrade**: stop service → `git fetch && git checkout <rev>` → rerun `bootstrap.sh --all` → start → smoke. Application binaries/config rollback = revisit `<rev>`; **database restore** is a separate, gated procedure (runbook §B) with data-loss implications — never conflate the two.
- **Uninstall**: supervisor stop/remove conf, remove install root + env file + logs; cluster teardown via `fie_test_pg_stop` (ephemeral) or owned `pg_ctl stop` (permanent).

## 8. Validated drill facts (6.9D-R2 Gate B)

| fact | value |
|---|---|
| clone source rev | bb8a66c81a6a76c16f21f02629755ede709c6f45 (fresh `git clone`) |
| bootstrap --all | exit 0, 12 s; second run exit 0 (idempotent) |
| legacy venv involvement | none (pyvenv.cfg inspected) |
| restore | `fie_prod` + `fie_prod_raw` from AES-encrypted off-host-retrieved ciphertext; counts/owners/grants validated vs `database-inventory.json` |
| app smoke | fresh-clone runtime + `/healthz` 200 + authenticated `/readyz` 200 on isolated port |
| drill evidence | `ABACUS_FIE_6_9D_R2_EVIDENCE/05_deployment/`, `06_restore_drill/` |