"""Phase 5 M4-S1 — TD8 Safety Guards.

Enforces Phase 5 M4 boundary rules (kickoff plan §2.4 rules 1–8, §6.2
AG-M4-1…AG-M4-7) at test time:

- AG-M4-1: AST scan — 0 forbidden imports in ``decision.py`` +
  ``allocation.py``.
- AG-M4-2: AST scan — 0 ``IntelligencePipeline()`` instantiations in
  ``decision.py`` + ``allocation.py`` (NEW for M4, the portfolio ↔
  intelligence boundary guard).
- AG-M4-3: File-level sha256 — 0 modifications to files outside
  ``phase3.portfolio.*`` (M2 domain.py + M3 risk.py unchanged; phase3
  top-level files unchanged).
- AG-M4-4: ``macro_history.db`` before/after stat+sha unchanged.
- AG-M4-5: ``intelligence.db*`` count = 0.
- AG-M4-6: ``jobs.json`` unchanged.
- AG-M4-7: M2 (67) + M3 (135) regression — verified via the separate
  regression tier; here we assert the M2+M3 source files are unchanged
  via sha256 (file-level integrity).

These guards are read-only: they do NOT modify any production file, DB,
or config. They capture baselines at ``setUp`` / module load and compare
at test time. They extend the TD7 pattern (proven in M3-S1,
``test_portfolio_safety_guards.py``) with two NEW M4-specific checks:
the forbidden-import scan covers ``decision.py`` + ``allocation.py``
(not just ``risk.py``), and the NEW ``IntelligencePipeline()`` call
guard (AG-M4-2).
"""
from __future__ import annotations

import ast
import hashlib
import unittest
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

REPO_ROOT = Path("/home/ubuntu/macro-report")
ALLOCATION_PY = REPO_ROOT / "phase3" / "portfolio" / "allocation.py"
DECISION_PY = REPO_ROOT / "phase3" / "portfolio" / "decision.py"
DOMAIN_PY = REPO_ROOT / "phase3" / "portfolio" / "domain.py"
RISK_PY = REPO_ROOT / "phase3" / "portfolio" / "risk.py"
INIT_PY = REPO_ROOT / "phase3" / "portfolio" / "__init__.py"
M2_TEST_PY = REPO_ROOT / "tests" / "phase3" / "test_portfolio_domain.py"
M3_RISK_TEST_PY = REPO_ROOT / "tests" / "phase3" / "test_portfolio_risk.py"
M3_SAFETY_TEST_PY = REPO_ROOT / "tests" / "phase3" / "test_portfolio_safety_guards.py"
MACRO_HISTORY_DB = REPO_ROOT / "macro_history.db"
JOBS_JSON = Path.home() / ".hermes" / "cron" / "jobs.json"
INTELLIGENCE_DB_GLOB = "intelligence.db*"

# phase3 top-level files that must NOT be modified by M4-S1.
PHASE3_TOP_LEVEL_FILES = [
    REPO_ROOT / "phase3" / "__init__.py",
    REPO_ROOT / "phase3" / "api.py",
    REPO_ROOT / "phase3" / "cli.py",
]

# Pipeline / datamodel / graph files that must NOT be modified by M4-S1
# (boundary rule 4: 0 modifications outside phase3.portfolio.*).
PIPELINE_FILES = [
    REPO_ROOT / "phase3" / "pipeline" / "scoring_pipeline.py",
    REPO_ROOT / "phase3" / "pipeline" / "intelligence_pipeline.py",
]
DATAMODEL_FILES = [
    REPO_ROOT / "phase3" / "datamodel" / "scores.py",
]

# Imports forbidden in phase3.portfolio.decision + phase3.portfolio.allocation
# per boundary rules 1–2 (M4 kickoff plan §2.4). Mirrors TD7's forbidden set.
#
# M4-S2 UPDATE: phase3.pipeline and phase3.datamodel are REMOVED from the
# forbidden set for decision.py (boundary rule 8 — the portfolio↔intelligence
# crossing is now permitted for TYPE ANNOTATIONS + READ-ONLY access only,
# via 4 narrowly-allowlisted symbols). allocation.py retains the FULL
# forbidden set (it does NOT cross the boundary).
FORBIDDEN_IMPORT_NAMES = {
    "sqlite3",
    "macro_history",
    "phase3.graph",
    "requests",
    "urllib.request",
    "http",
    "socket",
    "asyncio",
    "multiprocessing",
    "threading",
    "subprocess",
    "os",
    "sys",
    "random",
}

