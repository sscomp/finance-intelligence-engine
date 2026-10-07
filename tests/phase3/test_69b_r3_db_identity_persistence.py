"""Phase 6.9B-R3 acceptance suite — database identity, privilege,
persistence, migration authority (task order §3.5 / §8).

This suite is the R3 acceptance proof. It provisions its OWN isolated,
disposable PostgreSQL cluster (``initdb`` in a pytest temp directory,
ephemeral loopback port, scram-sha-256) — it never touches the
authoritative production PostgreSQL (port 5432), the legacy
``/tmp/fie-pg`` instance, or any foreign process. Synthetic test
credentials only.

Test classes mapped to the task order:

* **Identity positive** — the runtime identity gate PASSES for a
  correctly-provisioned least-privilege runtime role; a synthetic
  record round-trips through the supported write path as that role.
* **Identity negative** — superuser/CREATEROLE/CREATEDB/REPLICATION/
  BYPASSRLS attributes, schema-CREATE overprivilege, object ownership,
  inherited-role membership, and operator-pinned
  database/schema/role mismatches are all fail-closed rejections.
* **Privilege failure injection** — the runtime role is proven unable
  to perform ANY prohibited DDL/administration at the database level
  (structural guarantee, independent of code-path guards).
* **Least-privilege pin** — the runtime role's full grant surface is
  pinned from ``information_schema``; any extra grant fails.
* **Authority separation** — implicit-DDL call arcs refuse
  production-shaped targets without ``FIE_MIGRATION_AUTHORITY=1``
  and keep historical behavior elsewhere (no self-flip).
* **Readiness wiring** — ``get_health`` fails closed with
  ``IDENTITY_INCOMPATIBLE`` (503) and the gate degrades to
  ``DEPENDENCY_UNAVAILABLE`` when metadata is unreadable.
* **Schema/version gates** — missing registry, absent version record,
  future version, checksum divergence, failed migration, corrupt
  metadata, missing objects; read-only gate never auto-repairs.
* **Persistence (§3.5)** — a record written through the runtime
  identity survives application stop/restart and independent process
  re-verification; graceful teardown; the foreign PostgreSQL topology
  is untouched.

Skip discipline: when the isolated-cluster tooling (initdb/pg_ctl +
psycopg) is genuinely unavailable the session is skipped with an
explicit reason — no PostgreSQL is ever replaced with a mock.
"""

from __future__ import annotations

import glob
import importlib.util
import itertools
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

HAS_PSYCOPG = importlib.util.find_spec("psycopg") is not None


# ---------------------------------------------------------------------------
# PostgreSQL tooling discovery + isolated cluster (session-scoped)
# ---------------------------------------------------------------------------


def _find_pg_bin() -> Path | None:
    """Locate a usable initdb+pg_ctl pair (same discipline as
    scripts/provision-test-postgres.sh)."""
    candidates: list[Path] = []
    override = os.environ.get("FIE_TEST_PG_BIN", "")
    if override:
        candidates.append(Path(override))
    which = shutil.which("initdb")
    if which:
        candidates.append(Path(which).parent)
    candidates.extend(
        Path(p) for p in sorted(glob.glob("/usr/lib/postgresql/*/bin"))
    )
    for cand in candidates:
        if (cand / "initdb").is_file() and (cand / "pg_ctl").is_file():
            return cand
    return None


PG_BIN = _find_pg_bin()

#: The one PostgreSQL port the authoritative production instance listens on.
PRODUCTION_PG_PORT = 5432
#: The legacy foreign isolated instance port (must never be touched).
LEGACY_FIE_PG_PORT = 54329


