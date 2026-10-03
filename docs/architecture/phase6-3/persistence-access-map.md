# Phase 6.3 — Existing Persistence Access Map (§7 inventory)

Status: completed implementation inventory, written *after* the
Phase 6.3 refactor so every claim reflects the committed state
(branch `feature/fie-phase6-3-persistence`). Scope lines reference
file:line ranges as of this phase.

Scope boundary (work order §2): `macro_history.db` (historical data
layer, `db.py`) stays SQLite and keeps its own DDL; the refactoring
seam is the Phase 3B store (`intelligence.db`): signals, scores,
evidence graph, adapter runs, migrations, retention, backup.

---

## 1. Repository abstractions present (before → after)

| Repository | Before 6.3 | After 6.3 |
|---|---|---|
| `SignalRepository` (`phase3/persistence/signal_repo.py`) | SQLite-bound SQL (`?`, sqlite `strftime` defaults) | dialect-neutral (`%s`), app-layer `utc_now_iso()` stamps |
| `ScoreRepository` (`phase3/persistence/score_repo.py`) | SQLite-bound, `RETURNING` only on SQLite | portable `INSERT … RETURNING` (identity columns on both) |
| `GraphRepository` (`phase3/persistence/graph_repo.py`) | SQLite-bound, `ON CONFLICT` dual-clause SQL | explicit check-then-write sequence, `%s`; batch reads via seam methods |
| `MigrationManager` (`phase3/persistence/migrations.py`) | `sqlite_master`/sqlite DDL registry | `store.registry_ddl()` seam + `default_migrations_for(store)` |
| `SnapshotWriter`, `RecoveryManager`, backfill, retention | built directly on the repos | unchanged APIs, backend-agnostic through the repos |

## 2. Direct `sqlite3` usage — where it remains and why

| Module | Role | Phase 6.3 decision |
|---|---|---|
| `phase3/persistence/sqlite.py` | the SQLite `DatabaseStore` implementation | kept — this IS the backend seam |
| `phase3/persistence/score_repo.py` | `_db_error_classes()` trigger-probe helper | kept — SQLite exception class only (lazy psycopg import), boundary preserved |
| `phase3/persistence/postgres.py` | the PostgreSQL `DatabaseStore` (new) | new backend seam |
| `phase3/persistence/contracts.py` | `typing`-only `sqlite3` mention | docs only |
| `phase3/persistence/backup.py` | SQLite file backup (`VACUUM INTO`-style copy) | SQLite-only utility; backup of a PG DSN is out of scope (6.3 disposable-only) |
| `db.py` | owner of `macro_history.db` DDL & connection | untouched semantics; `DDL_MACRO_DAILY` / `DDL_STOCK_MONTHLY` / `DDL_INSTITUTIONAL_DAILY` promoted to single-owner constants |
| `phase3/backfill.py` | historical-data backfill onto `macro_history.db` | SQLite-only by domain; now imports the `db.py` DDL constants instead of re-declaring them |
| `phase3/bridge/seed_signals.py` | legacy bridge seeding (reads legacy DBs read-only) | untouched (Legacy Existing Deployment boundary; no abacus-claw access) |
| `phase3/portfolio/*` | no `sqlite3` imports (enforced by module docstrings) | unchanged |

## 3. SQL embedded outside repositories (before → after)

| Site | Before | After |
|---|---|---|
| `phase3/cli.py` | `PRAGMA query_only=1` dig into `store.conn` (read-only query commands) | `store.set_query_only()` seam method (PG: `SET default_transaction_read_only = on`) |
| `phase3/graph/optimization.py` | `conn.execute("SELECT …")` raw batching over `sqlite3.Connection` | `fetch_edges_by_nodes` / `fetch_nodes_by_ids` batch-repo methods; grouping semantics preserved exactly (two per-direction passes, `ORDER BY edge_id`, self-loop double-touch under `"both"`) |
| `phase3/graph/sqlite_store.py` | schema DDL in module (`SCHEMA_V1_SQL`) | unchanged owner — consumed via `default_migrations_for()` |
| `phase3/persistence/schema_pg.py` | — (new) | PostgreSQL DDL twin, single owner of the PG migration body |
| `db.py` / `phase3/backfill.py` | DDL duplicated in two files | single-owner `db.py` constants |

## 4. Schema initialization & migrations

- SQLite migration v1: `phase3/persistence/schema_v1.build()` — byte-compatible
  with 6.1-era DBs (kept `strftime` DDL defaults; all *runtime* writes now
  supply app-layer stamps, so old and new rows read identically).
- PostgreSQL migration v1: `phase3/persistence/schema_pg.build()` — column-for-column
  twin (`BIGINT GENERATED ALWAYS AS IDENTITY` for rowids, `DOUBLE PRECISION`,
  append-only guard function + triggers created idempotently inside `DO $migrate_n$ … EXCEPTION WHEN duplicate_object THEN NULL` blocks).
- Registry: `schema_migrations` table. SQLite DDL keeps the old shape; PG uses
  `PG_REGISTRY_DDL` (`version INTEGER PRIMARY KEY`, no defaults). Both applied via the
  `registry_ddl()` store seam (`executescript`). Upserts are portable
  `INSERT … ON CONFLICT(version) DO UPDATE`; `applied_at` is app-supplied `utc_now_iso()`.
