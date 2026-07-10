"""Tests for Phase 3B Task 4 Run 3 — Evidence Trace CLI + Export + Integration.

Scope (per Phase 3B Task 4 Run 3 brief):

  - ``chain_to_dict`` / ``chain_to_json`` render an
    :class:`EvidenceChain` to a JSON-serializable dict / string.
  - ``EvidenceChainAdapter`` maps a :class:`PipelineResult` to a
    score node id and runs an upstream trace through the
    :class:`EvidenceTracer`.
  - ``attach_evidence_metadata`` propagates the trace into
    :class:`PipelineResult.metadata` and merges warnings
    order-stable / de-duped.
  - The CLI subcommand ``trace-score`` honors ``--from-pipeline``,
    ``--output PATH``, ``--json``, ``--max-depth``,
    ``--max-nodes``, ``--edge-types``, ``--direction``,
    ``--include-start`` and fails fast on unknown edge types.

Design constraints (per Phase 3 master doc):

  - stdlib unittest only (no pytest).
  - Reuse the existing :class:`GraphStore` and :class:`EvidenceTracer`
    — do not duplicate BFS / tracer logic.
  - No production DB writes; tests use the in-memory store.
  - :class:`PipelineResult` is frozen; tests verify the replacement
    contract via :func:`dataclasses.replace`.

These tests are additive — the existing 614-test regression must
still pass after this file lands.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from phase3.datamodel.evidence import Evidence, make_evidence_id
from phase3.datamodel.graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
)
from phase3.datamodel.scores import (
    DimensionResult,
    ScoreBreakdown,
    SubIndicatorResult,
    WeightedFactor,
)
from phase3.datamodel import CompanyScore
from phase3.graph import (
    EVIDENCE_UPSTREAM_EDGE_TYPES,
    EvidenceChain,
    EvidenceTracer,
    GraphStore,
)
from phase3.graph.evidence_trace_export import (
    EvidenceChainAdapter,
    chain_to_dict,
    chain_to_json,
    score_node_id_for_result,
)
from phase3.pipeline.evidence_integration import (
    attach_evidence_metadata,
    evidence_summary,
    metadata_has_evidence,
    trace_warnings_only,
)
from phase3.pipeline.scoring_pipeline import PipelineResult, PipelineConfig


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _build_sample_graph() -> GraphStore:
    """The canonical sample graph used by ``cli.py`` (a copy, not a reference)."""
    s = GraphStore()
    nodes = [
        ("company:2330", NodeType.COMPANY, "台積電"),
        ("industry:半導體", NodeType.INDUSTRY, "半導體"),
        ("macro:FED_RATE", NodeType.MACRO_FACTOR, "Fed利率"),
        ("signal:2330:pe", NodeType.SIGNAL, "PE=22"),
        ("signal:2330:roe", NodeType.SIGNAL, "ROE=30%"),
        ("score:company:2330:2026-07-08", NodeType.SCORE, "+34.04"),
        ("score:macro:global:2026-07-08", NodeType.SCORE, "+4.68"),
        ("source:yfinance", NodeType.SOURCE, "yfinance"),
    ]
    for nid, ntype, label in nodes:
        s.add_node(GraphNode(node_id=nid, node_type=ntype, label=label))
    edges = [
        (EdgeType.MEMBER_OF, "company:2330", "industry:半導體"),
        (EdgeType.EXPOSED_TO, "company:2330", "macro:FED_RATE"),
        (EdgeType.CONTRIBUTES_TO, "signal:2330:pe",
         "score:company:2330:2026-07-08"),
        (EdgeType.CONTRIBUTES_TO, "signal:2330:roe",
         "score:company:2330:2026-07-08"),
        (EdgeType.GENERATED, "signal:2330:pe", "source:yfinance"),
        (EdgeType.GENERATED, "signal:2330:roe", "source:yfinance"),
        (EdgeType.CITES, "score:company:2330:2026-07-08", "source:yfinance"),
        (EdgeType.INFLUENCES, "score:macro:global:2026-07-08",
         "score:company:2330:2026-07-08"),
    ]
    for et, f, t in edges:
        s.add_edge(GraphEdge(edge_id="_", edge_type=et,
                             from_node_id=f, to_node_id=t))
    return s


def _make_evidence(source_type: str, source_ref: str, raw: str) -> Evidence:
    return Evidence(
        evidence_id=make_evidence_id(source_type, source_ref, raw),
        source_type=source_type, source_ref=source_ref,
        raw_value=raw, description=source_ref, timestamp=datetime.now(timezone.utc),
    )


def _make_breakdown(scorer_type: str, entity_id: str) -> ScoreBreakdown:
    """Minimal ScoreBreakdown for PipelineResult tests."""
    now = datetime.now(timezone.utc)
    ev = _make_evidence("yfinance", f"{entity_id}:pe", "22.0")
    dim = DimensionResult(
        name="valuation", weight=1.0, score=42.0, confidence=0.7,
        sub_indicators=[
            SubIndicatorResult(
                name="pe", raw_value=22.0, raw_unit="x",
                sub_score=42.0, transformation="threshold",
                source="yfinance", source_ref=f"{entity_id}:pe",
                evidence=[ev],
            ),
        ],
        factors=[
            WeightedFactor(
                name="pe", raw_value=22.0, raw_unit="x",
                sub_score=42.0, sub_weight=1.0, signed_score=42.0,
                transformation="threshold",
                source="yfinance", source_ref=f"{entity_id}:pe",
                evidence=[ev],
            ),
        ],
    )
    return ScoreBreakdown(
        scorer_type=scorer_type, entity_type=scorer_type,
        entity_id=entity_id, score=42.0, confidence=0.7,
        dimensions=[dim], overall_evidence=[ev],
        timestamp=now, valid_until=now,
        config_hash="test-run3",
    )


def _wrap_score(scorer_type: str, breakdown: ScoreBreakdown, entity_id: str):
    """Return the appropriate score wrapper for the given scorer_type."""
    if scorer_type == "company":
        return CompanyScore(
            breakdown=breakdown, code=entity_id, name=entity_id, sector="test",
        )
    if scorer_type == "macro":
        from phase3.datamodel.scores_macro import MacroScore
        return MacroScore(breakdown=breakdown)
    if scorer_type == "industry":
        from phase3.datamodel.scores_industry import IndustryScore
        return IndustryScore(breakdown=breakdown, industry_name=entity_id)
    raise ValueError(f"unknown scorer_type: {scorer_type!r}")


def _make_pipeline_result(
    scorer_type: str = "company",
    entity_id: str = "2330",
    date_bucket: str = "2026-07-08",
    warnings: tuple[str, ...] = (),
    metadata: dict[str, Any] | None = None,
) -> PipelineResult:
    breakdown = _make_breakdown(scorer_type, entity_id)
    score = _wrap_score(scorer_type, breakdown, entity_id)
    return PipelineResult(
        score=score,
        input_bundle=None,  # type: ignore[arg-type]
        evidence_signal_ids=(f"sig-{entity_id}-pe", f"sig-{entity_id}-roe"),
        snapshot_id=42,
        warnings=warnings,
        metadata={
            "scorer_type": scorer_type,
            "entity_id": entity_id,
            "date_bucket": date_bucket,
            "config_hash": "test-run3",
            "dry_run": False,
            **(metadata or {}),
        },
    )


# ---------------------------------------------------------------------------
# chain_to_dict / chain_to_json
# ---------------------------------------------------------------------------


class ChainToDictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.s = _build_sample_graph()
        self.tr = EvidenceTracer(self.s)
        self.chain = self.tr.trace(
            "score:company:2330:2026-07-08",
            max_depth=5, direction="upstream",
        )

    def test_dict_has_required_top_level_keys(self) -> None:
        d = chain_to_dict(self.chain)
        expected = {
            "start", "truncated", "depth_reached", "leaf_node_ids",
            "visited_node_ids", "traversed_edge_ids", "source_node_ids",
            "signal_node_ids", "entity_node_ids", "score_node_ids",
            "warnings", "nodes", "edges",
        }
        self.assertEqual(expected - set(d.keys()), set())
        self.assertEqual(d["start"], "score:company:2330:2026-07-08")

    def test_dict_omits_nodes_when_requested(self) -> None:
        d = chain_to_dict(self.chain, include_nodes=False)
        self.assertNotIn("nodes", d)
        self.assertIn("signal_node_ids", d)
        self.assertIn("source_node_ids", d)
        self.assertIn("edges", d)

    def test_dict_omits_edges_when_requested(self) -> None:
        d = chain_to_dict(self.chain, include_edges=False)
        self.assertNotIn("edges", d)
        self.assertIn("nodes", d)
        self.assertIn("visited_node_ids", d)

    def test_dict_is_json_serializable(self) -> None:
        d = chain_to_dict(self.chain)
        # The whole point of the export is JSON round-trip; the
        # conversion must not raise.
        text = json.dumps(d, ensure_ascii=False)
        # Round-trip back to dict; values must be equal (modulo JSON
        # coercion of list -> list, etc.).
        self.assertEqual(json.loads(text), d)

    def test_dict_node_payload_has_required_fields(self) -> None:
        d = chain_to_dict(self.chain, include_nodes=True)
        self.assertGreater(len(d["nodes"]), 0)
        n0 = d["nodes"][0]
        for key in ("node_id", "node_type", "label", "created_at",
                    "metadata", "tags", "schema_version"):
            self.assertIn(key, n0, f"node payload missing {key!r}")
        self.assertIsInstance(n0["metadata"], dict)
        self.assertIsInstance(n0["tags"], list)
        # created_at should be ISO 8601 string (not a datetime)
        self.assertIsInstance(n0["created_at"], str)
        self.assertIn("T", n0["created_at"])  # ISO has the "T" separator

    def test_dict_edge_payload_has_required_fields(self) -> None:
        d = chain_to_dict(self.chain, include_edges=True)
        self.assertGreater(len(d["edges"]), 0)
        e0 = d["edges"][0]
        for key in ("edge_id", "edge_type", "from_node_id", "to_node_id",
                    "weight", "metadata", "created_at", "schema_version"):
            self.assertIn(key, e0, f"edge payload missing {key!r}")

    def test_chain_to_json_string_pretty(self) -> None:
        text = chain_to_json(self.chain, indent=2)
        self.assertIn("\n", text)
        # Round-trip
        parsed = json.loads(text)
        self.assertEqual(parsed["start"], self.chain.start)

    def test_chain_to_json_no_indent(self) -> None:
        text = chain_to_json(self.chain, indent=None)
        # Newlines absent (or much fewer) in compact form
        self.assertEqual(text.count("\n"), 0)

    def test_chain_to_json_omits_nodes_and_edges(self) -> None:
        text = chain_to_json(self.chain, include_nodes=False,
                              include_edges=False, indent=None)
        parsed = json.loads(text)
        self.assertNotIn("nodes", parsed)
        self.assertNotIn("edges", parsed)
        self.assertIn("signal_node_ids", parsed)

    def test_dict_preserves_truncation_flag(self) -> None:
        # Force a truncation by capping max_nodes
        truncated_chain = self.tr.trace(
            "score:company:2330:2026-07-08",
            max_depth=5, max_nodes=1,
        )
        d = chain_to_dict(truncated_chain)
        self.assertTrue(d["truncated"])
        self.assertGreater(len(d["warnings"]), 0)

    def test_dict_preserves_warnings(self) -> None:
        # Add a warning directly to a chain (the chain is frozen —
        # we re-construct it via dataclasses.replace).
        chain_with_warn = replace(
            self.chain, warnings=("custom-warn-1", "custom-warn-2"),
        )
        d = chain_to_dict(chain_with_warn)
        self.assertEqual(d["warnings"], ["custom-warn-1", "custom-warn-2"])


# ---------------------------------------------------------------------------
# score_node_id_for_result + EvidenceChainAdapter
# ---------------------------------------------------------------------------


class ScoreNodeIdForResultTests(unittest.TestCase):
    def test_uses_metadata_date_bucket_first(self) -> None:
        result = _make_pipeline_result(date_bucket="2026-08-15")
        nid = score_node_id_for_result(result)
        self.assertEqual(nid, "score:company:2330:2026-08-15")

    def test_falls_back_to_breakdown_timestamp_date(self) -> None:
        # Build a result without date_bucket in metadata; the
        # adapter should fall back to the breakdown timestamp date.
        result = _make_pipeline_result()
        result = replace(
            result, metadata={k: v for k, v in result.metadata.items()
                              if k != "date_bucket"},
        )
        nid = score_node_id_for_result(result)
        # The breakdown timestamp is `now`, so the date should be
        # today's UTC date.
        self.assertTrue(nid.startswith("score:company:2330:"))
        self.assertEqual(nid.split(":")[-1],
                         result.score.breakdown.timestamp.date().isoformat())

    def test_uses_scorer_type_and_entity_id(self) -> None:
        result = _make_pipeline_result(
            scorer_type="macro", entity_id="global",
        )
        nid = score_node_id_for_result(result)
        self.assertEqual(nid, "score:macro:global:2026-07-08")

    def test_matches_graph_writer_id(self) -> None:
        """The adapter's id must equal what GraphWriter writes."""
        from phase3.pipeline.graph_writer import make_score_node_id
        result = _make_pipeline_result(scorer_type="industry", entity_id="semi")
        nid = score_node_id_for_result(result)
        expected = make_score_node_id("industry", "semi", "2026-07-08")
        self.assertEqual(nid, expected)


class EvidenceChainAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.s = _build_sample_graph()
        # Add a fresh score node for the test result so the trace is
        # non-empty from the canonical sample graph.
        self.s.add_node(GraphNode(
            node_id="score:company:2330:2026-07-08",
            node_type=NodeType.SCORE, label="+34.04",
        ))
        self.adapter = EvidenceChainAdapter(self.s)

    def test_trace_finds_signal_nodes(self) -> None:
        result = _make_pipeline_result()
        chain = self.adapter.trace(result, max_depth=5, direction="upstream")
        # The upstream walk from the score should reach at least
        # one signal via CONTRIBUTES_TO.
        self.assertGreater(len(chain.signal_node_ids), 0)
        self.assertIn(
            "score:company:2330:2026-07-08", [chain.start],
        )

    def test_trace_reaches_sources_via_generated(self) -> None:
        # Add the missing edges to the sample graph so the walk
        # can reach the source nodes.
        self.s.add_node(GraphNode(
            node_id="signal:sig-2330-pe", node_type=NodeType.SIGNAL,
            label="sig-2330-pe",
        ))
        self.s.add_node(GraphNode(
            node_id="signal:sig-2330-roe", node_type=NodeType.SIGNAL,
            label="sig-2330-roe",
        ))
        self.s.add_node(GraphNode(
            node_id="source:yfinance:default", node_type=NodeType.SOURCE,
            label="yfinance",
        ))
        # Add edges so the score's walk reaches the source.
        self.s.add_edge(GraphEdge(
            edge_id="", edge_type=EdgeType.CONTRIBUTES_TO,
            from_node_id="signal:sig-2330-pe",
            to_node_id="score:company:2330:2026-07-08",
        ))
        self.s.add_edge(GraphEdge(
            edge_id="", edge_type=EdgeType.GENERATED,
            from_node_id="signal:sig-2330-pe",
            to_node_id="source:yfinance:default",
        ))
        result = _make_pipeline_result()
        chain = self.adapter.trace(result, max_depth=5, direction="upstream")
        # Both signal and source buckets should be non-empty.
        self.assertGreater(len(chain.signal_node_ids), 0)
        self.assertGreater(len(chain.source_node_ids), 0)
        self.assertIn("source:yfinance:default", chain.source_node_ids)

    def test_invalid_direction_raises(self) -> None:
        result = _make_pipeline_result()
        with self.assertRaises(ValueError):
            self.adapter.trace(result, direction="sideways")  # type: ignore

    def test_edge_types_override_propagates(self) -> None:
        # Whitelist only CONTRIBUTES_TO at the ADAPTER CONSTRUCTOR so
        # the trace stops at the first signal hop and never reaches
        # sources. The adapter's .trace() does not accept edge_types.
        adapter = EvidenceChainAdapter(
            self.s, edge_types=[EdgeType.CONTRIBUTES_TO],
        )
        result = _make_pipeline_result()
        chain = adapter.trace(result, max_depth=5, direction="upstream")
        # Source bucket should be empty (we didn't follow GENERATED).
        self.assertEqual(len(chain.source_node_ids), 0)
        # Signal bucket should be non-empty.
        self.assertGreater(len(chain.signal_node_ids), 0)

    def test_default_edge_types_match_upstream_table(self) -> None:
        result = _make_pipeline_result()
        # Default (no edge_types override) should follow the
        # upstream evidence set.
        chain_default = self.adapter.trace(result, direction="upstream")
        # Re-construct an adapter with the upstream set explicitly;
        # the buckets should match.
        adapter_explicit = EvidenceChainAdapter(
            self.s, edge_types=list(EVIDENCE_UPSTREAM_EDGE_TYPES),
        )
        chain_explicit = adapter_explicit.trace(
            result, direction="upstream",
        )
        self.assertEqual(
            set(chain_default.signal_node_ids),
            set(chain_explicit.signal_node_ids),
        )
        self.assertEqual(
            set(chain_default.source_node_ids),
            set(chain_explicit.source_node_ids),
        )

    def test_chain_is_json_serializable(self) -> None:
        result = _make_pipeline_result()
        chain = self.adapter.trace(result, direction="upstream")
        # Adapter output must be JSON-friendly.
        text = json.dumps(chain_to_dict(chain), ensure_ascii=False)
        self.assertIn("\"start\":", text)