@pytest.fixture(scope="session")
def pg_cluster(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """One isolated, disposable PostgreSQL cluster for the whole session."""
    if PG_BIN is None or not HAS_PSYCOPG:
        pytest.skip(
            "isolated real-PostgreSQL tooling unavailable "
            "(initdb/pg_ctl and/or psycopg absent)",
        )
    import psycopg

    root = tmp_path_factory.mktemp("r3-pg-cluster")
    data = root / "data"
    sock = root / "sock"
    logfile = root / "pg.log"
    data.mkdir()
    sock.mkdir()

    port = _ephemeral_loopback_port()
    assert port not in (PRODUCTION_PG_PORT, LEGACY_FIE_PG_PORT)

    admin_password = "r3-fixture-synth-pw"
    pwfile = root / "admin-pwfile"
    pwfile.write_text(admin_password + "\n")
    pwfile.chmod(0o600)

    initdb = str(PG_BIN / "initdb")
    proc = subprocess.run(
        [
            initdb,
            "-D",
            str(data),
            "-U",
            "postgres",
            "--auth-local=trust",
            "--auth-host=scram-sha-256",
            "--pwfile",
            str(pwfile),
            "--encoding=UTF8",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"initdb failed (rc={proc.returncode}, isolated cluster only)")

    config_tail = (
        f"port = {port}\n"
        "listen_addresses = '127.0.0.1'\n"
        f"unix_socket_directories = '{sock}'\n"
    )
    (data / "postgresql.conf").open("a").write(config_tail)

    pg_ctl = str(PG_BIN / "pg_ctl")
    proc = subprocess.run(
        [pg_ctl, "-D", str(data), "-l", str(logfile), "-w", "-t", "60", "start"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pg_ctl start failed (rc={proc.returncode}; isolated cluster only)")

    class _Cluster:
        """Tiny handle: DSN builder + admin statement runner."""

        _counter = itertools.count(1)
        admin_pw = admin_password
        port_ = port

        def dsn(self, user: str, dbname: str, password: str) -> str:
            return f"postgres://{user}:{password}@127.0.0.1:{self.port_}/{dbname}"

        def connect(self, dsn: str) -> Any:
            return psycopg.connect(dsn, autocommit=True, connect_timeout=10)

        def admin_exec(self, statements: list[str], dbname: str = "postgres") -> None:
            conn = self.connect(self.dsn("postgres", dbname, self.admin_pw))
            try:
                for sql in statements:
                    conn.execute(sql)
            finally:
                conn.close()

        def stop(self) -> int:
            proc = subprocess.run(
                [str(PG_BIN / "pg_ctl"), "-D", str(data), "-w", "-t", "60", "stop"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            return proc.returncode

        # expose internals for evidence/tests
        data_dir = str(data)
        sock_dir = str(sock)

    cluster = _Cluster()
    # prove the cluster answers before handing it to the rig
    cluster.admin_exec(["SELECT 1"])

    yield cluster

    rc = cluster.stop()
    assert rc == 0, f"pg_ctl stop returned {rc} (isolated cluster only)"


def _ephemeral_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------------------
# Rig: roles + databases + least-privilege grants (one per session)
# ---------------------------------------------------------------------------

_APP_TABLES = (
    "signal_log",
    "score_snapshot",
    "graph_nodes",
    "graph_edges",
    "adapter_run_log",
    "ingestion_errors",
)

_PROBE_ROLES = (
    "createrole",
    "createdb",
    "replication",
    "bypassrls",
    "inherited",
    "owner",
)


@pytest.fixture(scope="session")
def rig(pg_cluster: Any) -> Any:
    """Provision the least-privilege authority fixture (manual-run shape).

    Roles (all NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    NOBYPASSRLS):
    * ``fie_mig_*`` — migration authority: owns the databases, schema,
      all migrated objects; applies the repository migrations.
    * ``fie_rt_*``  — runtime authority: USAGE+arwd on the six app
      tables, SELECT on the registry, nothing else.
    * ``fie_bad_*`` — overprivilege probe (CREATE on schema + ALL on
      all tables) whose ONLY purpose is proving the gate detects it.
    * ``fie_by_*``  — bystander role with zero grants.
    * attribute probes (CREATEROLE/CREATEDB/REPLICATION/BYPASSRLS),
      an inherited-membership probe, and an object-ownership probe.
    """
    run_id = secrets.token_hex(3)
    mig_user = f"fie_mig_{run_id}"
    rt_user = f"fie_rt_{run_id}"
    bad_user = f"fie_bad_{run_id}"
    by_user = f"fie_by_{run_id}"
    mig_pw = "r3-synth-mig-" + run_id
    rt_pw = "r3-synth-rt-" + run_id
    bad_pw = "r3-synth-bad-" + run_id
    intel_db = f"fie_r3i_{run_id}"
    other_db = f"fie_r3o_{run_id}"

    stmts = [
        f"CREATE ROLE {mig_user} LOGIN PASSWORD '{mig_pw}'",
        f"CREATE ROLE {rt_user} LOGIN PASSWORD '{rt_pw}'",
        f"CREATE ROLE {bad_user} LOGIN PASSWORD '{bad_pw}'",
        f"CREATE ROLE {by_user} LOGIN PASSWORD 'r3-synth-by-{run_id}'",
        f"CREATE DATABASE {intel_db} OWNER {mig_user}",
        f"CREATE DATABASE {other_db} OWNER {mig_user}",
        f"REVOKE ALL ON DATABASE {intel_db} FROM PUBLIC",
        f"REVOKE ALL ON DATABASE {other_db} FROM PUBLIC",
    ]
    # attribute probes: CONNECT only (their attributes are checked first)
    for suffix in _PROBE_ROLES:
        stmts.append(
            f"CREATE ROLE fie_{suffix}_{run_id} LOGIN "
            f"PASSWORD 'r3-synth-{suffix}' "
            + {
                "createrole": "CREATEROLE",
                "createdb": "CREATEDB",
                "replication": "REPLICATION",
                "bypassrls": "BYPASSRLS",
                "inherited": "",
                "owner": "",
            }[suffix]
        )
        stmts.append(f"GRANT CONNECT ON DATABASE {intel_db} TO fie_{suffix}_{run_id}")
    pg_cluster.admin_exec(stmts)

    # --- migrate under the MIGRATION authority and pin least grants ------
    from phase3.persistence.backend import DatabaseSpec, open_store
    from phase3.persistence.migrations import MigrationManager, default_migrations_for

    mig_dsn = pg_cluster.dsn(mig_user, intel_db, mig_pw)
    store = open_store(DatabaseSpec("postgres", mig_dsn, "explicit", mig_dsn))
    try:
        applied = MigrationManager(store, default_migrations_for(store)).apply()
        assert [m.version for m in applied] != [], "rig migrations did not apply"
    finally:
        store.close()

    grant_stmts = [
        f"REVOKE ALL ON SCHEMA public FROM PUBLIC",
        f"GRANT USAGE ON SCHEMA public TO {rt_user}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_APP_TABLES[0]}, "
        f"{_APP_TABLES[1]}, {_APP_TABLES[2]}, {_APP_TABLES[3]}, "
        f"{_APP_TABLES[4]}, {_APP_TABLES[5]} TO {rt_user}",
        f"GRANT SELECT ON schema_migrations TO {rt_user}",
        f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {rt_user}",
        # sequences present in this twin (IDENTITY columns)
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {rt_user}",
        # the overprivilege probe role (intentionally over-granted)
        f"GRANT CONNECT ON DATABASE {intel_db} TO {rt_user}",
        f"GRANT CONNECT ON DATABASE {intel_db} TO {bad_user}",
        f"GRANT CONNECT ON DATABASE {intel_db} TO {by_user}",
        f"GRANT CONNECT ON DATABASE {other_db} TO {rt_user}",
        f"GRANT USAGE ON SCHEMA public TO {bad_user}",
        f"GRANT CREATE ON SCHEMA public TO {bad_user}",
        f"GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO {bad_user}",
    ]
    mig_conn = pg_cluster.connect(mig_dsn)
    try:
        for sql in grant_stmts:
            mig_conn.execute(sql)
    finally:
        mig_conn.close()

    teardown_stmts = []
    for db in (intel_db, other_db):
        teardown_stmts.append(f"DROP DATABASE {db} WITH (FORCE)")
    for user in (
        mig_user,
        rt_user,
        bad_user,
        by_user,
        *(f"fie_{s}_{run_id}" for s in _PROBE_ROLES),
    ):
        teardown_stmts.append(f"DROP ROLE {user}")

    class _Rig:
        run_id_ = run_id
        mig_user_ = mig_user
        rt_user_ = rt_user
        bad_user_ = bad_user
        by_user_ = by_user
        intel_db_ = intel_db
        other_db_ = other_db
        probes = {
            "createrole": f"fie_createrole_{run_id}",
            "createdb": f"fie_createdb_{run_id}",
            "replication": f"fie_replication_{run_id}",
            "bypassrls": f"fie_bypassrls_{run_id}",
            "inherited": f"fie_inherited_{run_id}",
            "owner": f"fie_owner_{run_id}",
        }

        def dsn_mig(self, dbname: str = intel_db) -> str:
            return pg_cluster.dsn(mig_user, dbname, mig_pw)

        def dsn_rt(self, dbname: str = intel_db) -> str:
            return pg_cluster.dsn(rt_user, dbname, rt_pw)

        def dsn_bad(self, dbname: str = intel_db) -> str:
            return pg_cluster.dsn(bad_user, dbname, bad_pw)

        def dsn_by(self, dbname: str = intel_db) -> str:
            return pg_cluster.dsn(by_user, dbname, f"r3-synth-by-{run_id}")

        def dsn_admin(self, dbname: str = intel_db) -> str:
            return pg_cluster.dsn("postgres", dbname, pg_cluster.admin_pw)

        def admin(self) -> Any:
            return pg_cluster.connect(
                pg_cluster.dsn("postgres", "postgres", pg_cluster.admin_pw)
            )

    return _Rig()


# scratch databases for tests that must mutate schema state
@pytest.fixture(scope="session")
def make_db(pg_cluster: Any, rig: Any) -> Any:
    """Factory: provision a fresh migrated scratch database (mig-owned)."""
    from phase3.persistence.backend import DatabaseSpec, open_store
    from phase3.persistence.migrations import MigrationManager, default_migrations_for

    counter = itertools.count(1)
    created: list[str] = []

    def factory(prefix: str = "scratch", migrate: bool = True) -> str:
        n = next(counter)
        name = f"fie_r3s_{rig.run_id_}_{prefix}{n}"
        pg_cluster.admin_exec([f"CREATE DATABASE {name} OWNER {rig.mig_user_}"])
        dsn = rig.dsn_mig(name)
        if migrate:
            store = open_store(DatabaseSpec("postgres", dsn, "explicit", dsn))
            try:
                MigrationManager(store, default_migrations_for(store)).apply()
            finally:
                store.close()
        created.append(name)
        return dsn

    yield factory
    teardown = [
        f"DROP DATABASE {name} WITH (FORCE)" for name in created
    ]
    if teardown:
        pg_cluster.admin_exec(teardown)


# per-test convenience: open a store (and always close it)
@pytest.fixture()
def open_pg_store(pg_cluster: Any):
    from phase3.persistence.backend import DatabaseSpec, open_store

    opened: list[Any] = []

    def _open(dsn: str) -> Any:
        store = open_store(DatabaseSpec("postgres", dsn, "explicit", dsn))
        opened.append(store)
        return store

    yield _open
    for store in opened:
        try:
            store.close()
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass


def _clean_env(**overrides: str) -> dict[str, str]:
    """os.environ minus every FIE contract variable + explicit overrides.

    Test subprocesses must never inherit the host's production-shaped
    target/contract environment.
    """
    scrub = (
        "FIE_DB_TARGET_RAW",
        "FIE_DB_TARGET_INTELLIGENCE",
        "FIE_DB_TARGET_TEST",
        "FIE_DB_TARGET_ROLLBACK",
        "FIE_DB_PATH",
        "FIE_DATABASE_URL",
        "FIE_INTELLIGENCE_DB",
        "FIE_MIGRATION_AUTHORITY",
        "FIE_EXPECTED_RUNTIME_DB",
        "FIE_EXPECTED_RUNTIME_SCHEMA",
        "FIE_EXPECTED_RUNTIME_ROLE",
        "FIE_PRODUCTION_DB_CONTRACT",
        "FIE_SERVICE_ENV",
        "FIE_TEST_PG_DSN",
    )
    env = {k: v for k, v in os.environ.items() if k not in scrub}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(overrides)
    return env


def _last_json(text: str) -> dict:
    """Last stderr line that is a JSON object (store diagnostics may
    precede it)."""
    for line in reversed(text.strip().splitlines()):
        if line.strip().startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no JSON object line in: {text!r}")


def _run_python(code: str, env: dict[str, str], timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _sqlstate(exc: BaseException) -> str:
    return str(getattr(exc, "sqlstate", "") or "")


def _assert_insufficient(exc: BaseException) -> None:
    import psycopg

    assert isinstance(exc, psycopg.errors.InsufficientPrivilege), (
        f"expected InsufficientPrivilege, got {type(exc).__name__}: {exc}"
    )
    assert _sqlstate(exc) == "42501"


# ===========================================================================
# Identity positive (§8 class A — evidence 06)
# ===========================================================================


class TestIdentityPositive:
    """A least-privilege runtime identity is a PASS; DML round-trips."""

    def test_gate_passes_for_runtime_role(self, pg_cluster, rig, open_pg_store) -> None:
        from phase3.persistence.identity_gate import check_runtime_identity

        store = open_pg_store(rig.dsn_rt())
        verdict = check_runtime_identity(store)
        assert verdict["applicable"] is True
        assert verdict["role"] == rig.rt_user_
        assert verdict["database"] == rig.intel_db_
        assert verdict["schema"] == "public"
        for attr in ("rolsuper", "rolcreaterole", "rolcreatedb",
                     "rolreplication", "rolbypassrls"):
            assert verdict["role_attributes"][attr] is False
        assert verdict["schema_create_granted"] is False
        assert verdict["owned_relations"] == 0
        assert verdict["inherited_roles"] == 0
        assert verdict["expectations_enforced"] == {}

    def test_pinned_expectations_enforced_on_match(self, pg_cluster, rig, open_pg_store) -> None:
        from phase3.persistence.identity_gate import check_runtime_identity

        store = open_pg_store(rig.dsn_rt())
        env = {
            "FIE_EXPECTED_RUNTIME_DB": rig.intel_db_,
            "FIE_EXPECTED_RUNTIME_SCHEMA": "public",
            "FIE_EXPECTED_RUNTIME_ROLE": rig.rt_user_,
        }
        verdict = check_runtime_identity(store, env=env)
        assert verdict["expectations_enforced"] == {
            "database": True,
            "schema": True,
            "role": True,
        }

    def test_runtime_dml_positive_roundtrip(self, pg_cluster, rig, open_pg_store) -> None:
        from phase3.persistence.signal_repo import (
            SignalRecord,
            SignalRepository,
        )
        from phase3.persistence.timeutil import utc_now_iso

        store = open_pg_store(rig.dsn_rt())
        repo = SignalRepository(store)
        record = SignalRecord(
            signal_id=f"r3-accept-{secrets.token_hex(4)}",
            entity_type="index",
            entity_id="TEST",
            signal_type="close",
            value=1.0,
            unit="USD",
            direction="up",
            timestamp=utc_now_iso(),
            date_bucket="2099-01-01",
            source_id="r3-suite",
            source_type="synthetic",
            metadata={"purpose": "phase-6.9B-R3 acceptance"},
        )
        before = repo.count()
        assert repo.upsert(record) is True  # created
        assert repo.get(record.signal_id) is not None
        assert repo.count() == before + 1
        # second upsert = update, not duplicate
        assert repo.upsert(record) is False
        assert repo.count() == before + 1

    def test_sequence_backed_writes_allowed(self, pg_cluster, rig, open_pg_store) -> None:
        # identity-column inserts (the sequence-backed write path) work
        store = open_pg_store(rig.dsn_rt())
        cur = store.execute(
            "INSERT INTO ingestion_errors (source, message, details_json) "
            "VALUES ('r3-suite', 'synthetic sequence-backed write probe', '{}') "
            "RETURNING error_id"
        ).fetchone()
        assert cur is not None and int(cur[0]) > 0

    def test_sqlite_not_applicable(self) -> None:
        from phase3.persistence.identity_gate import check_runtime_identity
        from phase3.persistence.sqlite import SQLiteStore

        store = SQLiteStore(":memory:")
        verdict = check_runtime_identity(store)
        assert verdict["applicable"] is False
        store.close()


# ===========================================================================
# Identity negative (§8 class B — evidence 07)
# ===========================================================================


class TestIdentityNegative:
    def _gate(self, store, env: dict | None = None):
        from phase3.persistence.identity_gate import check_runtime_identity

        return check_runtime_identity(store, env=env)

    def test_superuser_rejected(self, pg_cluster, rig, open_pg_store) -> None:
        from phase3.persistence.identity_gate import RUNTIME_ROLE_SUPERUSER

        store = open_pg_store(rig.dsn_admin())
        with pytest.raises(Exception) as ei:
            self._gate(store)
        assert ei.value.code == RUNTIME_ROLE_SUPERUSER

    def test_createrole_rejected(self, pg_cluster, rig, open_pg_store) -> None:
        from phase3.persistence.identity_gate import RUNTIME_ROLE_CREATEROLE

        dsn = pg_cluster.dsn(rig.probes["createrole"], rig.intel_db_, "r3-synth-createrole")
        store = open_pg_store(dsn)
        with pytest.raises(Exception) as ei:
            self._gate(store)
        assert ei.value.code == RUNTIME_ROLE_CREATEROLE

    def test_createdb_rejected(self, pg_cluster, rig, open_pg_store) -> None:
        from phase3.persistence.identity_gate import RUNTIME_ROLE_CREATEDB

        dsn = pg_cluster.dsn(rig.probes["createdb"], rig.intel_db_, "r3-synth-createdb")
        store = open_pg_store(dsn)
        with pytest.raises(Exception) as ei:
            self._gate(store)
        assert ei.value.code == RUNTIME_ROLE_CREATEDB

    def test_replication_rejected(self, pg_cluster, rig, open_pg_store) -> None:
        from phase3.persistence.identity_gate import RUNTIME_ROLE_REPLICATION

        dsn = pg_cluster.dsn(rig.probes["replication"], rig.intel_db_, "r3-synth-replication")
        store = open_pg_store(dsn)
        with pytest.raises(Exception) as ei:
            self._gate(store)
        assert ei.value.code == RUNTIME_ROLE_REPLICATION

    def test_bypassrls_rejected(self, pg_cluster, rig, open_pg_store) -> None:
        from phase3.persistence.identity_gate import RUNTIME_ROLE_BYPASSRLS

        dsn = pg_cluster.dsn(rig.probes["bypassrls"], rig.intel_db_, "r3-synth-bypassrls")
        store = open_pg_store(dsn)
        with pytest.raises(Exception) as ei:
            self._gate(store)
        assert ei.value.code == RUNTIME_ROLE_BYPASSRLS

    def test_overprivileged_schema_rejected(self, pg_cluster, rig, open_pg_store) -> None:
        from phase3.persistence.identity_gate import RUNTIME_OVERPRIVILEGED_SCHEMA

        store = open_pg_store(rig.dsn_bad())
        with pytest.raises(Exception) as ei:
            self._gate(store)
        assert ei.value.code == RUNTIME_OVERPRIVILEGED_SCHEMA

    def test_object_ownership_rejected(self, pg_cluster, rig, make_db, open_pg_store) -> None:
        """A runtime-class role that owns ANY relation fails closed."""
        from phase3.persistence.identity_gate import RUNTIME_ROLE_OWNS_OBJECTS

        dsn_mig = make_db("ownprobe")
        dbname = dsn_mig.rsplit("/", 1)[-1]
        owner_probe = f"fie_own2_{rig.run_id_}"
        admin_conn = rig.admin()
        try:
            admin_conn.execute(
                f"CREATE ROLE {owner_probe} LOGIN PASSWORD 'r3-synth-own2'"
            )
        finally:
            admin_conn.close()
        mig_conn = pg_cluster.connect(dsn_mig)
        try:
            mig_conn.execute(f"GRANT CONNECT ON DATABASE {dbname} TO {owner_probe}")
            admin_in_db = pg_cluster.connect(
                pg_cluster.dsn("postgres", dbname, pg_cluster.admin_pw)
            )
            try:
                admin_in_db.execute(
                    "CREATE TABLE public.probe_ownership (k int primary key)"
                )
                # ownership transfer must be executed by someone who can
                # SET ROLE to the new owner (superuser, not the migrator)
                admin_in_db.execute(
                    f"ALTER TABLE public.probe_ownership OWNER TO {owner_probe}"
                )
            finally:
                admin_in_db.close()
            probe_dsn = pg_cluster.dsn(owner_probe, dbname, "r3-synth-own2")
            store = open_pg_store(probe_dsn)
            with pytest.raises(Exception) as ei:
                self._gate(store)
            assert ei.value.code == RUNTIME_ROLE_OWNS_OBJECTS
            store.close()  # release the probe session before DROP
        finally:
            mig_conn.close()
            # cleanup as admin (the table now belongs to the probe role);
            # the admin connection must be IN the scratch database
            admin_conn = pg_cluster.connect(
                pg_cluster.dsn("postgres", dbname, pg_cluster.admin_pw)
            )
            try:
                admin_conn.execute("DROP TABLE IF EXISTS public.probe_ownership")
                admin_conn.execute(
                    f"REVOKE CONNECT ON DATABASE {dbname} FROM {owner_probe}"
                )
                admin_conn.execute(f"DROP ROLE {owner_probe}")
            finally:
                admin_conn.close()

    def test_inherited_membership_rejected(self, pg_cluster, rig, open_pg_store) -> None:
        from phase3.persistence.identity_gate import RUNTIME_INHERITED_ROLE

        inherited = rig.probes["inherited"]
        conn = rig.admin()
        try:
            # inherit the ZERO-grant bystander role: membership itself
            # (not an inherited privilege) must abort the gate
            conn.execute(f"GRANT {rig.by_user_} TO {inherited}")
        finally:
            conn.close()
        store = open_pg_store(
            pg_cluster.dsn(inherited, rig.intel_db_, "r3-synth-inherited")
        )
        with pytest.raises(Exception) as ei:
            self._gate(store)
        assert ei.value.code == RUNTIME_INHERITED_ROLE

    def test_wrong_database_pin(self, pg_cluster, rig, open_pg_store) -> None:
        from phase3.persistence.identity_gate import WRONG_DATABASE

        store = open_pg_store(rig.dsn_rt(rig.other_db_))
        with pytest.raises(Exception) as ei:
            self._gate(store, env={"FIE_EXPECTED_RUNTIME_DB": rig.intel_db_})
        assert ei.value.code == WRONG_DATABASE

    def test_wrong_schema_pin(self, pg_cluster, rig, open_pg_store) -> None:
        from phase3.persistence.identity_gate import WRONG_SCHEMA

        store = open_pg_store(rig.dsn_rt())
        with pytest.raises(Exception) as ei:
            self._gate(store, env={"FIE_EXPECTED_RUNTIME_SCHEMA": "analytics"})
        assert ei.value.code == WRONG_SCHEMA

    def test_wrong_role_pin(self, pg_cluster, rig, open_pg_store) -> None:
        from phase3.persistence.identity_gate import WRONG_ROLE

        store = open_pg_store(rig.dsn_rt())
        with pytest.raises(Exception) as ei:
            self._gate(store, env={"FIE_EXPECTED_RUNTIME_ROLE": rig.mig_user_})
        assert ei.value.code == WRONG_ROLE

    def test_identity_gate_unavailable_on_broken_store(self, rig, open_pg_store) -> None:
        from unittest import mock

        from phase3.persistence.identity_gate import IdentityGateUnavailable

        store = open_pg_store(rig.dsn_rt())
        with mock.patch.object(
            store, "execute", side_effect=RuntimeError("backend lost")
        ):
            with pytest.raises(IdentityGateUnavailable):
                self._gate(store)


# ===========================================================================
# Privilege failure injection at the database level (§8 class C — evidence 08)
# ===========================================================================


class TestPrivilegeFailureInjection:
    """Structural guarantee: the runtime role CANNOT do any of this,
    regardless of code-path guards."""

    @pytest.fixture()
    def rt_store(self, rig, open_pg_store):
        return open_pg_store(rig.dsn_rt())

    # --- prohibited DDL ---------------------------------------------------

    @pytest.mark.parametrize(
        "sql_template",
        [
            "CREATE TABLE public.probe_rogue (k int)",
            "ALTER TABLE signal_log ADD COLUMN probe_col int",
            "DROP TABLE schema_migrations",
            "CREATE INDEX probe_idx ON signal_log (signal_id)",
            "CREATE SCHEMA publicrogue",
            "CREATE VIEW public.probe_view AS SELECT 1",
            "TRUNCATE TABLE signal_log",
            "TRUNCATE TABLE schema_migrations",
        ],
    )
    def test_prohibited_ddl_denied(self, rt_store, sql_template) -> None:
        with pytest.raises(Exception) as ei:
            rt_store.execute(sql_template)
        _assert_insufficient(ei.value)

    def test_drop_table_denied(self, rt_store) -> None:
        with pytest.raises(Exception) as ei:
            rt_store.execute("DROP TABLE signal_log CASCADE")
        _assert_insufficient(ei.value)

    def test_alter_rename_denied(self, rt_store) -> None:
        with pytest.raises(Exception) as ei:
            rt_store.execute("ALTER TABLE signal_log RENAME TO signal_log_probe")
        _assert_insufficient(ei.value)

    def test_ownership_takeover_denied(self, rt_store) -> None:
        with pytest.raises(Exception) as ei:
            rt_store.execute("ALTER TABLE signal_log OWNER TO CURRENT_USER")
        _assert_insufficient(ei.value)

    # --- prohibited registry writes ---------------------------------------

    def test_registry_insert_denied(self, rt_store) -> None:
        with pytest.raises(Exception) as ei:
            rt_store.execute(
                "INSERT INTO schema_migrations (version, name, checksum, "
                "applied_at, error) VALUES (99, 'probe', 'deadbeef', "
                "now(), NULL)"
            )
        _assert_insufficient(ei.value)

    def test_registry_update_denied(self, rt_store) -> None:
        with pytest.raises(Exception) as ei:
            rt_store.execute(
                "UPDATE schema_migrations SET checksum = 'tampered'"
            )
        _assert_insufficient(ei.value)

    def test_registry_delete_denied(self, rt_store) -> None:
        with pytest.raises(Exception) as ei:
            rt_store.execute("DELETE FROM schema_migrations WHERE version = 1")
        _assert_insufficient(ei.value)

    # --- prohibited administration ----------------------------------------

    @pytest.mark.parametrize(
        "sql",
        [
            "CREATE ROLE probe_role_r3 LOGIN",
            "CREATE DATABASE probe_db_r3",
            "ALTER ROLE postgres NOLOGIN",
        ],
    )
    def test_role_administration_denied(self, rt_store, sql) -> None:
        with pytest.raises(Exception) as ei:
            rt_store.execute(sql)
        _assert_insufficient(ei.value)

    def test_grant_redistribution_denied(self, rig, open_pg_store) -> None:
        # no grant option held → runtime must not mint privileges for anyone
        rt_store = open_pg_store(rig.dsn_rt())
        try:
            rt_store.execute(f"GRANT SELECT ON signal_log TO {rig.by_user_}")
        except Exception:  # noqa: BLE001 - PostgreSQL may warn rather than error
            pass
        # the security property: the bystander gains NOTHING
        by_store = open_pg_store(rig.dsn_by())
        with pytest.raises(Exception) as ei:
            by_store.execute("SELECT 1 FROM public.signal_log LIMIT 1")
        _assert_insufficient(ei.value)

    # --- sanctioned DML must still work ------------------------------------

    def test_delete_is_sanctioned(self, rt_store) -> None:
        rt_store.execute(
            "DELETE FROM signal_log WHERE source_id = 'r3-suite-nonexistent'"
        )

    def test_select_registry_allowed(self, rt_store) -> None:
        rows = rt_store.execute(
            "SELECT version, name, checksum, error FROM schema_migrations "
            "ORDER BY version"
        ).fetchall()
        assert rows, "runtime must be able to READ the migration registry"


# ===========================================================================
# Least-privilege grant-surface pin (§8 overprivilege detection — evidence 09)
# ===========================================================================


class TestGrantSurfacePin:
    _EXPECTED_PRIVS = ("SELECT", "INSERT", "UPDATE", "DELETE")

    def _surface(self, store, role: str) -> set[tuple[str, str]]:
        rows = store.execute(
            "SELECT table_name, privilege_type FROM "
            "information_schema.role_table_grants "
            "WHERE grantee = %s AND table_schema = 'public' ORDER BY 1, 2",
            (role,),
        ).fetchall()
        return {(str(r[0]), str(r[1])) for r in rows}

    def test_runtime_grant_surface_is_exactly_least_privilege(
        self, rig, open_pg_store
    ) -> None:
        store = open_pg_store(rig.dsn_rt())
        expected: set[tuple[str, str]] = set()
        for table in _APP_TABLES:
            for priv in self._EXPECTED_PRIVS:
                expected.add((table, priv))
        expected.add(("schema_migrations", "SELECT"))  # SELECT ONLY
        observed = self._surface(store, rig.rt_user_)
        assert observed == expected, (
            "runtime grant surface deviates from the least-privilege pin: "
            f"unexpected={sorted(observed - expected)} "
            f"missing={sorted(expected - observed)}"
        )

    def test_runtime_has_no_ownership_or_create(self, rig, open_pg_store) -> None:
        store = open_pg_store(rig.dsn_rt())
        owned = store.execute(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
            "AND c.relowner = (SELECT oid FROM pg_roles "
            "WHERE rolname = current_user)"
        ).fetchone()
        assert int(owned[0]) == 0
        create = store.execute(
            "SELECT has_schema_privilege(current_user, 'public', 'CREATE')"
        ).fetchone()
        assert create is not None and create[0] is False

    def test_migrations_own_all_objects(self, rig, open_pg_store) -> None:
        store = open_pg_store(rig.dsn_mig())
        owned = store.execute(
            "SELECT c.relname, c.relkind FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = ANY(ARRAY['r','S']) "
            "ORDER BY 1"
        ).fetchall()
        names = {str(r[0]) for r in owned}
        assert set(_APP_TABLES) <= names
        assert "schema_migrations" in names
        owners = store.execute(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
            "AND c.relowner <> (SELECT oid FROM pg_roles "
            "WHERE rolname = current_user)"
        ).fetchone()
        assert int(owners[0]) == 0

    def test_registry_writes_belong_to_migration_authority_only(
        self, rig, open_pg_store
    ) -> None:
        store = open_pg_store(rig.dsn_mig())
        rows = store.execute(
            "SELECT version, error FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert rows
        assert all(r[1] is None for r in rows), "rig registry has failed rows"


# ===========================================================================
# Migration/runtime authority separation (§8 class — evidence 11)
# ===========================================================================


def _write_shaped_contract(tmp_path: Path, target_value: str) -> Path:
    """A synthetic 'production contract' file naming a disposable target.

    This reuses the R1 fingerprint machinery against a target WE
    provisioned (never the real production contract) so the
    production-shaped machinery is provably exercised end to end
    without contacting production.
    """
    contract = tmp_path / "synthetic-production-contract.env"
    contract.write_text(
        "# synthetic contract: marks a disposable fixture target\n"
        f"FIE_DATABASE_URL={target_value}\n"
    )
    return contract


class TestAuthoritySeparation:
    def test_auto_ddl_refused_production_shaped_without_authority(
        self, pg_cluster, rig, tmp_path
    ) -> None:
        from phase3.persistence.migration_authority import (
            MigrationAuthorityRequired,
            auto_ddl_allowed,
            assert_auto_ddl_allowed,
        )

        contract = _write_shaped_contract(tmp_path, rig.dsn_mig())
        env = {"FIE_PRODUCTION_DB_CONTRACT": str(contract)}
        assert auto_ddl_allowed(rig.dsn_mig(), env=env) is False
        with pytest.raises(MigrationAuthorityRequired) as ei:
            assert_auto_ddl_allowed(
                rig.dsn_mig(), operation="probe-op", env=env
            )
        assert ei.value.code == "MIGRATION_AUTHORITY_REQUIRED"
        assert "postgres" not in str(ei.value).lower() or "***" in str(ei.value)

    def test_auto_ddl_allowed_under_explicit_authority(
        self, pg_cluster, rig, tmp_path
    ) -> None:
        from phase3.persistence.migration_authority import auto_ddl_allowed

        contract = _write_shaped_contract(tmp_path, rig.dsn_mig())
        env = {
            "FIE_PRODUCTION_DB_CONTRACT": str(contract),
            "FIE_MIGRATION_AUTHORITY": "1",
        }
        assert auto_ddl_allowed(rig.dsn_mig(), env=env) is True
        assert (
            auto_ddl_allowed(rig.dsn_mig(), env={"FIE_PRODUCTION_DB_CONTRACT": str(contract)})
            is False
        )

    def test_non_production_targets_keep_historical_behavior(self, tmp_path) -> None:
        from phase3.persistence.migration_authority import (
            auto_ddl_allowed,
            is_production_shaped,
        )

        # a disposable sqlite target on THIS host (real production
        # contract still on disk) is not production-shaped
        assert auto_ddl_allowed(str(tmp_path / "disposable.db")) is True
        spec_env = _clean_env()
        assert is_production_shaped(
            _parse_sqlite(tmp_path / "disposable.db"), spec_env
        ) is False

    def test_sqlite_graph_store_auto_migrate_refuses_production_shape(
        self, rig, tmp_path, monkeypatch
    ) -> None:
        from phase3.graph.sqlite_store import SQLiteGraphStore
        from phase3.persistence.migration_authority import (
            MigrationAuthorityRequired,
        )

        shaped_path = tmp_path / "shaped-production.db"
        contract = _write_shaped_contract(tmp_path, str(shaped_path))
        for var in _clean_env():  # ambient FIE vars must not leak in
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("FIE_PRODUCTION_DB_CONTRACT", str(contract))
        with pytest.raises(MigrationAuthorityRequired):
            SQLiteGraphStore(str(shaped_path), auto_migrate=True)

    def test_sqlite_graph_store_auto_migrate_allows_non_production(
        self, tmp_path
    ) -> None:
        from phase3.graph.sqlite_store import SQLiteGraphStore

        store = SQLiteGraphStore(str(tmp_path / "disposable-graph.db"), auto_migrate=True)
        store.close()

    def test_raw_init_db_no_ddl_on_production_shape(
        self, rig, tmp_path, monkeypatch
    ) -> None:
        import db as raw_db

        shaped_path = tmp_path / "shaped-production-raw.db"
        contract = _write_shaped_contract(tmp_path, str(shaped_path))
        for var in _clean_env():
            monkeypatch.delenv(var, raising=False)
        # The 6.8A rehearsal boundary (FIE_SERVICE_ENV=test) refuses to
        # OPEN any production-equivalent raw target. R3 simulates the
        # PRODUCTION raw runtime here — the guard under test is the R3
        # no-implicit-DDL refusal — so this leg must not carry the
        # rehearsal service profile.
        monkeypatch.delenv("FIE_SERVICE_ENV", raising=False)
        monkeypatch.setenv("FIE_DB_TARGET_RAW", str(shaped_path))
        monkeypatch.setenv("FIE_PRODUCTION_DB_CONTRACT", str(contract))
        # db.DB_PATH resolves at import time; the module contract is that
        # callers/tests patch the attribute directly.
        monkeypatch.setattr(raw_db, "DB_PATH", str(shaped_path))
        with pytest.raises(raw_db.RawSchemaNotInitialized):
            raw_db.init_db()

    def test_raw_init_db_runs_under_authority(self, tmp_path, monkeypatch) -> None:
        import db as raw_db

        shaped_path = tmp_path / "authorized-migration.db"
        contract = _write_shaped_contract(tmp_path, str(shaped_path))
        for var in _clean_env():
            monkeypatch.delenv(var, raising=False)
        # the explicit operator migration context is NOT a rehearsal
        # profile; see the no-DDL leg above for the service-env note
        monkeypatch.delenv("FIE_SERVICE_ENV", raising=False)
        monkeypatch.setenv("FIE_DB_TARGET_RAW", str(shaped_path))
        monkeypatch.setenv("FIE_PRODUCTION_DB_CONTRACT", str(contract))
        monkeypatch.setenv("FIE_MIGRATION_AUTHORITY", "1")
        monkeypatch.setattr(raw_db, "DB_PATH", str(shaped_path))
        raw_db.init_db()
        assert _sqlite_has_table(raw_db, shaped_path) is True

    def test_migration_authority_granted_flag_contract(self, monkeypatch) -> None:
        from phase3.persistence.migration_authority import migration_authority_granted

        for var in ("FIE_MIGRATION_AUTHORITY",):
            monkeypatch.delenv(var, raising=False)
        assert migration_authority_granted() is False
        monkeypatch.setenv("FIE_MIGRATION_AUTHORITY", "1")
        assert migration_authority_granted() is True
        monkeypatch.setenv("FIE_MIGRATION_AUTHORITY", "0")
        assert migration_authority_granted() is False

    def test_production_shaped_requires_readable_contract(self, tmp_path) -> None:
        from phase3.persistence.migration_authority import is_production_shaped

        env = _clean_env(FIE_PRODUCTION_DB_CONTRACT=str(tmp_path / "nonexistent.env"))
        spec = _parse_sqlite(tmp_path / "anything.db")
        # unprovable production identity is treated as non-production
        # by THIS guard (R3 doctrine) — and never as production.
        assert is_production_shaped(spec, env=env) is False

    def test_migrations_apply_and_rerun_noop_under_authority(
        self, make_db
    ) -> None:
        from phase3.persistence.backend import DatabaseSpec, open_store
        from phase3.persistence.migrations import (
            MigrationManager,
            default_migrations_for,
        )

        dsn = make_db("idem")
        spec = DatabaseSpec("postgres", dsn, "explicit", dsn)
        store = open_store(spec)
        try:
            first = MigrationManager(store, default_migrations_for(store)).apply()
            assert first == []
            rows = store.execute(
                "SELECT count(*) FROM schema_migrations"
            ).fetchone()
            assert int(rows[0]) >= 1
        finally:
            store.close()


# ===========================================================================
# Readiness wiring (§8 class — evidence 12)
# ===========================================================================


class TestReadinessWiring:
    def test_get_health_ok_passes_both_gates(self, pg_cluster, rig, monkeypatch) -> None:
        from phase3.persistence.backend import DatabaseSpec, open_store
        from phase3.service.boundary import DefaultIntelligenceService

        # The disposable rig target is NOT production-shaped, so the
        # readiness identity gate is scoped off by default (historical
        # parity for disposable targets); the explicit opt-in forces
        # enforcement so THIS leg proves a compliant least-privilege
        # runtime identity passes both gates end to end.
        monkeypatch.setenv("FIE_RUNTIME_IDENTITY_GATE", "1")
        spec = DatabaseSpec("postgres", rig.dsn_rt(), "explicit", rig.dsn_rt())
        store = open_store(spec)
        try:
            service = DefaultIntelligenceService(store, read_only=True)
            report = service.get_health()
            assert report.status == "ok"
            assert report.backend_kind == "postgres"
            assert isinstance(report.counts, dict)
        finally:
            store.close()

    def test_get_health_fails_closed_identity_incompatible(
        self, pg_cluster, rig, monkeypatch
    ) -> None:
        from phase3.persistence.backend import DatabaseSpec, open_store
        from phase3.service.boundary import DefaultIntelligenceService

        monkeypatch.setenv("FIE_EXPECTED_RUNTIME_ROLE", "definitely-not-" + rig.run_id_)
        spec = DatabaseSpec("postgres", rig.dsn_rt(), "explicit", rig.dsn_rt())
        store = open_store(spec)
        try:
            service = DefaultIntelligenceService(store, read_only=True)
            with pytest.raises(Exception) as ei:
                service.get_health()
            code_txt = str(getattr(ei.value, "code", ei.value))
            assert "IDENTITY_INCOMPATIBLE" in code_txt
        finally:
            store.close()

    def test_identity_incompatible_maps_to_503(self) -> None:
        from phase3.service.errors import ServiceErrorCode
        from phase3.transport.errors import http_status_for

        assert http_status_for(ServiceErrorCode.IDENTITY_INCOMPATIBLE) == 503

    def test_gate_depgrades_to_dependency_unavailable(
        self, pg_cluster, rig, monkeypatch
    ) -> None:
        from unittest import mock

        from phase3.persistence.backend import DatabaseSpec, open_store
        from phase3.service import boundary
        from phase3.transport.errors import http_status_for

        # force enforcement (disposable target is not production-shaped)
        monkeypatch.setenv("FIE_RUNTIME_IDENTITY_GATE", "1")
        spec = DatabaseSpec("postgres", rig.dsn_rt(), "explicit", rig.dsn_rt())
        store = open_store(spec)
        try:
            with mock.patch.object(
                store, "execute", side_effect=RuntimeError("backend lost")
            ):
                with pytest.raises(boundary.ServiceError) as ei:
                    boundary._readiness_identity_gate(store)
        finally:
            store.close()
        assert str(getattr(ei.value, "code", ei.value)).split(".")[-1] \
            == "DEPENDENCY_UNAVAILABLE"
        assert http_status_for(ei.value.code) == 503

    def test_preflight_reports_identity_expectation_pins(self, monkeypatch) -> None:
        from phase3.preflight import run_preflight

        env = _clean_env()
        report, rc = run_preflight(env)
        pins = report["checks"]["runtime_identity_expectations"]
        assert pins == {
            "role_pinned": False,
            "database_pinned": False,
            "schema_pinned": False,
        }
        monkeypatch.setenv("FIE_EXPECTED_RUNTIME_ROLE", "some-runtime-role")
        report2, _ = run_preflight()
        pins2 = report2["checks"]["runtime_identity_expectations"]
        assert pins2["role_pinned"] is True


# ===========================================================================
# Identity-gate enforcement scope (§8 class — evidence 12 appendix)
# ===========================================================================


class TestIdentityGateScope:
    """The readiness wiring scopes identity enforcement fail-closed.

    Enforcement: production-shaped targets, pinned deployments, explicit
    ``FIE_RUNTIME_IDENTITY_GATE=1``. Disposable test targets keep
    historical behavior; an unparseable PostgreSQL target stays
    ENFORCED (it cannot be proven non-production).
    """

    def _fake_pg(self, dsn: str):
        from types import SimpleNamespace

        return SimpleNamespace(backend="postgres", _dsn=dsn)

    def test_pins_force_enforcement_anywhere(self) -> None:
        from phase3.service.boundary import _identity_gate_applicable

        env = _clean_env(FIE_EXPECTED_RUNTIME_ROLE="r3-synth-role")
        assert _identity_gate_applicable(
            self._fake_pg("postgresql://u@/d?host=127.0.0.1&port=1"), env=env
        ) is True

    def test_explicit_opt_in_forces_enforcement(self, monkeypatch) -> None:
        import os

        from phase3.service.boundary import _identity_gate_applicable

        dsn = "postgresql://r3-synth@/r3-synth-db?host=127.0.0.1&port=1"
        env = _clean_env()
        assert _identity_gate_applicable(self._fake_pg(dsn), env=env) is False
        monkeypatch.setenv("FIE_RUNTIME_IDENTITY_GATE", "1")
        assert _identity_gate_applicable(
            self._fake_pg(dsn), env=dict(os.environ)) is True

    def test_unparseable_pg_target_stays_enforced(self) -> None:
        from phase3.service.boundary import _identity_gate_applicable

        assert _identity_gate_applicable(
            self._fake_pg("postgres host=/no/such"), env=_clean_env()
        ) is True

    def test_production_shaped_target_enforced(self, tmp_path) -> None:
        from phase3.service.boundary import _identity_gate_applicable

        shaped = "postgresql://r3-synth-u@/r3-synth-db?host=127.0.0.1&port=9"
        contract = _write_shaped_contract(tmp_path, shaped)
        env = _clean_env(FIE_PRODUCTION_DB_CONTRACT=str(contract))
        assert _identity_gate_applicable(
            self._fake_pg(shaped), env=env) is True

    def test_non_enforced_scope_answers_applicable_false(self, monkeypatch) -> None:
        import phase3.service.boundary

        # ambient pins/opt-ins must not accidentally enforce this verdict
        monkeypatch.delenv("FIE_EXPECTED_RUNTIME_DB", raising=False)
        monkeypatch.delenv("FIE_EXPECTED_RUNTIME_SCHEMA", raising=False)
        monkeypatch.delenv("FIE_EXPECTED_RUNTIME_ROLE", raising=False)
        monkeypatch.delenv("FIE_RUNTIME_IDENTITY_GATE", raising=False)
        dsn = "postgresql://r3-synth@/r3-synth-db?host=127.0.0.1&port=1"
        verdict = phase3.service.boundary._readiness_identity_gate(
            self._fake_pg(dsn))
        assert verdict["applicable"] is False
        assert verdict["scope"] == "NON_PRODUCTION_TARGET"

    def test_sqlite_scope_answers_non_postgresql_backend(self) -> None:
        from types import SimpleNamespace

        import phase3.service.boundary

        verdict = phase3.service.boundary._readiness_identity_gate(
            SimpleNamespace(backend="sqlite", _dsn=""))
        assert verdict["applicable"] is False
        assert verdict["backend"] == "sqlite"


# ===========================================================================
# Schema / version gates (§8 class — evidence 10)
# ===========================================================================


class TestSchemaVersionGates:
    def _gate(self, store):
        from phase3.persistence.schema_gate import check_schema_compatibility

        return check_schema_compatibility(store)

    def test_registry_missing(self, pg_cluster, rig, make_db, open_pg_store) -> None:
        from phase3.persistence.schema_gate import REGISTRY_TABLE_MISSING, SchemaIncompatible

        dsn = make_db("noreg", migrate=False)
        store = open_pg_store(dsn)
        with pytest.raises(SchemaIncompatible) as ei:
            self._gate(store)
        assert ei.value.code == REGISTRY_TABLE_MISSING

    def test_version_record_absent(self, pg_cluster, rig, make_db, open_pg_store) -> None:
        from phase3.persistence.schema_gate import SchemaIncompatible, VERSION_RECORD_ABSENT

        dsn = make_db("norecord")
        store = open_pg_store(dsn)
        store.execute("DELETE FROM schema_migrations")
        with pytest.raises(SchemaIncompatible) as ei:
            self._gate(store)
        assert ei.value.code == VERSION_RECORD_ABSENT

    def test_future_version_rejected(self, pg_cluster, rig, make_db, open_pg_store) -> None:
        from phase3.persistence.schema_gate import (
            SCHEMA_FUTURE_VERSION,
            SchemaIncompatible,
        )

        dsn = make_db("future")
        store = open_pg_store(dsn)
        store.execute("UPDATE schema_migrations SET version = 99")
        with pytest.raises(SchemaIncompatible) as ei:
            self._gate(store)
        assert ei.value.code == SCHEMA_FUTURE_VERSION

    def test_checksum_mismatch_rejected(self, pg_cluster, rig, make_db, open_pg_store) -> None:
        from phase3.persistence.schema_gate import (
            SCHEMA_CHECKSUM_MISMATCH,
            SchemaIncompatible,
        )

        dsn = make_db("checksum")
        store = open_pg_store(dsn)
        store.execute("UPDATE schema_migrations SET checksum = 'tampered'")
        with pytest.raises(SchemaIncompatible) as ei:
            self._gate(store)
        assert ei.value.code == SCHEMA_CHECKSUM_MISMATCH

    def test_failed_migration_rejected(self, pg_cluster, rig, make_db, open_pg_store) -> None:
        from phase3.persistence.schema_gate import (
            SCHEMA_MIGRATION_FAILED,
            SchemaIncompatible,
        )

        dsn = make_db("failedmig")
        store = open_pg_store(dsn)
        store.execute(
            "UPDATE schema_migrations SET error = 'synthetic failure record'"
        )
        with pytest.raises(SchemaIncompatible) as ei:
            self._gate(store)
        assert ei.value.code == SCHEMA_MIGRATION_FAILED

    def test_required_objects_missing(self, pg_cluster, rig, make_db, open_pg_store) -> None:
        from phase3.persistence.schema_gate import (
            REQUIRED_OBJECTS_MISSING,
            SchemaIncompatible,
        )

        dsn = make_db("noobjects")
        store = open_pg_store(dsn)
        # registry intact, but the application object is gone
        store.execute("DROP TABLE signal_log")
        with pytest.raises(SchemaIncompatible) as ei:
            self._gate(store)
        assert ei.value.code == REQUIRED_OBJECTS_MISSING

    def test_gate_never_mutates(self, pg_cluster, rig, make_db, open_pg_store) -> None:
        """The failure paths above are read-only; the registry row that a
        gate refused must remain exactly as it was."""
        from phase3.persistence.schema_gate import SchemaIncompatible

        dsn = make_db("readonly")
        store = open_pg_store(dsn)
        store.execute("UPDATE schema_migrations SET version = 99")
        before = store.execute(
            "SELECT version, checksum FROM schema_migrations"
        ).fetchall()
        with pytest.raises(SchemaIncompatible):
            self._gate(store)
        after = store.execute(
            "SELECT version, checksum FROM schema_migrations"
        ).fetchall()
        assert before == after


def _parse_sqlite(path: Path):
    from phase3.runtime_contract import parse_target

    return parse_target(str(path))


def _sqlite_has_table(raw_db: Any, path: Path, table: str = "macro_daily") -> bool:
    import sqlite3

    if not path.exists():
        return False
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return int(row[0]) == 1
    finally:
        conn.close()


# ===========================================================================
# Cross-process CLI proof (evidence 06/07 artifacts)
# ===========================================================================


class TestIdentityGateCLI:
    def test_cli_pass_as_runtime_role(self, pg_cluster, rig) -> None:
        proc = _run_python(
            "import json,sys;"
            "from phase3.persistence.identity_gate import _cli;"
            "sys.exit(_cli())",
            _clean_env(FIE_DB_TARGET_INTELLIGENCE=rig.dsn_rt()),
        )
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["result"] == "PASS"
        assert payload["role"] == rig.rt_user_
        assert payload["database"] == rig.intel_db_

    def test_cli_rejects_overprivileged_role(self, pg_cluster, rig) -> None:
        proc = _run_python(
            "import sys;"
            "from phase3.persistence.identity_gate import _cli;"
            "sys.exit(_cli())",
            _clean_env(FIE_DB_TARGET_INTELLIGENCE=rig.dsn_bad()),
        )
        assert proc.returncode == 2
        payload = _last_json(proc.stderr)
        assert payload["result"] == "REJECTED"
        assert payload["code"] == "RUNTIME_OVERPRIVILEGED_SCHEMA"

    def test_cli_missing_target_fails_closed(self) -> None:
        proc = _run_python(
            "import sys;"
            "from phase3.persistence.identity_gate import _cli;"
            "sys.exit(_cli())",
            _clean_env(),
        )
        assert proc.returncode == 78

    def test_cli_unreachable_target_is_refusal_not_pass(self, pg_cluster, rig) -> None:
        # a dangling database name on the isolated fixture → open failure
        unreachable = rig.dsn_rt("no-such-db")
        proc = _run_python(
            "import sys;"
            "from phase3.persistence.identity_gate import _cli;"
            "sys.exit(_cli())",
            _clean_env(FIE_DB_TARGET_INTELLIGENCE=unreachable),
        )
        assert proc.returncode == 2
        payload = _last_json(proc.stderr)
        assert payload["result"] == "REFUSED"


# ===========================================================================
# Persistence lifecycle (§3.5 / §8 — evidence 13/14)
# ===========================================================================

_WRITE_SNIPPET = (
    "import json,os\n"
    "from phase3.persistence.backend import open_store, resolve_spec\n"
    "from phase3.persistence.signal_repo import SignalRecord, SignalRepository\n"
    "payload = json.loads(os.environ['__FIE_R3_PAYLOAD__'])\n"
    "store = open_store(resolve_spec())\n"
    "repo = SignalRepository(store)\n"
    "created = repo.upsert(SignalRecord(**payload))\n"
    "print(json.dumps({'created': created, 'count': repo.count()}))\n"
    "store.close()\n"
)

_READ_SNIPPET = (
    "import json,os\n"
    "from phase3.persistence.backend import open_store, resolve_spec\n"
    "from phase3.persistence.signal_repo import SignalRepository\n"
    "wanted = os.environ['__FIE_R3_SIGNAL_ID__']\n"
    "store = open_store(resolve_spec())\n"
    "repo = SignalRepository(store)\n"
    "record = repo.get(wanted)\n"
    "print(json.dumps({'found': record is not None, "
    "'value': (record.value if record else None), "
    "'source_id': (record.source_id if record else None)}))\n"
    "store.close()\n"
)


class TestPersistenceLifecycle:
    def _payload(self) -> dict[str, Any]:
        return {
            "signal_id": f"r3-persist-{secrets.token_hex(5)}",
            "entity_type": "index",
            "entity_id": "PERSIST",
            "signal_type": "close",
            "value": 42.5,
            "unit": "USD",
            "direction": "up",
            "timestamp": "2099-01-01T00:00:00Z",
            "date_bucket": "2099-01-01",
            "source_id": "r3-suite",
            "source_type": "synthetic",
            "metadata": {"purpose": "persistence cross-process proof"},
        }

    def test_close_reopen_in_process_survives(self, pg_cluster, rig, open_pg_store) -> None:
        from phase3.persistence.backend import DatabaseSpec, open_store
        from phase3.persistence.signal_repo import (
            SignalRecord,
            SignalRepository,
        )

        payload = self._payload()
        dsn = rig.dsn_rt()
        spec = DatabaseSpec("postgres", dsn, "explicit", dsn)

        store = open_store(spec)
        repo = SignalRepository(store)
        assert repo.upsert(SignalRecord(**payload)) is True
        count_first = repo.count()
        repo_ = repo
        store.close()  # === application stop

        fresh = open_store(spec)  # === application restart
        fresh_repo = SignalRepository(fresh)
        record = fresh_repo.get(payload["signal_id"])
        assert record is not None
        assert record.value == payload["value"]
        assert record.source_id == payload["source_id"]
        assert fresh_repo.count() == count_first
        del repo_
        fresh.close()

    def test_process_boundary_persistence(self, pg_cluster, rig) -> None:
        """Write in one (sub)process with the RUNTIME identity — read in
        a completely independent process after the first exited."""
        payload = self._payload()
        writer = _run_python(
            _WRITE_SNIPPET.replace("__FIE_R3_PAYLOAD__", "__FIE_R3_PAYLOAD__"),
            _clean_env(
                FIE_DB_TARGET_INTELLIGENCE=rig.dsn_rt(),
                __FIE_R3_PAYLOAD__=json.dumps(payload),
            ),
        )
        assert writer.returncode == 0, writer.stderr
        assert json.loads(writer.stdout)["created"] is True

        # the writer process is GONE here — a brand new process reads back
        reader = _run_python(
            _READ_SNIPPET,
            _clean_env(
                FIE_DB_TARGET_INTELLIGENCE=rig.dsn_rt(),
                __FIE_R3_SIGNAL_ID__=payload["signal_id"],
            ),
        )
        assert reader.returncode == 0, reader.stderr
        found = json.loads(reader.stdout)
        assert found["found"] is True
        assert found["value"] == payload["value"]
        assert found["source_id"] == payload["source_id"]

    def test_service_readiness_after_restart(
        self, pg_cluster, rig
    ) -> None:
        """Application restart → readiness answers gates-inclusive."""
        from phase3.persistence.backend import DatabaseSpec, open_store
        from phase3.service.boundary import DefaultIntelligenceService

        dsn = rig.dsn_rt()

        # --- one application "process" (in-thread instantiation) -----------
        store = open_store(DatabaseSpec("postgres", dsn, "explicit", dsn))
        try:
            service = DefaultIntelligenceService(store, read_only=True)
            first = service.get_health()
            assert first.status == "ok"
        finally:
            store.close()
        # --- restart: brand-new store/service instance ---------------------
        store2 = open_store(DatabaseSpec("postgres", dsn, "explicit", dsn))
        try:
            service2 = DefaultIntelligenceService(store2, read_only=True)
            second = service2.get_health()
            assert second.status == "ok"
            assert second.counts == first.counts
            assert second.backend_kind == "postgres"
        finally:
            store2.close()


# ===========================================================================
# Teardown + topology safety (§8 class — evidence 15)
# ===========================================================================


class TestTeardownAndTopology:
    def test_graceful_teardown_leaves_no_process(self, tmp_path) -> None:
        """A function-scoped mini-cluster proves the stop path: the
        postmaster exits and the port is free again."""
        if PG_BIN is None or not HAS_PSYCOPG:
            pytest.skip("isolated real-PostgreSQL tooling unavailable")
        import psycopg  # noqa: F401

        data = tmp_path / "data"
        data.mkdir()
        (tmp_path / "sock").mkdir()
        port = _ephemeral_loopback_port()
        pwfile = tmp_path / "pw"
        pwfile.write_text("r3-fixture-synth-pw\n")
        pwfile.chmod(0o600)
        subprocess.run(
            [
                str(PG_BIN / "initdb"),
                "-D",
                str(data),
                "-U",
                "postgres",
                "--auth-local=trust",
                "--auth-host=scram-sha-256",
                "--pwfile",
                str(pwfile),
                "--encoding=UTF8",
            ],
            capture_output=True,
            timeout=180,
            check=True,
        )
        (data / "postgresql.conf").open("a").write(
            f"port = {port}\nlisten_addresses = '127.0.0.1'\n"
            f"unix_socket_directories = '{tmp_path}/sock'\n"
        )
        subprocess.run(
            [
                str(PG_BIN / "pg_ctl"),
                "-D",
                str(data),
                "-l",
                str(tmp_path / "pg.log"),
                "-w",
                "start",
            ],
            capture_output=True,
            timeout=120,
            check=True,
        )
        try:
            assert _port_free_after_close(port) is False  # in use now
        finally:
            result = subprocess.run(
                [str(PG_BIN / "pg_ctl"), "-D", str(data), "-w", "-t", "60", "stop"],
                capture_output=True,
                timeout=120,
            )
            assert result.returncode == 0
        # graceful shutdown → port must come right back
        import socket as _socket

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _port_free_after_close(port):
                break
            time.sleep(0.2)
        assert _port_free_after_close(port) is True

    def test_isolated_cluster_is_not_foreign_topology(self, pg_cluster) -> None:
        """The cluster this suite provisions must never be (nor be
        listening on) the production port, the legacy instance port,
        or the legacy /tmp/fie-pg data directory."""
        assert pg_cluster.port_ not in (PRODUCTION_PG_PORT, LEGACY_FIE_PG_PORT)
        assert pg_cluster.port_ >= 1024
        assert "fie-pg" not in pg_cluster.data_dir
        assert Path(pg_cluster.data_dir).is_absolute()

    def test_no_implicit_target_fallback(self, monkeypatch) -> None:
        """R1 fail-closed target resolution: with every target variable
        absent, opening a store is a refusal, never an implicit default."""
        from phase3.persistence.backend import resolve_spec
        from phase3.runtime_contract import FailClosedTarget

        for var in (
            "FIE_DB_TARGET_INTELLIGENCE",
            "FIE_DATABASE_URL",
            "FIE_INTELLIGENCE_DB",
        ):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(FailClosedTarget):
            resolve_spec()

    def test_sanitized_dsn_never_carries_password(self, pg_cluster, rig) -> None:
        from phase3.persistence.backend import (
            DatabaseSpec,
            sanitize_db_url,
        )

        spec = DatabaseSpec("postgres", rig.dsn_rt(), "explicit", rig.dsn_rt())
        masked = sanitize_db_url(spec.original)
        assert password_fragment(rig) not in masked
        assert ":***@" in masked


def password_fragment(rig: Any) -> str:
    return rig.dsn_rt().rsplit("@", 1)[0].rsplit(":", 1)[-1]


def _port_free_after_close(port: int) -> bool:
    import errno

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return False
            raise
        return True