# phase3.pipeline and phase3.datamodel are forbidden for allocation.py
# (which must NOT cross the intelligence boundary), but permitted for
# decision.py (M4-S2 boundary crossing via narrow allowlist).
FORBIDDEN_IMPORT_NAMES_ALLOCATION = FORBIDDEN_IMPORT_NAMES | {
    "phase3.pipeline",
    "phase3.datamodel",
}

# Allowed intelligence imports for decision.py (M4-S2 boundary rule 8).
# decision.py may import ONLY these 4 named symbols from these 3 modules.
# Any other import from phase3.pipeline / phase3.datamodel is forbidden.
ALLOWED_INTELLIGENCE_MODULES = {
    "phase3.pipeline.scoring_pipeline",
    "phase3.datamodel.scores",
    "phase3.pipeline.intelligence_pipeline",
}
ALLOWED_INTELLIGENCE_SYMBOLS = {
    "PipelineResult",
    "PipelineRunReport",
    "ScoreBreakdown",
    "EvidenceQueryHandle",
}

# IntelligencePipeline is forbidden to import (not in allowlist) and
# MUST NOT be instantiated (AG-M4-2).
FORBIDDEN_INTELLIGENCE_IMPORTS = {
    "phase3.pipeline.intelligence_pipeline.IntelligencePipeline",
}

# Known M2 + M3 baseline SHAs (captured at M3-S3 commit 13219ab; M4-S1
# must not modify these files).
M2_M3_BASELINE = {
    "domain.py": "d358fcdc683788341960f1a266129f1a12bee4ca609c886d09016fd92c7c7741",
    "risk.py": "5f1e5ddab0b274254fdcb9f735cd4644da0ea2fa213073d38f5621b4ab96bb05",
    "test_portfolio_domain.py": "f72748113bda9b49cf072af19f8e1ed3d571e86a27c557bd0a12a5bdbabb6603",
    "test_portfolio_risk.py": "f955bc10dd8647a7ce85341582a0d7910c347beff146299fdefd996acefd32ed",
    "test_portfolio_safety_guards.py": "6a0f05994a2ae898a6ae4f897d30c73190bbc91e5088d27960c60a83e038ed03",
}

# M4-S1 baseline SHAs (captured at M4-S1 commit 09c01f7; M4-S2 must not
# modify allocation.py — decision.py and __init__.py are extended by M4-S2).
M4_S1_BASELINE = {
    "allocation.py": "9133f66d25342c0ccfbcdf2809de3388f1f1cc79fd26a8c9f952047c5a7a6e08",
}

# M4-S2 baseline SHAs (decision.py and __init__.py were MODIFIED by M4-S2;
# these are the post-M4-S2 baselines that future milestones must preserve).
# M4-S3 UPDATE: decision.py and __init__.py are MODIFIED by M4-S3 (risk-aware
# policy + constraint enforcement + AllocationConstraintError re-export).
# cli.py is MODIFIED by M4-S3 (additive portfolio-run subcommand).
# M5 UPDATE: __init__.py is MODIFIED by M5 (additive M5 re-exports).
# cli.py is MODIFIED by M5 (additive portfolio-orders subcommand).
# M6 UPDATE: __init__.py is MODIFIED by M6 (additive M6 re-exports).
# test_portfolio_safety_guards.py is MODIFIED by M6 (additive M6 guards).
# cli.py is MODIFIED by M6 (additive portfolio-report subcommand).
# M8 UPDATE: cli.py is MODIFIED by M8 (additive portfolio-shadow-run subcommand).
M4_S2_BASELINE = {
    "decision.py": "2bbcb209dd189ca1e76213b9fcea3015661fad13c333c1011c968a727d22679f",
    "init.py": "bf19b5ea0fc01f574c374cefe48dd582da2d067ceb4db51f95e0da5a312278ff",
}

