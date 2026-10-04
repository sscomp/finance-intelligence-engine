# FIE Phase 6.7B Accepted Production Baseline

**Document class:** Authoritative record of the accepted Finance Intelligence Engine (FIE) Phase 6.7B Production baseline.

**Document status:** Published — documentation only. This document describes an already accepted and already deployed Production release. It is not itself a release, rebuild, or redeploy of the FIE application. See §1.2 for the identity separation.

**Freeze date:** 2026-10-04

---

# 1. Baseline Status

## 1.1 Governance outcomes

| Item | State |
|---|---|
| Architecture Review verdict | **PASS** |
| Baseline freeze | **APPROVED** (2026-10-04) |
| Baseline freeze gates | 15/15 PASS (BF-01 … BF-15, 0 FAIL / 0 BLOCKED / 0 UNKNOWN) |
| Production drift vs accepted baseline | **NONE** |
| Producing review | `FIE_6_7B_POST_PRODUCTION_CONSOLIDATED_ARCHITECTURE_REVIEW_AND_BASELINE_FREEZE_DECISION` |

## 1.2 Authoritative identities

These identifiers are **authoritative** and were independently re-verified at publication time against the local repository, `origin/master`, the release tag, and the running Production system:

| Item | Value |
|---|---|
| Authoritative release tag | `fie-6.7b-r4` (annotated tag object `c522d1043016a46a746d90bb94824591a0f98a7e`) |
| Authoritative release SHA (`refs/heads/master` at acceptance) | `9c3be3a6c471b8309cd4e23f3e24c0d46035c242` |
| Deployed image digest | `sha256:4a0418395886038d91adecaad796ace46d3505b2e6713e806384819b27f8945c` |
| Schema version | `1` |

---

# 2. Release Provenance

The provenance chain of the accepted Production baseline is:

```
GitHub source
  → authoritative release SHA  9c3be3a6c471b8309cd4e23f3e24c0d46035c242
  → release tag                fie-6.7b-r4 (annotated, c522d1043…)
  → built image digest         sha256:4a0418395886…f8945c
  → deployed runtime           container fie-prod-fie (running Production)
  → schema identity            version 1 = phase3b_initial_schema
  → accepted Production baseline (frozen 2026-10-04)
```

- The **authoritative identifiers are the release SHA, tag, and image digest** above. They were the acceptance target of the consolidated Architecture Review and were verified consistent across local repository, `origin/master`, tag dereference, and the running containers at freeze time.
- The source code at the release SHA was merged to `master` by fast-forward; there is exactly one authoritative release lineage.
- `origin/master` moved forward once after acceptance, by the documentation-only commit created in this publication. That commit adds documentation under `docs/production/` only. **It is not a rebuilt or redeployed FIE release**, and it is not part of the application provenance chain above.
- Any commit on `master` newer than the release SHA is non-application documentation unless a future accepted work order states otherwise.

---

# 3. Production Topology

Documented behavior of the accepted Production deployment (no credentials or secrets included anywhere in this document):

## 3.1 Application

| Property | Accepted value |
|---|---|
| Container | `fie-prod-fie` |
| Runtime user | uid/gid `10001:10001` (non-root) |
| Network exposure | `127.0.0.1:8787` only (loopback bind; no public interface) |
| Restart policy | `unless-stopped` |

## 3.2 PostgreSQL

| Property | Accepted value |
|---|---|
| Container | `fie-prod-pg` |
| Image | `postgres:18-alpine` |
| Published ports | **NONE** (no port bindings; reachable only on the internal Docker network) |
| Network | `fie-prod-net` |
| Persistent volume | `fie-prod-pgdata` |
| Database | `fie_prod` |
| External PostgreSQL exposure | **NONE** |

Secrets are provisioned to the application container at creation time via environment-file injection; secret file permissions are `0600`. Secret values, connection strings, and credential-bearing configuration are deliberately excluded from this record.

---

# 4. Schema Baseline

| Property | Accepted value |
|---|---|
| Schema version | `1` |
| Schema name | `phase3b_initial_schema` |
| Schema checksum | `a281dddeb5f1b1887a5efd0c4d0f43338c753e5b02c3f6a7d6c06033b7145888` |

