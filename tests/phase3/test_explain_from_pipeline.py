"""Tests for Phase 4 Task 3B Run 2 — ``--from-pipeline --pipeline-artifact``.

Scope (per Run 2 brief):
  - The new ``phase3.graph.explain_from_pipeline`` adapter loads a
    JSON artifact produced by ``build_json_export`` and rebuilds a
    ``PipelineResult`` + a fresh in-memory ``GraphStore`` from it.
  - The CLI subcommand ``explain-score`` honors
    ``--from-pipeline --pipeline-artifact PATH`` as a real
    integration path (not a no-op hint), threading the rebuilt
    ``PipelineResult`` into ``explain_score`` and propagating
    ``run_id`` / ``config_hash`` / ``date_bucket`` into
    ``score_metadata``.
  - The output is deterministic across repeated invocations
    (JSON and Markdown views).
  - The case-insensitive ``macro_history.db`` basename guard
    remains in force for the new flag.
  - Direct ``--node`` mode is preserved unchanged.
  - Run 1 back-compat (``--from-pipeline`` without
    ``--pipeline-artifact`` is a no-op hint) is preserved.

Design constraints:
  - stdlib ``unittest`` only (no pytest).
  - Reuse the existing ``explain_score`` / ``build_json_export``
    / ``IntelligencePipeline`` APIs.
  - No production DB writes; tests use the in-memory store and
    a temp SQLite store built by the test helper.
  - No ``intelligence.db*`` artifacts created in the repo.
  - No mutations to ``macro_history.db``.

These tests are additive — the existing ~1072-test regression
must still pass after this file lands.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_FIE_REPO_ROOT = str(Path(__file__).resolve().parents[2])
from phase3.datamodel.graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
)
from phase3.graph import GraphStore, GraphStoreLike
from phase3.graph.explain_from_pipeline import (
    ExplainPipelineArtifactError,
    PipelineEnvelope,
    SCHEMA_VERSION,
    load_pipeline_envelope,
)


# ---------------------------------------------------------------------------
# Shared fixture: build a real IntelligenceRunResult, export to a temp
# JSON artifact, and stash the path + run_id for tests to consume.
# ---------------------------------------------------------------------------


_REPO = str(Path(__file__).resolve().parents[2])
_PYTHONPATH_ENV = {**os.environ, "PYTHONPATH": _REPO}
_BUILD_HARNESS = r'''
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, {repo!r})
from tests.phase3.test_intelligence_pipeline import (
    DATE,
    CONFIG_HASH,
    _new_store,
    _seed_company_signals,
    _build_intelligence_pipeline,
)
from phase3.persistence.signal_repo import SignalRepository
from phase3.pipeline.intelligence_pipeline import IntelligencePipelineConfig
from phase3.pipeline.reporting import build_json_export

db_path, store = _new_store()
signal_repo = SignalRepository(store)
_seed_company_signals(signal_repo, "2330")
orchestrator, _, _ = _build_intelligence_pipeline(
    store, scoring_kwargs={{"dry_run": True}}
)
cfg = IntelligencePipelineConfig(
    date_bucket=DATE,
    industry_ids=(),
    company_specs=({{"code": "2330", "name": "TSMC", "sector": "Technology"}},),
    run_macro=False,
    persist=False,
    config_hash=CONFIG_HASH,
)
result = orchestrator.run(cfg)
out_dir = Path(tempfile.mkdtemp(prefix="explain_from_pipeline_"))
artifact = out_dir / "report.json"
envelope = build_json_export(
    result=result, config=None, generated_at="2026-07-11T00:00:00Z"
)
artifact.write_text(
    json.dumps(envelope, sort_keys=True, default=str),
    encoding="utf-8",
)
print("ARTIFACT=" + str(artifact))
print("RUN_ID=" + str(result.run_id))
print("SCORE_NODE_ID=" + str(result.graph_writes[0].score_node_id))
print("CONFIG_HASH=" + str(result.config_hash))
print("DATE_BUCKET=" + str(result.date_bucket))
'''.format(repo=_REPO)


def _build_real_artifact() -> dict[str, str]:
    """Build a real Phase 3 intelligence artifact via the pipeline.

    Returns a dict with the absolute artifact path + the canonical
    score node id the artifact carries. The artifact is written
    under ``/tmp``; nothing is created inside the repo.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _BUILD_HARNESS],
        capture_output=True, text=True, env=_PYTHONPATH_ENV, timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"harness failed (rc={proc.returncode}): {proc.stderr[:1500]}"
        )
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


# ---------------------------------------------------------------------------
# 1) Adapter unit tests
# ---------------------------------------------------------------------------


