"""Phase 3B schema v1 — PostgreSQL twin (Phase 6.3, disposable parity).

This module is the backend twin of :mod:`phase3.persistence.schema_v1`.
The *logical* schema (tables, columns, keys, unique constraints,
append-only policy) is identical; only storage types and dialect
differ. Per Phase 6.2 ADR-C03 this backend is **disposable/synthetic
parity** — it exists to prove the persistence contracts port, not (in
this phase) to become a production target.

Differences from the SQLite twin
--------------------------------
* ``INTEGER PRIMARY KEY AUTOINCREMENT`` →
  ``BIGINT GENERATED ALWAYS AS IDENTITY`` (explicit id inserts are
  therefore prohibited by the database, mirroring the repo contract).
* ``REAL`` → ``DOUBLE PRECISION`` (both are IEEE-754 doubles, so
  float round-trips are bit-identical — asserted in the contract
  tests).
* Timestamps have **no** SQL default. The application layer stamps
  every row (``phase3.persistence.timeutil.utc_now_iso``), so no
  backend-specific ``now()`` / ``strftime`` appears in the schema.
* Append-only enforcement: SQLite uses inline ``RAISE(ABORT)``
  triggers; PostgreSQL needs a small plpgsql guard function. The
  function is CREATE OR REPLACE and the triggers are created inside
  ``DO`` blocks with duplicate-object swallowing so a re-apply (e.g.
  after a failed-migration retry) stays idempotent.
* No ``AUTOINCREMENT`` sqlite_sequence side table — nothing to
  port.

Apply discipline: this SQL is registered as :class:`Migration`
version 1 (same version/name as the SQLite twin in each backend's own
``schema_migrations`` registry; checksums differ per backend by
design — each database tracks its own registry).
"""
from __future__ import annotations

from phase3.persistence.migrations import Migration

SCHEMA_PG_V1_NAME: str = "phase3b_initial_schema"

