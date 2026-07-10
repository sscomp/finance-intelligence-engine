"""Phase 3B pipeline primitives (SignalLoader, InputBuilder, bundle DTOs).

This package hosts the glue between the Phase 3B ingestion adapters and the
scoring layers. Run 1 (Task 3) intentionally ships only the data loading and
input-shaping helpers – no scoring orchestration, snapshotting, or graph writes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from phase3.datamodel import DimensionResult
from phase3.datamodel.signals import Signal

ScorerType = Literal["macro", "industry", "company"]


@dataclass(frozen=True)
class LoadedSignals:
    """Container produced by SignalLoader.

    Attributes:
        signals: List of hydrated Signal datamodels.
        source: Human readable hint (e.g. DB path) for debugging.
        filters: Dict of query filters that produced the signals.
    """

    signals: list[Signal] = field(default_factory=list)
    source: str = ""
    filters: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class InputDimension:
    """Per-dimension inputs passed to scorers.

    Each dimension carries:
        values: Arbitrary scoring inputs (numeric or categorical values).
        signal_ids: Raw signals that contributed to this dimension.
        confidence: Optional confidence override (0-1).
        warnings: Human readable warnings about missing data.
    """

    values: dict[str, object] = field(default_factory=dict)
    signal_ids: list[str] = field(default_factory=list)
    confidence: float | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class InputBundle:
    """Typed scorer input bundle emitted by InputBuilder.

    Attributes:
        scorer_type: "macro" | "industry" | "company".
        entity_type / entity_id: Target entity for the scorer.
        date_bucket: YYYY-MM-DD bucket shared by the signals.
        dimensions: Map of dimension name -> InputDimension.
        warnings: Global warnings (missing data, gaps).
        evidence_dimension_results: Optional cache of per-dimension aggregated
            data (Phase 3B later runs will store DimensionResult output).
    """

    scorer_type: ScorerType
    entity_type: str
    entity_id: str
    date_bucket: str
    dimensions: dict[str, InputDimension] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    evidence_dimension_results: dict[str, DimensionResult] = field(default_factory=dict)
    generated_at: datetime = field(default_factory=datetime.utcnow)


__all__ = [
    "LoadedSignals",
    "InputDimension",
    "InputBundle",
]

# SnapshotWriter lives in a sibling module to avoid a circular import
# with `scoring_pipeline` (which defines the SnapshotSink Protocol that
# SnapshotWriter satisfies). Re-exported here so callers can reach it
# via the same `phase3.pipeline` namespace.
from phase3.pipeline.snapshot_writer import SnapshotWriter, SnapshotWriterConfig  # noqa: E402

# GraphWriter (Phase 3B Task 4) lives in a sibling module to avoid a
# circular import with `scoring_pipeline` (it consumes PipelineResult).
# Re-exported here so callers can reach it via the same `phase3.pipeline`
# namespace.
from phase3.pipeline.graph_writer import (  # noqa: E402
    GraphWriteResult,
    GraphWriter,
    GraphWriterStore,
    make_entity_node_id,
    make_score_node_id,
    make_signal_node_id,
    make_source_node_id,
)

__all__ = [
    "LoadedSignals",
    "InputDimension",
    "InputBundle",
    "SnapshotWriter",
    "SnapshotWriterConfig",
    "GraphWriter",
    "GraphWriteResult",
    "GraphWriterStore",
    "make_source_node_id",
    "make_signal_node_id",
    "make_score_node_id",
    "make_entity_node_id",
]
