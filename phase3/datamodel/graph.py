"""Research Graph data model: GraphNode, GraphEdge, NodeType, EdgeType.

Plain frozen dataclasses. The graph store (InMemoryGraphStore /
SQLiteGraphStore) lives in phase3.graph.*; this file is pure data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from phase3.datamodel._version import SCHEMA_VERSION


class NodeType(str, Enum):
    """Kinds of nodes the research graph can hold.

    Phase 3A ships the union from docs/phase3/06_research_graph.md §1.
    Adapters are not required to populate every type — empty graphs are
    valid. NodeType.COMPANY corresponds to a listed company; NodeType.SIGNAL
    corresponds to a SignalEngine-produced NormalizedSignal (referenced by
    signal_id); NodeType.SCORE corresponds to a scorer output (referenced
    by score_id).
    """

    COMPANY = "company"
    INDUSTRY = "industry"
    MACRO_FACTOR = "macro_factor"
    NEWS = "news"
    SIGNAL = "signal"
    SCORE = "score"
    REPORT = "report"
    PERSON = "person"
    SOURCE = "source"


class EdgeType(str, Enum):
    """Kinds of edges in the research graph.

    From docs/phase3/06_research_graph.md §2. Phase 3A uses a small subset
    sufficient for evidence tracing; the rest are reserved for Phase 3B+.
    """

    MEMBER_OF = "member_of"  # Company -> Industry
    BELONGS_TO = "belongs_to"  # Industry -> MacroFactor
    EXPOSED_TO = "exposed_to"  # Company -> MacroFactor
    GENERATED = "generated"  # Signal -> Source
    REFERS_TO = "refers_to"  # News -> Company/Industry/MacroFactor/Person
    CONTRIBUTES_TO = "contributes_to"  # Signal -> Score
    INFLUENCES = "influences"  # Score -> Score (cross-layer)
    INCLUDES = "includes"  # Report -> Score
    REPORT_BY = "report_by"  # Report -> Person
    WORKS_AT = "works_at"  # Person -> Company
    CITES = "cites"  # Score/Report -> Source
    DERIVED_FROM = "derived_from"  # Score -> Score


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class GraphNode:
    """A node in the research graph.

    `node_id` is content-derived by convention (e.g. "company:2330",
    "score:macro:global:2026-07-08") so re-creation is idempotent.
    """

    node_id: str
    node_type: NodeType
    label: str
    created_at: datetime = field(default_factory=_utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type.value,
            "label": self.label,
            "created_at": self.created_at.isoformat(),
            "metadata": dict(self.metadata),
            "tags": list(self.tags),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GraphNode":
        return cls(
            node_id=data["node_id"],
            node_type=NodeType(data["node_type"]),
            label=data["label"],
            created_at=datetime.fromisoformat(data["created_at"]),
            metadata=dict(data.get("metadata", {})),
            tags=list(data.get("tags", [])),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )


@dataclass(frozen=True)
class GraphEdge:
    """A directed edge in the research graph.

    `weight` is optional and only used for weighted shortest-path queries.
    For MEMBER_OF edges, weight can be a membership fraction (0..1).
    """

    edge_id: str
    edge_type: EdgeType
    from_node_id: str
    to_node_id: str
    weight: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utcnow)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "edge_type": self.edge_type.value,
            "from_node_id": self.from_node_id,
            "to_node_id": self.to_node_id,
            "weight": self.weight,
            "metadata": dict(self.metadata),
            "created_at": self.created_at.isoformat(),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GraphEdge":
        return cls(
            edge_id=data["edge_id"],
            edge_type=EdgeType(data["edge_type"]),
            from_node_id=data["from_node_id"],
            to_node_id=data["to_node_id"],
            weight=data.get("weight"),
            metadata=dict(data.get("metadata", {})),
            created_at=datetime.fromisoformat(data["created_at"]),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )


def make_graph_edge_id(
    edge_type: EdgeType, from_node_id: str, to_node_id: str
) -> str:
    """Deterministic edge id so re-inserting the same logical edge is idempotent.

    Phase 3A upsert contract: an edge is identified by (edge_type, from, to)
    — weight/metadata can be updated, identity cannot.
    """
    import hashlib

    payload = f"{edge_type.value}|{from_node_id}|{to_node_id}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


# Default graph runtime config. Kept here (next to data model) so the
# default graph behavior is inspectable without loading YAML.
DEFAULT_GRAPH_CONFIG: dict[str, Any] = {
    "max_traversal_depth": 10,
    "default_shortest_path_algorithm": "dijkstra",  # or "bfs" for unweighted
    "evidence_tracer_max_hops": 5,
}
