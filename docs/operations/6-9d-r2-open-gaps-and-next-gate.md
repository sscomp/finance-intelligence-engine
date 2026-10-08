# OPEN GAPS AND NEXT GATE — FIE 6.9D-R2

**As of:** 2026-10-08T18:10Z · work order 6.9D-R2 · **verdict: PASS_WITH_CONDITIONS**
(sole unmet FULL_PASS criterion = recovery-key off-host custody = P0 owner action below; all data-path gates proven from off-host-retrieved ciphertext)

## Open gaps (ranked)

| # | gap | severity | owner action available now |
|---|---|---|---|
| 1 | **Recovery private key still host-local** — custodian handoff file (`~/.fie-r2-staging/owner-handoff/recovery-private-key-20261008.asc`, 0600) has not been confirmed moved off-host; pod loss before relocation = key loss (ciphertext unrecoverable). | **P0** | Copy the 0600 file to a secure store on a different device now; keep one copy off the Abacus pod. After confirming off-host custody, host copies can be destroyed on request (deleting local GNUPGHOME requires its own confirmation). |
| 2 | **No deletion protection / versioning on the sync folder** — deletions propagate irreversibly in both directions (observed live when the Air-side relocation produced delete events). Syncthing File Versioning is off. | P1 | Enable "File Versioning (Trash)" for `abacus-shared` on both peers; or keep retention copies outside the sync tree. |
| 3 | **Off-host copies remain inside the synced tree** (`/Users/sscomp/Abacus/databases`, `/home/ubuntu/Abacus/...`) — same-tree copies add no independent failure domain beyond the two sync peers. | P1 | Move one retention copy outside `~/Abacus` on the Air, or authorize a second unrelated destination (cloud bucket/SFTP) in a follow-up. |
| 4 | **Backup scheduler is host-local & same-failure-domain** — the daily `postgresql-backup-scheduler` writes only to the pod; the 6.9D-R2 bundle was a one-shot. No recurring encrypted off-host rotation exists. | P1 | Approve a follow-up work order: recurring encrypted bundle + rotation + alerting (the scheduler template and drill script shipped in this delivery make that incremental). |
| 5 | **No hash-pinned dependency lock** — `pip lock` unavailable on the drill host; lock delivered as `requirements-frozen.txt` (no hashes). pyproject pins exist but are looser. | P2 | Approve PR that adds `requirements.lock` generated with hashes on a pinned CI/toolchain, or accept freeze-level lock. |
| 6 | **Runtime interpreter still legacy `fie-venv-67b`** on the production service (6.9D-R1 finding, confirmed live at preflight) — the fresh-clone path is now validated to NOT need it. | P2 (carried from R1) | Plan a controlled interpreter re-point (separate authorization; NOT part of R2). |
| 7 | `fie_prod_raw` not covered by the repo backup contract (`pg_backup.py` covers the intelligence DB only) — the R2 bundle covers raw, but the recurring contract does not yet. | P2 | Include raw DB in the follow-up scheduler work order. |
| 8 | Fresh-cloud/other-OS install validation untested (drill host = Abacus pod sibling environment). | P2 | Optional later drill on a genuinely different OS/cloud. |

## Gate status
- **Gate A: APPROVED + executed** (export · Syncthing→Air destination · GPG cv25519 · cleanup-on-consult) — record: `02_export/gate_a_authorization_record.json`.
- **Gate B: APPROVED + executed** (in-pod ephemeral PG 18.4, dispose-after-test) — receipts: `06_restore_drill/`.
- **Gate C (publication): NOT YET AUTHORIZED** — T4 deliverables (drill script, deployment guide, runbooks) exist as local delivery (`~/fie-6_9d_r2_delivery/`) + patch candidate; no repo commit/push done. Next gate = owner decides PR vs local-only retention.

## Next actions for owner (checklist)
1. Move the recovery key off-host (P0, item 1).
2. Decide Gate C: publish T4 assets to the repo (PR) or keep local patch only.
3. Decide on recurring encrypted off-host rotation (item 4) as a follow-up work order.
4. Consider enabling Syncthing File Versioning on both peers (item 2).