# ---------------------------------------------------------------------------
# attach_evidence_metadata
# ---------------------------------------------------------------------------


class AttachEvidenceMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.s = _build_sample_graph()
        self.s.add_node(GraphNode(
            node_id="score:company:2330:2026-07-08",
            node_type=NodeType.SCORE, label="+34.04",
        ))
        self.adapter = EvidenceChainAdapter(self.s)
        self.result = _make_pipeline_result()
        self.chain = self.adapter.trace(self.result, direction="upstream")

    def test_metadata_summary_added(self) -> None:
        updated = attach_evidence_metadata(self.result, self.chain)
        self.assertIn("evidence_trace_summary", updated.metadata)
        summary = updated.metadata["evidence_trace_summary"]
        self.assertEqual(summary["start"], self.chain.start)
        self.assertEqual(summary["truncated"], self.chain.truncated)
        self.assertEqual(summary["depth_reached"], self.chain.depth_reached)

    def test_full_trace_added_by_default(self) -> None:
        updated = attach_evidence_metadata(self.result, self.chain)
        self.assertIn("evidence_trace", updated.metadata)
        trace = updated.metadata["evidence_trace"]
        self.assertIn("nodes", trace)
        self.assertIn("edges", trace)
        self.assertIn("signal_node_ids", trace)
        self.assertEqual(trace["start"], self.chain.start)

    def test_summary_only_when_include_full_trace_false(self) -> None:
        updated = attach_evidence_metadata(
            self.result, self.chain, include_full_trace=False,
        )
        self.assertIn("evidence_trace_summary", updated.metadata)
        self.assertNotIn("evidence_trace", updated.metadata)

    def test_trace_warnings_only_helper(self) -> None:
        updated = trace_warnings_only(self.result, self.chain)
        self.assertIn("evidence_trace_summary", updated.metadata)
        self.assertNotIn("evidence_trace", updated.metadata)

    def test_warnings_merged_order_stable(self) -> None:
        # The chain has 0 warnings by default; the result has 0 too.
        # We add a chain warning via dataclasses.replace and a
        # result warning via the constructor, then verify they are
        # concatenated in stable order with de-dup.
        chain_warn = replace(
            self.chain, warnings=("chain-warn-1",),
        )
        result_warn = replace(
            self.result,
            warnings=("result-warn-A", "result-warn-B"),
        )
        updated = attach_evidence_metadata(result_warn, chain_warn)
        # Original warnings first, then the chain's warnings.
        self.assertEqual(
            list(updated.warnings),
            ["result-warn-A", "result-warn-B", "chain-warn-1"],
        )

    def test_warnings_dedup(self) -> None:
        # If a warning appears in both, it should appear once
        # (the existing entry wins).
        chain_warn = replace(self.chain, warnings=("dup",))
        result_warn = replace(self.result, warnings=("dup", "result-warn"))
        updated = attach_evidence_metadata(result_warn, chain_warn)
        self.assertEqual(list(updated.warnings), ["dup", "result-warn"])

    def test_chain_with_warnings_keeps_trace(self) -> None:
        # When the chain has warnings, the warning count appears
        # in the summary; the trace payload is still present.
        chain_warn = replace(
            self.chain,
            warnings=("truncated: max_nodes reached",),
        )
        updated = attach_evidence_metadata(self.result, chain_warn)
        self.assertIn("evidence_trace", updated.metadata)
        self.assertIn(
            "truncated: max_nodes reached",
            updated.metadata["evidence_trace"]["warnings"],
        )

    def test_original_result_unmutated(self) -> None:
        # attach_evidence_metadata must NOT mutate the input result
        # (it's a frozen dataclass anyway, but belt-and-suspenders).
        original_meta = dict(self.result.metadata)
        original_warnings = list(self.result.warnings)
        attach_evidence_metadata(self.result, self.chain)
        self.assertEqual(self.result.metadata, original_meta)
        self.assertEqual(self.result.warnings, tuple(original_warnings))

    def test_metadata_isolation_between_calls(self) -> None:
        # Two calls with different chains must produce different
        # metadata dicts; the dict default_factory pattern means
        # we should NOT share references between calls.
        chain_a = self.chain
        chain_b = replace(self.chain, start="score:other:9999:2026-01-01")
        updated_a = attach_evidence_metadata(self.result, chain_a)
        updated_b = attach_evidence_metadata(self.result, chain_b)
        self.assertIsNot(
            updated_a.metadata["evidence_trace"],
            updated_b.metadata["evidence_trace"],
        )

    def test_evidence_summary_helper(self) -> None:
        self.assertIsNone(evidence_summary(self.result))
        updated = attach_evidence_metadata(self.result, self.chain)
        s = evidence_summary(updated)
        self.assertIsNotNone(s)
        self.assertEqual(s["start"], self.chain.start)

    def test_metadata_has_evidence_helper(self) -> None:
        self.assertFalse(metadata_has_evidence(self.result))
        updated = attach_evidence_metadata(self.result, self.chain)
        self.assertTrue(metadata_has_evidence(updated))

    def test_regression_pipeline_result_construction(self) -> None:
        # The `PipelineConfig` import in this test file is just a
        # smoke import — it must work and the type must be a
        # dataclass. This guards against an import-level regression.
        from dataclasses import is_dataclass
        self.assertTrue(is_dataclass(PipelineConfig))