class AdapterImportTests(unittest.TestCase):
    """The adapter module's public symbols are importable and the
    ``SCHEMA_VERSION`` is a non-empty string.
    """

    def test_schema_version_is_nonempty_string(self) -> None:
        self.assertIsInstance(SCHEMA_VERSION, str)
        self.assertGreater(len(SCHEMA_VERSION), 0)

    def test_load_pipeline_envelope_is_callable(self) -> None:
        self.assertTrue(callable(load_pipeline_envelope))

    def test_pipeline_envelope_is_dataclass(self) -> None:
        from dataclasses import is_dataclass
        self.assertTrue(is_dataclass(PipelineEnvelope))

    def test_error_is_exception_subclass(self) -> None:
        self.assertTrue(
            issubclass(ExplainPipelineArtifactError, Exception)
        )


class AdapterHappyPathTests(unittest.TestCase):
    """``load_pipeline_envelope`` rebuilds a graph + pipeline result
    from a valid artifact and propagates the canonical metadata.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = _build_real_artifact()
        cls.artifact = Path(cls.fixture["ARTIFACT"])
        cls.score_id = cls.fixture["SCORE_NODE_ID"]
        cls.run_id = cls.fixture["RUN_ID"]
        cls.config_hash = cls.fixture["CONFIG_HASH"]
        cls.date_bucket = cls.fixture["DATE_BUCKET"]

    def test_loads_envelope(self) -> None:
        env = load_pipeline_envelope(self.artifact)
        self.assertIsInstance(env, PipelineEnvelope)
        self.assertEqual(env.run_id, self.run_id)
        self.assertEqual(env.config_hash, self.config_hash)
        self.assertEqual(env.date_bucket, self.date_bucket)
        self.assertFalse(env.persisted)
        self.assertEqual(env.score_node_id, self.score_id)

    def test_rebuilds_graph_with_score_and_signals(self) -> None:
        env = load_pipeline_envelope(self.artifact)
        self.assertGreater(env.graph.node_count(), 0)
        self.assertGreater(env.graph.edge_count(), 0)
        self.assertTrue(env.graph.has_node(self.score_id))
        # The artifact carries 6 company signals (roe, peg, pm1m,
        # beta, news, sent) per the test harness — they should all
        # be materialized as signal nodes.
        signal_ids = [
            n.node_id for n in env.graph.query_nodes(node_type=NodeType.SIGNAL)
        ]
        self.assertEqual(len(signal_ids), 6)

    def test_pipeline_result_score_metadata_set(self) -> None:
        env = load_pipeline_envelope(self.artifact)
        # The reconstructed PipelineResult should expose the
        # canonical scorer metadata so explain_score can project it
        # into score_metadata.
        pr = env.pipeline_result
        self.assertIsNotNone(pr)
        self.assertIsNotNone(pr.score.breakdown)
        self.assertEqual(
            pr.score.breakdown.config_hash, self.config_hash,
        )
        self.assertEqual(
            pr.score.breakdown.scorer_type, "company",
        )
        self.assertEqual(
            pr.score.breakdown.entity_id, "entity:company:2330",
        )

    def test_explicit_score_node_id(self) -> None:
        env = load_pipeline_envelope(
            self.artifact, score_node_id=self.score_id,
        )
        self.assertEqual(env.score_node_id, self.score_id)

    def test_determinism_across_repeated_loads(self) -> None:
        env1 = load_pipeline_envelope(self.artifact)
        env2 = load_pipeline_envelope(self.artifact)
        d1 = hashlib.md5(
            json.dumps(env1.to_dict(), sort_keys=True, default=str).encode()
        ).hexdigest()
        d2 = hashlib.md5(
            json.dumps(env2.to_dict(), sort_keys=True, default=str).encode()
        ).hexdigest()
        self.assertEqual(d1, d2)


class AdapterErrorPathTests(unittest.TestCase):
    """The adapter surfaces typed errors for every failure mode
    the brief requires.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = _build_real_artifact()
        cls.artifact = Path(cls.fixture["ARTIFACT"])
        cls.score_id = cls.fixture["SCORE_NODE_ID"]

    def test_missing_artifact(self) -> None:
        with self.assertRaises(ExplainPipelineArtifactError) as cm:
            load_pipeline_envelope(Path("/tmp/definitely_missing_run2.json"))
        msg = str(cm.exception).lower()
        self.assertIn("not found", msg)

    def test_invalid_json(self) -> None:
        tmp = tempfile.NamedTemporaryFile(
            delete=False, suffix=".json", mode="w",
            prefix="bad_artifact_",
        )
        tmp.write("{not valid json")
        tmp.close()
        try:
            with self.assertRaises(ExplainPipelineArtifactError) as cm:
                load_pipeline_envelope(Path(tmp.name))
            self.assertIn("json", str(cm.exception).lower())
        finally:
            os.unlink(tmp.name)

    def test_unknown_score_id(self) -> None:
        with self.assertRaises(ExplainPipelineArtifactError) as cm:
            load_pipeline_envelope(
                self.artifact, score_node_id="score:nope:not-there",
            )
        self.assertIn("not present", str(cm.exception).lower())


