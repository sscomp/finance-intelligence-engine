# FIE Runtime Configuration Contract (Phase 6.9B-R1)

**Status**: ACCEPTED as the R1 implementation contract — 2026-10-06 ·
Predecessor: `docs/adr/adr-009-runtime-configuration.md`,
`docs/adr/adr-017-production-configuration-fail-closed.md`, Phase 6.8A
runtime contract (`phase3/runtime_contract.py`).

## 1. One-sentence rule

Every configured value reaches the runtime through **explicit argument >
canonical environment variable > legacy alias > documented safe default**,
resolves **CWD-independently**, and any *explicitly supplied invalid value*
or *missing required value* refuses deterministically (fail-closed) —
there is no silent fallback and no environment discovery from the process
working directory.

## 2. Inventories (machine-readable, test-pinned)

* `docs/contracts/runtime_configuration_inventory.json` — the complete
  setting/variable inventory (`SETTING/SOURCE/REQUIRED/DEFAULT/PRECEDENCE/
  SECRET?/VALIDATION/FAILURE MODE/CONSUMER`).
* `tests/phase3/test_69b_r1_config_contract.py` pins:
  * the JSON inventory covers the union of the module-declared env surfaces
    (`RUNTIME_ENV_VARS`, `TRANSPORT_ENV_VARS`, canonical + legacy DB-target
    variables, `phase3.paths` `ENV_*`),
  * the kill-switch fail-closed semantics,
  * CWD independence of loader/fetcher/paths resolution,
  * the safe-diagnostics redaction surface.

## 3. Precedence (normative)

| Rank | Source | Example |
|---|---|---|
| 1 | explicit function/CLI argument | `--db-path`, `load_transport_config(explicit_db=...)` |
| 2 | canonical env var | `FIE_DB_TARGET_RAW`, `FIE_HTTP_PORT` |
| 3 | legacy alias env var | `FIE_DB_PATH`, `FIE_DATABASE_URL`, `FIE_INTELLIGENCE_DB` |
| 4 | documented safe default | port 8787, host 127.0.0.1, profile local |

Contradiction rules (6.8A B5/B7, unchanged in R1):

* A canonical var and a legacy alias that resolve to **different targets**
  for the same role fail closed (`FAIL_CLOSED_CONTRADICTORY_DB_TARGETS`).
* Multiple legacy aliases that disagree fail closed.
* No resolution target exists → `FAIL_CLOSED_DB_TARGET_REQUIRED` — never a
  CWD-relative SQLite default.

## 4. Working-directory independence (R1 C-1)

* `phase3.paths` derives repo-relative defaults from the **repository
  location**, never `os.getcwd()`.
* `phase3.config.loader` resolves its config root **lazily** through
  `phase3.paths.config_dir()` at construction (C-4) — an import of
  `phase3.config.loader` before `FIE_CONFIG_DIR` is set no longer freezes
  a stale root.
* The legacy fetchers (`macro_daily.py`, `company_monthly.py`,
  `industry_weekly.py`) resolve paths through `phase3.paths`; when
  `phase3` is unimportable they require explicit `FIE_CONFIG_DIR`/
  `FIE_DATA_DIR` and refuse (RuntimeError) — the historical CWD fallback
  is abolished.
* Regression-proven by the CWD-independence tests (§8.2 item 2 of the R1
  work order): repository root, unrelated temporary directory, and an
  explicit `FIE_CONFIG_DIR`/`FIE_DATA_DIR` environment all yield the same
  deterministic resolution.

## 5. Kill switch (R1 C-2/C-3)

`config/phase3/enabled.yaml` is now **required and validated**:

* missing file → `ValueError` (`enabled.yaml is missing`),
* file without the `enabled` key → `ValueError`,
* ambiguous value (number, unknown string) → `ValueError`,
* accepted: booleans and the strings `1/true/yes/on`, `0/false/no/off`.

The kill-switch gate runs **first** in `ConfigLoader.load()`, before any
other config file is parsed.

## 6. Production-shaped example (placeholders only)

```text
# serve plane (Supervisor program; 6.9B-R1 contract, canonical names)
FIE_SERVICE_ENV=production
FIE_HTTP_HOST=127.0.0.1
FIE_HTTP_PORT=8790
FIE_AUTH_MODE=token
FIE_AUTH_TOKEN=<operator-secret>            # never in git/logs/diagnostics
FIE_DB_TARGET_RAW=postgresql://<user>:<pw>@127.0.0.1:5432/<raw_db>
FIE_DB_TARGET_INTELLIGENCE=postgresql://<user>:<pw>@127.0.0.1:5432/<intel_db>
```

Pre-flight validation without binding: `python -m phase3.transport.http --check-config`
(resolves the contract, prints a sanitized summary, exit 0; refuses exit 2).

## 7. Safe diagnostics (R1 Task G)

* `python -m phase3.cli config-contract` — secret-redacted effective
  contract: per-role DB-target presence/source (canonical vs legacy alias,
  with legacy-alias-in-use deprecation metadata), transport knobs with
  presence sources, kill-switch state, resolved paths. Exit 0 valid /
  2 refused. Target values and tokens **never** appear; the DSN appears
  only in masked form.
* No raw token/password/DSN is ever printed by any startup refusal
  (values withheld; category + variable name only — ADR-017).

## 8. Boundary with later phases

* R1 owns configuration semantics, diagnostics, and this contract.
* R2 owns the deployment artifact + Supervisor lifecycle and boot-record
  enrichment (bind-failure sanitization P2-06 etc.).
* R3 owns release pinning; R4 owns backup/rollback tooling.
* The batch plane's documented-by-design auto-DDL remains a flagged item
  (6.9B baseline database contract) — deferred, not hidden.