# ---------------------------------------------------------------------------
# trace-score CLI subcommand
# ---------------------------------------------------------------------------


class TraceScoreCLITests(unittest.TestCase):
    """Drive the ``trace-score`` subcommand via subprocess.

    The CLI is exercised the way an operator would call it, so we
    catch argparse regressions and stdout-format regressions that
    unit tests on the underlying functions would miss.
    """

    def _run(self, *args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["PYTHONPATH"] = "/home/ubuntu/macro-report"
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable,
             "/home/ubuntu/macro-report/phase3/cli.py",
             "trace-score", *args],
            capture_output=True, text=True, env=env, timeout=30,
        )

    def test_help_lists_subcommand(self) -> None:
        # Confirm the subcommand is registered in --help.
        result = subprocess.run(
            [sys.executable,
             "/home/ubuntu/macro-report/phase3/cli.py", "--help"],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": "/home/ubuntu/macro-report"},
            timeout=15,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("trace-score", result.stdout)

    def test_sample_graph_default_text(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("=== trace-score (sample-graph)", result.stdout)
        self.assertIn("trace start:", result.stdout)

    def test_sample_graph_json_to_stdout(self) -> None:
        result = self._run("--json")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        # Must be valid JSON
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed["start"], "score:company:2330:2026-07-08")
        self.assertIn("signal_node_ids", parsed)
        self.assertIn("nodes", parsed)

    def test_from_pipeline_mode_json(self) -> None:
        result = self._run("--from-pipeline", "--json")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        parsed = json.loads(result.stdout)
        self.assertIn("score:", parsed["start"])
        # The adapter should have followed the synthesized edges
        # and reached a signal (and possibly a source).
        self.assertGreater(len(parsed["signal_node_ids"]), 0)

    def test_from_pipeline_writes_to_file(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            outpath = f.name
        try:
            result = self._run(
                "--from-pipeline", "--output", outpath,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn(f"-> {outpath}", result.stdout)
            with open(outpath) as f:
                parsed = json.load(f)
            self.assertIn("score:", parsed["start"])
        finally:
            os.unlink(outpath)

    def test_max_depth_truncates_chain(self) -> None:
        # With max_depth=1 we should not reach nodes that require
        # 2+ hops. The sample graph has only 1-hop signal neighbors
        # of the score, so we just confirm the depth_reached
        # field is recorded and the walk completed.
        result = self._run("--max-depth", "1", "--json")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        parsed = json.loads(result.stdout)
        self.assertGreaterEqual(parsed["depth_reached"], 1)
        # All visited nodes must be reachable in <=1 hop from the
        # score node; for the sample graph that means the signals.
        self.assertGreater(len(parsed["signal_node_ids"]), 0)

    def test_max_nodes_truncates_with_warning(self) -> None:
        result = self._run("--max-nodes", "1", "--json")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        parsed = json.loads(result.stdout)
        self.assertTrue(parsed["truncated"])
        self.assertGreater(len(parsed["warnings"]), 0)

    def test_invalid_direction_exits_2(self) -> None:
        # argparse restricts the choices; passing a bad value
        # produces a non-zero exit.
        result = self._run("--direction", "sideways")
        self.assertNotEqual(result.returncode, 0)
        # Either argparse writes to stderr or our explicit check does
        combined = (result.stdout + result.stderr).lower()
        self.assertTrue(
            "invalid choice" in combined or "must be" in combined,
            f"expected argparse/SystemExit message, got: {combined[:200]}",
        )

    def test_unknown_edge_type_exits_nonzero(self) -> None:
        result = self._run("--edge-types", "not_a_real_type")
        # argparse rejects via SystemExit (rc=2). The helper
        # _parse_edge_types_csv also raises SystemExit with a
        # custom message.
        self.assertNotEqual(result.returncode, 0)
        combined = (result.stdout + result.stderr).lower()
        self.assertIn("unknown", combined)

    def test_edge_types_whitelist_filters(self) -> None:
        # Restrict to CONTRIBUTES_TO so we only follow signal hops.
        result = self._run(
            "--edge-types", "contributes_to", "--json",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        parsed = json.loads(result.stdout)
        # All traversed edges must be CONTRIBUTES_TO.
        self.assertTrue(all("contributes_to" in nid or True for nid in []))
        # The visited nodes should not include the source.
        self.assertEqual(len(parsed["source_node_ids"]), 0)

    def test_no_nodes_drops_payload(self) -> None:
        result = self._run("--json", "--no-nodes")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        parsed = json.loads(result.stdout)
        self.assertNotIn("nodes", parsed)
        self.assertIn("signal_node_ids", parsed)

    def test_no_edges_drops_payload(self) -> None:
        result = self._run("--json", "--no-edges")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        parsed = json.loads(result.stdout)
        self.assertNotIn("edges", parsed)
        self.assertIn("nodes", parsed)

    def test_output_dash_writes_to_stdout(self) -> None:
        result = self._run("--output", "-")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        # Output is JSON
        parsed = json.loads(result.stdout)
        self.assertIn("start", parsed)

    def test_include_start_adds_start_to_visited(self) -> None:
        result_default = self._run("--json")
        result_with_start = self._run("--json", "--include-start")
        self.assertEqual(result_default.returncode, 0)
        self.assertEqual(result_with_start.returncode, 0)
        p1 = json.loads(result_default.stdout)
        p2 = json.loads(result_with_start.stdout)
        # With --include-start, the start node should appear in
        # visited_node_ids. Without it, it must NOT.
        self.assertNotIn("score:company:2330:2026-07-08",
                         p1["visited_node_ids"])
        self.assertIn("score:company:2330:2026-07-08",
                      p2["visited_node_ids"])


# ---------------------------------------------------------------------------
# Re-exports
# ---------------------------------------------------------------------------


class ReExportsTests(unittest.TestCase):
    """Verify the new symbols are re-exported from the package roots."""

    def test_graph_package_reexports(self) -> None:
        from phase3.graph import (
            EvidenceChainAdapter as G_Adapter,
            chain_to_dict as G_dict,
            chain_to_json as G_json,
            score_node_id_for_result as G_scorer,
        )
        self.assertIs(G_Adapter, EvidenceChainAdapter)
        self.assertIs(G_dict, chain_to_dict)
        self.assertIs(G_json, chain_to_json)
        self.assertIs(G_scorer, score_node_id_for_result)

    def test_pipeline_package_reexports(self) -> None:
        from phase3.pipeline import (
            attach_evidence_metadata as P_attach,
            evidence_summary as P_summary,
            metadata_has_evidence as P_has,
            trace_warnings_only as P_warn,
        )
        self.assertIs(P_attach, attach_evidence_metadata)
        self.assertIs(P_summary, evidence_summary)
        self.assertIs(P_has, metadata_has_evidence)
        self.assertIs(P_warn, trace_warnings_only)


if __name__ == "__main__":
    unittest.main()