- Checksum-tamper detection preserved on both (contract test).
- Migration state: `applied` list (newly applied) + `current` version, surfaced by
  `SqlGraphStore.ensure_schema()` → printed by `phase3.cli init-db`.

## 5. Transaction boundaries

- `SQLiteStore.transaction()`: `BEGIN IMMEDIATE` / commit / rollback-on-error;
  `executemany` inside the same transaction for batch writes.
- `PostgresStore.transaction()`: explicit `BEGIN` / `COMMIT` / `ROLLBACK`
  (autocommit connection), `PostgresTransactionError` raised if COMMIT fails.
  No nested-transaction semantics introduced anywhere.
- Repos never open transactions implicitly around single statements — the
  caller (API/CLI) owns batching, as before.

## 6. Upsert behavior

- `signal_log`: `INSERT … ON CONFLICT(signal_id) DO UPDATE SET …` — identical
  semantics on both backends (18 explicit columns; `ingested_at` now app-supplied).
- `graph_nodes`: check-then-insert/UPDATE (explicit, portable; no dialect-specific
  `ON CONFLICT` clauses in SQL text).
- `graph_edges`: by `edge_id` → full-row UPDATE; by unique triple → UPDATE
  weight/metadata/schema keeping the existing `edge_id`; else INSERT. (The old
  dead dual-clause `ON CONFLICT` SQL was removed.)
- `score_snapshot`: never upserted — append-only (`RAISE`-trigger on both).

## 7. Ordering assumptions

- Edge/node batch reads: `ORDER BY edge_id` / `ORDER BY node_id` — fixed
  deterministic order on both backends (PG unordered-by-default mitigated).
- Score reads: `ORDER BY computed_at DESC, snapshot_id DESC` on both; ties
  resolved by `snapshot_id` (contract test pins this on both backends).
- Signal reads: explicit `ORDER BY fetched_at DESC, …` equivalents preserved.

## 8. SQLite PRAGMAs

`_DEFAULT_PRAGMAS` (`journal_mode=WAL`, `foreign_keys=ON`,
`synchronous=NORMAL`, `busy_timeout`) remain the SQLite connection
profile. Read-only mode is expressed through the `set_query_only()`
seam. PostgreSQL gets no SQLite PRAGMAs; its read-only counterpart is
`SET default_transaction_read_only = on`.

## 9. Row factories / types

- SQLite: `sqlite3.Row` (dict + positional access).
- PostgreSQL: custom `_Row(dict)` subclass with positional fallback via a
  `(cursor) -> row_maker` factory closure (psycopg 3 signature) — so every
  `row["col"]` access in the repos works unmodified.
- Floats: `float(row[…])` coercion at repo boundaries (SQLite INTEGER/PG
  DOUBLE PRECISION both covered). Booleans: none in the seam tables.
- NULLs: preserved; `fetched_at` NULL allowed (contract test on both).

## 10. Datetime / JSON / boolean serialization

- All *runtime* timestamps: `phase3/persistence/timeutil.utc_now_iso()` — UTC,
  `YYYY-MM-DDTHH:MM:SS.mmmZ` (byte-identical to the historical SQLite
  `strftime('%Y-%m-%dT%H:%M:%fZ','now')` output; millisecond fraction, `Z` suffix).
- DDL-level `DEFAULT CURRENT_TIMESTAMP`-equivalents removed from *write* paths —
  only legacy SQLite DDL defaults remain (byte-compat, never hit by new writes).
- JSON: `json.dumps(..., sort_keys=True)` for storage, `json.loads` for reads —
  unchanged; PG stores the same TEXT JSON columns (no `jsonb`, keeping string
  comparison semantics identical).
- Booleans: no boolean columns in the seam schema.

## 11. Auto-increment assumptions

- `score_snapshot.snapshot_id`, `signal_log.rowid`-backed PK,
  adapter-run/error tables: SQLite `AUTOINCREMENT` ↔ PG
  `BIGINT GENERATED ALWAYS AS IDENTITY`, both read back via `RETURNING`
  (`INSERT … RETURNING snapshot_id`) — no `lastrowid()` dependence in
  the ported code.

## 12. Locking / concurrency assumptions

- SQLite single-writer (WAL + `busy_timeout`); `BEGIN IMMEDIATE` for
  write transactions. No concurrent-writer support is assumed anywhere.
- PostgreSQL disposable clusters in this phase are single-user, trust-auth,
  `/tmp` sockdir — no multi-writer claims are made. Cloud-readiness claims
  are strictly structural (dialect-neutral SQL + backend seam), not
  concurrency-performance claims.
- `PRAGMA query_only` / `SET default_transaction_read_only` enforce
  read-only CLI query commands on both backends.

## 13. Dispatch surfaces (after 6.3)

- One predicate: `phase3.persistence.backend.is_pg_dsn(specifier)`
  (`postgres://` / `postgresql://` scheme, whitespace/case tolerant).
- Precedence: explicit API/CLI argument > `FIE_DATABASE_URL` > SQLite default
  (`phase3/data/intelligence.db`).
- Consumers: `phase3/api.py` (`_open_store`, `_build_graph_store` — DSN check
  on the *original* specifier, before the file-path guard), `phase3/cli.py`
  (`cmd_init_db`, `ingest-signals`, `_resolve_graph_store`).
- DSNs containing passwords are masked by `sanitize_db_url()` before any
  print/log; raw DSNs never reach logs or reports.