SCHEMA_PG_V1_SQL: str = """
-- ---------------------------------------------------------------------------
-- signal_log
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signal_log (
    signal_id      TEXT    PRIMARY KEY,
    entity_type    TEXT    NOT NULL,
    entity_id      TEXT    NOT NULL,
    signal_type    TEXT    NOT NULL,
    value          DOUBLE PRECISION NOT NULL,
    unit           TEXT    NOT NULL,
    direction      TEXT    NOT NULL,
    timestamp      TEXT    NOT NULL,
    date_bucket    TEXT    NOT NULL DEFAULT '',
    source_id      TEXT    NOT NULL DEFAULT '',
    source_type    TEXT    NOT NULL DEFAULT '',
    ref            TEXT    NOT NULL DEFAULT '',
    fetched_at     TEXT,
    fetch_id       TEXT    NOT NULL DEFAULT '',
    schema_version TEXT    NOT NULL DEFAULT '3.0',
    metadata_json  TEXT    NOT NULL DEFAULT '{}',
    raw_payload    TEXT    NOT NULL DEFAULT '{}',
    ingested_at    TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_signal_log_entity
    ON signal_log (entity_type, entity_id, date_bucket);
CREATE INDEX IF NOT EXISTS idx_signal_log_source
    ON signal_log (source_type, date_bucket);
CREATE INDEX IF NOT EXISTS idx_signal_log_signal_type
    ON signal_log (signal_type, date_bucket);

-- ---------------------------------------------------------------------------
-- score_snapshot (append-only, enforced by trigger)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS score_snapshot (
    snapshot_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scorer        TEXT    NOT NULL,
    entity_type   TEXT    NOT NULL,
    entity_id     TEXT    NOT NULL,
    score         DOUBLE PRECISION NOT NULL,
    breakdown_json TEXT   NOT NULL DEFAULT '{}',
    inputs_json   TEXT    NOT NULL DEFAULT '{}',
    notes         TEXT    NOT NULL DEFAULT '',
    schema_version TEXT   NOT NULL DEFAULT '3.0',
    computed_at   TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_score_snapshot_lookup
    ON score_snapshot (scorer, entity_type, entity_id, computed_at);

-- Append-only enforcement mirrors the SQLite RAISE(ABORT) triggers.
-- PG triggers cannot be created IF NOT EXISTS; the DO block swallows
-- duplicate_object so replaying after a retry stays idempotent.
CREATE OR REPLACE FUNCTION fie_score_snapshot_append_only() RETURNS trigger AS $fie_guard$
BEGIN
    RAISE EXCEPTION 'score_snapshot is append-only: % rejected', TG_OP;
END;
$fie_guard$ LANGUAGE plpgsql;

DO $migrate_1$ BEGIN
    CREATE TRIGGER trg_score_snapshot_no_update
    BEFORE UPDATE ON score_snapshot
    FOR EACH ROW EXECUTE FUNCTION fie_score_snapshot_append_only();
EXCEPTION WHEN duplicate_object THEN NULL;
END $migrate_1$;

DO $migrate_2$ BEGIN
    CREATE TRIGGER trg_score_snapshot_no_delete
    BEFORE DELETE ON score_snapshot
    FOR EACH ROW EXECUTE FUNCTION fie_score_snapshot_append_only();
EXCEPTION WHEN duplicate_object THEN NULL;
END $migrate_2$;

-- ---------------------------------------------------------------------------
-- graph_nodes / graph_edges
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS graph_nodes (
    node_id        TEXT    PRIMARY KEY,
    node_type      TEXT    NOT NULL,
    label          TEXT    NOT NULL,
    created_at     TEXT    NOT NULL DEFAULT '',
    metadata_json  TEXT    NOT NULL DEFAULT '{}',
    tags_json      TEXT    NOT NULL DEFAULT '[]',
    schema_version TEXT    NOT NULL DEFAULT '3.0'
);

CREATE INDEX IF NOT EXISTS idx_graph_nodes_type ON graph_nodes (node_type);

CREATE TABLE IF NOT EXISTS graph_edges (
    edge_id        TEXT    PRIMARY KEY,
    edge_type      TEXT    NOT NULL,
    from_node_id   TEXT    NOT NULL,
    to_node_id     TEXT    NOT NULL,
    weight         DOUBLE PRECISION,
    metadata_json  TEXT    NOT NULL DEFAULT '{}',
    created_at     TEXT    NOT NULL DEFAULT '',
    schema_version TEXT    NOT NULL DEFAULT '3.0',
    UNIQUE (edge_type, from_node_id, to_node_id)
);

CREATE INDEX IF NOT EXISTS idx_graph_edges_from
    ON graph_edges (from_node_id, edge_type);
CREATE INDEX IF NOT EXISTS idx_graph_edges_to
    ON graph_edges (to_node_id, edge_type);
CREATE INDEX IF NOT EXISTS idx_graph_edges_type
    ON graph_edges (edge_type);

-- ---------------------------------------------------------------------------
-- adapter_run_log
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS adapter_run_log (
    run_id         TEXT    PRIMARY KEY,
    adapter_name   TEXT    NOT NULL,
    started_at     TEXT    NOT NULL,
    finished_at    TEXT,
    status         TEXT    NOT NULL DEFAULT 'running',
    signals_written INTEGER NOT NULL DEFAULT 0,
    error          TEXT,
    metadata_json  TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_adapter_run_log_adapter
    ON adapter_run_log (adapter_name, started_at);

-- ---------------------------------------------------------------------------
-- ingestion_errors
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingestion_errors (
    error_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source         TEXT    NOT NULL,
    occurred_at    TEXT    NOT NULL DEFAULT '',
    signal_id      TEXT,
    message        TEXT    NOT NULL,
    details_json   TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_ingestion_errors_source
    ON ingestion_errors (source, occurred_at);
"""


def build() -> Migration:
    """Return the PG twin of the v1 :class:`Migration`."""
    return Migration(version=1, name=SCHEMA_PG_V1_NAME, sql=SCHEMA_PG_V1_SQL)


__all__ = ["SCHEMA_PG_V1_NAME", "SCHEMA_PG_V1_SQL", "build"]