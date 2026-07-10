"""SnapshotWriter — Phase 3B Task 3 Run 2A.

Concrete :class:`~phase3.pipeline.scoring_pipeline.SnapshotSink` that
persists :class:`ScoreBreakdown` output through :class:`ScoreRepository`.

Why this exists
---------------
The pipeline intentionally decouples scoring from persistence via the
:class:`SnapshotSink` Protocol. ``ScoreRepositorySink`` (in
``scoring_pipeline.py``) is the safe default — it appends one row per
score. ``SnapshotWriter`` is the **canonical** writer:

* Deterministic ``score_id`` derived from the same five inputs the
  scorer used (``scorer_type``, ``entity_type``, ``entity_id``,
  ``date_bucket``, ``config_hash``, plus the sorted evidence signal
  list and the run mode). Two runs over the same inputs and the
  same run mode get the *same* ``score_id``, which is a stable
  cross-run handle for the audit trail.
* Append-only — it goes through ``ScoreRepository.append`` and never
  mutates. :class:`ScoreSnapshotMutationError` still fires for
  callers that try to update or delete.
* Dry-run — every call is prepared but never written. Returns
  ``None`` (the same shape the Protocol uses for skipped writes).
* Persist — writes the prepared record and returns the assigned
  ``snapshot_id`` (an int).

The class is intentionally small. The score snapshot table is
append-only and the row already carries everything we need; the
"writer" layer is mostly a deterministic ID + a thin shim around
:class:`ScoreRepository.append`.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from phase3.persistence.score_repo import ScoreRepository, ScoreSnapshotRecord


# ---------------------------------------------------------------------------
# Public configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SnapshotWriterConfig:
    """Per-instance writer configuration.

    Attributes:
        run_mode: Free-form label for the run context (e.g.
            ``"live"``, ``"backtest"``, ``"shadow"``). It is part of
            the deterministic :meth:`SnapshotWriter.compute_score_id`
            input — same scorer/entity/date/config/evidence but
            different ``run_mode`` ⇒ different score_id. Stored on
            the row's ``notes`` if no explicit ``notes`` is passed
            to :meth:`write`.
        run_id: Optional caller-supplied run handle for cross-run
            bookkeeping. Stored on the row when provided. Does not
            influence the deterministic score_id (run_id is by
            definition non-deterministic).
        dry_run: If True, :meth:`write` prepares the record and
            returns ``None`` without touching the repo. If False,
            the record is appended and the assigned ``snapshot_id``
            is returned.
    """

    run_mode: str = "live"
    run_id: str | None = None
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class SnapshotWriter:
    """Append-only snapshot writer that satisfies :class:`SnapshotSink`.

    Construction:
        >>> writer = SnapshotWriter(score_repo, SnapshotWriterConfig(
        ...     run_mode="live", run_id="2026-07-09T08:00Z",
        ... ))

    Usage through the pipeline:
        >>> pipeline = ScoringPipeline(
        ...     signal_loader=...,
        ...     sink=writer,            # satisfies SnapshotSink Protocol
        ...     config=PipelineConfig(notes="morning-brief"),
        ... )

    Direct usage (the Protocol contract):
        >>> snapshot_id = writer.write(
        ...     score=macro_score,
        ...     evidence_signal_ids=("m-gdp", "m-cpi"),
        ...     notes="morning-brief",
        ...     config_hash="cfg-v1",
        ... )
    """

    def __init__(
        self,
        score_repo: ScoreRepository,
        config: SnapshotWriterConfig | None = None,
    ) -> None:
        self._repo = score_repo
        self._config = config or SnapshotWriterConfig()

    # ----- protocol surface ----------------------------------------------

    def write(
        self,
        score: Any,
        evidence_signal_ids: Sequence[str],
        notes: str = "",
        config_hash: str = "no-config",
    ) -> int | None:
        """Append a snapshot row. Returns the assigned ``snapshot_id`` or
        ``None`` if dry-run is active.

        Satisfies :class:`~phase3.pipeline.scoring_pipeline.SnapshotSink`.
        Errors from the underlying repo are propagated unchanged; this
        class never swallows persistence errors.

        Args:
            score: A score object exposing a ``.breakdown`` attribute of
                type :class:`ScoreBreakdown` (the union of
                ``MacroScore``/``IndustryScore``/``CompanyScore``).
            evidence_signal_ids: Signal identifiers that fed the run.
                Stored on the row inside ``breakdown_json`` under the
                ``evidence_signal_ids`` key.
            notes: Caller note. Forwarded to ``ScoreSnapshotRecord.notes``.
                When empty, falls back to a synthesized
                ``"[run_mode=<mode>] [run_id=<id>]"`` string so the
                run context is never lost.
            config_hash: Stable scorer-config hash. Stored verbatim in
                ``breakdown_json["config_hash"]`` and embedded in the
                deterministic ``score_id`` input.
        """
        record = self.build_record(
            score=score,
            evidence_signal_ids=evidence_signal_ids,
            notes=notes,
            config_hash=config_hash,
        )
        if self._config.dry_run:
            return None
        return self._repo.append(record)

    # ----- deterministic score id ----------------------------------------

    def compute_score_id(
        self,
        *,
        scorer_type: str,
        entity_type: str,
        entity_id: str,
        date_bucket: str,
        config_hash: str,
        evidence_signal_ids: Sequence[str],
    ) -> str:
        """Return a stable 16-char hex score id for the given inputs.

        The id is derived from a JSON-serialized tuple of the seven
        deterministic inputs (run_mode is included so two runs over
        the same data but in different modes get distinct ids). The
        field names are sorted and the evidence list is sorted too,
        so the hash is invariant under re-orderings of the same
        set of signals.

        Note: ``run_id`` is intentionally NOT part of the id. It is
        per-execution bookkeeping; a re-run of the same scoring
        over the same inputs in the same mode should produce the
        same score_id so consumers can de-duplicate.
        """
        payload = {
            "scorer_type": str(scorer_type),
            "entity_type": str(entity_type),
            "entity_id": str(entity_id),
            "date_bucket": str(date_bucket),
            "config_hash": str(config_hash),
            "evidence_signal_ids": sorted(str(s) for s in evidence_signal_ids),
            "run_mode": str(self._config.run_mode),
        }
        # sort_keys ensures field order is irrelevant; the JSON itself
        # is what feeds the digest. Separators strip whitespace so
        # two calls on the same data produce the same string.
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]

    # ----- record assembly ------------------------------------------------

    def build_record(
        self,
        *,
        score: Any,
        evidence_signal_ids: Sequence[str],
        notes: str,
        config_hash: str,
    ) -> ScoreSnapshotRecord:
        """Build the :class:`ScoreSnapshotRecord` for a given score.

        Centralizes the breakdown-to-payload mapping and the
        deterministic score_id stamping. The record is fully
        populated; persistence is a separate :meth:`write` call so
        dry-run callers can inspect what *would* have been written.
        """
        breakdown = score.breakdown
        # De-dupe the evidence list to a sorted tuple so the JSON
        # payload is stable across calls that pass the same set in
        # different order.
        deduped_evidence = tuple(
            dict.fromkeys(str(s) for s in evidence_signal_ids)
        )
        payload = self._build_breakdown_payload(
            breakdown=breakdown,
            evidence_signal_ids=deduped_evidence,
            config_hash=config_hash,
        )
        inputs_payload = self._build_inputs_payload(breakdown)
        final_notes = self._compose_notes(notes)

        # The date_bucket comes from the breakdown's timestamp when
        # available (the scorer stamps a UTC timestamp at score
        # time). Fall back to a YYYY-MM-DD slice of breakdown.timestamp.
        date_bucket = self._date_bucket(breakdown)

        score_id = self.compute_score_id(
            scorer_type=breakdown.scorer_type,
            entity_type=breakdown.entity_type,
            entity_id=breakdown.entity_id,
            date_bucket=date_bucket,
            config_hash=config_hash,
            evidence_signal_ids=deduped_evidence,
        )

        computed_at = self._iso(breakdown.timestamp)
        # Score id and run_id are smuggled into the breakdown JSON
        # payload because the ScoreSnapshotRecord DTO has no column
        # for them; consumers can read them out of breakdown_json
        # with the reserved ``_score_id`` and ``_run_id`` keys.
        payload_with_meta: dict[str, Any] = {
            **payload,
            "_score_id": score_id,
            "_run_id": self._config.run_id,
        }
        return ScoreSnapshotRecord(
            snapshot_id=None,  # always DB-assigned
            scorer=breakdown.scorer_type,
            entity_type=breakdown.entity_type,
            entity_id=breakdown.entity_id,
            score=float(breakdown.score),
            breakdown=payload_with_meta,
            inputs=inputs_payload,
            notes=final_notes,
            schema_version=breakdown.schema_version,
            computed_at=computed_at,
        )

    # ----- public properties (read-only) ----------------------------------

    @property
    def config(self) -> SnapshotWriterConfig:
        return self._config

    @property
    def repo(self) -> ScoreRepository:
        return self._repo

    # ----- internals ------------------------------------------------------

    def _build_breakdown_payload(
        self,
        *,
        breakdown: Any,
        evidence_signal_ids: Sequence[str],
        config_hash: str,
    ) -> dict[str, Any]:
        """Serialize the breakdown to a JSON-safe payload.

        The breakdown itself is JSON-serializable via ``to_dict()``;
        we copy it, append the ``config_hash`` (so it round-trips
        back to the same caller value) and the deduped evidence
        signal ids (so a future reader can pull them out without
        re-running the loader).
        """
        base = breakdown.to_dict()
        base["config_hash"] = str(config_hash)
        base["evidence_signal_ids"] = list(evidence_signal_ids)
        return base

    def _build_inputs_payload(self, breakdown: Any) -> dict[str, Any]:
        """Per-indicator raw values captured at score time.

        Mirrors :func:`_score_to_inputs` in ``scoring_pipeline.py`` —
        we keep the shape small and well-typed so a future GraphWriter
        can replay the exact input set if it needs to.
        """
        out: dict[str, dict[str, Any]] = {}
        for dim in breakdown.dimensions:
            bucket: dict[str, Any] = {}
            for f in dim.factors:
                bucket[f.name] = {
                    "raw_value": f.raw_value,
                    "raw_unit": f.raw_unit,
                    "sub_score": f.sub_score,
                    "sub_weight": f.sub_weight,
                    "signed_score": f.signed_score,
                    "transformation": f.transformation,
                    "source": f.source,
                    "source_ref": f.source_ref,
                }
            out[dim.name] = bucket
        return out

    def _compose_notes(self, caller_notes: str) -> str:
        """Resolve final notes value.

        Strategy: if the caller supplied a non-empty note, use it
        as-is (do not silently mutate their input). Otherwise
        synthesize a minimal ``run_mode=`` / ``run_id=`` string so
        the row still carries the run context.
        """
        if caller_notes:
            return str(caller_notes)
        parts: list[str] = [f"run_mode={self._config.run_mode}"]
        if self._config.run_id is not None:
            parts.append(f"run_id={self._config.run_id}")
        return " ".join(parts)

    def _date_bucket(self, breakdown: Any) -> str:
        """Derive a YYYY-MM-DD bucket from the breakdown timestamp.

        The score's ``breakdown.timestamp`` is a UTC-aware
        ``datetime``; the bucket is the UTC date.
        """
        ts: datetime = breakdown.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc).date().isoformat()

    def _iso(self, ts: datetime) -> str:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc).isoformat()


__all__ = [
    "SnapshotWriter",
    "SnapshotWriterConfig",
]
