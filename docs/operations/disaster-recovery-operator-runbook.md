# DISASTER-RECOVERY OPERATOR RUNBOOK — FIE 6.9D-R2
**Scenario: total loss of an Abacus-Claw pod (host + volume destroyed).**

---

## 0. Roles & contact pointers (roles, not personal data)

| role | responsibility | where recorded |
|---|---|---|
| FIE owner | every authorization gate; sole recovery-key custodian | this file + OPEN_GAPS_AND_NEXT_GATE.md |
| operator (executor) | preflight, export, transfer, drills, evidence | this runbook |
| backup custodian | retention copy outside sync domains | OFF_HOST_RECOVERY_ATTESTATION.md |

## 1. What exists, where (reference — not credentials)

| artifact | current location | failure domain |
|---|---|---|
| Encrypted recovery bundle `recovery-bundle-20261008T1640Z-r2a0` | MacBook Air `/Users/sscomp/Abacus/recovery-bundles/…` (owner-verified 11/11 SHA256 OK; DB ciphertext copies also at `/Users/sscomp/Abacus/databases/`) | **different host** (Air) |
| Same bundle, pod-local mirror | Abacus pod `/home/ubuntu/Abacus/recovery-bundles/…` + `/home/ubuntu/Abacus/databases/*.enc` | Abacus pod (primary) |
| Recovery private key handoff | Abacus pod `~/.fie-r2-staging/owner-handoff/recovery-private-key-20261008.asc` (0600) — **owner must move off-host; treat pod loss as key loss until then** | Abacus pod (temporary!) |
| Source repository | `/home/ubuntu/macro-report` @ bb8a66c8 (origin/master; pushed) | Git remote |
| Daily scheduler backups (unencrypted, same-pod) | `/home/ubuntu/postgresql-18/backups/YYYYMMDD/` | Abacus pod |
| Key fingerprint | `CFF8A1D35039163ABD4D0C8FC7605C38F0CD5C38` (`FIE-R2-RECOVERY-20261008`, never expires) | public material |

## 2. Recovery procedure (Air copy + key survived)

1. **Stabilize** — obtain any Linux x86_64 host (see CLEAN_HOST_DEPLOYMENT_GUIDE §0).
2. **Retrieve the bundle** — copy `recovery-bundle-…r2a0` from the Air (any authenticated channel). Verify first: `cd <bundle> && sha256sum -c SHA256SUMS` (11 OK).
3. **Retrieve the key** — from the owner's secure off-host custody (fingerprint above must match: `gpg --import <keyfile> && gpg --list-keys`).
4. **Fresh clone + install** — `bash scripts/bootstrap.sh --all` (no legacy venv; validated).
5. **Restore databases** — `deploy/scripts/restore-drill.sh <bundle> <repo-root>` (fail-closed; validated; runbook §B). Creates roles + `fie_prod` + `fie_prod_raw` with production ownership/grants on an isolated or permanent target **you own**.
6. **Wire the environment** — `fie-runtime.env` 0600 with fresh `FIE_AUTH_TOKEN` (old token is lost with the pod), DSNs to the restored DBs.
7. **Service** — supervisor template + staging per deploy/README; smoke: `/healthz` 200, authenticated `/readyz` 200.
8. **Cutover authorization** — pointing traffic at the new host is an OWNER gate; this runbook delivers a validated instance, not a production cutover.
9. **Data-loss window** — RPO = data written after the last captured export; with the 2026-10-08 16:19Z bundle: anything newer than that timestamp is unrecoverable unless newer bundles exist downstream. RTO observed in drill receipt only — do not extrapolate to production SLA.

## 3. If the key did NOT survive
Recovery is **cryptographically impossible by design** (no escrow; host-local custodian copy deleted after handoff). Fallback that still works: the repo contract path (`deploy/bin/fie-restore`), and — after total pod loss — nothing else; the daily unencrypted dumps are also gone with the pod. **This is why key off-host handoff is step 0 of every bundle creation.**

## 4. If the Air copy is also gone
No verified off-host copy remains ⇒ `TOTAL_ABACUS_LOSS_RECOVERABLE=NO`. Mitigations before losing everything: refresh the bundle + immediately transfer + verify (this whole runbook §1's flow), and add a second independent destination (see OPEN_GAPS).

## 5. Rollback hierarchy (application vs data)
- **Application/config rollback** = `git checkout <previous-rev>` + service restart. No data semantics.
- **Database restore/rollback** = gated drill + restore procedure with explicit owner authorization; irreversible by nature (restoring replaces newer state with an older snapshot).
- **Never** interleave the two: a DB restore must not be used to "fix" an application deploy, and a deploy must never be used to repair data.

## 6. Drill obligations after any real recovery
Repeat the Gate B drill on the new host before considering the recovery closed; archive the receipt next to this runbook.