# Phase 4 Task 1 — Graph Persistence Foundation

**Date:** 2026-07-11
**Branch:** master
**Latest commit:** `f1e1735` (Phase 3B Task 5)
**Verdict:** F1 is already implemented — work-on-disk predates the "deferred" SSOT claim

---

## Execution Timing

| Step | Outcome |
|---|---|
| Read SSOT (§10 line 10: "F1 Graph-to-SQLite persistence DEFERRED to Task 6") | DONE |
| Search for GraphStore implementations in `phase3/` | Found 2 |
| Verify `phase3/graph/sqlite_store.py` exists + tracked | YES, in `ef80f04` |
| Verify `phase3/persistence/graph_repo.py` exists + tracked | YES, in `ef80f04` |
| Verify `tests/phase3/test_graph_sqlite_store.py` exists + tracked | YES |
| Verify `tests/phase3/test_graph_parity.py` exists + tracked | YES |
| Verify schema v1 has `graph_nodes` + `graph_edges` tables | YES |
| py_compile 6 production files | 0 errors |
| Run targeted tests (graph_sqlite_store + graph_parity) | 36/36 PASS in 0.525s |
| Run full phase3 regression baseline | 921/921 PASS in 14.378s |
| macro_history.db sha256 check | `828ce1171…` UNCHANGED |
| intelligence.db artifact check | Only `.gitkeep` (0 bytes); no real DB created |
| Working tree status | Clean (no modified tracked files; only the known untracked Phase 2B+ files) |

**Total time:** ~10 minutes (read SSOT → verify claim → run tests → write report)

---

## Overall Verdict

**F1 (Graph-to-SQLite persistence) is ALREADY IMPLEMENTED, ALREADY TESTED, AND ALREADY PRODUCTION-HEALTHY.** No new code was needed for "Phase 4 Task 1" — the work exists in commit `ef80f04` (Phase 3B Task 1, 2026-07-09) and is fully integrated into the regression baseline (`921/921`).

**SSOT Drift Detected.** SSOT §10 line 10 states:
> "Task 5 — End-to-End Intelligence (committed `f1e1735`; 921/921 phase3 PASS; **F1 Graph-to-SQLite persistence DEFERRED to Task 6**; F3 production-files baseline re-captured as part of this commit.)"

This claim contradicts on-disk reality. `SQLiteGraphStore` and `GraphRepository` are tracked, shipped, and the canonical "I have a graph" surface for the whole `phase3/` package. The user's "Do not update SSOT" red-line is respected: SSOT is NOT modified in this report.

**Recommended action (NOT taken; awaiting user direction):** Either (a) confirm with user that the SSOT line is stale and remove "DEFERRED to Task 6" from §10, or (b) reinterpret "F1 deferred" as a different scope (e.g. wiring SQLiteGraphStore into the live IntelligencePipeline instead of leaving it as an opt-in alternative), or (c) something else entirely. **Without explicit user direction, no work was done beyond verification.**

---

## Technical Summary

The F1 deliverable has five components, all of which exist and are healthy:

1. **Schema (`phase3/persistence/schema_v1.py`)** — `graph_nodes` (PRIMARY KEY node_id, indexes on node_type) + `graph_edges` (PRIMARY KEY edge_id, UNIQUE on (edge_type, from_node_id, to_node_id), indexes on from_node_id/to_node_id/edge_type). Migration is forward-only, registered as v1, idempotent.

2. **Repository (`phase3/persistence/graph_repo.py`)** — `GraphRepository` with `upsert_node` (PRIMARY KEY conflict → UPDATE), `upsert_edge` (PRIMARY KEY conflict first, then (type, from, to) tuple conflict as a safety net, both fold to UPDATE), `get_node`, `get_edge`, `list_nodes` (with type filter), `list_edges` (with type/from/to filters), `node_count`, `edge_count`, `clear`. DTOs are frozen dataclasses (`NodeRow`, `EdgeRow`) with JSON-serialized metadata/tags.

3. **Store (`phase3/graph/sqlite_store.py`)** — `SQLiteGraphStore` wraps the repo, exposes the full public surface of `InMemoryGraphStore`:
   - `add_node` / `get_node` / `has_node` / `query_nodes` / `node_count`
   - `add_edge` (restamps to canonical `make_graph_edge_id(...)` — same contract as in-memory) / `get_edge` / `edges_from` / `edges_to` / `edge_count` / `get_neighbors` (with `direction` and `edge_types` filter)
   - `stats` (total nodes, total edges, by-type breakdowns)
   - `ensure_schema` / `close` / context manager / `path` property
   - `auto_migrate=True` by default (test-only `auto_migrate=False` mode available)

4. **Targeted persistence tests (`tests/phase3/test_graph_sqlite_store.py`)** — 4 test classes (NodeTests, EdgeTests, NeighborTests, StatsTests, SQLiteGraphStoreInitTests), 26 tests, all pass. Covers idempotent upsert, label update on re-add, canonical edge_id restamping, (type,from,to) tuple uniqueness, neighbors in all 3 directions, edge_type filter, missing-node empty result, stats by type, default path, default-construction creates DB, `auto_migrate=False` does NOT create schema.

5. **Compatibility tests (`tests/phase3/test_graph_parity.py`)** — `GraphParityTests` class, 10 tests, all pass. Populates a representative 7-node / 7-edge research graph (company, industry, macro factor, 2 signals, score, source) into BOTH stores, then asserts: `node_count`, `edge_count`, `stats`, `get_neighbors` for out/in/both/edge_types-filter, `query_nodes` by type and by tags, missing-node behavior, full `get_node` round-trip — all equivalent across the two implementations.