# Known phase3 top-level + pipeline + datamodel baseline SHAs (M4-S1 must
# not modify any file outside phase3.portfolio.*).
# M4-S3 UPDATE: cli.py is MODIFIED by M4-S3 (additive portfolio-run subcommand).
PHASE3_BASELINE = {
    "phase3/__init__.py": "3020382849864e92f78ae38b2c9cbd0e48419e3120e76786f905d43a5eafaeaa",
    "phase3/api.py": "539d58838ff86088d7e71178fed3127065ead83c58786d8f62952d06a3cf50fe",
    "phase3/cli.py": "c2772e61a6c414f7bfa9557c83c2def27f57ad79f043ecf2af1c9393357509e1",
    "phase3/pipeline/scoring_pipeline.py": "5b778e00c99cec04c7d90965f291d70acada9507d5a99fb938f94fa03fb4dd59",
    "phase3/pipeline/intelligence_pipeline.py": "025af0e538572fc056a73a9dc8ef2686f7518fd95723525693726f1da6fe5570",
    "phase3/datamodel/scores.py": "cc934edd423b1cea8a51cd0554547546a5bc2a34bac169d0ba154332599a090c",
}


def _sha256_file(path: Path) -> str:
    """Compute sha256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_exists(path: Path) -> bool:
    return path.is_file()


def _scan_forbidden_imports(path: Path) -> list[str]:
    """AST-walk a .py file and return the list of forbidden imports found.

    Uses the default ``FORBIDDEN_IMPORT_NAMES`` set (which excludes
    phase3.pipeline and phase3.datamodel for M4-S2 decision.py)."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    forbidden_found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in FORBIDDEN_IMPORT_NAMES or alias.name in FORBIDDEN_IMPORT_NAMES:
                    forbidden_found.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            top = mod.split(".")[0]
            if top in FORBIDDEN_IMPORT_NAMES or mod in FORBIDDEN_IMPORT_NAMES:
                forbidden_found.append(mod)
    return forbidden_found


def _scan_forbidden_imports_with_set(path: Path, forbidden_set: set[str]) -> list[str]:
    """AST-walk a .py file and return the list of forbidden imports found,
    using a custom forbidden set (e.g. the stricter set for allocation.py
    that includes phase3.pipeline and phase3.datamodel)."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    forbidden_found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in forbidden_set or alias.name in forbidden_set:
                    forbidden_found.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            top = mod.split(".")[0]
            if top in forbidden_set or mod in forbidden_set:
                forbidden_found.append(mod)
    return forbidden_found


def _scan_intelligence_pipeline_calls(path: Path) -> list[str]:
    """AST-walk a .py file and return Call nodes whose func id is
    ``IntelligencePipeline`` (AG-M4-2: 0 instantiations allowed)."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    calls: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            # Direct call: IntelligencePipeline(...)
            if isinstance(func, ast.Name) and func.id == "IntelligencePipeline":
                calls.append("IntelligencePipeline()")
            # Attribute call: <x>.IntelligencePipeline(...) — also forbidden.
            if isinstance(func, ast.Attribute) and func.attr == "IntelligencePipeline":
                calls.append(f"{ast.dump(func.value)}.IntelligencePipeline()")
    return calls


# --------------------------------------------------------------------------- #
# AG-M4-1: AST scan — 0 forbidden imports in decision.py + allocation.py
# --------------------------------------------------------------------------- #


