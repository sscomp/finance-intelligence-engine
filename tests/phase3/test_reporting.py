"""Tests for Phase 3B Task 5 Run 3 — Report / Export Layer.

The reporting layer is pure presentation: it serializes
``IntelligenceRunResult`` and ``RecoveryResult`` envelopes into
deterministic JSON + Markdown artifacts. No business logic, no DB
writes, no scorer/store access. The tests cover the 10 scenarios the
brief asked for:

1.  JSON export structure (header + summary + result payload)
2.  Markdown report generation (sections present + deterministic)
3.  Empty pipeline result (None input ⇒ "empty run" branch)
4.  Warning propagation (warnings are echoed into JSON + Markdown)
5.  Deterministic repeated export (same input + same generated_at ⇒
    byte-identical files)
6.  Temp directory output (writes go to ``tempfile.TemporaryDirectory``)
7.  Invalid destination handling (missing / non-dir / non-writable ⇒
    ``ReportExportError``)
8.  UTF-8 encoding (Chinese characters round-trip through JSON +
    Markdown)
9.  Recovery metadata inclusion (RecoveryResult produces a recovery
    variant of the export)
10. Evidence / query summary inclusion (evidence_handles survive into
    the JSON + Markdown when ``include_evidence_payload=True``)

Verification contract
---------------------
* No full Phase 3 regression — only targeted + compatibility tests.
* macro_history.db must be untouched.
* No intelligence.db* files in phase3/data/ after the run.
* Re-uses the existing pipeline fixtures (no duplicate logic).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timezone
from typing import Any

from phase3.datamodel.scores import (
    DimensionResult,
    ScoreBreakdown,
    WeightedFactor,
)
from phase3.datamodel.scores_macro import (
    MACRO_ENTITY_ID,
    MACRO_SCORER_TYPE,
)
from phase3.graph.evidence_trace_export import EvidenceChainAdapter
from phase3.graph.in_memory_store import GraphStore
from phase3.persistence.migrations import MigrationManager
from phase3.persistence.score_repo import ScoreRepository
from phase3.persistence.schema_v1 import build as build_v1
from phase3.persistence.signal_repo import SignalRepository
from phase3.persistence.sqlite import SQLiteStore
from phase3.pipeline.graph_writer import GraphWriter
from phase3.pipeline.intelligence_pipeline import (
    IntelligencePipeline,
    IntelligencePipelineConfig,
    IntelligenceRunResult,
)
from phase3.pipeline.recovery import (
    FailureCategory,
    RecoveryConfig,
    RecoveryManager,
    RecoveryResult,
    RunState,
    Stage,
    StageAttempt,
)
from phase3.pipeline.reporting import (
    ReportArtifact,
    ReportConfig,
    ReportExportError,
    SummaryStatistics,
    build_json_export,
    build_recovery_json_export,
    build_summary_statistics,
    export_report,
    render_markdown_report,
    render_recovery_markdown_report,
)
from phase3.pipeline.scoring_pipeline import PipelineConfig, ScoringPipeline
from phase3.pipeline.signal_loader import SignalLoader
from phase3.pipeline.snapshot_writer import SnapshotWriter, SnapshotWriterConfig
from phase3.scoring.company import CompanyScorer
from phase3.scoring.industry import IndustryScorer
from phase3.scoring.macro import MacroScorer

# Reuse the fixtures from the Run 1 / Run 2 test modules — do not
# duplicate business logic.
from tests.phase3.test_intelligence_pipeline import (  # noqa: E402
    _build_intelligence_pipeline,
    _new_store,
    _seed_company_signals,
    _seed_industry_signals,
    _seed_macro_signals,
)
from tests.phase3.test_recovery import _build_wired  # noqa: E402


DATE = "2026-07-09"
TS = datetime(2026, 7, 9, 12, tzinfo=timezone.utc)
CONFIG_HASH = "report-test-cfg"
FIXED_TS = "2026-07-11T08:00:00+00:00"


# ---------------------------------------------------------------------------
# Fixtures — local helpers that build an IntelligenceRunResult/RecoveryResult
# without re-implementing the orchestrator wiring.
# ---------------------------------------------------------------------------


def _run_macro_only_dry_run() -> IntelligenceRunResult:
    """Return a real IntelligenceRunResult from a macro-only dry run."""
    path, store = _new_store()
    try:
        signal_repo = SignalRepository(store)
        _seed_macro_signals(signal_repo)
        orchestrator, _, _ = _build_intelligence_pipeline(
            store, scoring_kwargs={"dry_run": True},
        )
        cfg = IntelligencePipelineConfig(
            date_bucket=DATE,
            industry_ids=(),
            company_specs=(),
            run_macro=True,
            persist=False,
            config_hash=CONFIG_HASH,
        )
        return orchestrator.run(cfg)
    finally:
        os.unlink(path)


def _run_full_chain_persisted() -> IntelligenceRunResult:
    """Return a real IntelligenceRunResult from a macro+industry+company run."""
    path, store = _new_store()
    try:
        signal_repo = SignalRepository(store)
        _seed_macro_signals(signal_repo)
        _seed_industry_signals(signal_repo, industry_id="AI")
        _seed_company_signals(signal_repo, code="2330")
        orchestrator, _, _ = _build_intelligence_pipeline(store)
        cfg = IntelligencePipelineConfig(
            date_bucket=DATE,
            industry_ids=("AI",),
            company_specs=({
                "code": "2330", "name": "TSMC",
                "sector": "Technology",
                "industry_id_for_adjustment": "AI",
            },),
            run_macro=True,
            persist=True,
            config_hash=CONFIG_HASH,
        )
        return orchestrator.run(cfg)
    finally:
        os.unlink(path)


def _run_recovery_terminal() -> RecoveryResult:
    """Return a real RecoveryResult where graph write fails terminally.

    Reuses the Run 2 test fixture (``_build_wired`` + ``gw.write``
    monkey-patch) so we do not duplicate the orchestrator wiring.
    """
    path, store = _new_store()
    try:
        orch, score_repo, signal_repo, graph_store, gw, _adapter = _build_wired(store)
        _seed_macro_signals(signal_repo)
        cfg = IntelligencePipelineConfig(
            date_bucket=DATE,
            run_id="reporting-graphfail",
            config_hash=CONFIG_HASH,
            persist=True,
        )
        # Replace the graph writer with a function that always fails.
        def _always_fail_write(pr: Any) -> Any:
            raise ValueError("simulated graph write failure")

        gw.write = _always_fail_write  # type: ignore[method-assign]
        manager = RecoveryManager(
            score_repo=score_repo,
            pipeline=orch,
            graph_store=graph_store,
            config=RecoveryConfig(max_attempts=1, retry_backoff_seconds=0.0),
        )
        return manager.resume(cfg)
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class JsonExportStructureTests(unittest.TestCase):
    """Scenario 1: JSON export structure."""

    def test_json_export_has_envelope_header_and_summary(self) -> None:
        result = _run_full_chain_persisted()
        payload = build_json_export(result, generated_at=FIXED_TS)
        # Envelope header
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["generated_at"], FIXED_TS)
        self.assertFalse(payload["recovery"])
        # Artifact metadata block
        self.assertEqual(payload["artifact"]["run_id"], result.run_id)
        self.assertEqual(payload["artifact"]["config_hash"], CONFIG_HASH)
        self.assertEqual(payload["artifact"]["date_bucket"], DATE)
        self.assertEqual(payload["artifact"]["duration_seconds"], result.duration_seconds)
        # Summary statistics
        self.assertIn("summary", payload)
        for key in (
            "signal_count", "score_count", "snapshot_count", "graph_node_count",
            "graph_edge_count", "evidence_handle_count", "warning_count",
            "error_count", "company_score_count", "industry_score_count",
            "macro_score_count",
        ):
            self.assertIn(key, payload["summary"])
        # Warnings + errors propagated at envelope level
        self.assertEqual(payload["warnings"], list(result.warnings))
        self.assertEqual(payload["errors"], list(result.errors))
        # Result payload embedded
        self.assertIsNotNone(payload["result"])
        self.assertEqual(payload["result"]["run_id"], result.run_id)
        # JSON-serializable
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        roundtrip = json.loads(encoded)
        self.assertEqual(roundtrip["artifact"]["run_id"], result.run_id)


class MarkdownReportTests(unittest.TestCase):
    """Scenario 2: Markdown report generation."""

    def test_markdown_sections_present_and_deterministic(self) -> None:
        result = _run_full_chain_persisted()
        md = render_markdown_report(result, generated_at=FIXED_TS)
        # Section headers
        for header in (
            "# Intelligence Report",
            "## Run Metadata",
            "## Summary",
            "## Score Breakdown",
            "## Snapshots",
            "## Graph Writes",
            "## Evidence Handles",
            "## Warnings",
            "## Errors",
        ):
            self.assertIn(header, md, f"missing section {header!r}")
        # Schema version footer
        self.assertIn("schema v1", md)
        # Run metadata
        self.assertIn(f"`{result.run_id}`", md)
        self.assertIn(f"`{CONFIG_HASH}`", md)
        # Persisted run ⇒ at least one snapshot row in the table
        self.assertIn("| `macro` |", md)
        # Deterministic: same input + same generated_at ⇒ same string
        md_again = render_markdown_report(result, generated_at=FIXED_TS)
        self.assertEqual(md, md_again)


class EmptyResultTests(unittest.TestCase):
    """Scenario 3: Empty pipeline result (None input)."""

    def test_empty_input_emits_empty_envelope(self) -> None:
        payload = build_json_export(None, generated_at=FIXED_TS)
        self.assertIsNone(payload["result"])
        self.assertTrue(payload["artifact"]["empty"])
        # All-zero summary
        self.assertEqual(payload["summary"]["signal_count"], 0)
        self.assertEqual(payload["summary"]["score_count"], 0)
        self.assertEqual(payload["summary"]["graph_node_count"], 0)
        self.assertEqual(payload["summary"]["warning_count"], 0)
        # Markdown
        md = render_markdown_report(None, generated_at=FIXED_TS)
        self.assertIn("**Empty run**", md)
        self.assertIn("# Intelligence Report", md)
        # Summary helper is also safe with None
        stats = build_summary_statistics(None)
        self.assertEqual(stats.score_count, 0)
        self.assertEqual(stats.signal_count, 0)


class WarningPropagationTests(unittest.TestCase):
    """Scenario 4: Warning propagation through the export layer."""

    def test_warnings_round_trip_into_json_and_markdown(self) -> None:
        result = _run_macro_only_dry_run()
        # Sanity: the dry-run macro result has at least zero warnings
        # (in our seeded fixture it may carry the "no inputs" warning
        # if any dimension is empty). Inject a warning via metadata to
        # make the assertion deterministic.
        from dataclasses import replace
        result = replace(result, warnings=("test warning 1", "test warning 2"))
        payload = build_json_export(result, generated_at=FIXED_TS)
        self.assertIn("test warning 1", payload["warnings"])
        self.assertIn("test warning 2", payload["warnings"])
        md = render_markdown_report(result, generated_at=FIXED_TS)
        self.assertIn("test warning 1", md)
        self.assertIn("test warning 2", md)
        # Summary counter reflects the new warning list
        self.assertEqual(payload["summary"]["warning_count"], 2)


class DeterministicExportTests(unittest.TestCase):
    """Scenario 5: Deterministic repeated export."""

    def test_same_input_same_timestamp_byte_identical(self) -> None:
        result = _run_full_chain_persisted()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ReportConfig(output_dir=tmp, run_label="det")
            json_a, md_a = export_report(result, config=cfg, generated_at=FIXED_TS)
            json_b, md_b = export_report(result, config=cfg, generated_at=FIXED_TS)
            self.assertEqual(json_a.sha256, json_b.sha256)
            self.assertEqual(md_a.sha256, md_b.sha256)
            # File contents byte-identical
            with open(json_a.path, "rb") as fa, open(json_b.path, "rb") as fb:
                self.assertEqual(fa.read(), fb.read())
            with open(md_a.path, "rb") as fa, open(md_b.path, "rb") as fb:
                self.assertEqual(fa.read(), fb.read())
        # Pure builders also deterministic
        payload_a = build_json_export(result, generated_at=FIXED_TS)
        payload_b = build_json_export(result, generated_at=FIXED_TS)
        self.assertEqual(
            json.dumps(payload_a, sort_keys=True),
            json.dumps(payload_b, sort_keys=True),
        )


class TempDirectoryOutputTests(unittest.TestCase):
    """Scenario 6: Temp directory output."""

    def test_export_writes_to_tempdir(self) -> None:
        result = _run_macro_only_dry_run()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ReportConfig(output_dir=tmp)
            json_art, md_art = export_report(result, config=cfg, generated_at=FIXED_TS)
            # Both files exist on disk
            self.assertTrue(os.path.exists(json_art.path))
            self.assertTrue(os.path.exists(md_art.path))
            # Kinds match the brief's expected names
            self.assertEqual(json_art.kind, "intelligence_report")
            self.assertEqual(md_art.kind, "intelligence_report")
            # Sizes are non-zero
            self.assertGreater(json_art.size_bytes, 0)
            self.assertGreater(md_art.size_bytes, 0)
            # SHA-256 is 64 hex chars
            self.assertEqual(len(json_art.sha256), 64)
            self.assertEqual(len(md_art.sha256), 64)
            # JSON file is valid JSON
            with open(json_art.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data["artifact"]["run_id"], result.run_id)
            # Markdown is a string
            with open(md_art.path, "r", encoding="utf-8") as fh:
                self.assertIsInstance(fh.read(), str)
            # Files are in the temp dir, not in cwd or elsewhere
            self.assertTrue(json_art.path.startswith(tmp))
            self.assertTrue(md_art.path.startswith(tmp))


class InvalidDestinationTests(unittest.TestCase):
    """Scenario 7: Invalid destination handling."""

    def test_missing_directory_raises(self) -> None:
        result = _run_macro_only_dry_run()
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "does-not-exist")
            cfg = ReportConfig(output_dir=missing)
            with self.assertRaises(ReportExportError) as ctx:
                export_report(result, config=cfg, generated_at=FIXED_TS)
            self.assertIn("does not exist", str(ctx.exception))

    def test_non_directory_raises(self) -> None:
        result = _run_macro_only_dry_run()
        with tempfile.NamedTemporaryFile() as tmp:
            cfg = ReportConfig(output_dir=tmp.name)
            with self.assertRaises(ReportExportError) as ctx:
                export_report(result, config=cfg, generated_at=FIXED_TS)
            self.assertIn("not a directory", str(ctx.exception))

    def test_non_writable_directory_raises(self) -> None:
        result = _run_macro_only_dry_run()
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o500)  # r-x for owner; no write
            try:
                cfg = ReportConfig(output_dir=tmp)
                with self.assertRaises(ReportExportError) as ctx:
                    export_report(result, config=cfg, generated_at=FIXED_TS)
                # On some platforms (root) chmod 0o500 is still writable;
                # accept either the writability error or a successful
                # write that produces the expected files.
                if "not writable" not in str(ctx.exception):
                    # Root bypass — confirm files exist anyway
                    self.assertTrue(
                        os.path.exists(os.path.join(tmp, "intelligence_report.json"))
                    )
            finally:
                os.chmod(tmp, 0o700)

    def test_empty_output_dir_raises_at_config(self) -> None:
        with self.assertRaises(ReportExportError):
            ReportConfig(output_dir="")

    def test_negative_schema_version_raises_at_config(self) -> None:
        with self.assertRaises(ReportExportError):
            ReportConfig(output_dir=tempfile.gettempdir(), schema_version=0)


class Utf8EncodingTests(unittest.TestCase):
    """Scenario 8: UTF-8 encoding (Chinese characters)."""

    def test_chinese_characters_round_trip(self) -> None:
        result = _run_macro_only_dry_run()
        # Patch in Chinese text via dataclasses.replace so we do not
        # have to seed a Chinese entity into the scorer.
        from dataclasses import replace
        result = replace(
            result,
            warnings=("中文警告：缺少某些維度", "測試訊息：台灣市場"),
            metadata={**result.metadata, "notes": "投資情報測試 — 鼎鼎"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ReportConfig(output_dir=tmp)
            json_art, md_art = export_report(result, config=cfg, generated_at=FIXED_TS)
            # File is valid UTF-8
            with open(json_art.path, "r", encoding="utf-8") as fh:
                raw = fh.read()
            self.assertIn("中文警告", raw)
            self.assertIn("台灣市場", raw)
            self.assertIn("鼎鼎", raw)
            with open(md_art.path, "r", encoding="utf-8") as fh:
                md = fh.read()
            self.assertIn("中文警告", md)
            self.assertIn("台灣市場", md)
            # Bytes-on-disk are valid UTF-8 (decode round-trip)
            with open(json_art.path, "rb") as fh:
                bytes_data = fh.read()
            decoded = bytes_data.decode("utf-8")
            self.assertIn("中文", decoded)
            with open(md_art.path, "rb") as fh:
                md_bytes = fh.read()
            self.assertIn("中文".encode("utf-8"), md_bytes)
            # JSON parse is happy
            payload = json.loads(bytes_data.decode("utf-8"))
            self.assertIn("中文警告", payload["warnings"][0])


class RecoveryExportTests(unittest.TestCase):
    """Scenario 9: Recovery metadata inclusion (RecoveryResult)."""

    def test_recovery_export_has_recovery_metadata(self) -> None:
        recovery = _run_recovery_terminal()
        payload = build_recovery_json_export(recovery, generated_at=FIXED_TS)
        # Envelope
        self.assertEqual(payload["schema_version"], 1)
        self.assertTrue(payload["recovery"])
        # Artifact metadata captures the run
        self.assertEqual(payload["artifact"]["run_id"], recovery.run_id)
        self.assertEqual(payload["artifact"]["config_hash"], CONFIG_HASH)
        self.assertEqual(payload["artifact"]["date_bucket"], DATE)
        # Summary has the recovery counters
        self.assertIn("attempt_count", payload["summary"])
        self.assertIn("succeeded_attempt_count", payload["summary"])
        self.assertIn("failed_attempt_count", payload["summary"])
        # Failed attempts: at least one (graph write failed)
        self.assertGreaterEqual(payload["summary"]["failed_attempt_count"], 1)
        # Recovery payload includes the state + attempts
        self.assertIsNotNone(payload["recovery"])
        self.assertEqual(payload["recovery"]["run_id"], recovery.run_id)
        self.assertIn("attempts", payload["recovery"])
        self.assertIn("state", payload["recovery"])
        self.assertIn("completed_through", payload["recovery"])
        # Markdown variant
        md = render_recovery_markdown_report(recovery, generated_at=FIXED_TS)
        self.assertIn("# Recovery Report", md)
        self.assertIn("## Run Metadata", md)
        self.assertIn("## Attempts", md)
        self.assertIn("## Warnings", md)
        # Recovery kind names in file artifacts
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ReportConfig(output_dir=tmp, recovery=True)
            json_art, md_art = export_report(
                recovery, config=cfg, generated_at=FIXED_TS,
            )
            self.assertEqual(json_art.kind, "recovery_report")
            self.assertEqual(md_art.kind, "recovery_report")


class EvidencePayloadInclusionTests(unittest.TestCase):
    """Scenario 10: Evidence / query summary inclusion."""

    def test_evidence_handles_included_by_default(self) -> None:
        result = _run_full_chain_persisted()
        # The Run 1 orchestrator populates evidence_handles with
        # evidence_summary dicts (None for sample fixtures that have
        # no upstream chain, but the key is present).
        payload = build_json_export(result, generated_at=FIXED_TS)
        # The handles survived into the result payload
        self.assertIn("evidence_handles", payload["result"])
        self.assertGreater(len(payload["result"]["evidence_handles"]), 0)
        # And there is a Markdown section for them
        md = render_markdown_report(result, generated_at=FIXED_TS)
        self.assertIn("## Evidence Handles", md)
        self.assertIn("| scorer_type |", md)
        # graph_node_count/edge_count in the summary reflect the nodes
        # *written during this run* (sum across graph_writes). The
        # ``node_count`` / ``edge_count`` on the result envelope are
        # the totals in the underlying graph store, which is a
        # superset (carries cross-layer and source nodes the
        # summary does not double-count).
        self.assertGreaterEqual(
            payload["result"]["node_count"],
            payload["summary"]["graph_node_count"],
        )
        self.assertGreaterEqual(
            payload["result"]["edge_count"],
            payload["summary"]["graph_edge_count"],
        )

    def test_evidence_handles_can_be_omitted(self) -> None:
        result = _run_full_chain_persisted()
        cfg = ReportConfig(
            output_dir=tempfile.gettempdir(),
            include_evidence_payload=False,
        )
        payload = build_json_export(result, config=cfg, generated_at=FIXED_TS)
        # evidence_summary keys were stripped from each handle
        for handle in payload["result"]["evidence_handles"]:
            self.assertNotIn("evidence_summary", handle)
        # And the metadata.evidence_trace key (if any) was also dropped
        if payload["result"].get("metadata"):
            self.assertNotIn("evidence_trace", payload["result"]["metadata"])

    def test_evidence_summary_with_real_chain_is_embedded(self) -> None:
        """A handle with a non-None evidence_summary must round-trip."""
        from dataclasses import replace
        from phase3.pipeline.intelligence_pipeline import EvidenceQueryHandle

        result = _run_full_chain_persisted()
        # Inject a fake handle with a populated evidence_summary
        fake_summary = {
            "n_visited": 5,
            "n_traversed_edges": 4,
            "n_leaves": 1,
            "n_source_nodes": 1,
            "n_signal_nodes": 3,
            "truncated": False,
        }
        fake_handle = EvidenceQueryHandle(
            scorer_type="macro",
            entity_id="global",
            date_bucket=DATE,
            score_node_id=result.graph_writes[0].score_node_id,
            upstream=None,
            downstream=None,
            evidence_summary=fake_summary,
        )
        new_handles = tuple(result.evidence_handles) + (fake_handle,)
        result = replace(result, evidence_handles=new_handles)

        payload = build_json_export(result, generated_at=FIXED_TS)
        last_handle = payload["result"]["evidence_handles"][-1]
        self.assertIn("evidence_summary", last_handle)
        self.assertEqual(last_handle["evidence_summary"]["n_visited"], 5)


class SummaryStatisticsTests(unittest.TestCase):
    """Cross-cutting: build_summary_statistics contract."""

    def test_macro_only_counts(self) -> None:
        result = _run_macro_only_dry_run()
        stats = build_summary_statistics(result)
        self.assertEqual(stats.macro_score_count, 1)
        self.assertEqual(stats.industry_score_count, 0)
        self.assertEqual(stats.company_score_count, 0)
        self.assertEqual(stats.score_count, 1)
        self.assertEqual(stats.snapshot_count, 0)  # dry-run
        self.assertGreaterEqual(stats.graph_node_count, 0)
        self.assertEqual(stats.error_count, 0)

    def test_full_chain_counts(self) -> None:
        result = _run_full_chain_persisted()
        stats = build_summary_statistics(result)
        self.assertEqual(stats.macro_score_count, 1)
        self.assertEqual(stats.industry_score_count, 1)
        self.assertEqual(stats.company_score_count, 1)
        self.assertEqual(stats.score_count, 3)
        self.assertEqual(stats.snapshot_count, 3)
        self.assertGreaterEqual(stats.graph_node_count, 3)
        self.assertGreaterEqual(stats.graph_edge_count, 1)
        self.assertEqual(stats.evidence_handle_count, 3)

    def test_to_dict_round_trips(self) -> None:
        stats = build_summary_statistics(None)
        d = stats.to_dict()
        for k in (
            "signal_count", "score_count", "snapshot_count",
            "graph_node_count", "graph_edge_count", "evidence_handle_count",
            "warning_count", "error_count",
            "company_score_count", "industry_score_count", "macro_score_count",
        ):
            self.assertIn(k, d)
            self.assertEqual(d[k], 0)


class PackageReExportTests(unittest.TestCase):
    """The reporting API must be reachable from the package root."""

    def test_imports_from_phase3_pipeline(self) -> None:
        from phase3.pipeline import (  # noqa: F401
            ReportArtifact,
            ReportConfig,
            ReportExportError,
            SummaryStatistics,
            build_json_export,
            build_recovery_json_export,
            build_summary_statistics,
            export_report,
            render_markdown_report,
            render_recovery_markdown_report,
        )


if __name__ == "__main__":
    unittest.main()