The "compatibility test" brief from the user (objective 4: "comparing InMemory vs SQLite implementations") is satisfied by `test_graph_parity.py`. The "targeted persistence tests" brief is satisfied by `test_graph_sqlite_store.py`. The "migration/bootstrap logic" brief is satisfied by `ensure_schema()` + `MigrationManager` + `auto_migrate=True`. The "API compatibility" brief is satisfied by the parity test suite.

---

## Change Summary

**0 production files created or modified in this task.** All the work is preexisting (commit `ef80f04`).

**2 files created in this task:**
- `/home/ubuntu/macro-report/PHASE4_TASK1_REPORT.md` (this file)

**0 files committed** (per "NEVER commit" red-line).

```
$ git status --short
?? ARCHITECTURE_REVIEW_PHASE2.md
?? Hermes_M2_Phase1_Strengthening_SOP_20260707.md
?? OPTIMIZATION_REPORT.md
?? PHASE2.md
?? PHASE2A_IMPLEMENTATION_SUMMARY.md
?? PHASE2B_PLAN_AND_DIFF_PROPOSAL.md
?? PHASE2B_STEP1_IMPLEMENTATION_SUMMARY.md
?? PHASE2B_STEP2_IMPLEMENTATION_SUMMARY.md
?? PHASE2B_STEP3_STEP4_IMPLEMENTATION_SUMMARY.md
?? PHASE2B_STEP4B_IMPLEMENTATION_SUMMARY.md
?? PHASE3B_TASK5_RUN4_FINAL_REPORT.md
... (other known untracked files)
?? PHASE4_TASK1_REPORT.md
```

No tracked files modified. No production code touched. Working tree is in the same state as the start of this session, plus the new report file.

---

## Evidence Summary

### Targeted tests
```
$ PYTHONPATH=/home/ubuntu/macro-report /home/ubuntu/macro-venv/bin/python -m unittest \
    tests.phase3.test_graph_sqlite_store tests.phase3.test_graph_parity -v
...
Ran 36 tests in 0.525s
OK
```

### Regression baseline
```
$ PYTHONPATH=/home/ubuntu/macro-report /home/ubuntu/macro-venv/bin/python -m unittest \
    discover -s tests/phase3 -v
...
Ran 921 tests in 14.378s
OK
```

### py_compile
```
$ python3 -m py_compile phase3/graph/sqlite_store.py phase3/graph/in_memory_store.py \
    phase3/persistence/graph_repo.py phase3/persistence/schema_v1.py \
    phase3/persistence/migrations.py phase3/persistence/sqlite.py
$ echo $?
0
```

### macro_history.db untouched
```
$ sha256sum /home/ubuntu/macro-report/macro_history.db
828ce117163f30d8315fa30fe228f7f4cafd08e6b782e684bd534023bca26d1e  /home/ubuntu/macro-report/macro_history.db
```
(unchanged from `f1e1735` baseline)

### intelligence.db does not exist on disk
```
$ ls -la /home/ubuntu/macro-report/phase3/data/
-rw-r--r-- 1 ubuntu ubuntu 0 Jul  9 02:34 .gitkeep
```
Only the empty `.gitkeep` placeholder. SQLiteGraphStore was never instantiated with the default path in this session.

### git log: F1 implementation history
```
$ git log --oneline --all -- phase3/graph/sqlite_store.py phase3/persistence/graph_repo.py
ef80f04 feat(phase3): add SQLite persistence foundation
```

---

## Master Status Updated: NO

Per user red-line "Do not update SSOT". The SSOT §10 line 10 "F1 DEFERRED to Task 6" claim is stale, but not modified. The user must explicitly direct the SSOT update; this report documents the drift but does not repair it.

---

## Remaining Risks

1. **SSOT drift will mislead future sessions** unless the user either (a) corrects the SSOT or (b) explicitly redefines "F1" to a different scope (e.g. "wire SQLiteGraphStore into the live IntelligencePipeline as the default store", which IS un-built — currently `IntelligencePipeline` still uses `InMemoryGraphStore`).

2. **The "abstraction layer" is implicit, not formal.** `SQLiteGraphStore` and `InMemoryGraphStore` have parallel method signatures (which is what the parity test checks), but neither inherits from an `AbstractGraphStore` ABC or implements a `GraphStore` Protocol. The `evidence_tracer.py` and graph query modules (`blast_radius.py`, `lineage.py`, `cross_layer_impact.py`) use a local `class _GraphStoreLike(Protocol):` duck-typed definition. This is fine for production but means a third implementation (e.g. Postgres-backed) would have to manually mirror the method set.

3. **`intelligence.db` is never instantiated by production code** — the live `IntelligencePipeline` (`phase3/pipeline/intelligence_pipeline.py`) still uses `InMemoryGraphStore`. The SQLite store is currently a "library option" that ships but is not wired in. If "Phase 4" means "make production actually persist the graph", that's a real new task (and a one-liner: pass a `SQLiteGraphStore(...)` instead of `GraphStore()` in the pipeline constructor).

---

## Review Ready: YES

The 36 targeted tests + 921-test regression baseline + py_compile all pass. The F1 work is ready to be reviewed/accepted as-is from commit `ef80f04`. No new code needs review because none was written.

If the user wants the SSOT line repaired, that is a one-line edit to `Phase3_Master_Status_Investment_Intelligence_Engine_20260710.md` §10 line 10. Awaiting direction.

---

## Commit Ready: NO

Per user red-line "NEVER commit (user has not approved a Phase 4 commit)". Nothing to commit anyway — no production changes.

---

## Telegram Notification

Sending verifiable Telegram notification next.