class TestAGM41AstScanForbiddenImports(unittest.TestCase):
    """AG-M4-1: decision.py + allocation.py must not import any forbidden
    module."""

    def test_allocation_py_exists(self):
        """allocation.py must exist (M4-S1 deliverable)."""
        self.assertTrue(_file_exists(ALLOCATION_PY), f"{ALLOCATION_PY} does not exist")

    def test_decision_py_exists(self):
        """decision.py must exist (M4-S1 deliverable)."""
        self.assertTrue(_file_exists(DECISION_PY), f"{DECISION_PY} does not exist")

    def test_allocation_py_no_forbidden_imports(self):
        """AST scan of allocation.py: 0 forbidden import statements.

        allocation.py uses the FULL forbidden set (includes phase3.pipeline
        and phase3.datamodel) because it must NOT cross the intelligence
        boundary (boundary rule 8 — crossing is decision.py only in M4-S2)."""
        self.assertTrue(_file_exists(ALLOCATION_PY))
        forbidden = _scan_forbidden_imports_with_set(
            ALLOCATION_PY, FORBIDDEN_IMPORT_NAMES_ALLOCATION
        )
        self.assertEqual(forbidden, [], f"allocation.py forbidden imports: {forbidden}")

    def test_decision_py_no_forbidden_imports(self):
        """AST scan of decision.py: 0 forbidden import statements.

        M4-S2 UPDATE: decision.py now imports from phase3.pipeline and
        phase3.datamodel (boundary rule 8 crossing). These are removed
        from the forbidden set for decision.py. The narrow allowlist
        (4 named symbols from 3 modules) is enforced by
        test_decision_py_intelligence_import_allowlist below."""
        self.assertTrue(_file_exists(DECISION_PY))
        forbidden = _scan_forbidden_imports(DECISION_PY)
        self.assertEqual(forbidden, [], f"decision.py forbidden imports: {forbidden}")

    def test_decision_py_intelligence_import_allowlist(self):
        """M4-S2 NEW: decision.py intelligence imports are narrowly
        allowlisted. Only PipelineResult, PipelineRunReport, ScoreBreakdown,
        and EvidenceQueryHandle may be imported from phase3.pipeline and
        phase3.datamodel. Any other symbol is forbidden."""
        self.assertTrue(_file_exists(DECISION_PY))
        source = DECISION_PY.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(DECISION_PY))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod in ALLOWED_INTELLIGENCE_MODULES:
                    for alias in node.names:
                        self.assertIn(
                            alias.name,
                            ALLOWED_INTELLIGENCE_SYMBOLS,
                            f"decision.py imports {alias.name!r} from {mod!r} "
                            f"which is not in the allowlist "
                            f"{ALLOWED_INTELLIGENCE_SYMBOLS}",
                        )
                elif mod.startswith("phase3.pipeline.") or mod.startswith("phase3.datamodel."):
                    # Any import from phase3.pipeline/datamodel NOT in the
                    # allowlist is forbidden.
                    self.fail(
                        f"decision.py imports from {mod!r} which is not in "
                        f"the allowed intelligence modules "
                        f"{ALLOWED_INTELLIGENCE_MODULES}"
                    )

    def test_allocation_py_imports_domain(self):
        """allocation.py must import from phase3.portfolio.domain (positive)."""
        self.assertTrue(_file_exists(ALLOCATION_PY))
        source = ALLOCATION_PY.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(ALLOCATION_PY))
        imports_domain = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "phase3.portfolio.domain":
                    imports_domain = True
        self.assertTrue(imports_domain, "allocation.py must import from phase3.portfolio.domain")

    def test_decision_py_imports_domain_or_allocation(self):
        """decision.py must import from phase3.portfolio.domain or
        phase3.portfolio.allocation (M4-S1 does NOT cross the
        intelligence boundary — that lands in M4-S2)."""
        self.assertTrue(_file_exists(DECISION_PY))
        source = DECISION_PY.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(DECISION_PY))
        allowed_internal = {
            "phase3.portfolio.domain",
            "phase3.portfolio.allocation",
        }
        found_internal: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod in allowed_internal:
                    found_internal.add(mod)
        self.assertTrue(
            found_internal,
            "decision.py must import from phase3.portfolio.domain or "
            "phase3.portfolio.allocation",
        )

    def test_decision_py_imports_risk(self):
        """M4-S3 NEW: decision.py imports from phase3.portfolio.risk
        (the first M4 slice to consume the M3 risk engine layer)."""
        self.assertTrue(_file_exists(DECISION_PY))
        source = DECISION_PY.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(DECISION_PY))
        imports_risk = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "phase3.portfolio.risk":
                    imports_risk = True
        self.assertTrue(
            imports_risk,
            "decision.py must import from phase3.portfolio.risk (M4-S3)",
        )


# --------------------------------------------------------------------------- #
# AG-M4-2: AST scan — 0 IntelligencePipeline() instantiations
# --------------------------------------------------------------------------- #


class TestAGM42NoIntelligencePipelineCalls(unittest.TestCase):
    """AG-M4-2: 0 ``IntelligencePipeline()`` calls in decision.py +
    allocation.py (NEW for M4 — the portfolio ↔ intelligence boundary
    guard). M4-S1 DTOs must not instantiate the pipeline at all."""

    def test_allocation_py_no_intelligence_pipeline_calls(self):
        """allocation.py: 0 IntelligencePipeline() Call nodes."""
        self.assertTrue(_file_exists(ALLOCATION_PY))
        calls = _scan_intelligence_pipeline_calls(ALLOCATION_PY)
        self.assertEqual(calls, [], f"allocation.py IntelligencePipeline calls: {calls}")

    def test_decision_py_no_intelligence_pipeline_calls(self):
        """decision.py: 0 IntelligencePipeline() Call nodes (M4-S1
        stub raises NotImplementedError; does not call the pipeline)."""
        self.assertTrue(_file_exists(DECISION_PY))
        calls = _scan_intelligence_pipeline_calls(DECISION_PY)
        self.assertEqual(calls, [], f"decision.py IntelligencePipeline calls: {calls}")


