"""Phase 3B schema v1.

Single source of truth for the initial Phase 3B table layout. The
:class:`~phase3.persistence.migrations.Migration` here is the
forward-only change that creates the v1 schema.

Tables
------
* ``schema_migrations`` is created by the
  :class:`~phase3.persistence.migrations.MigrationManager` itself,
  not here, so the manager can run on a brand-new database without
  depending on any migration having applied first.
* ``signal_log`` — append-only log of signal observations. Idempotent
  on ``(signal_id)`` via UPSERT.
* ``score_snapshot`` — append-only history of score computations.
  Enforced by a ``BEFORE UPDATE`` trigger that raises an error.
* ``graph_nodes`` / ``graph_edges`` — SQLite-backed graph store,
  parallel to :class:`~phase3.graph.in_memory_store.GraphStore`.
* ``adapter_run_log`` and ``ingestion_errors`` are placeholders for
  Phase 3B Task 2 / 3. Their tables exist now so v1 is the only
  schema version that adds a new table — future versions only
  ALTER or add indexes.
"""
from __future__ import annotations

from phase3.persistence.migrations import Migration


#: Human-readable name of the initial schema migration.
SCHEMA_V1_NAME: str = "phase3b_initial_schema"

#: Canonical SQL for the initial Phase 3B schema.
SCHEMA_V1_SQL: str = """
-- ---------------------------------------------------------------------------
-- signal_log
-- ---------------------------------------------------------------------------
-- One row per (signal_id). Idempotent UPSERT keyed on signal_id.
-- JSON columns store structured payload + metadata so we never need
-- to migrate them when the Signal dataclass gains new fields.
CREATE TABLE IF NOT EXISTS signal_log (
    signal_id      TEXT    PRIMARY KEY,
    entity_type    TEXT    NOT NULL,
    entity_id      TEXT    NOT NULL,
    signal_type    TEXT    NOT NULL,
    value          REAL    NOT NULL,
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
    ingested_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_signal_log_entity
    ON signal_log (entity_type, entity_id, date_bucket);
CREATE INDEX IF NOT EXISTS idx_signal_log_source
    ON signal_log (source_type, date_bucket);
CREATE INDEX IF NOT EXISTS idx_signal_log_signal_type
    ON signal_log (signal_type, date_bucket);

-- ---------------------------------------------------------------------------
-- score_snapshot
-- ---------------------------------------------------------------------------
-- One row per (scorer, entity_type, entity_id, computed_at) computation.
-- Append-only; updates and deletes raise an error from a trigger.
CREATE TABLE IF NOT EXISTS score_snapshot (
    snapshot_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    scorer        TEXT    NOT NULL,
    entity_type   TEXT    NOT NULL,
    entity_id     TEXT    NOT NULL,
    score         REAL    NOT NULL,
    breakdown_json TEXT   NOT NULL DEFAULT '{}',
    inputs_json   TEXT    NOT NULL DEFAULT '{}',
    notes         TEXT    NOT NULL DEFAULT '',
    schema_version TEXT   NOT NULL DEFAULT '3.0',
    computed_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_score_snapshot_lookup
    ON score_snapshot (scorer, entity_type, entity_id, computed_at);

-- Append-only enforcement. UPDATE and DELETE on score_snapshot are
-- rejected; re-scoring must insert a fresh row.
CREATE TRIGGER IF NOT EXISTS trg_score_snapshot_no_update
BEFORE UPDATE ON score_snapshot
BEGIN
    SELECT RAISE(ABORT, 'score_snapshot is append-only: UPDATE rejected');
END;

CREATE TRIGGER IF NOT EXISTS trg_score_snapshot_no_delete
BEFORE DELETE ON score_snapshot
BEGIN
    SELECT RAISE(ABORT, 'score_snapshot is append-only: DELETE rejected');
END;

-- ---------------------------------------------------------------------------
-- graph_nodes / graph_edges
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS graph_nodes (
    node_id        TEXT    PRIMARY KEY,
    node_type      TEXT    NOT NULL,
    label          TEXT    NOT NULL,
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
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
    weight         REAL,
    metadata_json  TEXT    NOT NULL DEFAULT '{}',
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
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
-- Phase 3B Task 2+ will write one row per adapter run. The table
-- exists in v1 so a single migration creates the whole Phase 3B
-- foundation; later migrations only need to ALTER.
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
    error_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT    NOT NULL,
    occurred_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    signal_id      TEXT,
    message        TEXT    NOT NULL,
    details_json   TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_ingestion_errors_source
    ON ingestion_errors (source, occurred_at);
"""


def build() -> Migration:
    """Return the v1 :class:`Migration`."""
    return Migration(version=1, name=SCHEMA_V1_NAME, sql=SCHEMA_V1_SQL)


__all__ = ["SCHEMA_V1_NAME", "SCHEMA_V1_SQL", "build"]
