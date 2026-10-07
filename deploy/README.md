# FIE Deployment Artifact — Supervisor lifecycle (Phase 6.9B-R2)

Canonical entrypoint (invariant, 6.9B-R1): `python -m phase3.transport.http` —
this artifact wraps exactly one runtime shape, no second path.

## Layout

```text
deploy/
  supervisor/fie-http.conf        REPOSITORY_ARTIFACT — portable template
                                  ({{PLACEHOLDER}} tokens, no secrets, no
                                  host paths)
  scripts/fie-start               start wrapper: load operator 0600 env
                                  file → cd FIE_PROJECT_ROOT → resolve
                                  interpreter (canonical resolver, no host
                                  fallback) → exec python -m
                                  phase3.transport.http (single process,
                                  Supervisor-owned; no daemonization)
  scripts/fie-preflight           deterministic preflight (§8): sanitized
                                  JSON report, exit 0/2; never connects to
                                  a database, never starts the service
  scripts/fie-stage               staging generator: template → STAGED
                                  RUNTIME ARTIFACT (fail-closed on
                                  unresolved placeholders and non-0600 env
                                  files); never activates
  env/fie-runtime.env.example     env-file SCHEMA (placeholders only;
                                  values exported; host file must be 0600)
  staged/                         (created at staging; never committed with
                                   secrets)
```

## The three state classes (WO §11)

| Class | What it is | Where it lives |
|---|---|---|
| `REPOSITORY_ARTIFACT` | reviewable template + scripts (this directory) | git-tracked |
| `STAGED_RUNTIME_ARTIFACT` | generated conf with substituted paths + the operator 0600 env file | installation tree, e.g. `<root>/deploy/staged/fie-http.conf` |
| `ACTIVE_SUPERVISOR_CONFIGURATION` | the file Supervisor's `[include]` reads (production: `/etc/supervisor/conf.d/…`) | ONLY via an explicitly authorized activation step |

## Staging procedure (validated in R2; safe to run anywhere)

```bash
<install_root>/deploy/scripts/fie-stage \
    --install-root <install_root> \
    --env-file <operator 0600 env file> \
    --runtime-user <user> \
    --log-dir <log dir>
# writes <install_root>/deploy/staged/fie-http.conf (0644)
```

Validation BEFORE any activation (isolated, job-owned):

```bash
# preflight (config/kill-switch/DB targets/auth/dirs/imports/fallback-free):
FIE_ENV_FILE=<env file> <install_root>/deploy/scripts/fie-preflight
# isolated Supervisor lifecycle (test port, own socket+tmp — never the
# production supervisor): see tests/phase3/test_69b_r2_deployment_lifecycle.py
```

## Activation (DOCUMENTED ONLY — NOT executed by R2)

```bash
install -m 0644 <install_root>/deploy/staged/fie-http.conf \
                <active supervisor include dir>/fie-http.conf
sudo supervisorctl reread
sudo supervisorctl update                       # adds program:fie-http
sudo supervisorctl status fie-http              # RUNNING after startsecs=5
sudo supervisorctl start fie-http               # (if not autostarted)
```

## Rollback / removal (VALIDATED in isolated fixture only; WO §12)

```bash
0. BEFORE activation: cp active_conf <backup-dir>/fie-http.conf.pre-r2
1. cp <backup-dir>/fie-http.conf.pre-r2 <active include dir>/fie-http.conf
2. sudo supervisorctl reread && sudo supervisorctl update
3. sudo supervisorctl restart fie-http           # back to the prior conf
# Removal entirely: supervisorctl stop fie-http && rm staged+active confs
# && reread && update. Application/data rollback boundaries: this artifact
# owns PROCESS LIFECYCLE ONLY — schema/data rollback belongs to the
# database-contract phases (6.9B-R3/R4); no data migration or DSN change
# is implied by artifact activation.
```

## Runtime environment / secret boundary (WO §9)

* Supervisor conf contains NO secrets: it carries only the non-secret
  path reference `FIE_ENV_FILE=<0600 operator file>`.
* The env file is operator-owned, mode 0600, values `export`ed,
  template/schema tracked in git (`deploy/env/fie-runtime.env.example`).
* Missing required values refuse (`deploy/scripts/fie-start`/`-preflight`
  exit 78); diagnostics name variables, never values; DSNs in reports are
  masked (`user:***@host`).
* Nothing real is ever archived into Evidence — synthetic values only.

## Logging contract (WO §10)

* single unified stream (`redirect_stderr=true`) at
  `{{FIE_LOG_DIR}}/fie-http.log` (20MB × 3 backups — supervisor-native;
  central aggregation is later Observability work, out of R2 scope);
* startup refusal → one sanitized JSON line on stderr (`{"error":{…}}`),
  exit 2 — observable, no crash-looping (exit 2 is an expected code);
* bind conflict → sanitized `{"error":{"code":"BIND_UNAVAILABLE",…}}`, exit 2;
* graceful shutdown → log events `shutdown_signal` / `server_stopped
  clean=true` (JSON) — the crash/shutdown reason is always observable;
* test harness logs live in the job-owned test root only (never
  interleaved with production logs).

## Process ownership model (WO §7.1)

`fie-start` `exec`s the interpreter — the Supervisor-spawned shell *is*
the service (no daemonizer, no double fork, no detached child);
`stopasgroup`/`killasgroup=true` keep Python threads and any helper
within the supervised process group. After a graceful stop there are by
construction no owned processes, no reapable children, no lock files
(the service holds no lifecycle locks — configuration refuses deterministically
instead of arbitrating with stale state), and no supervisor state.