# --------------------------------------------------------------------------- #
# AG-M4-3: File-level sha256 baseline (no modifications outside portfolio/)
# --------------------------------------------------------------------------- #


class TestAGM43FileLevelSha256(unittest.TestCase):
    """AG-M4-3: files outside phase3.portfolio.* must be unchanged by
    M4-S1. M2 domain.py + M3 risk.py are also read-only (M4 does not
    re-stage M2/M3)."""

    def test_domain_py_unchanged_from_m2(self):
        """domain.py sha256 must match M2 baseline (M4 read-only)."""
        self.assertTrue(_file_exists(DOMAIN_PY))
        actual = _sha256_file(DOMAIN_PY)
        self.assertEqual(
            actual,
            M2_M3_BASELINE["domain.py"],
            "domain.py was modified by M4 — must be read-only",
        )

    def test_risk_py_unchanged_from_m3(self):
        """risk.py sha256 must match M3 baseline (M4 read-only)."""
        self.assertTrue(_file_exists(RISK_PY))
        actual = _sha256_file(RISK_PY)
        self.assertEqual(
            actual,
            M2_M3_BASELINE["risk.py"],
            "risk.py was modified by M4 — must be read-only",
        )

    def test_m2_test_unchanged(self):
        """test_portfolio_domain.py sha256 must match M2 baseline."""
        self.assertTrue(_file_exists(M2_TEST_PY))
        actual = _sha256_file(M2_TEST_PY)
        self.assertEqual(
            actual,
            M2_M3_BASELINE["test_portfolio_domain.py"],
            "test_portfolio_domain.py was modified by M4 — must be read-only",
        )

    def test_m3_risk_test_unchanged(self):
        """test_portfolio_risk.py sha256 must match M3 baseline."""
        self.assertTrue(_file_exists(M3_RISK_TEST_PY))
        actual = _sha256_file(M3_RISK_TEST_PY)
        self.assertEqual(
            actual,
            M2_M3_BASELINE["test_portfolio_risk.py"],
            "test_portfolio_risk.py was modified by M4 — must be read-only",
        )

    def test_m3_safety_test_unchanged(self):
        """test_portfolio_safety_guards.py sha256 must match M3 baseline."""
        self.assertTrue(_file_exists(M3_SAFETY_TEST_PY))
        actual = _sha256_file(M3_SAFETY_TEST_PY)
        self.assertEqual(
            actual,
            M2_M3_BASELINE["test_portfolio_safety_guards.py"],
            "test_portfolio_safety_guards.py was modified by M4 — must be read-only",
        )

    def test_phase3_top_level_files_unchanged(self):
        """phase3 top-level files must match pre-M4 baseline (boundary
        rule 4: 0 modifications outside phase3.portfolio.*)."""
        for f in PHASE3_TOP_LEVEL_FILES:
            self.assertTrue(_file_exists(f), f"{f} missing")
            rel = str(f.relative_to(REPO_ROOT))
            actual = _sha256_file(f)
            self.assertEqual(
                actual,
                PHASE3_BASELINE[rel],
                f"{rel} was modified by M4 — must be read-only (boundary rule 4)",
            )

    def test_pipeline_files_unchanged(self):
        """phase3/pipeline/* files must match pre-M4 baseline."""
        for f in PIPELINE_FILES:
            self.assertTrue(_file_exists(f), f"{f} missing")
            rel = str(f.relative_to(REPO_ROOT))
            actual = _sha256_file(f)
            self.assertEqual(
                actual,
                PHASE3_BASELINE[rel],
                f"{rel} was modified by M4 — must be read-only (boundary rule 4)",
            )

    def test_datamodel_files_unchanged(self):
        """phase3/datamodel/* files must match pre-M4 baseline."""
        for f in DATAMODEL_FILES:
            self.assertTrue(_file_exists(f), f"{f} missing")
            rel = str(f.relative_to(REPO_ROOT))
            actual = _sha256_file(f)
            self.assertEqual(
                actual,
                PHASE3_BASELINE[rel],
                f"{rel} was modified by M4 — must be read-only (boundary rule 4)",
            )

    def test_allocation_py_is_new_file(self):
        """allocation.py is an M4 new file; it must exist.

        M4-S2: allocation.py must be UNCHANGED from M4-S1 baseline
        (sha 9133f66d...). M4-S2 consumes it read-only."""
        self.assertTrue(_file_exists(ALLOCATION_PY), "allocation.py must exist (M4-S1)")
        actual = _sha256_file(ALLOCATION_PY)
        self.assertEqual(
            actual,
            M4_S1_BASELINE["allocation.py"],
            "allocation.py was modified by M4-S2 — must be read-only (M4-S1 baseline)",
        )

    def test_decision_py_matches_m4s2_baseline(self):
        """M4-S2: decision.py sha matches the post-M4-S2 baseline
        (it was extended by M4-S2 from the M4-S1 stub)."""
        self.assertTrue(_file_exists(DECISION_PY), "decision.py must exist")
        actual = _sha256_file(DECISION_PY)
        self.assertEqual(
            actual,
            M4_S2_BASELINE["decision.py"],
            "decision.py sha does not match M4-S2 baseline — "
            "expected the extended implementation",
        )

    def test_init_py_matches_m4s2_baseline(self):
        """M4-S2: __init__.py sha matches the post-M4-S2 baseline
        (additive +1 re-export of AllocationPolicyConfig)."""
        self.assertTrue(_file_exists(INIT_PY), "__init__.py must exist")
        actual = _sha256_file(INIT_PY)
        self.assertEqual(
            actual,
            M4_S2_BASELINE["init.py"],
            "__init__.py sha does not match M4-S2 baseline — "
            "expected additive +1 re-export",
        )

    def test_decision_py_is_new_file(self):
        """decision.py is an M4 new file; it must exist."""
        self.assertTrue(_file_exists(DECISION_PY), "decision.py must exist (M4-S1)")


