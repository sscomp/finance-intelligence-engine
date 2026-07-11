"""Tests for Phase 4 Task 4 Run 1 — Shadow Run / Decision Replay Foundation.

Scope
-----
* :class:`ShadowRunConfig` validates its own inputs and rejects
  the macro_history.db basename.
* :class:`ShadowDecision` / :class:`ShadowComparison` /
  :class:`ShadowRunResult` round-trip through ``json.dumps`` /
  ``json.loads`` and produce a stable structural shape.
* :func:`run_shadow` consumes a ``build_json_export``-shaped
  artifact and emits per-scorer comparisons + explain-score
  refs for changed decisions.
* The CLI subcommand ``shadow-run`` honors ``--artifact``,
  ``--replay-config-hash``, ``--output``, ``--json`` and
  ``--include-unchanged`` and refuses to open
  ``macro_history.db`` (case-insensitive basename).

Design constraints
------------------
* stdlib ``unittest`` only (no pytest).
* No production DB writes; tests use the in-memory store
  and write artifacts to a temp directory.
* Tests are additive — the existing ~614-test regression
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

from phase3.pipeline.shadow_run import (
    SCHEMA_VERSION,
    ShadowComparison,
    ShadowDecision,
    ShadowRunConfig,
    ShadowRunError,
    ShadowRunResult,
    run_shadow,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _build_artifact(
    *,
    run_id: str = "run-2026-07-11-0001",
    config_hash: str = "cfg-v1",
    date_bucket: str = "2026-07-08",
    evidence_handles: list[dict[str, Any]] | None = None,
    graph_writes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a minimal ``build_json_export`` JSON artifact.

    Defaults to a 3-scorer sample (macro, industry, company)
    matching the explain-score test fixture's pattern. The
    minimum-viable artifact must include at least one
    ``score_node_id`` (in either ``graph_writes`` or
    ``evidence_handles``) for :func:`load_pipeline_envelope`
    to resolve a default score.
    """
    if evidence_handles is None:
        evidence_handles = [
            {
                "scorer_type": "macro",
                "entity_id": "global",
                "date_bucket": date_bucket,
                "score_node_id": f"score:macro:global:{date_bucket}",
                "score": 4.68,
                "confidence": 0.5,
                "evidence_signal_ids": ["signal:fed_rate", "signal:cpi_yoy"],
                "warnings": [],
                "config_hash": config_hash,
            },
            {
                "scorer_type": "industry",
                "entity_id": "半導體",
                "date_bucket": date_bucket,
                "score_node_id": f"score:industry:半導體:{date_bucket}",
                "score": 12.30,
                "confidence": 0.4,
                "evidence_signal_ids": ["signal:sm_pe", "signal:sm_capex"],
                "warnings": [],
                "config_hash": config_hash,
            },
            {
                "scorer_type": "company",
                "entity_id": "2330",
                "date_bucket": date_bucket,
                "score_node_id": f"score:company:2330:{date_bucket}",
                "score": 34.04,
                "confidence": 0.7,
                "evidence_signal_ids": ["signal:2330:pe", "signal:2330:roe"],
                "warnings": [],
                "config_hash": config_hash,
            },
        ]
    if graph_writes is None:
        # Provide graph_writes so the loader can resolve a default
        # score without scanning evidence_handles; the macro
        # score is the canonical "first" per the upstream order.
        graph_writes = [
            {
                "score_node_id": f"score:macro:global:{date_bucket}",
                "entity_node_id": "entity:macro:global",
                "signal_node_ids": [
                    "signal:fed_rate",
                    "signal:cpi_yoy",
                ],
                "source_node_ids": ["source:yfinance"],
                "cross_layer_edge_ids": [],
            },
            {
                "score_node_id": f"score:industry:半導體:{date_bucket}",
                "entity_node_id": "entity:industry:半導體",
                "signal_node_ids": [
                    "signal:sm_pe",
                    "signal:sm_capex",
                ],
                "source_node_ids": ["source:yfinance"],
                "cross_layer_edge_ids": [],
            },
            {
                "score_node_id": f"score:company:2330:{date_bucket}",
                "entity_node_id": "entity:company:2330",
                "signal_node_ids": [
                    "signal:2330:pe",
                    "signal:2330:roe",
                ],
                "source_node_ids": ["source:yfinance"],
                "cross_layer_edge_ids": [],
            },
        ]
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact": {
            "run_id": run_id,
            "config_hash": config_hash,
            "date_bucket": date_bucket,
            "persist": False,
        },
        "result": {
            "graph_writes": graph_writes,
            "evidence_handles": evidence_handles,
            "snapshot_ids": [],
            "warnings": [],
            "errors": [],
            "metadata": {
                "run_id": run_id,
                "config_hash": config_hash,
                "date_bucket": date_bucket,
            },
        },
    }


