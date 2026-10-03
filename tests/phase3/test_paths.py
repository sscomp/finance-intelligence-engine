"""Tests for the Phase 6.1 central path boundary (``phase3.paths``).

Workstream B acceptance: every runtime path resolves through the
boundary with the documented precedence

    explicit CLI/API override  >  FIE_* env var  >  portable default

where the portable default is derived from the module's own repository
location — never from a user's home directory or a fixed venv.
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from phase3 import paths


REPO_ROOT = Path(__file__).resolve().parents[2]

_ENV_KEYS = (
    paths.ENV_PROJECT_ROOT, paths.ENV_DATA_DIR, paths.ENV_CONFIG_DIR,
    paths.ENV_ARTIFACT_DIR, paths.ENV_DB_PATH,
)


class EnvSandbox(unittest.TestCase):
    """Base that saves/restores all FIE_* env vars around each test."""

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
            if self._saved[k] is not None:
                os.environ[k] = self._saved[k]


class PathsDiscoveryTests(EnvSandbox):
    def test_default_project_root_is_this_checkout(self) -> None:
        # No env override: discovery must land on the checkout that holds
        # the imported phase3 package (NOT any operator's home directory).
        self.assertEqual(paths.project_root(), REPO_ROOT)

    def test_env_project_root_overrides_discovery(self) -> None:
        sandbox = "/tmp/fie-paths-sandbox-root"
        os.environ[paths.ENV_PROJECT_ROOT] = sandbox
        self.assertEqual(paths.project_root(), Path(sandbox))

    def test_data_config_dirs_default_to_project_root(self) -> None:
        for fn in (paths.data_dir, paths.config_dir):
            default = fn()
            self.assertTrue(default.is_absolute())
            self.assertEqual(default, paths.project_root())

    def test_artifact_dir_default_layout(self) -> None:
        self.assertEqual(
            paths.artifact_dir(),
            paths.project_root() / "metadata" / "reports" / "artifacts",
        )

    def test_db_path_precedence_env_over_default(self) -> None:
        # Default: <data_dir>/macro_history.db
        self.assertEqual(paths.macro_history_db_path(),
                         paths.data_dir() / "macro_history.db")
        # Env override wins over the portable default
        custom = "/tmp/fie-paths-sandbox/hist.db"
        os.environ[paths.ENV_DB_PATH] = custom
        self.assertEqual(paths.macro_history_db_path(), Path(custom))

    def test_data_dir_env_override_propagates_to_db_default(self) -> None:
        sandbox = "/tmp/fie-paths-sandbox-data"
        os.environ[paths.ENV_DATA_DIR] = sandbox
        self.assertEqual(paths.macro_history_db_path(),
                         Path(sandbox) / "macro_history.db")


if __name__ == "__main__":
    unittest.main()