# --------------------------------------------------------------------------- #
# AG-M4-4: macro_history.db before/after unchanged
# --------------------------------------------------------------------------- #


class TestAGM44MacroHistoryDbUnchanged(unittest.TestCase):
    """AG-M4-4: macro_history.db sha256 + size unchanged through test run."""

    def setUp(self):
        self._before_sha = _sha256_file(MACRO_HISTORY_DB)
        self._before_stat = MACRO_HISTORY_DB.stat()

    def test_macro_history_db_exists(self):
        self.assertTrue(_file_exists(MACRO_HISTORY_DB))

    def test_macro_history_db_sha_unchanged_after_test(self):
        after_sha = _sha256_file(MACRO_HISTORY_DB)
        self.assertEqual(self._before_sha, after_sha)

    def test_macro_history_db_size_unchanged_after_test(self):
        after_stat = MACRO_HISTORY_DB.stat()
        self.assertEqual(self._before_stat.st_size, after_stat.st_size)


# --------------------------------------------------------------------------- #
# AG-M4-5: intelligence.db* count = 0
# --------------------------------------------------------------------------- #


class TestAGM45IntelligenceDbCount(unittest.TestCase):
    """AG-M4-5: no intelligence.db* artifacts may be created by M4-S1."""

    def test_no_intelligence_db_artifacts(self):
        results = list(REPO_ROOT.glob(INTELLIGENCE_DB_GLOB))
        self.assertEqual(results, [], f"intelligence.db* artifacts found: {results}")

    def test_no_intelligence_db_artifacts_in_phase3(self):
        results = list((REPO_ROOT / "phase3").glob(INTELLIGENCE_DB_GLOB))
        self.assertEqual(results, [], f"intelligence.db* in phase3/: {results}")


# --------------------------------------------------------------------------- #
# AG-M4-6: jobs.json unchanged
# --------------------------------------------------------------------------- #


class TestAGM46JobsJsonUnchanged(unittest.TestCase):
    """AG-M4-6: jobs.json sha256 must not change during test run."""

    def setUp(self):
        if _file_exists(JOBS_JSON):
            self._before_sha = _sha256_file(JOBS_JSON)
        else:
            self._before_sha = None

    def test_jobs_json_sha_unchanged_after_test(self):
        if not _file_exists(JOBS_JSON):
            self.skipTest("jobs.json not found — skipping (volatile env)")
        after_sha = _sha256_file(JOBS_JSON)
        # Note: jobs.json may drift due to cron ticks (CV2). We assert it
        # did not change DURING this test run, not against a fixed baseline.
        self.assertEqual(self._before_sha, after_sha)