def _write_artifact_to_temp(artifact: dict[str, Any]) -> str:
    """Write an artifact to a temp file and return the path."""
    fd, path = tempfile.mkstemp(suffix=".json", prefix="shadow_run_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(artifact, f, ensure_ascii=False, sort_keys=True)
    except Exception:
        if os.path.exists(path):
            os.unlink(path)
        raise
    return path


# ---------------------------------------------------------------------------
# 1) Config validation
# ---------------------------------------------------------------------------


class ConfigValidationTests(unittest.TestCase):
    """:class:`ShadowRunConfig` must reject invalid inputs at
    construction time, before any artifact I/O."""

    def test_empty_artifact_path_raises(self) -> None:
        with self.assertRaises(ShadowRunError) as cm:
            ShadowRunConfig(artifact_path="")
        self.assertIn("artifact_path", str(cm.exception))

    def test_negative_score_tolerance_raises(self) -> None:
        with self.assertRaises(ShadowRunError) as cm:
            ShadowRunConfig(artifact_path="/tmp/x.json", score_tolerance=-0.1)
        self.assertIn("score_tolerance", str(cm.exception))

    def test_negative_confidence_tolerance_raises(self) -> None:
        with self.assertRaises(ShadowRunError) as cm:
            ShadowRunConfig(
                artifact_path="/tmp/x.json", confidence_tolerance=-0.1
            )
        self.assertIn("confidence_tolerance", str(cm.exception))

    def test_empty_replay_label_raises(self) -> None:
        with self.assertRaises(ShadowRunError) as cm:
            ShadowRunConfig(artifact_path="/tmp/x.json", replay_label="")
        self.assertIn("replay_label", str(cm.exception))

    def test_default_config_is_valid(self) -> None:
        # All defaults are valid; constructor must not raise.
        cfg = ShadowRunConfig(artifact_path="/tmp/x.json")
        self.assertEqual(cfg.replay_label, "replay")
        self.assertEqual(cfg.score_tolerance, 0.0)
        self.assertEqual(cfg.confidence_tolerance, 0.0)
        self.assertTrue(cfg.include_unchanged)
        self.assertIsNone(cfg.output_path)
        self.assertIsNone(cfg.replay_config_hash)


# ---------------------------------------------------------------------------
# 2) DTO round-trip / structural shape
# ---------------------------------------------------------------------------


class DTORoundTripTests(unittest.TestCase):
    """The result DTOs must round-trip through JSON without
    losing data or order. The structural shape is part of the
    public contract — operators downstream read these JSON
    files."""

    def test_shadow_decision_to_dict_keys(self) -> None:
        d = ShadowDecision(
            scorer_type="company",
            entity_id="2330",
            date_bucket="2026-07-08",
            score_node_id="score:company:2330:2026-07-08",
            baseline_score=34.04,
            replay_score=34.04,
            baseline_confidence=0.7,
            replay_confidence=0.7,
            baseline_config_hash="cfg-v1",
            replay_config_hash="cfg-v1",
            baseline_evidence_signal_ids=("signal:2330:pe", "signal:2330:roe"),
            replay_evidence_signal_ids=("signal:2330:pe", "signal:2330:roe"),
            baseline_warnings=(),
            replay_warnings=(),
            changed=False,
            score_delta=0.0,
            confidence_delta=0.0,
            evidence_diff=(),
            warnings_diff=(),
            explain_score_ref="",
        )
        d_dict = d.to_dict()
        expected_keys = {
            "scorer_type",
            "entity_id",
            "date_bucket",
            "score_node_id",
            "baseline_score",
            "replay_score",
            "baseline_confidence",
            "replay_confidence",
            "baseline_config_hash",
            "replay_config_hash",
            "baseline_evidence_signal_ids",
            "replay_evidence_signal_ids",
            "baseline_warnings",
            "replay_warnings",
            "changed",
            "score_delta",
            "confidence_delta",
            "evidence_diff",
            "warnings_diff",
            "explain_score_ref",
        }
        self.assertEqual(set(d_dict.keys()), expected_keys)

    def test_shadow_decision_round_trip(self) -> None:
        d = ShadowDecision(
            scorer_type="macro",
            entity_id="global",
            date_bucket="2026-07-08",
            score_node_id="score:macro:global:2026-07-08",
            baseline_score=4.68,
            replay_score=4.68,
            baseline_confidence=0.5,
            replay_confidence=0.5,
            baseline_config_hash="cfg-v1",
            replay_config_hash="cfg-v2",
            baseline_evidence_signal_ids=("a", "b"),
            replay_evidence_signal_ids=("a", "b", "c"),
            baseline_warnings=(),
            replay_warnings=("warn-1",),
            changed=True,
            score_delta=0.0,
            confidence_delta=0.0,
            evidence_diff=("c",),
            warnings_diff=("warn-1",),
            explain_score_ref="score:macro:global:2026-07-08",
        )
        d_dict = d.to_dict()
        # Round-trip: dump -> load -> reconstruct.
        blob = json.dumps(d_dict, sort_keys=True, ensure_ascii=False)
        loaded = json.loads(blob)
        self.assertEqual(loaded, d_dict)
        self.assertEqual(loaded["changed"], True)
        self.assertEqual(loaded["evidence_diff"], ["c"])
        self.assertEqual(loaded["warnings_diff"], ["warn-1"])

    def test_shadow_comparison_round_trip(self) -> None:
        d = ShadowDecision(
            scorer_type="macro",
            entity_id="global",
            date_bucket="2026-07-08",
            score_node_id="score:macro:global:2026-07-08",
            baseline_score=1.0,
            replay_score=2.0,
            baseline_confidence=0.0,
            replay_confidence=0.0,
            baseline_config_hash="x",
            replay_config_hash="x",
            baseline_evidence_signal_ids=(),
            replay_evidence_signal_ids=(),
            baseline_warnings=(),
            replay_warnings=(),
            changed=True,
            score_delta=1.0,
            confidence_delta=0.0,
            evidence_diff=(),
            warnings_diff=(),
            explain_score_ref="score:macro:global:2026-07-08",
        )
        c = ShadowComparison(
            scorer_type="macro",
            decision_count=1,
            changed_count=1,
            unchanged_count=0,
            decisions=(d,),
        )
        c_dict = c.to_dict()
        self.assertEqual(c_dict["scorer_type"], "macro")
        self.assertEqual(c_dict["decision_count"], 1)
        self.assertEqual(c_dict["changed_count"], 1)
        self.assertEqual(c_dict["unchanged_count"], 0)
        self.assertEqual(len(c_dict["decisions"]), 1)
        blob = json.dumps(c_dict, sort_keys=True, ensure_ascii=False)
        loaded = json.loads(blob)
        self.assertEqual(loaded, c_dict)

    def test_shadow_run_result_to_dict_has_summary(self) -> None:
        r = ShadowRunResult(
            schema_version=SCHEMA_VERSION,
            generated_at="2026-07-11T00:00:00+00:00",
            artifact_path="/tmp/x.json",
            artifact_run_id="run-1",
            artifact_config_hash="cfg-v1",
            artifact_date_bucket="2026-07-08",
            replay_label="replay",
            replay_config_hash="cfg-v1",
            comparisons=(),
            total_decision_count=0,
            total_changed_count=0,
            total_unchanged_count=0,
            explain_score_refs=(),
        )
        r_dict = r.to_dict()
        # ``summary`` is the operator handoff contract.
        self.assertIn("summary", r_dict)
        self.assertEqual(
            r_dict["summary"]["total_decision_count"], 0
        )
        self.assertEqual(
            r_dict["summary"]["total_changed_count"], 0
        )
        self.assertEqual(
            r_dict["summary"]["total_unchanged_count"], 0
        )
        self.assertEqual(
            r_dict["summary"]["explain_score_ref_count"], 0
        )
        self.assertEqual(r_dict["schema_version"], SCHEMA_VERSION)
        self.assertEqual(r_dict["replay_label"], "replay")
        self.assertEqual(r_dict["replay_config_hash"], "cfg-v1")
        # Must round-trip cleanly.
        blob = json.dumps(r_dict, sort_keys=True, ensure_ascii=False)
        loaded = json.loads(blob)
        self.assertEqual(loaded, r_dict)


# ---------------------------------------------------------------------------
# 3) run_shadow() — determinism check (same config_hash)
# ---------------------------------------------------------------------------


class DeterminismCheckTests(unittest.TestCase):
    """When the replay config_hash is left as ``None``, the run is
    a determinism check: baseline == replay byte-equal. All
    decisions should be ``changed=False``."""

    def setUp(self) -> None:
        self.artifact = _build_artifact()
        self.path = _write_artifact_to_temp(self.artifact)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def test_all_unchanged_when_replay_config_hash_matches(self) -> None:
        cfg = ShadowRunConfig(artifact_path=self.path)
        result = run_shadow(cfg)
        self.assertEqual(result.artifact_config_hash, "cfg-v1")
        self.assertEqual(result.replay_config_hash, "cfg-v1")
        # All 3 decisions, 0 changed.
        self.assertEqual(result.total_decision_count, 3)
        self.assertEqual(result.total_changed_count, 0)
        self.assertEqual(result.total_unchanged_count, 3)
        self.assertEqual(result.explain_score_refs, ())

    def test_comparisons_cover_three_scorer_types(self) -> None:
        cfg = ShadowRunConfig(artifact_path=self.path)
        result = run_shadow(cfg)
        scorer_types = [c.scorer_type for c in result.comparisons]
        self.assertEqual(scorer_types, ["macro", "industry", "company"])

    def test_per_scorer_decision_counts(self) -> None:
        cfg = ShadowRunConfig(artifact_path=self.path)
        result = run_shadow(cfg)
        counts = {c.scorer_type: c.decision_count for c in result.comparisons}
        self.assertEqual(counts, {"macro": 1, "industry": 1, "company": 1})

    def test_no_warnings_for_well_formed_artifact(self) -> None:
        cfg = ShadowRunConfig(artifact_path=self.path)
        result = run_shadow(cfg)
        self.assertEqual(result.warnings, ())


# ---------------------------------------------------------------------------
# 4) run_shadow() — drift check (different config_hash)
# ---------------------------------------------------------------------------


class DriftCheckTests(unittest.TestCase):
    """When ``replay_config_hash`` differs from the artifact's
    ``config_hash``, every decision should be ``changed=True``
    (since the replay declares a new config). The framework
    does not re-execute scoring in Run 1 — only the config
    field changes."""

    def setUp(self) -> None:
        self.artifact = _build_artifact(config_hash="cfg-v1")
        self.path = _write_artifact_to_temp(self.artifact)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def test_all_changed_when_replay_config_hash_differs(self) -> None:
        cfg = ShadowRunConfig(
            artifact_path=self.path,
            replay_config_hash="cfg-v2",
        )
        result = run_shadow(cfg)
        self.assertEqual(result.artifact_config_hash, "cfg-v1")
        self.assertEqual(result.replay_config_hash, "cfg-v2")
        self.assertEqual(result.total_decision_count, 3)
        self.assertEqual(result.total_changed_count, 3)
        self.assertEqual(result.total_unchanged_count, 0)

    def test_explain_score_refs_populated_for_changed(self) -> None:
        cfg = ShadowRunConfig(
            artifact_path=self.path,
            replay_config_hash="cfg-v2",
        )
        result = run_shadow(cfg)
        # Each changed decision contributes one ref.
        self.assertEqual(len(result.explain_score_refs), 3)
        for ref in result.explain_score_refs:
            self.assertTrue(ref.startswith("score:"))

    def test_explain_score_refs_match_score_node_ids(self) -> None:
        cfg = ShadowRunConfig(
            artifact_path=self.path,
            replay_config_hash="cfg-v2",
        )
        result = run_shadow(cfg)
        expected = {
            "score:macro:global:2026-07-08",
            "score:industry:半導體:2026-07-08",
            "score:company:2330:2026-07-08",
        }
        self.assertEqual(set(result.explain_score_refs), expected)


# ---------------------------------------------------------------------------
# 5) run_shadow() — include_unchanged toggle
# ---------------------------------------------------------------------------


class IncludeUnchangedTests(unittest.TestCase):
    """``include_unchanged=False`` must surface only changed
    decisions in the per-scorer tuple, while the unchanged
    count still reflects the total."""

    def setUp(self) -> None:
        self.artifact = _build_artifact(config_hash="cfg-v1")
        self.path = _write_artifact_to_temp(self.artifact)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def test_changed_only_when_drift(self) -> None:
        cfg = ShadowRunConfig(
            artifact_path=self.path,
            replay_config_hash="cfg-v2",
            include_unchanged=False,
        )
        result = run_shadow(cfg)
        # All 3 are changed under drift, so all surfaced.
        for c in result.comparisons:
            self.assertEqual(c.decision_count, 1)
            self.assertEqual(c.changed_count, 1)
            self.assertEqual(c.unchanged_count, 0)
            self.assertEqual(len(c.decisions), 1)

    def test_no_surfaced_decisions_when_determinism(self) -> None:
        cfg = ShadowRunConfig(
            artifact_path=self.path,
            include_unchanged=False,
        )
        result = run_shadow(cfg)
        for c in result.comparisons:
            # All unchanged under determinism; surfaced tuple is
            # empty even though decision_count is 1.
            self.assertEqual(c.decision_count, 1)
            self.assertEqual(c.changed_count, 0)
            self.assertEqual(c.unchanged_count, 1)
            self.assertEqual(c.decisions, ())


# ---------------------------------------------------------------------------
# 6) run_shadow() — output_path
# ---------------------------------------------------------------------------


class OutputPathTests(unittest.TestCase):
    """``output_path`` writes the result JSON to a caller-supplied
    path. The parent directory must exist; missing parent must
    raise :class:`ShadowRunError`."""

    def setUp(self) -> None:
        self.artifact = _build_artifact()
        self.artifact_path = _write_artifact_to_temp(self.artifact)
        self.addCleanup(
            lambda: os.path.exists(self.artifact_path)
            and os.unlink(self.artifact_path)
        )

    def test_writes_json_to_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "shadow.json")
            cfg = ShadowRunConfig(
                artifact_path=self.artifact_path,
                output_path=out,
            )
            result = run_shadow(cfg)
            self.assertTrue(os.path.exists(out))
            with open(out, encoding="utf-8") as f:
                payload = json.load(f)
            self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
            self.assertEqual(
                payload["artifact_run_id"], result.artifact_run_id
            )

    def test_missing_parent_dir_raises(self) -> None:
        cfg = ShadowRunConfig(
            artifact_path=self.artifact_path,
            output_path="/nonexistent/sub/shadow.json",
        )
        with self.assertRaises(ShadowRunError) as cm:
            run_shadow(cfg)
        self.assertIn("parent directory", str(cm.exception))

    def test_no_output_when_unset(self) -> None:
        # No output_path; nothing is written, result is returned.
        with tempfile.TemporaryDirectory() as td:
            before = set(os.listdir(td))
            cfg = ShadowRunConfig(artifact_path=self.artifact_path)
            run_shadow(cfg)
            after = set(os.listdir(td))
            self.assertEqual(before, after)