Schema compatibility is **enforced by the Production readiness/runtime contract**, not by the application mutating state on open: the running application does not create or alter schema objects; a production-compatible migration registry (version `1`, name and checksum above) must already be present, and the readiness probe reports `schema_current_version: 1`. The runtime fails closed against schema drift and classifies unknown/forward registry entries as a future-schema condition (no serving after unreviewed forward migration without an accepted change record plus a verified pre-migration recovery point).

---

# 5. Authentication Boundary

Behavioral contract of the accepted baseline:

| Interaction | Accepted behavior |
|---|---|
| `GET /healthz` | Public health endpoint (no credential required) |
| Protected routes without credential | `401` — fail closed |
| Protected routes with malformed credential | `401` — fail closed |
| Protected routes with valid credential | Authorized behavior (`2xx`) |
| Application → PostgreSQL authentication | `SCRAM-SHA-256` path (`host all all all scram-sha-256` for the cross-container network path) |

No token values, credential material, or authorization headers are documented here. Credential handling and rotation are governed by independent operational records.

---

# 6. Recovery Baseline

Accepted encrypted recovery points exist and were integrity-verified at acceptance and re-verified at publication:

| Recovery point | SHA-256 | Mode |
|---|---|---|
| `20261004T062520Z-fie_prod-empty-FULL.enc` (pre-migration) | `5d07a3a8a13c9dab11b6811049efd615d91be8e8d2c132d41f28e8febc779933` | `0600` |
| `20261004T063141Z-fie_prod-schemaV1-FULL.enc` (post-migration / baseline) | `28e1c4e6ba326a1f2c43d9cc64bb7a4f5414dd88ed10035684036d1794299842` | `0600` |
| `20261004T105203Z-fie_prod-pre_obs005_rotation-FULL.enc` (pre-credential-rotation) | `7a7b00251f579c8f67e5e2021cf61f66719de591a56c938b95cfdd92bdc808ab` | `0600` |

- The recovery inventory is verified: all accepted recovery points hash-match their recorded digests.
- Encrypted backup file permissions are `0600` (OBS-003 closure, below).
- Recovery artifacts (encrypted payloads, keys/passphrases) live outside Git by design. Only the digests and security mode above are recorded here. **No encrypted payloads or secrets are embedded in this document.**
- OBS-003 remains **CLOSED**.

---

# 7. Post-Production Closure State

| Observation | State |
|---|---|
| OBS-003 (backup file permissions) | **CLOSED** — remediated and verified; permissions `0600` |
| OBS-004 (probe transcript exposure) | **CLOSED** — token rotation completed and verified |
| OBS-005 (database credential exposure) | **CLOSED** — server-side credential rotation, all host mirrors updated, SCRAM path proven |
| OBS-006 (PostgreSQL local trust boundary) | **NON_BLOCKING_HARDENING** — future hardening item only |
| OBS-007 (application PostgreSQL role least privilege) | **NON_BLOCKING_HARDENING CANDIDATE / OWNER DECISION** |

- **OBS-006** concerns PostgreSQL local trust-boundary hardening: the official `postgres` image default `pg_hba` configuration permits trust-based local (socket/localhost) access inside the `fie-prod-pg` container. Docker-exec access to the database is therefore credential-free from inside the container host. The application's network path is unaffected (SCRAM-SHA-256). Classified as a non-blocking future hardening item; no remediation has occurred.
- **OBS-007** concerns a least-privilege review of the application's PostgreSQL role: role `fie` currently runs with PostgreSQL superuser privileges (a consequence of the container initialization convention used at deployment). A least-privilege review and possible role downgrade is a candidate future hardening item owned by an explicit owner decision. **It has NOT been remediated** — the role's privilege level is unchanged from the accepted baseline.
- Neither OBS-006 nor OBS-007 is blocking for the accepted Production baseline, and neither was remediated by this publication.

---

# 8. Operational Boundary

- Production is currently **accepted**: deployment, independent acceptance, and handoff gates all passed on 2026-10-04.
- The Phase 6.7B baseline is **frozen**; `EXPECTED_PRODUCTION_DRIFT = NONE`.
- This baseline publication is documentation only. It does not mutate Production: no restart, rebuild, redeploy, container recreation, schema change, credential rotation, configuration change, or network change occurs as part of it.
- **Phase 6.8 is NOT authorized by this document.**
- Any future Production change (application, schema, infrastructure, credentials, OBS-006/OBS-007 remediation, Phase 6.8) requires an independent governance decision / work order with its own verification gates.