# ---------------------------------------------------------------------------
# 2) CLI integration tests
# ---------------------------------------------------------------------------


class CliIntegrationTests(unittest.TestCase):
    """The ``explain-score`` CLI subcommand honors
    ``--from-pipeline --pipeline-artifact PATH`` as a real
    integration path.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = _build_real_artifact()
        cls.artifact = cls.fixture["ARTIFACT"]
        cls.run_id = cls.fixture["RUN_ID"]
        cls.config_hash = cls.fixture["CONFIG_HASH"]
        cls.date_bucket = cls.fixture["DATE_BUCKET"]
        cls.score_id = cls.fixture["SCORE_NODE_ID"]

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable,
             _FIE_REPO_ROOT + "/phase3/cli.py",
             "explain-score", *args],
            capture_output=True, text=True,
            env=_PYTHONPATH_ENV, timeout=30,
        )

    def test_from_pipeline_with_artifact_returns_envelope(self) -> None:
        result = self._run(
            "--from-pipeline", "--pipeline-artifact", self.artifact,
            "--json",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        parsed = json.loads(result.stdout)
        # The new top-level ``pipeline_envelope`` block surfaces
        # the run_id / config_hash / date_bucket / persisted /
        # artifact_path that ``explain_score`` would otherwise
        # have no way to see from a bare graph query.
        env = parsed.get("pipeline_envelope")
        self.assertIsNotNone(env)
        self.assertEqual(env["run_id"], self.run_id)
        self.assertEqual(env["config_hash"], self.config_hash)
        self.assertEqual(env["date_bucket"], self.date_bucket)
        self.assertFalse(env["persisted"])
        self.assertEqual(env["artifact_path"], self.artifact)
        # The explained score itself should be well-formed.
        self.assertTrue(parsed["present"])
        self.assertGreaterEqual(parsed["depth_reached"], 1)
        # And the score metadata should carry the propagated
        # run_id + config_hash from the artifact.
        md = parsed.get("score_metadata", {})
        self.assertEqual(md.get("run_id"), self.run_id)
        self.assertEqual(md.get("config_hash"), self.config_hash)

    def test_from_pipeline_with_explicit_node(self) -> None:
        result = self._run(
            "--from-pipeline", "--pipeline-artifact", self.artifact,
            "--node", self.score_id, "--json",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed["score_node_id"], self.score_id)
        self.assertEqual(
            parsed["pipeline_envelope"]["run_id"], self.run_id,
        )

    def test_from_pipeline_with_bad_node_returns_nonzero(self) -> None:
        result = self._run(
            "--from-pipeline", "--pipeline-artifact", self.artifact,
            "--node", "score:nope", "--json",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not present", result.stderr.lower())

    def test_from_pipeline_alone_is_run1_backcompat(self) -> None:
        # Run 1 back-compat: --from-pipeline without
        # --pipeline-artifact prints a hint and still completes.
        result = self._run("--from-pipeline", "--json")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("--from-pipeline", result.stderr)
        parsed = json.loads(result.stdout)
        # And the pipeline_envelope block should be absent (no
        # artifact was loaded).
        self.assertNotIn("pipeline_envelope", parsed)

    def test_from_pipeline_with_missing_artifact(self) -> None:
        result = self._run(
            "--from-pipeline", "--pipeline-artifact",
            "/tmp/nope_run2_xxx.json", "--json",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not found", result.stderr.lower())

    def test_from_pipeline_with_db_path_is_mutually_exclusive(self) -> None:
        result = self._run(
            "--from-pipeline", "--pipeline-artifact", self.artifact,
            "--db-path", "/tmp/some.db", "--json",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("mutually exclusive", result.stderr.lower())

    def test_from_pipeline_with_macro_history_db_refused(self) -> None:
        # The case-insensitive basename guard must remain in force
        # for the new --pipeline-artifact path.
        tmpdir = tempfile.mkdtemp()
        guarded = os.path.join(tmpdir, "macro_history.db")
        try:
            result = self._run(
                "--from-pipeline", "--pipeline-artifact", guarded,
                "--json",
            )
            self.assertNotEqual(result.returncode, 0)
            combined = (result.stdout + result.stderr).lower()
            self.assertIn("macro_history.db", combined)
            self.assertIn("case-insensitive", combined)
        finally:
            try:
                os.unlink(guarded)
            except FileNotFoundError:
                pass
            os.rmdir(tmpdir)

    def test_from_pipeline_case_bypass_refused(self) -> None:
        # Case-bypass hardening: ``Macro_History.db`` must still
        # trip the guard.
        tmpdir = tempfile.mkdtemp()
        guarded = os.path.join(tmpdir, "Macro_History.db")
        with open(guarded, "wb") as f:
            f.write(b"")
        try:
            result = self._run(
                "--from-pipeline", "--pipeline-artifact", guarded,
                "--json",
            )
            self.assertNotEqual(result.returncode, 0)
            combined = (result.stdout + result.stderr).lower()
            self.assertIn("macro_history.db", combined)
            self.assertIn("case-insensitive", combined)
        finally:
            os.unlink(guarded)
            os.rmdir(tmpdir)

    def test_from_pipeline_deterministic_json(self) -> None:
        # Two consecutive CLI invocations against the same artifact
        # must produce byte-identical JSON output. The pinned
        # ``created_at`` / ``valid_until`` in the adapter prevents
        # the natural ``datetime.now()`` injection.
        r1 = self._run(
            "--from-pipeline", "--pipeline-artifact", self.artifact,
            "--json",
        )
        r2 = self._run(
            "--from-pipeline", "--pipeline-artifact", self.artifact,
            "--json",
        )
        self.assertEqual(r1.returncode, 0)
        self.assertEqual(r2.returncode, 0)
        h1 = hashlib.md5(r1.stdout.encode()).hexdigest()
        h2 = hashlib.md5(r2.stdout.encode()).hexdigest()
        self.assertEqual(h1, h2)

    def test_from_pipeline_deterministic_markdown(self) -> None:
        # Same determinism contract for the Markdown view.
        r1 = self._run(
            "--from-pipeline", "--pipeline-artifact", self.artifact,
        )
        r2 = self._run(
            "--from-pipeline", "--pipeline-artifact", self.artifact,
        )
        self.assertEqual(r1.returncode, 0)
        self.assertEqual(r2.returncode, 0)
        h1 = hashlib.md5(r1.stdout.encode()).hexdigest()
        h2 = hashlib.md5(r2.stdout.encode()).hexdigest()
        self.assertEqual(h1, h2)

    def test_from_pipeline_json_md_parity(self) -> None:
        # The JSON and Markdown views must agree on the headline
        # fields the explain-score contract promises.
        r_json = self._run(
            "--from-pipeline", "--pipeline-artifact", self.artifact,
            "--json",
        )
        r_md = self._run(
            "--from-pipeline", "--pipeline-artifact", self.artifact,
        )
        self.assertEqual(r_json.returncode, 0)
        self.assertEqual(r_md.returncode, 0)
        j = json.loads(r_json.stdout)
        # The Markdown header line must include the same
        # present=True / depth_reached=N status that the JSON
        # body asserts.
        self.assertIn("present=True", r_md.stdout)
        self.assertIn(
            f"depth_reached={j['depth_reached']}",
            r_md.stdout,
        )
        # And the score_node_id should match.
        self.assertIn(j["score_node_id"], r_md.stdout)

    def test_from_pipeline_default_node_picks_first_score(self) -> None:
        # When the user does not pass --node, the adapter should
        # pick the artifact's first score. The argparse default
        # is a sentinel that is detected and overridden.
        result = self._run(
            "--from-pipeline", "--pipeline-artifact", self.artifact,
            "--json",
        )
        self.assertEqual(result.returncode, 0)
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed["score_node_id"], self.score_id)


# ---------------------------------------------------------------------------
# 3) Backward-compat: direct --node mode (no --from-pipeline)
# ---------------------------------------------------------------------------


class DirectNodeModeTests(unittest.TestCase):
    """The original ``explain-score --node ... --db-path ...`` path
    must remain unchanged: the Run 2 changes only activate when
    ``--from-pipeline --pipeline-artifact`` is given.
    """

    def test_explain_score_help_lists_pipeline_artifact(self) -> None:
        result = subprocess.run(
            [sys.executable,
             _FIE_REPO_ROOT + "/phase3/cli.py",
             "explain-score", "--help"],
            capture_output=True, text=True, env=_PYTHONPATH_ENV, timeout=15,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--from-pipeline", result.stdout)
        self.assertIn("--pipeline-artifact", result.stdout)


if __name__ == "__main__":
    unittest.main()