# ---------------------------------------------------------------------------
# 7) run_shadow() — empty evidence_handles
# ---------------------------------------------------------------------------


class EmptyHandlesTests(unittest.TestCase):
    """An artifact that carries no ``evidence_handles`` must still
    load successfully (the loader only requires at least one
    ``score_node_id``, which we put in ``graph_writes``). The
    comparison is empty, and a single warning is emitted."""

    def setUp(self) -> None:
        self.artifact = _build_artifact(evidence_handles=[])
        self.path = _write_artifact_to_temp(self.artifact)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def test_empty_comparisons(self) -> None:
        cfg = ShadowRunConfig(artifact_path=self.path)
        result = run_shadow(cfg)
        self.assertEqual(result.total_decision_count, 0)
        self.assertEqual(result.total_changed_count, 0)
        self.assertEqual(result.explain_score_refs, ())
        for c in result.comparisons:
            self.assertEqual(c.decision_count, 0)

    def test_warning_emitted(self) -> None:
        cfg = ShadowRunConfig(artifact_path=self.path)
        result = run_shadow(cfg)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("no evidence_handles", result.warnings[0])


# ---------------------------------------------------------------------------
# 8) CLI subcommand
# ---------------------------------------------------------------------------


class ShadowRunCLITests(unittest.TestCase):
    """The ``shadow-run`` CLI subcommand must:

    * exit 2 on missing ``--artifact`` or empty ``--output``;
    * exit 1 on artifact load failure;
    * exit 2 on macro_history.db basename (case-insensitive);
    * exit 0 with human-readable text by default;
    * honor ``--output`` to write a JSON file;
    * honor ``--json`` to emit JSON on stdout.
    """

    def setUp(self) -> None:
        self.artifact = _build_artifact()
        self.artifact_path = _write_artifact_to_temp(self.artifact)
        self.addCleanup(
            lambda: os.path.exists(self.artifact_path)
            and os.unlink(self.artifact_path)
        )
        self.cwd = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )

    def _run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "phase3.cli", "shadow-run", *args],
            capture_output=True,
            text=True,
            cwd=self.cwd,
            check=False,
        )

    def test_missing_artifact_exits_2(self) -> None:
        p = self._run_cli()
        self.assertEqual(p.returncode, 2)
        self.assertIn("--artifact", p.stderr)

    def test_macro_history_db_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            # Even a non-existent file with that basename is
            # rejected at the CLI guard.
            fake = os.path.join(td, "macro_history.db")
            p = self._run_cli("--artifact", fake)
            self.assertEqual(p.returncode, 2)
            self.assertIn("reserved", p.stderr)
            # And the file must not have been created.
            self.assertFalse(os.path.exists(fake))

    def test_empty_output_path_exits_2(self) -> None:
        p = self._run_cli(
            "--artifact", self.artifact_path, "--output", ""
        )
        self.assertEqual(p.returncode, 2)
        self.assertIn("--output", p.stderr)

    def test_human_readable_text_default(self) -> None:
        p = self._run_cli("--artifact", self.artifact_path)
        self.assertEqual(p.returncode, 0, msg=p.stderr)
        self.assertIn("=== Shadow Run", p.stdout)
        self.assertIn("total_decisions", p.stdout)
        # The CLI must NOT print a JSON blob in default mode.
        self.assertNotIn('"schema_version":', p.stdout)

    def test_json_to_stdout(self) -> None:
        p = self._run_cli(
            "--artifact", self.artifact_path, "--json"
        )
        self.assertEqual(p.returncode, 0, msg=p.stderr)
        payload = json.loads(p.stdout)
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(
            payload["artifact_run_id"], "run-2026-07-11-0001"
        )

    def test_output_file_written(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "shadow.json")
            p = self._run_cli(
                "--artifact", self.artifact_path,
                "--output", out,
            )
            self.assertEqual(p.returncode, 0, msg=p.stderr)
            self.assertTrue(os.path.exists(out))
            with open(out, encoding="utf-8") as f:
                payload = json.load(f)
            self.assertEqual(payload["schema_version"], SCHEMA_VERSION)

    def test_missing_parent_for_output_exits_1(self) -> None:
        p = self._run_cli(
            "--artifact", self.artifact_path,
            "--output", "/nonexistent/sub/shadow.json",
        )
        # The framework raises ShadowRunError, CLI maps to exit 1.
        self.assertEqual(p.returncode, 1)
        self.assertIn("parent directory", p.stderr)

    def test_drift_via_replay_config_hash(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "shadow.json")
            p = self._run_cli(
                "--artifact", self.artifact_path,
                "--replay-config-hash", "cfg-v2",
                "--output", out,
            )
            self.assertEqual(p.returncode, 0, msg=p.stderr)
            with open(out, encoding="utf-8") as f:
                payload = json.load(f)
            self.assertEqual(
                payload["summary"]["total_changed_count"], 3
            )
            self.assertEqual(
                payload["summary"]["total_unchanged_count"], 0
            )
            self.assertEqual(
                len(payload["explain_score_refs"]), 3
            )

    def test_artifact_load_failure_exits_1(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            f.write("not a json object")
            bad_path = f.name
        try:
            p = self._run_cli("--artifact", bad_path)
            self.assertEqual(p.returncode, 1)
            self.assertIn("shadow-run failed", p.stderr)
        finally:
            os.unlink(bad_path)


# ---------------------------------------------------------------------------
# 9) explain_score_ref round-trip / structural stability
# ---------------------------------------------------------------------------


class ExplainRefStabilityTests(unittest.TestCase):
    """``explain_score_refs`` must be a stable list of
    ``score:<scorer_type>:<entity_id>:<date_bucket>`` strings
    that match the artifact's decisions. Dedupe is not
    required for Run 1 (each decision is unique), but the
    format must be stable across re-runs."""

    def setUp(self) -> None:
        self.artifact = _build_artifact()
        self.path = _write_artifact_to_temp(self.artifact)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def test_refs_match_artifact_score_ids(self) -> None:
        cfg = ShadowRunConfig(
            artifact_path=self.path,
            replay_config_hash="cfg-v2",
        )
        result = run_shadow(cfg)
        artifact_ids = {
            eh["score_node_id"] for eh in self.artifact["result"]["evidence_handles"]
        }
        self.assertEqual(set(result.explain_score_refs), artifact_ids)

    def test_no_refs_when_determinism(self) -> None:
        cfg = ShadowRunConfig(artifact_path=self.path)
        result = run_shadow(cfg)
        self.assertEqual(result.explain_score_refs, ())

    def test_result_to_dict_summary_count(self) -> None:
        cfg = ShadowRunConfig(
            artifact_path=self.path,
            replay_config_hash="cfg-v2",
        )
        result = run_shadow(cfg)
        d = result.to_dict()
        self.assertEqual(
            d["summary"]["explain_score_ref_count"],
            len(result.explain_score_refs),
        )
        self.assertEqual(
            d["summary"]["explain_score_ref_count"],
            d["summary"]["total_changed_count"],
        )


if __name__ == "__main__":
    unittest.main()