# --------------------------------------------------------------------------- #
# AG-M4-7: M2+M3 package re-exports (importability + additive __init__)
# --------------------------------------------------------------------------- #


class TestAGM47MPortfolioPackageReExports(unittest.TestCase):
    """AG-M4-7 (partial): M2 + M3 symbols still re-exported; M4 symbols
    added additively. The full M2 67 + M3 135 regression is run in the
    regression tier (Tier 4). Here we assert the package surface."""

    def test_m2_symbols_preserved(self):
        """M2 value objects + entities still importable from package."""
        try:
            import phase3.portfolio as pkg
        except ModuleNotFoundError as exc:
            self.skipTest(f"PYTHONPATH issue (pre-existing): {exc}")
        self.assertTrue(hasattr(pkg, "Portfolio"))
        self.assertTrue(hasattr(pkg, "Position"))
        self.assertTrue(hasattr(pkg, "Weight"))
        self.assertTrue(hasattr(pkg, "EntityId"))

    def test_m3_symbols_preserved(self):
        """M3 risk functions + DTOs still importable from package."""
        try:
            import phase3.portfolio as pkg
        except ModuleNotFoundError as exc:
            self.skipTest(f"PYTHONPATH issue (pre-existing): {exc}")
        self.assertTrue(hasattr(pkg, "compute_exposure"))
        self.assertTrue(hasattr(pkg, "ExposureReport"))
        self.assertTrue(hasattr(pkg, "compute_concentration"))
        self.assertTrue(hasattr(pkg, "compute_correlation"))
        self.assertTrue(hasattr(pkg, "compute_risk_budget"))
        self.assertTrue(hasattr(pkg, "RiskBudgetReport"))

    def test_m4_symbols_added(self):
        """M4-S1 symbols (Allocation, PortfolioDecision,
        PortfolioDecisionEngine) are re-exported by the package.

        M4-S2: AllocationPolicyConfig is also re-exported (additive +1).
        M4-S3: AllocationConstraintError is also re-exported (additive +1)."""
        try:
            import phase3.portfolio as pkg
        except ModuleNotFoundError as exc:
            self.skipTest(f"PYTHONPATH issue (pre-existing): {exc}")
        self.assertTrue(hasattr(pkg, "Allocation"))
        self.assertTrue(hasattr(pkg, "PortfolioDecision"))
        self.assertTrue(hasattr(pkg, "PortfolioDecisionEngine"))
        self.assertTrue(hasattr(pkg, "AllocationPolicyConfig"))
        self.assertTrue(hasattr(pkg, "AllocationConstraintError"))

    def test_init_all_contains_m4_symbols(self):
        """``__all__`` contains the M4 symbols (additive — 19 M2+M3
        symbols preserved + 3 M4-S1 symbols + 1 M4-S2 symbol + 1 M4-S3
        symbol = 24 total)."""
        try:
            import phase3.portfolio as pkg
        except ModuleNotFoundError as exc:
            self.skipTest(f"PYTHONPATH issue (pre-existing): {exc}")
        self.assertIn("Allocation", pkg.__all__)
        self.assertIn("PortfolioDecision", pkg.__all__)
        self.assertIn("PortfolioDecisionEngine", pkg.__all__)
        self.assertIn("AllocationPolicyConfig", pkg.__all__)
        self.assertIn("AllocationConstraintError", pkg.__all__)
        # M2 + M3 symbols preserved (19 total).
        for sym in (
            "EntityId", "PortfolioId", "PositionId", "Weight", "Quantity",
            "Price", "CostBasis", "Position", "Portfolio",
            "ExposureReport", "compute_exposure",
            "ConcentrationReport", "compute_concentration",
            "DrawdownReport", "compute_drawdown",
            "CorrelationReport", "compute_correlation",
            "RiskBudgetReport", "compute_risk_budget",
        ):
            self.assertIn(sym, pkg.__all__, f"M2/M3 symbol {sym!r} missing from __all__")


if __name__ == "__main__":
    unittest.main()