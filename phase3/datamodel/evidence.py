"""Evidence — provenance for a single data point that fed a score."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from phase3.datamodel._version import SCHEMA_VERSION


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Evidence:
    """A single piece of provenance attached to a score or weighted signal.

    The 'weight' field records the *actual* contribution of this evidence
    in the calculation that produced the parent score (e.g. 0.087 if it
    was 8.7% of the weighted sum). It is informational, not a re-computation
    input.
    """

    evidence_id: str  # sha256(source_type+ref+raw_value)[:16]
    source_type: str  # "yfinance" | "rss" | "t86" | "config" | "computed"
    source_ref: str  # original URL / API path / config key
    raw_value: str  # original value as string (for debug)
    description: str  # human-readable: "yfinance PE ratio 32.8"
    timestamp: datetime
    weight: float | None = None
    schema_version: str = SCHEMA_VERSION
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "raw_value": self.raw_value,
            "description": self.description,
            "timestamp": self.timestamp.isoformat(),
            "weight": self.weight,
            "schema_version": self.schema_version,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Evidence":
        return cls(
            evidence_id=data["evidence_id"],
            source_type=data["source_type"],
            source_ref=data["source_ref"],
            raw_value=data["raw_value"],
            description=data["description"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            weight=data.get("weight"),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            metadata=dict(data.get("metadata", {})),
        )


def make_evidence_id(source_type: str, source_ref: str, raw_value: str) -> str:
    """sha256-derived 16-char id. Stable across runs."""
    import hashlib

    payload = f"{source_type}|{source_ref}|{raw_value}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]
