# FIE Runtime Configuration — Operator Guide (Phase 6.9B-R1)

How to configure and validate the FIE runtime before/without touching the
serve plane. Companion to `docs/contracts/runtime_configuration_contract.md`
(the normative contract) and `docs/contracts/runtime_configuration_inventory.json`
(machine-readable inventory, test-pinned).

## 1. HTTP invocation (serve plane)

```text
command:  python3 -m phase3.transport.http        # canonical entrypoint
console:  fie-http-server                          # pyproject entry point
precheck: python3 -m phase3.transport.http --check-config
```

`--check-config` resolves the transport contract exactly as startup does,
prints a sanitized JSON summary, and exits — it never binds and never
connects to a database.

## 2. Required configuration

| Profile | Required |
|---|---|
| local/test | nothing required to load; DB-target resolution still fails closed at first use (no implicit default) |
| staging/production | explicit non-`none` auth mode + non-empty `FIE_AUTH_TOKEN`, and an explicit `FIE_DATABASE_URL`/DB-target contract (no CWD-relative SQLite fallback) |

Production-shaped example (placeholders only; operator puts real values in
an untracked 0600 env file, e.g. `~/fie-67b-upgrade/fie-service.env`):

```text
FIE_SERVICE_ENV=production
FIE_HTTP_HOST=127.0.0.1
FIE_HTTP_PORT=8790
FIE_AUTH_MODE=token
FIE_AUTH_TOKEN=<operator-secret>
FIE_DB_TARGET_RAW=postgresql://<user>:<pw>@127.0.0.1:5432/<raw_db>
FIE_DB_TARGET_INTELLIGENCE=postgresql://<user>:<pw>@127.0.0.1:5432/<intel_db>
```

Canonical role names (`FIE_DB_TARGET_{RAW,INTELLIGENCE,TEST,ROLLBACK}`) are
preferred. Legacy aliases (`FIE_DB_PATH`, `FIE_DATABASE_URL`,
`FIE_INTELLIGENCE_DB`) still resolve when the canonical var is unset but
are deprecated; alias/canonical contradictions fail closed.

## 3. Configuration precedence

```text
explicit argument > canonical env var > legacy alias env var >
documented safe default
```

The full inventory (required/optional, defaults, validation, refusal
classes, consumers) is `docs/contracts/runtime_configuration_inventory.json`.

## 4. Batch invocation (batch plane)

```text
wrappers: run.sh / run_weekly.sh / run_monthly.sh     # source the operator
                                                      # env file, then the
                                                      # 6.8A DB-target guard
cli:      python3 -m phase3.cli <subcommand>
template: examples/fie-wrapper.env.example            # synthetic placeholders
```

Batch scripts resolve config paths CWD-independently (`phase3.paths`:
`FIE_CONFIG_DIR`/`FIE_DATA_DIR` > repo location). If `phase3` is not
importable, the operator MUST set `FIE_CONFIG_DIR` and `FIE_DATA_DIR`
explicitly — the historical CWD fallback refuses with an actionable
RuntimeError.

## 5. Diagnostics (secret-safe)

```text
python3 -m phase3.cli config-contract
```

Prints the effective contract as sanitized JSON: DB-target role
presence/source (canonical vs legacy alias), transport knobs (token shown
only as `AUTH_TOKEN_PRESENT`), kill-switch state, resolved paths. Exit 0 =
valid, 2 = the contract refuses (sanitized refusal classes included).
It never binds, connects, or mutates anything.

## 6. Startup failure semantics

`python -m phase3.transport.http` refuses BEFORE binding with a single
sanitized refusal JSON (category + variable name; value withheld) and a
non-zero exit — no partial service, no degraded mode, no fallback. Stable
categories: `UNKNOWN_SERVICE_ENV`, `UNKNOWN_AUTH_MODE`,
`AUTH_MODE_FORBIDDEN_IN_PRODUCTION`, `AUTH_CREDENTIAL_MISSING`,
`DATABASE_URL_MISSING`, `DATABASE_URL_INVALID`, `CONTRADICTORY_DB_TARGETS`,
`UNKNOWN_ACCESS_MODE`, `INVALID_HTTP_PORT`, `INVALID_LOG_LEVEL`,
`INVALID_REQUEST_TIMEOUT`, `INVALID_LOG_FORMAT`.

Refusal happens **before workload**: in the batch plane, an invalid
configuration refuses before any DB connectivity or schema mutation.

## 7. Secret-handling rule

Never place real credentials in source, fixtures, git history, reports,
receipts, evidence archives, logs or diagnostics. Secrets travel only via
operator-owned env files (mode 0600) sourced by the runtime. Diagnostics
show presence metadata only (`AUTH_TOKEN_PRESENT=true`); DSNs appear only
in masked form (`:***`); loader/refusal messages name variables and
categories, never values. The diagnostic redaction surface is
regression-tested (`tests/phase3/test_69b_r1_config_contract.py`).

## 8. Phase boundary

R1 = configuration semantics + diagnostics + this contract. Supervisor
lifecycle/deployment artifact = 6.9B-R2. Release pinning = 6.9B-R3.
Backup/restore/rollback tooling = 6.9B-R4. Production deployment remains
gated behind 6.9E.