# Isolated rehearsal remediation and activation boundaries

The 2026-10-25 remediation adds four controls while keeping opening (09:30),
pre-close (13:00) and local publisher disabled. This is not live-session acceptance.
Free yfinance daily bars remain delayed/indicative, with no guaranteed realtime
freshness. Bar labels, fetch times and generation times stay separate.

- Expired checkpoints return `DATA_STALE`, unusable, with an empty row list and
  separate row count. Reports return controlled `DATA_DELAYED`/`DEGRADED` and a
  blocked strategy instead of iterating a receipt count. Missing, invalid, future
  and cached future timestamps fail closed. Prior close is never substituted.
- `publish()` is the production boundary. It requires the canonical installed
  root, valid activation seal, accepted production policy, verified current
  source lineage, current-session report, exact JSON/Markdown agreement, report
  and strategy agreement with source rows, and artifact hashes. It rejects
  rehearsal/test metadata, simulated slots, isolated roots, path escapes and
  symlinks before changing the local publication pointer. No external transport
  is implemented by this adapter.
- `simulate_publish()` is explicit and separate. It requires a private isolated
  root, a `DRY_RUN.json` marker with `production_authorization=false`, rehearsal
  configuration and report provenance. It refuses the module's installation
  root and its descendants. Load the installed module and pass a separate
  fixture root; do not execute a copied fixture installation as production.
- Reports execute `T2_SOURCE_ADMISSION_V1`: source-bound per-instrument
  open-to-close observations and a fail-closed admission decision. Current daily
  bars support `LIMITED` observations only. Macro score is null and admission
  `BLOCKED` until compatible verified inputs exist. This is not full M2 strategy
  parity. M2's scalar macro model and consecutive-session/discretionary rules
  differ from the repository's multidimensional scorer. The latter's missing
  dimension defaults must not be presented as M2 parity. No cross-instance
  runtime dependency, private cards, holdings or historical trading bands are
  imported.

## Hermes explicit read

Install `instances/t2/bin/read_dry_run.py` beside the existing Hermes read helper.
The helper's explicit `dry-run` branch calls its `main(args)` without loading
production runtime or credentials. Do not change existing read operations.
The operator creates a mode-0600, owned, nonsymlink `dry-run-allowlist.json` beside
that helper, with this shape (values are deployment-specific, never committed):

```json
{
  "evidence_root": "/absolute/instance/evidence",
  "namespaces": {
    "rehearsal_name": {
      "artifacts": {
        "opening": {
          "root": "/absolute/instance/evidence/rehearsal/DRY_RUN_fixture",
          "path": "reports/t2-yfinance-0123456789abcdef0123.json",
          "sha256": "reviewed 64-character digest"
        }
      }
    }
  }
}
```

Invoke `read.py dry-run rehearsal_name opening` or the registered `tail` alias.
Only names and aliases are accepted, never paths. The registry pins exact JSON
report hashes under a DRY_RUN evidence folder; reader enforces ownership, 0600,
regular files, canonical paths, bounded size and DRY_RUN report/provenance labels.
Traversal, symlink escapes, secret/unrelated paths, extra arguments and unknown
namespaces are refused. Rehearsal output is never the production latest report.
Remove only the new registry/branch/helper to revoke this capability; other
Hermes commands and jobs need no changes.

## Validation, deployment and rollback

Run `python -B -m unittest discover -s instances/t2/tests -v`,
`bash scripts/bootstrap.sh --all`, `bash scripts/test-cloud.sh`, and the secret
scanner commands in `.github/workflows/ci.yml`. Use independent private config
and an ephemeral, network-isolated database for the full replay. The temporary
writer adapter must prove container/cluster/database identity and must never
redirect the production writer's guard. Keep original check names and record
actual exit codes, failures, hashes, cleanup and protection snapshots.

Before installing: fingerprint M2 static files/jobs read-only, T2 P2 files and
rowsets, P3 schemas/rowsets/cluster identities, pointers, schedules and services;
record dirty files and preserve unrelated work. Back up each changed code/test,
Hermes helper and activation seal. Apply tested source under the T2 instance lock
and refresh only source hashes in the existing seal. Preserve enabled jobs,
calendar, source policy, database identities and market-day acceptance flag.
Resealing code is not approval to activate production. No DB migration is needed.
If validation fails, restore reviewed backup bytes and the prior seal under that
same lock, verify hashes/gate, and leave the three report/publisher jobs disabled.
Do not roll back data history, stop unrelated services or restore rehearsal dumps
into production. Refuse rollback if files have since changed unexpectedly.

A full historical session cannot be inferred from daily bars first fetched later.
If 09:20/12:20 per-session snapshots are unavailable, test deterministic temporal
ordering in a separately labelled synthetic lane. Retain a truthful historical
lane that blocks lookahead. Watchdogs should remain DEGRADED when original feed
receipts are absent; do not synthesize healthy dependency receipts.

## Later live-session acceptance

October 27 is the next expected session subject to fresh calendar/emergency
closure verification. Separate authorization is required for any schedule-based
live acceptance and for subsequent production activation. Capture real 09:20
and 12:20 receipts, then validate 09:30 opening, 09:45 watchdog, 13:00 pre-close,
13:15 watchdog, dedup/retry/restart, local publication and Hermes reads. Report
LIMITED daily-bar scope and blocked macro strategy honestly. A later fetch does
not establish a later trade. Strict realtime needs a separately proven source.
Only accepted evidence plus explicit later approval may change the market-day
acceptance flag or enable opening/pre-close/publisher. External notifications
remain outside this work order. M2 stays permanently independent; full backup
expansion remains deferred.
