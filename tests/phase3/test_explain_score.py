"""Tests for Phase 4 Task 3B Run 1 — Explain-Score / Decision Trace.

Scope (per Phase 4 Task 3B Run 1 brief):

  - ``explain_score`` composes the three canonical graph queries
    (lineage, blast-radius, cross-layer impact) plus the score
    node's metadata, returning an :class:`ExplainedScore`.
  - The CLI subcommand ``explain-score`` honors ``--node``,
    ``--max-depth``, ``--max-nodes``, ``--db-path``, ``--output``,
    ``--json`` and ``--from-pipeline`` and refuses to open
    ``macro_history.db``.
  - The result is deterministic across repeated calls and
    round-trips through :func:`json.dumps` / :func:`json.loads`.
  - The Markdown and JSON views are stable / structural-only.

Design constraints (per Phase 3 master doc):
  - stdlib ``unittest`` only (no pytest).
  - Reuse the existing :class:`GraphStore` and the three
    canonical query modules — do not duplicate BFS / tracer
    logic.
  - No production DB writes; tests use the in-memory store.
  - The SQLite/InMemory parity check uses an in-memory
    :class:`SQLiteGraphStore` so the production DB is never
    touched.

These tests are additive — the existing ~614-test regression
must still pass after this file lands.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from typing import Any

from phase3.datamodel.graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
)
from phase3.graph import (
    GraphStore,
    GraphStoreLike,
    compute_blast_radius,
    compute_cross_layer_impact,
    compute_lineage,
    explain_score,
    ExplainedScore,
)
from phase3.graph.explain_score import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_NODES,
)


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
        (EdgeType.CITES, "score:company:2330:2026-07-08",
         "source:yfinance"),
        (EdgeType.INFLUENCES, "score:macro:global:2026-07-08",
         "score:company:2330:2026-07-08"),
    ]
    for et, f, t in edges:
        s.add_edge(GraphEdge(edge_id="_", edge_type=et,
                             from_node_id=f, to_node_id=t))
    return s


def _build_solo_graph() -> GraphStore:
    """A graph with a score that has NO upstream evidence at all.

    Used to verify the "score with no evidence" path: the score
    node exists, present=True, but the lineage / blast-radius /
    cross-layer queries all return empty result sets.
    """
    s = GraphStore()
    s.add_node(GraphNode(
        node_id="score:company:9999:2026-07-08",
        node_type=NodeType.SCORE,
        label="+0.00",
    ))
    return s


def _build_inmemory_sqlite() -> Any:
    """Build a Phase 3B SQLiteGraphStore with the same content
    as ``_build_sample_graph``, but stored in a temp-file DB so
    we can verify SQLite / InMemory parity without touching
    the production DB.

    Why a temp file and not ``:memory:``: the Python sqlite3
    module treats ``:memory:`` as a special name in the
    CPython stdlib but the underlying build on this
    container writes the literal file ``:memory:`` to the
    current working directory when the path goes through
    ``os.PathLike`` round-trips. A temp file avoids the
    leak entirely.

    Returns ``(store, dir_path)``. The caller is responsible
    for closing the store and removing ``dir_path`` (use
    :func:`_close_inmemory_sqlite`).
    """
    import shutil
    import tempfile as _tempfile
    from phase3.graph.sqlite_store import SQLiteGraphStore
    tmp = _tempfile.mkdtemp(prefix="explain_score_sqlite_")
    db_file = os.path.join(tmp, "graph.db")
    store = SQLiteGraphStore(db_file, auto_migrate=True)
    sample = _build_sample_graph()
    # Migrate nodes / edges from in-memory to SQLite.
    for node in sample._nodes.values():
        store.add_node(node)
    for edge_id, edge in sample._edges.items():
        # The in-memory store may have a synthetic "_" edge id;
        # let the SQLite store assign a deterministic one.
        store.add_edge(edge)
    return store, tmp


def _close_inmemory_sqlite(store: Any, tmp: str) -> None:
    """Close ``store`` and remove the temp directory."""
    close = getattr(store, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1) Full evidence path
# ---------------------------------------------------------------------------


class FullEvidenceTests(unittest.TestCase):
    """``explain_score`` over the canonical sample graph returns a
    well-formed :class:`ExplainedScore` with all three query
    payloads populated and the metadata projected.
    """

    def setUp(self) -> None:
        self.s = _build_sample_graph()
        self.r = explain_score(
            self.s, "score:company:2330:2026-07-08", max_depth=5,
        )

    def test_present_true(self) -> None:
        self.assertTrue(self.r.present)

    def test_score_metadata_has_canonical_fields(self) -> None:
        sm = self.r.score_metadata
        self.assertEqual(sm["score_id"], "score:company:2330:2026-07-08")
        self.assertEqual(sm["score_value_label"], "+34.04")
        self.assertEqual(sm["node_type"], "score")
        self.assertEqual(sm["label"], "+34.04")
        # metadata / tags / schema_version are also present.
        self.assertIn("metadata", sm)
        self.assertIn("tags", sm)
        self.assertIn("schema_version", sm)

    def test_lineage_visited_signals_and_source(self) -> None:
        l = self.r.lineage
        self.assertEqual(set(l.signal_node_ids),
                         {"signal:2330:pe", "signal:2330:roe"})
        self.assertIn("source:yfinance", l.source_node_ids)
        # The macro score appears in the upstream score list
        # because there is an INFLUENCES edge from macro -> company.
        self.assertIn("score:macro:global:2026-07-08", l.score_node_ids)

    def test_blast_radius_visited_empty_for_score(self) -> None:
        # The sample score has no downstream consumers, so the
        # blast radius is empty. This is the expected / correct
        # outcome (not a "no evidence" error case).
        br = self.r.blast_radius
        self.assertEqual(br.visited_node_ids, [])
        self.assertEqual(br.score_node_ids, [])
        self.assertEqual(br.report_node_ids, [])

    def test_cross_layer_includes_macro_influence(self) -> None:
        cli = self.r.cross_layer_impact
        # The macro score is upstream of the company score
        # (INFLUENCES: macro -> company). Upstream chain lists
        # the influencer; downstream is empty for this anchor.
        self.assertIn("score:macro:global:2026-07-08",
                      cli.upstream_chain)
        self.assertEqual(cli.downstream_chain, [])
        self.assertEqual(cli.depth_reached_upstream, 1)

    def test_warnings_empty_for_full_evidence(self) -> None:
        # No truncation, no missing fields — no warnings.
        self.assertEqual(self.r.warnings, [])

    def test_truncated_false(self) -> None:
        self.assertFalse(self.r.truncated)

    def test_depth_reached_positive(self) -> None:
        self.assertGreater(self.r.depth_reached, 0)


# ---------------------------------------------------------------------------
# 2) Missing score / invalid id
# ---------------------------------------------------------------------------


class MissingScoreTests(unittest.TestCase):
    """When the requested score node is not in the graph, the
    explanation must report ``present=False`` and emit a single
    top-level warning. The three query payloads must be empty
    default-constructed DTOs.
    """

    def setUp(self) -> None:
        self.s = _build_sample_graph()
        self.r = explain_score(
            self.s, "score:nonexistent:9999", max_depth=5,
        )

    def test_present_false(self) -> None:
        self.assertFalse(self.r.present)

    def test_score_node_id_echoed_back(self) -> None:
        # The request id must be echoed back even when the node
        # is missing so the consumer can match the response.
        self.assertEqual(self.r.score_node_id, "score:nonexistent:9999")

    def test_score_metadata_empty(self) -> None:
        self.assertEqual(self.r.score_metadata, {})

    def test_warnings_has_top_level_entry(self) -> None:
        self.assertEqual(len(self.r.warnings), 1)
        self.assertIn("score:nonexistent:9999", self.r.warnings[0])
        self.assertIn("not in graph", self.r.warnings[0])

    def test_truncated_false_and_depth_zero(self) -> None:
        self.assertFalse(self.r.truncated)
        self.assertEqual(self.r.depth_reached, 0)

    def test_three_query_payloads_are_default_constructed(self) -> None:
        # Lineage / blast / cross-layer should be empty DTOs.
        self.assertEqual(self.r.lineage.visited_node_ids, [])
        self.assertEqual(self.r.blast_radius.visited_node_ids, [])
        self.assertEqual(self.r.cross_layer_impact.upstream_chain, [])
        self.assertEqual(self.r.cross_layer_impact.downstream_chain, [])


# ---------------------------------------------------------------------------
# 3) Score with no evidence
# ---------------------------------------------------------------------------


class NoEvidenceTests(unittest.TestCase):
    """A score node that exists but has no upstream or downstream
    edges must still be explained correctly. The result is
    ``present=True`` with empty / zeroed-out query payloads.
    """

    def setUp(self) -> None:
        self.s = _build_solo_graph()
        self.r = explain_score(
            self.s, "score:company:9999:2026-07-08", max_depth=5,
        )

    def test_present_true(self) -> None:
        self.assertTrue(self.r.present)

    def test_score_metadata_project(self) -> None:
        sm = self.r.score_metadata
        self.assertEqual(sm["score_id"], "score:company:9999:2026-07-08")
        self.assertEqual(sm["score_value_label"], "+0.00")

    def test_no_upstream(self) -> None:
        l = self.r.lineage
        self.assertEqual(l.visited_node_ids, [])
        self.assertEqual(l.signal_node_ids, [])
        self.assertEqual(l.source_node_ids, [])

    def test_no_downstream(self) -> None:
        br = self.r.blast_radius
        self.assertEqual(br.visited_node_ids, [])

    def test_no_cross_layer(self) -> None:
        cli = self.r.cross_layer_impact
        self.assertEqual(cli.upstream_chain, [])
        self.assertEqual(cli.downstream_chain, [])

    def test_no_warnings(self) -> None:
        self.assertEqual(self.r.warnings, [])

    def test_truncated_false(self) -> None:
        self.assertFalse(self.r.truncated)


# ---------------------------------------------------------------------------
# 4) Truncated trace (max_nodes cap)
# ---------------------------------------------------------------------------


class TruncatedTests(unittest.TestCase):
    """When ``max_nodes`` is small enough to cut off the lineage
    walk, the top-level ``truncated`` flag must flip to True
    and a synthetic warning must be appended (the canonical
    lineage / blast / cross-layer query modules do not emit a
    warning for max_nodes-driven truncation — only a boolean).
    """

    def setUp(self) -> None:
        self.s = _build_sample_graph()
        # With max_nodes=1 the lineage walk stops after 1
        # visited node, so the result is truncated.
        self.r = explain_score(
            self.s, "score:company:2330:2026-07-08",
            max_depth=5, max_nodes=1,
        )

    def test_truncated_true(self) -> None:
        self.assertTrue(self.r.truncated)

    def test_synthetic_truncation_warning_emitted(self) -> None:
        # The bottom of the function synthesises a warning
        # when ``truncated`` is True and the underlying
        # queries emitted no warnings of their own.
        self.assertTrue(any("truncated" in w for w in self.r.warnings))

    def test_lineage_was_truncated(self) -> None:
        # Direct check on the lineage DTO.
        self.assertTrue(self.r.lineage.truncated)
        self.assertEqual(
            len(self.r.lineage.visited_node_ids), 1,
        )

    def test_present_still_true(self) -> None:
        # The score node was still found — the trace is just
        # incomplete.
        self.assertTrue(self.r.present)


# ---------------------------------------------------------------------------
# 5) Cross-layer influence included
# ---------------------------------------------------------------------------


class CrossLayerInfluenceTests(unittest.TestCase):
    """The cross-layer-impact payload is the third of the three
    canonical queries. We verify that a graph with a multi-hop
    score-to-score INFLUENCES chain is reflected in the
    explanation.
    """

    def setUp(self) -> None:
        # Build a 3-hop INFLUENCES chain:
        #   score:A -> score:B -> score:C -> score:D
        # and explain from score:C (the middle).
        s = GraphStore()
        for nid in ("score:A", "score:B", "score:C", "score:D"):
            s.add_node(GraphNode(
                node_id=nid, node_type=NodeType.SCORE,
                label=f"+{nid[-1]}.0",
            ))
        s.add_edge(GraphEdge(
            edge_id="_", edge_type=EdgeType.INFLUENCES,
            from_node_id="score:A", to_node_id="score:B",
        ))
        s.add_edge(GraphEdge(
            edge_id="_", edge_type=EdgeType.INFLUENCES,
            from_node_id="score:B", to_node_id="score:C",
        ))
        s.add_edge(GraphEdge(
            edge_id="_", edge_type=EdgeType.INFLUENCES,
            from_node_id="score:C", to_node_id="score:D",
        ))
        self.s = s
        self.r = explain_score(self.s, "score:C", max_depth=5)

    def test_upstream_chain_includes_A_and_B(self) -> None:
        self.assertIn("score:A", self.r.cross_layer_impact.upstream_chain)
        self.assertIn("score:B", self.r.cross_layer_impact.upstream_chain)

    def test_downstream_chain_includes_D(self) -> None:
        self.assertIn("score:D",
                      self.r.cross_layer_impact.downstream_chain)

    def test_depth_reached_upstream_is_2(self) -> None:
        self.assertEqual(
            self.r.cross_layer_impact.depth_reached_upstream, 2,
        )

    def test_depth_reached_downstream_is_1(self) -> None:
        self.assertEqual(
            self.r.cross_layer_impact.depth_reached_downstream, 1,
        )


# ---------------------------------------------------------------------------
# 6) Deterministic output
# ---------------------------------------------------------------------------


class DeterministicTests(unittest.TestCase):
    """Two explanations of the same node over the same graph must
    produce byte-for-byte identical :meth:`to_dict` and
    :meth:`to_markdown` output. The three canonical query
    modules are themselves deterministic, so composition must
    be too.
    """

    def test_dict_is_byte_identical(self) -> None:
        s = _build_sample_graph()
        r1 = explain_score(s, "score:company:2330:2026-07-08")
        r2 = explain_score(s, "score:company:2330:2026-07-08")
        d1 = r1.to_dict()
        d2 = r2.to_dict()
        # The created_at timestamp on the score node was set
        # at fixture time and is therefore stable. The query
        # DTOs are stable too. So the dicts must be equal.
        self.assertEqual(d1, d2)

    def test_markdown_is_byte_identical(self) -> None:
        s = _build_sample_graph()
        r1 = explain_score(s, "score:company:2330:2026-07-08")
        r2 = explain_score(s, "score:company:2330:2026-07-08")
        self.assertEqual(r1.to_markdown(), r2.to_markdown())

    def test_json_round_trip_is_lossless(self) -> None:
        s = _build_sample_graph()
        r = explain_score(s, "score:company:2330:2026-07-08")
        text = json.dumps(r.to_dict(), ensure_ascii=False)
        restored = json.loads(text)
        self.assertEqual(restored, r.to_dict())


# ---------------------------------------------------------------------------
# 7) JSON schema / shape
# ---------------------------------------------------------------------------


class JsonShapeTests(unittest.TestCase):
    """The :meth:`ExplainedScore.to_dict` payload must have a
    stable top-level shape (sorted keys, no missing required
    fields, JSON-serializable).
    """

    def setUp(self) -> None:
        self.s = _build_sample_graph()
        self.r = explain_score(
            self.s, "score:company:2330:2026-07-08", max_depth=5,
        )

    def test_top_level_keys(self) -> None:
        d = self.r.to_dict()
        expected = {
            "score_node_id", "score_metadata", "present",
            "lineage", "blast_radius", "cross_layer_impact",
            "warnings", "truncated", "depth_reached",
        }
        self.assertEqual(expected, set(d.keys()))

    def test_lineage_dict_is_canonical(self) -> None:
        d = self.r.to_dict()
        # The lineage payload is the canonical to_dict() view
        # of LineageQuery — keys vary by run but must include
        # at minimum the "start" and "warnings" fields.
        l = d["lineage"]
        self.assertEqual(l["start"], "score:company:2330:2026-07-08")
        self.assertIn("warnings", l)
        self.assertIn("visited_node_ids", l)
        self.assertIn("truncated", l)

    def test_blast_dict_is_canonical(self) -> None:
        d = self.r.to_dict()
        br = d["blast_radius"]
        self.assertEqual(br["start"], "score:company:2330:2026-07-08")
        self.assertIn("warnings", br)
        self.assertIn("visited_node_ids", br)
        self.assertIn("truncated", br)

    def test_cross_layer_dict_is_canonical(self) -> None:
        d = self.r.to_dict()
        cli = d["cross_layer_impact"]
        self.assertEqual(cli["start"], "score:company:2330:2026-07-08")
        self.assertIn("upstream_chain", cli)
        self.assertIn("downstream_chain", cli)

    def test_score_metadata_dict_is_canonical(self) -> None:
        d = self.r.to_dict()
        sm = d["score_metadata"]
        # Required operator-facing fields.
        for key in (
            "score_id", "score_value_label", "node_type",
            "label", "created_at", "schema_version",
        ):
            self.assertIn(key, sm, f"missing {key!r}")

    def test_serializable_via_json_dumps(self) -> None:
        # Round-trip must not raise.
        text = json.dumps(self.r.to_dict(), ensure_ascii=False)
        self.assertIsInstance(text, str)
        # The text must re-parse.
        self.assertIsInstance(json.loads(text), dict)


# ---------------------------------------------------------------------------
# 8) Text / Markdown output
# ---------------------------------------------------------------------------


class MarkdownTests(unittest.TestCase):
    """The :meth:`ExplainedScore.to_markdown` payload must be a
    stable, copy-pastable, human-readable document.
    """

    def setUp(self) -> None:
        self.s = _build_sample_graph()
        self.r = explain_score(
            self.s, "score:company:2330:2026-07-08", max_depth=5,
        )
        self.md = self.r.to_markdown()

    def test_has_top_level_header(self) -> None:
        self.assertTrue(
            self.md.startswith("# Explain-Score: `score:company:2330"),
        )

    def test_has_score_metadata_section(self) -> None:
        self.assertIn("## Score metadata", self.md)
        self.assertIn("`+34.04`", self.md)

    def test_has_lineage_section(self) -> None:
        self.assertIn("## Lineage", self.md)
        self.assertIn("source_node_ids", self.md)
        self.assertIn("signal_node_ids", self.md)

    def test_has_blast_radius_section(self) -> None:
        self.assertIn("## Blast Radius", self.md)
        self.assertIn("report_node_ids", self.md)

    def test_has_cross_layer_section(self) -> None:
        self.assertIn("## Cross-layer Impact", self.md)
        self.assertIn("upstream_chain", self.md)
        self.assertIn("downstream_chain", self.md)

    def test_has_warning_footer(self) -> None:
        self.assertIn("truncated: `False`", self.md)
        self.assertIn("depth_reached: `1`", self.md)
        self.assertIn("warnings: `0`", self.md)

    def test_markdown_for_missing_node(self) -> None:
        r = explain_score(
            self.s, "score:nonexistent:9999", max_depth=5,
        )
        md = r.to_markdown()
        self.assertIn("not found in the graph store", md)
        # All three sub-sections should be marked skipped.
        self.assertIn("_(skipped — node not in graph)_", md)


# ---------------------------------------------------------------------------
# 9) SQLite / InMemory parity
# ---------------------------------------------------------------------------


class SqliteInMemoryParityTests(unittest.TestCase):
    """The same content stored in the in-memory GraphStore and
    in a SQLiteGraphStore (in-memory) must produce equivalent
    explain-score results. The shapes are identical; the
    ordering of visited_node_ids is stable (the canonical
    query modules emit in BFS discovery order which is
    deterministic for the same node set + edge set).
    """

    def test_to_dict_is_equivalent(self) -> None:
        s_mem = _build_sample_graph()
        s_sql, tmp = _build_inmemory_sqlite()
        try:
            r_mem = explain_score(
                s_mem, "score:company:2330:2026-07-08",
            )
            r_sql = explain_score(
                s_sql, "score:company:2330:2026-07-08",
            )
            d_mem = r_mem.to_dict()
            d_sql = r_sql.to_dict()
            # Compare field-by-field. ``score_metadata``
            # contains the graph node's ``created_at`` which
            # is set at fixture-construction time and differs
            # between the two stores by a few microseconds —
            # the comparison ignores that key because the test
            # is asserting semantic equivalence, not
            # timestamp equivalence.
            def _strip_created_at(d: dict[str, Any]) -> dict[str, Any]:
                out = dict(d)
                if "created_at" in out:
                    out["created_at"] = "<stripped>"
                return out
            for k in d_mem:
                if k in ("depth_reached", "truncated", "warnings"):
                    self.assertEqual(
                        d_mem[k], d_sql[k],
                        f"mismatch at {k!r}",
                    )
                elif k == "score_metadata":
                    self.assertEqual(
                        _strip_created_at(d_mem[k]),
                        _strip_created_at(d_sql[k]),
                        f"mismatch at {k!r}",
                    )
                elif k == "lineage":
                    self.assertEqual(
                        d_mem[k]["start"], d_sql[k]["start"],
                    )
                    self.assertEqual(
                        set(d_mem[k]["visited_node_ids"]),
                        set(d_sql[k]["visited_node_ids"]),
                    )
                    self.assertEqual(
                        d_mem[k]["signal_node_ids"],
                        d_sql[k]["signal_node_ids"],
                    )
                elif k == "blast_radius":
                    self.assertEqual(
                        d_mem[k]["start"], d_sql[k]["start"],
                    )
                    self.assertEqual(
                        d_mem[k]["visited_node_ids"],
                        d_sql[k]["visited_node_ids"],
                    )
                elif k == "cross_layer_impact":
                    self.assertEqual(
                        d_mem[k]["upstream_chain"],
                        d_sql[k]["upstream_chain"],
                    )
                    self.assertEqual(
                        d_mem[k]["downstream_chain"],
                        d_sql[k]["downstream_chain"],
                    )
                else:
                    self.assertEqual(d_mem[k], d_sql[k])
        finally:
            _close_inmemory_sqlite(s_sql, tmp)

    def test_present_flag_is_true_in_both(self) -> None:
        s_mem = _build_sample_graph()
        s_sql, tmp = _build_inmemory_sqlite()
        try:
            r_mem = explain_score(
                s_mem, "score:company:2330:2026-07-08",
            )
            r_sql = explain_score(
                s_sql, "score:company:2330:2026-07-08",
            )
            self.assertTrue(r_mem.present)
            self.assertTrue(r_sql.present)
        finally:
            _close_inmemory_sqlite(s_sql, tmp)


# ---------------------------------------------------------------------------
# 10) CLI exit codes + db path guard
# ---------------------------------------------------------------------------


class ExplainScoreCLITests(unittest.TestCase):
    """Drive the ``explain-score`` subcommand via subprocess and
    verify exit codes, output formats, and the macro_history.db
    guard. Mirrors the test patterns in
    :mod:`tests.phase3.test_evidence_trace_cli`.
    """

    def _run(
        self, *args: str, env_extra: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["PYTHONPATH"] = "/home/ubuntu/macro-report"
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable,
             "/home/ubuntu/macro-report/phase3/cli.py",
             "explain-score", *args],
            capture_output=True, text=True, env=env, timeout=30,
        )

    def test_help_lists_subcommand(self) -> None:
        result = subprocess.run(
            [sys.executable,
             "/home/ubuntu/macro-report/phase3/cli.py", "--help"],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": "/home/ubuntu/macro-report"},
            timeout=15,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("explain-score", result.stdout)

    def test_explain_score_help_shows_all_flags(self) -> None:
        result = self._run("--help")
        self.assertEqual(result.returncode, 0)
        for flag in (
            "--node", "--max-depth", "--max-nodes",
            "--db-path", "--output", "--json", "--from-pipeline",
        ):
            self.assertIn(flag, result.stdout)

    def test_sample_graph_default_markdown(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("=== explain-score (sample-graph) ===",
                      result.stdout)
        self.assertIn("# Explain-Score:", result.stdout)
        self.assertIn("score:company:2330:2026-07-08", result.stdout)

    def test_sample_graph_json_to_stdout(self) -> None:
        result = self._run("--json")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        parsed = json.loads(result.stdout)
        self.assertEqual(
            parsed["score_node_id"],
            "score:company:2330:2026-07-08",
        )
        self.assertTrue(parsed["present"])
        # The three canonical payloads are present.
        for key in ("lineage", "blast_radius", "cross_layer_impact"):
            self.assertIn(key, parsed)

    def test_explicit_node(self) -> None:
        result = self._run(
            "--node", "score:macro:global:2026-07-08", "--json",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        parsed = json.loads(result.stdout)
        self.assertEqual(
            parsed["score_node_id"],
            "score:macro:global:2026-07-08",
        )
        self.assertTrue(parsed["present"])

    def test_missing_node_reports_present_false(self) -> None:
        result = self._run("--node", "score:nope", "--json")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        parsed = json.loads(result.stdout)
        self.assertFalse(parsed["present"])
        # The single top-level warning is emitted.
        self.assertEqual(len(parsed["warnings"]), 1)
        self.assertIn("not in graph", parsed["warnings"][0])

    def test_output_to_file(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
        ) as f:
            outpath = f.name
        try:
            result = self._run("--output", outpath)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn(f"-> {outpath}", result.stdout)
            with open(outpath) as f:
                parsed = json.load(f)
            self.assertIn("score_node_id", parsed)
        finally:
            os.unlink(outpath)

    def test_output_dash_writes_to_stdout(self) -> None:
        result = self._run("--output", "-")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        parsed = json.loads(result.stdout)
        self.assertIn("score_node_id", parsed)

    def test_max_nodes_truncates_with_warning(self) -> None:
        result = self._run("--max-nodes", "1", "--json")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        parsed = json.loads(result.stdout)
        self.assertTrue(parsed["truncated"])
        # The synthetic top-level truncation warning is present.
        self.assertTrue(
            any("truncated" in w for w in parsed["warnings"]),
        )

    def test_macro_history_db_is_refused(self) -> None:
        # Create a stub file with the reserved name so the
        # basename guard fires. The CLI should refuse to
        # open it and exit with code 1.
        with tempfile.NamedTemporaryFile(
            prefix="macro_history", suffix=".db", delete=False,
        ) as f:
            f.write(b"")
            tmp = f.name
        # Rename to the exact reserved basename inside a temp
        # dir so the basename check matches.
        tmpdir = tempfile.mkdtemp()
        guarded = os.path.join(tmpdir, "macro_history.db")
        os.replace(tmp, guarded)
        try:
            result = self._run("--db-path", guarded)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("macro_history.db",
                          (result.stdout + result.stderr).lower())
        finally:
            os.unlink(guarded)
            os.rmdir(tmpdir)

    def test_case_bypass_macro_history_db_is_refused(self) -> None:
        # F2 hardening: a case-bypass (``Macro_History.db``) must
        # still trip the basename guard. On a case-sensitive
        # filesystem the bypass was previously silent because
        # the basename comparison was case-sensitive.
        tmpdir = tempfile.mkdtemp()
        guarded = os.path.join(tmpdir, "Macro_History.db")
        # Touch the file so the bypass test is realistic (a real
        # caller would have a real file at the path).
        with open(guarded, "wb") as f:
            f.write(b"")
        try:
            result = self._run("--db-path", guarded)
            self.assertNotEqual(result.returncode, 0)
            combined = (result.stdout + result.stderr).lower()
            self.assertIn("macro_history.db", combined)
            # Also assert that the case-insensitive hint is
            # present in the error message.
            self.assertIn("case-insensitive", combined)
        finally:
            os.unlink(guarded)
            os.rmdir(tmpdir)

    def test_case_bypass_all_caps_is_refused(self) -> None:
        # F2 hardening variant: ``MACRO_HISTORY.DB`` (full
        # uppercase) must also trip the basename guard.
        tmpdir = tempfile.mkdtemp()
        guarded = os.path.join(tmpdir, "MACRO_HISTORY.DB")
        with open(guarded, "wb") as f:
            f.write(b"")
        try:
            result = self._run("--db-path", guarded)
            self.assertNotEqual(result.returncode, 0)
            combined = (result.stdout + result.stderr).lower()
            self.assertIn("macro_history.db", combined)
            self.assertIn("case-insensitive", combined)
        finally:
            os.unlink(guarded)
            os.rmdir(tmpdir)

    def test_case_bypass_mixed_case_is_refused(self) -> None:
        # F2 hardening variant: ``Macro_history.DB`` (mixed
        # case) must also trip the basename guard.
        tmpdir = tempfile.mkdtemp()
        guarded = os.path.join(tmpdir, "Macro_history.DB")
        with open(guarded, "wb") as f:
            f.write(b"")
        try:
            result = self._run("--db-path", guarded)
            self.assertNotEqual(result.returncode, 0)
            combined = (result.stdout + result.stderr).lower()
            self.assertIn("macro_history.db", combined)
            self.assertIn("case-insensitive", combined)
        finally:
            os.unlink(guarded)
            os.rmdir(tmpdir)

    def test_from_pipeline_flag_prints_hint_but_succeeds(self) -> None:
        # --from-pipeline is a no-op in Run 1; the CLI prints
        # a hint to stderr and the run still completes.
        result = self._run("--from-pipeline", "--json")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("--from-pipeline", result.stderr)
        parsed = json.loads(result.stdout)
        self.assertTrue(parsed["present"])


# ---------------------------------------------------------------------------
# 11) Re-exports
# ---------------------------------------------------------------------------


class ReExportsTests(unittest.TestCase):
    """The new symbols must be re-exported from the canonical
    package roots. Mirrors the pattern in
    :mod:`tests.phase3.test_evidence_trace_cli`.
    """

    def test_graph_package_reexports_explain_score_symbols(self) -> None:
        from phase3.graph import (
            DEFAULT_MAX_DEPTH as G_DEPTH,
            DEFAULT_MAX_NODES as G_NODES,
            ExplainedScore as G_Explained,
            GraphStoreLike as G_StoreLike,
            explain_score as G_explain,
        )
        self.assertIs(G_DEPTH, DEFAULT_MAX_DEPTH)
        self.assertIs(G_NODES, DEFAULT_MAX_NODES)
        self.assertIs(G_Explained, ExplainedScore)
        self.assertIs(G_StoreLike, GraphStoreLike)
        self.assertIs(G_explain, explain_score)

    def test_explained_score_dataclass_metadata(self) -> None:
        # Sanity check: ExplainedScore is a frozen dataclass
        # with the documented fields.
        from dataclasses import fields, is_dataclass
        self.assertTrue(is_dataclass(ExplainedScore))
        names = {f.name for f in fields(ExplainedScore)}
        self.assertEqual(
            names,
            {
                "score_node_id", "score_metadata", "present",
                "lineage", "blast_radius", "cross_layer_impact",
                "warnings", "truncated", "depth_reached",
            },
        )


if __name__ == "__main__":
    unittest.main()
