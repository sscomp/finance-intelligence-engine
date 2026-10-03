"""ScoringPipeline — Phase 3B Task 3 Run 2B.

Orchestrates the end-to-end scoring chain:

    SignalRepository
        │   (via SignalLoader)
        ▼
    InputBuilder
        │   (per scorer type)
        ▼
    MacroScorer | IndustryScorer | CompanyScorer
        │
        ▼
    SnapshotSink  (optional; default = in-memory no-op)

The pipeline is intentionally decoupled from any concrete persistence
mechanism: it consumes a :class:`SnapshotSink` Protocol and produces
typed :class:`PipelineResult` envelopes. Run 2A will deliver a concrete
:class:`~phase3.pipeline.snapshot_writer.SnapshotWriter` that satisfies
the same Protocol; for now, callers can use :class:`ScoreRepositorySink`
(append-only, reuses :class:`phase3.persistence.score_repo.ScoreRepository`)
or pass ``dry_run=True`` to skip persistence entirely.

Design constraints (from the Phase 3 master status document "Investment
Intelligence Engine" 2026-07-10,
§5 + §10):

* Reuse existing :class:`SignalLoader`, :class:`InputBuilder`, scorer
  classes, and repositories. No new scoring logic in this file.
* Append-only snapshots (the sink owns that contract; we never mutate
  in place).
* Deterministic: same inputs + same config → same outputs.
* Per-result evidence: signal_ids from the InputBundle are exposed
  on the result so the GraphWriter (Phase 3B Task 4) can wire them
  into the research graph later.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from phase3.datamodel import (
    CompanyScore,
    IndustryScore,
    MacroScore,
    ScoreBreakdown,
)
from phase3.pipeline import InputBundle as _PipelineInputBundle
from phase3.pipeline.input_builder import InputBuilder
from phase3.pipeline.signal_loader import SignalLoader, SignalLoaderFilters
from phase3.persistence.score_repo import ScoreRepository, ScoreSnapshotRecord
from phase3.scoring.company import CompanyScorer
from phase3.scoring.industry import IndustryScorer
from phase3.scoring.macro import MacroScorer


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class PipelineConfig:
    """Pipeline-level runtime configuration.

    Attributes:
        dry_run: If True, no snapshot is persisted. Snapshot id on results
            will be None and the configured ``sink`` is never invoked.
        notes: Human-readable note attached to each persisted snapshot
            (forwarded to :class:`ScoreSnapshotRecord.notes`).
        config_hash: Stable hash identifying the scorer config (forwarded
            to the snapshot record and stored in the breakdown).
        as_of: ``datetime`` to stamp on every score (defaults to
            ``datetime.now(timezone.utc)`` when None).
    """

    dry_run: bool = False
    notes: str = ""
    config_hash: str = "no-config"
    as_of: datetime | None = None

    def resolved_as_of(self) -> datetime:
        return self.as_of or datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Snapshot sink — Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SnapshotSink(Protocol):
    """Protocol a snapshot persistence layer must implement.

    The contract is intentionally minimal — return the assigned
    snapshot_id (an int) or ``None`` if persistence was skipped.
    Errors raised by the sink are propagated to the caller; the
    pipeline does not swallow them.
    """

    def write(
        self,
        score: Any,
        evidence_signal_ids: Sequence[str],
        notes: str = "",
        config_hash: str = "no-config",
    ) -> int | None:
        ...


# ---------------------------------------------------------------------------
# Result envelopes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PipelineResult:
    """Per-scorer result envelope.

    Attributes:
        score: One of ``MacroScore | IndustryScore | CompanyScore``.
        input_bundle: The :class:`InputBundle` that produced the score.
        evidence_signal_ids: De-duplicated union of every
            ``InputDimension.signal_ids`` referenced by the bundle.
        snapshot_id: Identifier returned by the configured
            :class:`SnapshotSink` (``None`` in dry-run mode or when
            no sink is configured).
        warnings: Pipeline-level warnings (e.g. dimensions with no
            signals, unknown scorer_type).
        metadata: Free-form per-run metadata (scorer_type, entity_id,
            date_bucket, config_hash, dry_run flag).
    """

    score: Any
    input_bundle: _PipelineInputBundle
    evidence_signal_ids: tuple[str, ...]
    snapshot_id: int | None
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineRunReport:
    """Aggregate result of :meth:`ScoringPipeline.run_all`."""

    macro: PipelineResult | None
    industries: tuple[PipelineResult, ...] = ()
    companies: tuple[PipelineResult, ...] = ()
    warnings: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Default sink — adapts ScoreRepository (append-only)
# ---------------------------------------------------------------------------


class ScoreRepositorySink:
    """Default :class:`SnapshotSink` backed by :class:`ScoreRepository`.

    The score_snapshot table is append-only (enforced by trigger + by
    the repo's own delete/update raising :class:`ScoreSnapshotMutationError`),
    so this sink is the safe default until SnapshotWriter ships in
    Run 2A. When SnapshotWriter lands it will satisfy the same Protocol
    and can be swapped in via the ``sink`` constructor arg with no
    caller-side changes.
    """

    def __init__(self, repo: ScoreRepository) -> None:
        self._repo = repo

    def write(
        self,
        score: Any,
        evidence_signal_ids: Sequence[str],
        notes: str = "",
        config_hash: str = "no-config",
    ) -> int:
        breakdown: ScoreBreakdown = score.breakdown
        record = ScoreSnapshotRecord(
            snapshot_id=None,
            scorer=breakdown.scorer_type,
            entity_type=breakdown.entity_type,
            entity_id=breakdown.entity_id,
            score=breakdown.score,
            breakdown=_breakdown_to_payload(breakdown, evidence_signal_ids),
            inputs=_score_to_inputs(score),
            notes=notes,
            schema_version=breakdown.schema_version,
            computed_at=breakdown.timestamp.isoformat(),
        )
        return self._repo.append(record)


class _NullSink:
    """No-op sink used when ``sink=None`` is explicitly passed."""

    def write(
        self,
        score: Any,
        evidence_signal_ids: Sequence[str],
        notes: str = "",
        config_hash: str = "no-config",
    ) -> None:
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dimension_signal_ids(bundle: _PipelineInputBundle) -> list[str]:
    ids: list[str] = []
    for dim in bundle.dimensions.values():
        ids.extend(dim.signal_ids)
    # preserve order, dedupe
    seen: set[str] = set()
    out: list[str] = []
    for sid in ids:
        if sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def _breakdown_to_payload(
    breakdown: ScoreBreakdown,
    evidence_signal_ids: Sequence[str],
) -> dict[str, Any]:
    """Build a JSON-serializable breakdown payload for the snapshot."""
    payload = breakdown.to_dict()
    # Annotate the payload with the signal_ids that fed the run so
    # downstream GraphWriter (Phase 3B Task 4) can read them straight
    # from the snapshot without re-running the loader.
    payload["evidence_signal_ids"] = list(evidence_signal_ids)
    return payload


def _score_to_inputs(score: Any) -> dict[str, Any]:
    """Serialize the dimension-level indicator inputs the scorer saw.

    We reconstruct the inputs from the SubIndicatorResult/WeightedFactor
    contents of each DimensionResult. This is best-effort: it captures
    the per-indicator raw values that the scorer consumed, so future
    audits can compare "what we scored" with "what the input was".
    """
    breakdown: ScoreBreakdown = score.breakdown
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


def _metadata(
    scorer_type: str,
    entity_id: str,
    date_bucket: str,
    config_hash: str,
    dry_run: bool,
) -> dict[str, Any]:
    return {
        "scorer_type": scorer_type,
        "entity_id": entity_id,
        "date_bucket": date_bucket,
        "config_hash": config_hash,
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class ScoringPipeline:
    """End-to-end scoring orchestrator.

    Construct with the dependencies it needs. Every ``run_*`` method
    returns a fully-populated :class:`PipelineResult`; persistence is
    delegated to the configured :class:`SnapshotSink` and is a no-op in
    dry-run mode or when the sink is the :class:`_NullSink`.

    Typical usage:

        >>> pipeline = ScoringPipeline(
        ...     signal_loader=SignalLoader(signal_repo),
        ...     input_builder=InputBuilder(),
        ...     macro_scorer=MacroScorer(config_hash="..."),
        ...     industry_scorer=IndustryScorer(),
        ...     company_scorer=CompanyScorer(),
        ...     sink=ScoreRepositorySink(score_repo),
        ... )
        >>> macro_result = pipeline.run_macro(date_bucket="2026-07-09")
        >>> macro_result.score.score
    """

    def __init__(
        self,
        signal_loader: SignalLoader,
        input_builder: InputBuilder | None = None,
        macro_scorer: MacroScorer | None = None,
        industry_scorer: IndustryScorer | None = None,
        company_scorer: CompanyScorer | None = None,
        sink: SnapshotSink | None = None,
        config: PipelineConfig | None = None,
    ) -> None:
        self._loader = signal_loader
        self._builder = input_builder or InputBuilder()
        self._macro = macro_scorer or MacroScorer()
        self._industry = industry_scorer or IndustryScorer()
        self._company = company_scorer or CompanyScorer()
        # ``sink=None`` is interpreted as "do not write". The internal
        # _NullSink makes the persist path uniform (no branches in
        # run_* methods).
        self._sink: SnapshotSink = sink if sink is not None else _NullSink()
        self._config = config or PipelineConfig()

    # ------------------------------------------------------------------ #
    # Public surface
    # ------------------------------------------------------------------ #

    def run_macro(
        self,
        date_bucket: str,
        entity_id: str = "global",
    ) -> PipelineResult:
        """Score the macro environment for a given date bucket.

        Args:
            date_bucket: YYYY-MM-DD. Used to filter signals AND stamp
                the snapshot.
            entity_id: Defaults to ``"global"`` (the only macro entity
                in Phase 3A design).
        """
        warnings: list[str] = []
        loaded = self._loader.load(
            SignalLoaderFilters(
                entity_type="macro",
                entity_id=entity_id,
                date_bucket=date_bucket,
            )
        )
        bundle = self._builder.build_macro(
            loaded.signals, entity_id=entity_id, date_bucket=date_bucket,
        )
        warnings.extend(bundle.warnings)
        if not bundle.dimensions:
            warnings.append(f"macro bundle has no dimensions for {date_bucket}")
        score = self._score_macro(bundle, entity_id=entity_id)
        return self._finalize(
            score=score,
            bundle=bundle,
            scorer_type="macro",
            entity_id=entity_id,
            date_bucket=date_bucket,
            warnings=warnings,
        )

    def run_industry(
        self,
        industry_id: str,
        date_bucket: str,
        industry_name: str = "",
        macro_context: ScoreBreakdown | None = None,
        industry_macro_beta: Mapping[str, float] | None = None,
    ) -> PipelineResult:
        """Score a single industry sector for a date bucket."""
        warnings: list[str] = []
        loaded = self._loader.load(
            SignalLoaderFilters(
                entity_type="industry",
                entity_id=industry_id,
                date_bucket=date_bucket,
            )
        )
        bundle = self._builder.build_industry(
            loaded.signals,
            industry_id=industry_id,
            date_bucket=date_bucket,
        )
        warnings.extend(bundle.warnings)
        if not bundle.dimensions:
            warnings.append(
                f"industry bundle has no dimensions for {industry_id}/{date_bucket}"
            )
        score = self._score_industry(
            bundle,
            industry_id=industry_id,
            industry_name=industry_name or industry_id,
            macro_context=macro_context,
            industry_macro_beta=industry_macro_beta,
        )
        return self._finalize(
            score=score,
            bundle=bundle,
            scorer_type="industry",
            entity_id=industry_id,
            date_bucket=date_bucket,
            warnings=warnings,
        )

    def run_company(
        self,
        code: str,
        date_bucket: str,
        name: str = "",
        sector: str = "",
        is_financial_sector: bool | None = None,
        macro_context: Any = None,
        industry_score: Any = None,
    ) -> PipelineResult:
        """Score a single company for a date bucket.

        ``is_financial_sector`` defaults to a heuristic: sector strings
        containing the substring ``"financ"`` (matches "financial",
        "finance", "Financials", "金融") are treated as financial. Pass
        ``True`` / ``False`` explicitly to override.
        """
        warnings: list[str] = []
        loaded = self._loader.load(
            SignalLoaderFilters(
                entity_type="company",
                entity_id=code,
                date_bucket=date_bucket,
            )
        )
        bundle = self._builder.build_company(
            loaded.signals,
            company_id=code,
            date_bucket=date_bucket,
        )
        warnings.extend(bundle.warnings)
        if not bundle.dimensions:
            warnings.append(
                f"company bundle has no dimensions for {code}/{date_bucket}"
            )
        if is_financial_sector is None:
            is_financial = bool(sector) and "financ" in sector.lower()
        else:
            is_financial = bool(is_financial_sector)
        score = self._score_company(
            bundle,
            code=code,
            name=name or code,
            sector=sector,
            is_financial=is_financial,
            macro_context=macro_context,
            industry_score=industry_score,
        )
        return self._finalize(
            score=score,
            bundle=bundle,
            scorer_type="company",
            entity_id=code,
            date_bucket=date_bucket,
            warnings=warnings,
        )

    def run_all(
        self,
        date_bucket: str,
        industry_ids: Sequence[str] = (),
        company_specs: Sequence[Mapping[str, Any]] = (),
        run_macro: bool = True,
    ) -> PipelineRunReport:
        """Run macro → industries → companies, threading the cross-layer
        context through.

        Order is fixed: macro first (so its breakdown can be threaded
        into industry `macro_sensitivity` and company macro
        adjustment); industries second; companies last (so each
        company's industry adjustment can use the matching sector's
        IndustryScore).

        Args:
            date_bucket: Shared bucket for every score in the run.
            industry_ids: Industries to score after macro.
            company_specs: Iterable of dicts with at least
                ``code``; optional ``name``, ``sector``,
                ``is_financial_sector``,
                ``industry_id_for_adjustment``.
            run_macro: Set False to skip the macro leg.
        """
        warnings: list[str] = []
        macro_result: PipelineResult | None = None
        macro_breakdown: ScoreBreakdown | None = None
        if run_macro:
            macro_result = self.run_macro(date_bucket=date_bucket)
            macro_breakdown = macro_result.score.breakdown
        else:
            warnings.append("run_all: macro leg skipped (run_macro=False)")

        industry_results: list[PipelineResult] = []
        industry_index: dict[str, PipelineResult] = {}
        for iid in industry_ids:
            ind_result = self.run_industry(
                industry_id=iid,
                date_bucket=date_bucket,
                macro_context=macro_breakdown,
            )
            industry_results.append(ind_result)
            industry_index[iid] = ind_result

        company_results: list[PipelineResult] = []
        for spec in company_specs:
            if "code" not in spec:
                warnings.append(
                    f"run_all: company_spec missing 'code': {spec!r}"
                )
                continue
            code = str(spec["code"])
            spec_industry_id = spec.get("industry_id_for_adjustment")
            industry_for_company = (
                industry_index[spec_industry_id].score
                if isinstance(spec_industry_id, str)
                and spec_industry_id in industry_index
                else None
            )
            comp_result = self.run_company(
                code=code,
                date_bucket=date_bucket,
                name=str(spec.get("name", code)),
                sector=str(spec.get("sector", "")),
                is_financial_sector=spec.get("is_financial_sector"),
                macro_context=macro_breakdown,
                industry_score=industry_for_company,
            )
            company_results.append(comp_result)

        return PipelineRunReport(
            macro=macro_result,
            industries=tuple(industry_results),
            companies=tuple(company_results),
            warnings=tuple(warnings),
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _score_macro(
        self,
        bundle: _PipelineInputBundle,
        entity_id: str,
    ) -> MacroScore:
        dim_inputs = _bundle_to_scorer_inputs(bundle)
        # The macro scorer uses config=None today; threading bundle
        # warnings into evidence is the pipeline's job, not the scorer's.
        breakdown: ScoreBreakdown = self._macro.score(
            entity_id=entity_id, inputs=dim_inputs, config=None,
        )
        return MacroScore(breakdown=breakdown, _entity_id=entity_id)

    def _score_industry(
        self,
        bundle: _PipelineInputBundle,
        industry_id: str,
        industry_name: str,
        macro_context: ScoreBreakdown | None,
        industry_macro_beta: Mapping[str, float] | None,
    ) -> IndustryScore:
        dim_inputs = _bundle_to_scorer_inputs(bundle)
        config: dict[str, Any] = {}
        if macro_context is not None:
            config["macro_context"] = macro_context
        if industry_macro_beta:
            config["industry_macro_beta"] = dict(industry_macro_beta)
        breakdown: ScoreBreakdown = self._industry.score(
            entity_id=industry_id, inputs=dim_inputs, config=config or None,
        )
        return IndustryScore(
            breakdown=breakdown,
            industry_name=industry_name or industry_id,
            constituent_count=0,
        )

    def _score_company(
        self,
        bundle: _PipelineInputBundle,
        code: str,
        name: str,
        sector: str,
        is_financial: bool,
        macro_context: Any,
        industry_score: Any,
    ) -> CompanyScore:
        dim_inputs = _bundle_to_scorer_inputs(bundle)
        config: dict[str, Any] = {"is_financial_sector": is_financial}
        breakdown: ScoreBreakdown = self._company.score(
            entity_id=code, inputs=dim_inputs, config=config,
        )
        # Use the scorer's high-level wrapper so the cross-layer
        # adjustment (and audit trail in `breakdown.cross_layer_adjustments`)
        # is computed.
        return self._company.score_company(
            code=code,
            name=name,
            sector=sector,
            inputs=dim_inputs,
            config=config,
            macro_context=macro_context,
            industry_score=industry_score,
        )

    def _finalize(
        self,
        *,
        score: Any,
        bundle: _PipelineInputBundle,
        scorer_type: str,
        entity_id: str,
        date_bucket: str,
        warnings: list[str],
    ) -> PipelineResult:
        evidence_signal_ids = tuple(_dimension_signal_ids(bundle))
        meta = _metadata(
            scorer_type=scorer_type,
            entity_id=entity_id,
            date_bucket=date_bucket,
            config_hash=self._config.config_hash,
            dry_run=self._config.dry_run,
        )
        if self._config.dry_run:
            snapshot_id: int | None = None
        else:
            snapshot_id = self._sink.write(
                score=score,
                evidence_signal_ids=evidence_signal_ids,
                notes=self._config.notes,
                config_hash=self._config.config_hash,
            )
        return PipelineResult(
            score=score,
            input_bundle=bundle,
            evidence_signal_ids=evidence_signal_ids,
            snapshot_id=snapshot_id,
            warnings=tuple(warnings),
            metadata=meta,
        )


# ---------------------------------------------------------------------------
# Scorer input shape conversion
# ---------------------------------------------------------------------------


def _bundle_to_scorer_inputs(
    bundle: _PipelineInputBundle,
) -> dict[str, dict[str, Any]]:
    """Flatten :class:`InputBundle.dimensions` into the per-dimension
    ``{indicator_name: value}`` shape scorers expect.

    The pipeline is the boundary that knows about the input shape
    contract — scorers consume raw indicator dicts and never see
    :class:`InputBundle` or :class:`InputDimension`. Keeping this
    translation here means :class:`InputBuilder` and the scorers
    stay decoupled.
    """
    out: dict[str, dict[str, Any]] = {}
    for name, dim in bundle.dimensions.items():
        out[name] = dict(dim.values)
    return out


__all__ = [
    "PipelineConfig",
    "PipelineResult",
    "PipelineRunReport",
    "ScoringPipeline",
    "ScoreRepositorySink",
    "SnapshotSink",
]
