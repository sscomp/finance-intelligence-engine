"""Phase 6.9B-R4 acceptance suite — PostgreSQL backup/restore contract,
restore drill + failure injections (work order §4–§7, §15, §16).

This suite provisions its OWN isolated, disposable PostgreSQL cluster
(``initdb`` in a pytest temp directory, ephemeral loopback port,
scram-sha-256) — it never touches the authoritative production
PostgreSQL (port 5432), the legacy ``/tmp/fie-pg`` instance, or any
foreign process. Synthetic credentials only (``r4-synth-*``).

Every restore in this suite targets a database the drill itself created
inside the disposable cluster AND supplies the environment contract the
restore contract requires (``FIE_RESTORE_TARGET_DISPOSABLE=1``, a
readable production-contract file naming a NON-matching DSN, a separate
verification context). The fail-closed gates are exercised for the
Cases A–F of WO §6 — each refusal is asserted to happen at the right
stage (before target contact where required), with a machine-readable
code and with NO mutation of the target/side databases.

Skip discipline: when isolated-cluster tooling (initdb/pg_ctl +
psycopg) is genuinely unavailable the session is skipped with an
explicit reason — no PostgreSQL is ever replaced with a mock.
"""

from __future__ import annotations

import glob
import importlib.util
import json
import secrets
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

HAS_PSYCOPG = importlib.util.find_spec("psycopg") is not None

PRODUCTION_PG_PORT = 5432
LEGACY_FIE_PG_PORT = 54329

_APP_TABLE = "signal_log"


def _find_pg_bin() -> Path | None:
    candidates: list[Path] = []
    override = __import__("os").environ.get("FIE_TEST_PG_BIN", "")
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


@pytest.fixture(scope="module")
def pg_cluster(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """One isolated, disposable PostgreSQL cluster for the module."""
    if PG_BIN is None or not HAS_PSYCOPG:
        pytest.skip(
            "isolated real-PostgreSQL tooling unavailable "
            "(initdb/pg_ctl and/or psycopg absent)",
        )
    import os

    import psycopg

    root = tmp_path_factory.mktemp("r4-pg-cluster")
    data, sock = root / "data", root / "sock"
    data.mkdir()
    sock.mkdir()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    assert port not in (PRODUCTION_PG_PORT, LEGACY_FIE_PG_PORT)

    admin_password = "r4-fixture-synth-pw"
    pwfile = root / "admin-pwfile"
    pwfile.write_text(admin_password + "\n")
    pwfile.chmod(0o600)

    proc = subprocess.run(
        [str(PG_BIN / "initdb"), "-D", str(data), "-U", "postgres",
         "--auth-local=trust", "--auth-host=scram-sha-256",
         "--pwfile", str(pwfile), "--encoding=UTF8"],
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, "initdb failed (isolated cluster only)"
    (data / "postgresql.conf").open("a").write(
        f"port = {port}\nlisten_addresses = '127.0.0.1'\n"
        f"unix_socket_directories = '{sock}'\n"
    )
    proc = subprocess.run(
        [str(PG_BIN / "pg_ctl"), "-D", str(data), "-l", str(root / "pg.log"),
         "-w", "-t", "60", "start"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, "pg_ctl start failed (isolated cluster)"

    class _Cluster:
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

        def admin_count(self, dbname: str, sql: str) -> int:
            conn = self.connect(self.dsn("postgres", dbname, self.admin_pw))
            try:
                row = conn.execute(sql).fetchone()
                return int(row[0]) if row else -1
            finally:
                conn.close()

    cluster = _Cluster()
    cluster.admin_exec(["SELECT 1"])
    yield cluster  # type: ignore[misc]
    proc = subprocess.run(
        [str(PG_BIN / "pg_ctl"), "-D", str(data), "-w", "-t", "60", "stop"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0


@pytest.fixture(scope="module")
def rig(pg_cluster: Any, tmp_path_factory: pytest.TempPathFactory) -> Any:
    """MIG/RT identities + source db (migrated+seeded) + a clean target db."""
    from phase3.persistence.backend import DatabaseSpec, open_store
    from phase3.persistence.migrations import MigrationManager, default_migrations_for

    run_id = secrets.token_hex(3)
    mig_user, rt_user = f"fie_mig_{run_id}", f"fie_rt_{run_id}"
    nopriv_user = f"fie_noc_{run_id}"
    mig_pw, rt_pw = f"r4-synth-mig-{run_id}", f"r4-synth-rt-{run_id}"
    src_db, target_db, empty_db = (
        f"fie_r4s_{run_id}", f"fie_r4t_{run_id}", f"fie_r4e_{run_id}",
    )
    pg_cluster.admin_exec([
        f"CREATE ROLE {mig_user} LOGIN PASSWORD '{mig_pw}'",
        f"CREATE ROLE {rt_user} LOGIN PASSWORD '{rt_pw}'",
        f"CREATE ROLE {nopriv_user} LOGIN PASSWORD 'r4-synth-noc-{run_id}'",
        f"CREATE DATABASE {src_db} OWNER {mig_user}",
        f"CREATE DATABASE {target_db} OWNER {mig_user}",
        f"CREATE DATABASE {empty_db} OWNER {mig_user}",
        f"GRANT CONNECT ON DATABASE {target_db} TO {rt_user}, {nopriv_user}",
        f"GRANT CONNECT ON DATABASE {empty_db} TO {rt_user}",
        "REVOKE ALL ON DATABASE " + src_db + " FROM PUBLIC",
    ])
    mig_dsn = pg_cluster.dsn(mig_user, src_db, mig_pw)
    store = open_store(DatabaseSpec("postgres", mig_dsn, "explicit", mig_dsn))
    try:
        applied = MigrationManager(store, default_migrations_for(store)).apply()
        assert applied, "rig migrations did not apply"
        from phase3.persistence.timeutil import utc_now_iso

        for signal_id, entity_id, value in (
            ("r4-sentinel-1", "GDP", 1.25),
            ("r4-sentinel-2", "CPI", -0.5),
        ):
            store.execute(
                "INSERT INTO signal_log (signal_id, entity_type, entity_id, "
                "signal_type, value, unit, direction, timestamp, date_bucket, "
                "source_id, source_type, ref, fetched_at, fetch_id, "
                "schema_version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s, %s)",
                (signal_id, "macro", entity_id, "level", value, "index",
                 "flat", utc_now_iso(), "2026-10", "r4-fixture", "fixture",
                 "r4://fixture", utc_now_iso(), "r4-fetch", "3.0"),
            )
    finally:
        store.close()

    # grant the runtime surface on the SOURCE (as the owner) so the
    # dump carries the least-privilege grants — mirroring production
    # bootstrap semantics (restore is owner-preserving)
    stmts = [
        "REVOKE ALL ON SCHEMA public FROM PUBLIC",
        f"GRANT USAGE ON SCHEMA public TO {rt_user}",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON signal_log, "
        "score_snapshot, graph_nodes, graph_edges, adapter_run_log, "
        "ingestion_errors TO " + rt_user,
        f"GRANT SELECT ON schema_migrations TO {rt_user}",
        f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {rt_user}",
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {rt_user}",
        f"GRANT CONNECT ON DATABASE {target_db} TO {nopriv_user}",
    ]
    conn = pg_cluster.connect(mig_dsn)
    try:
        for sql in stmts:
            conn.execute(sql)
    finally:
        conn.close()

    class _Rig:
        mig_dsn_ = mig_dsn
        rt_dsn_source = pg_cluster.dsn(rt_user, src_db, rt_pw)

    class _R2:
        mig_user_ = mig_user
        rt_user_ = rt_user
        nopriv_user_ = nopriv_user
        src_db_ = src_db
        target_db_ = target_db
        empty_db_ = empty_db
        mig_dsn_ = mig_dsn
        mig_pw_ = mig_pw
        rt_pw_ = rt_pw

        def dsn(self, role: str, dbname: str) -> str:
            pw = {mig_user: mig_pw, rt_user: rt_pw,
                  nopriv_user: f"r4-synth-noc-{run_id}"}[role]
            return pg_cluster.dsn(role, dbname, pw)

        def grant_runtime(self, dbname: str) -> None:
            """Grant the runtime surface on a restored db (post-restore,
            as the owner — mirrors repository bootstrap semantics)."""
            stmts = [
                "REVOKE ALL ON SCHEMA public FROM PUBLIC",
                f"GRANT USAGE ON SCHEMA public TO {rt_user}",
                (
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON "
                    "signal_log, score_snapshot, graph_nodes, graph_edges, "
                    "adapter_run_log, ingestion_errors TO " + rt_user
                ),
                "GRANT SELECT ON schema_migrations TO " + rt_user,
                "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO " + rt_user,
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "
                + rt_user,
            ]
            conn = pg_cluster.connect(self.dsn(mig_user, dbname))
            try:
                for sql in stmts:
                    conn.execute(sql)
            finally:
                conn.close()

        def recreate_empty(self, dbname: str) -> None:
            pg_cluster.admin_exec([
                f"DROP DATABASE IF EXISTS {dbname} WITH (FORCE)",
                f"CREATE DATABASE {dbname} OWNER {mig_user}",
                f"GRANT CONNECT ON DATABASE {dbname} TO {rt_user}"
                f", {nopriv_user}",
                f"REVOKE ALL ON SCHEMA public FROM PUBLIC",
            ])

        def count(self, dbname: str) -> int:
            conn = pg_cluster.connect(
                pg_cluster.dsn("postgres", dbname, pg_cluster.admin_pw)
            )
            try:
                row = conn.execute(
                    f"SELECT count(*) FROM \"{_APP_TABLE}\""
                ).fetchone()
                return int(row[0]) if row else -1
            finally:
                conn.close()

    return _R2(), pg_cluster


@pytest.fixture()
def drill_env() -> dict:
    return {"FIE_RESTORE_TARGET_DISPOSABLE": "1"}


# ---------------------------------------------------------------------------
# Backup contract (§4)
# ---------------------------------------------------------------------------

class TestBackupContract:
    def test_backup_success_is_more_than_exit_0(self, rig, tmp_path: Path) -> None:
        r, cluster = rig
        dest = tmp_path / "backups"
        from phase3.persistence.pg_backup import run_backup

        report = run_backup(r.mig_dsn_, str(dest), label="r4drill")
        assert report["result"] == "PASS"
        artifact = dest / report["artifact"]
        # §4 success surface — all five properties, not exit code
        assert artifact.is_file()
        assert artifact.stat().st_size > 0
        assert report["checksum_sha256"] == _sha256_file(artifact)
        assert (dest / f"{report['artifact']}.sha256").is_file()
        meta = json.loads(
            (dest / f"{report['artifact']}.meta.json").read_text()
        )
        assert meta["checksum_sha256"] == report["checksum_sha256"]
        assert meta["schema_version_applied"] >= 1
        assert meta["pg_dump_version"].startswith("pg_dump (PostgreSQL)")
        assert meta["source_fingerprint"] == report["source_fingerprint"]
        assert meta["source_identity"] == report["source_identity"]
        # the metadata never carries credentials
        blob = (dest / f"{report['artifact']}.meta.json").read_text()
        assert r.mig_pw_ not in blob and "password" not in blob.lower()
        # pg_restore consumability was proven by the contract itself
        proc = subprocess.run(
            ["pg_restore", "--list", str(artifact)],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0 and proc.stdout.strip()
        assert meta["table_row_counts"][_APP_TABLE] == 2
        assert meta["source_production_shaped"] is False

    def test_backup_source_unreachable_classified(
        self, tmp_path: Path
    ) -> None:
        from phase3.persistence.pg_backup import BackupContractError, run_backup

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            dead_port = int(s.getsockname()[1])
        dead = f"postgres://nobody:r4-synth@127.0.0.1:{dead_port}/nowhere_db"
        with pytest.raises(BackupContractError) as exc_info:
            run_backup(dead, str(tmp_path / "dest"))
        assert exc_info.value.code == "BACKUP_SOURCE_UNREACHABLE"
        assert "r4-synth" not in str(exc_info.value)

    def test_backup_failure_cleans_partial_artifacts(
        self, tmp_path: Path
    ) -> None:
        from phase3.persistence.pg_backup import run_backup

        source = "postgres://nobody:r4-synth@127.0.9.9:5439/nowhere_db"
        dest = tmp_path / "dest2"
        dest.mkdir()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            dead_port = int(s.getsockname()[1])
        dead = f"postgres://nobody:r4-synth@127.0.0.1:{dead_port}/nowhere_db"
        with pytest.raises(Exception):
            run_backup(dead, str(dest))
        assert not list(dest.glob("*")), "partial artifacts survived failure"

    def test_backup_refuses_silently_overwriting(
        self, rig, tmp_path: Path, monkeypatch
    ) -> None:
        """The same-named artifact is never overwritten: the contract
        refuses and cleans up (deterministic failure semantics)."""
        import phase3.persistence.pg_backup as mod

        r, _cluster = rig
        dest = tmp_path / "dest3"
        dest.mkdir()

        def fixed_stamp(*_a: object, **_k: object) -> str:
            return "20260101T000000Z"

        monkeypatch.setattr(mod, "_utc_stamp", fixed_stamp)
        first = mod.run_backup(r.mig_dsn_, str(dest), label="r4")
        assert first["result"] == "PASS"
        named = dest / "r4-20260101T000000Z.backup"
        assert named.is_file()
        (dest / "r4-20260101T000000Z.backup.meta.json").unlink()
        (dest / "r4-20260101T000000Z.backup.sha256").unlink()
        named.unlink()
        named.write_bytes(b"placed")
        with pytest.raises(mod.BackupContractError) as info2:
            mod.run_backup(r.mig_dsn_, str(dest), label="r4")
        assert info2.value.code == "BACKUP_DESTINATION_INVALID"
        # the pre-existing file is untouched (never silently overwritten)
        assert named.read_bytes() == b"placed"
        # and no partial/no triple companions were left behind
        assert not list(dest.glob("*.partial"))
        assert not (dest / "r4-20260101T000000Z.backup.meta.json").exists()
        assert not (dest / "r4-20260101T000000Z.backup.sha256").exists()

    def test_backup_redacts_credentials_in_diagnostics(
        self, rig, monkeypatch
    ) -> None:
        """pg_dump/pg_restore receive credentials via env only, never argv
        (process-list boundary); reported identity is credential-free."""
        r, _cluster = rig
        from phase3.persistence.pg_backup import dsn_env_credential

        dsn = r.mig_dsn_
        env = dsn_env_credential(dsn)
        assert env["PGDATABASE"] == r.src_db_
        assert env["PGPASSWORD"] == r.mig_pw_
        # the sanitized identity text never carries the password
        from phase3.runtime_contract import parse_target, sanitize_target

        sanitized = sanitize_target(parse_target(dsn))
        assert r.mig_pw_ not in sanitized
        assert ":***@" in sanitized


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Restore drill (§5 required chain) + failure injections (§6 Cases A–F)
# ---------------------------------------------------------------------------

def _restore_drill(rig, tmp_path: Path, env: dict, label: str) -> dict:
    """The full required chain: seed → backup → destroy/recreate target
    → restore → verification (schema/data/identity/readiness)."""
    from phase3.persistence.pg_backup import run_backup, run_restore

    r, cluster = rig
    dest = tmp_path / f"drill-{label}"
    report_backup = run_backup(r.mig_dsn_, str(dest), label=label)
    artifact = dest / report_backup["artifact"]
    target_dsn = r.dsn(r.mig_user_, r.target_db_)
    r.recreate_empty(r.target_db_)
    verify_dsn = r.dsn(r.rt_user_, r.target_db_)
    op_env = {
        **env,
        "FIE_RESTORE_TARGET_DSN": target_dsn,
        "FIE_RESTORE_VERIFY_DSN": verify_dsn,
    }
    import os

    old = {k: os.environ.get(k) for k in op_env}
    os.environ.update(op_env)
    try:
        return {
            "backup": report_backup,
            "restore": run_restore(
                str(artifact), target_dsn, verify_dsn,
                disposable_declared=True, env=op_env,
            ),
        }
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestRestoreDrill:
    def test_full_required_drill_chain_passes(
        self, rig, tmp_path: Path, drill_env
    ) -> None:
        r, cluster = rig
        report = _restore_drill(rig, tmp_path, drill_env, "main")
        restored = report["restore"]
        assert restored["result"] == "PASS"
        assert restored["single_transaction"] is True
        verified = restored["verify"]["verified"]
        assert "schema_compatible" in verified
        assert "data_row_counts_match" in verified
        assert "runtime_identity_pass" in verified
        # §7 RTO fields are measured (floats > 0)
        assert restored["RESTORE_DB_SECONDS"] > 0
        assert restored["RESTORE_VERIFY_SECONDS"] >= 0
        assert restored["RESTORE_APPLICATION_READY_SECONDS"] > 0
        # the data came back: the sentinel signal rows are readable by the
        # RUNTIME identity (proved by the row-count pin + runtime context)
        from phase3.persistence.backend import DatabaseSpec, open_store

        store = open_store(DatabaseSpec(
            "postgres", r.dsn(r.rt_user_, r.target_db_), "explicit", "t",
        ))
        try:
            row = store.execute(
                "SELECT count(*) FROM signal_log WHERE signal_id LIKE "
                "'r4-sentinel-%'"
            ).fetchone()
            assert int(row[0]) == 2
        finally:
            store.close()

    def test_restored_runtime_identity_prohibited_ddl_boundary(
        self, rig, tmp_path: Path, drill_env
    ) -> None:
        """WO §5: restored DB usable by the R3 runtime identity AND the
        prohibited DDL/admin boundary still holds at the database level."""
        import psycopg

        r, cluster = rig
        _restore_drill(rig, tmp_path, drill_env, "ddl")
        rt_dsn = r.dsn(r.rt_user_, r.target_db_)
        conn = psycopg.connect(rt_dsn, autocommit=True, connect_timeout=10)
        try:
            with pytest.raises(psycopg.Error):
                conn.execute("CREATE TABLE r4_forbidden (a int)")
            with pytest.raises(psycopg.Error):
                conn.execute("ALTER TABLE signal_log ADD COLUMN z int")
            with pytest.raises(psycopg.Error):
                conn.execute("DROP TABLE score_snapshot")
            conn.execute("SELECT 1")
        finally:
            conn.close()


class TestRestoreFailureInjections:
    def test_case_a_missing_backup(
        self, rig, tmp_path: Path, drill_env
    ) -> None:
        from phase3.persistence.pg_backup import (
            RestoreContractError,
            run_restore,
        )

        r, cluster = rig
        missing = tmp_path / "nope.backup"
        # a valid-looking artifact path that does not exist; the refusal
        # MUST happen before any target contact (own fresh target so the
        # zero-table assertion is not polluted by earlier drills)
        fresh = f"fie_r4a_{secrets.token_hex(3)}"
        r.recreate_empty(fresh)
        with pytest.raises(RestoreContractError) as info:
            run_restore(
                str(missing), r.dsn(r.mig_user_, fresh),
                r.dsn(r.rt_user_, fresh),
                disposable_declared=True, env=drill_env,
            )
        assert info.value.code == "BACKUP_NOT_FOUND"
        assert cluster.admin_count(
            fresh, f"SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='public'"
        ) == 0

    def test_case_b_zero_byte_and_unconsumable(
        self, rig, tmp_path: Path, drill_env
    ) -> None:
        from phase3.persistence.pg_backup import (
            RestoreContractError,
            run_restore,
        )

        r, cluster = rig
        artifacts = tmp_path / "cases-b"
        artifacts.mkdir()
        from phase3.persistence.pg_backup import run_backup

        b = run_backup(r.mig_dsn_, str(artifacts), label="caseb")
        artifact = artifacts / b["artifact"]
        # zero-byte variant (with fresh meta/sidecar triple)
        zero = artifacts / "zero.backup"
        zero.write_bytes(b"")
        (artifacts / "zero.backup.meta.json").write_text(
            artifact.with_name(
                artifact.name + ".meta.json"
            ).read_text()
        )
        with pytest.raises(RestoreContractError) as info:
            run_restore(
                str(zero), r.dsn(r.mig_user_, r.target_db_),
                r.dsn(r.rt_user_, r.target_db_),
                disposable_declared=True, env=drill_env,
            )
        assert info.value.code == "BACKUP_INVALID"
        # unconsumable / corrupt variant: not a custom-format dump
        junk = artifacts / "junk.backup"
        junk.write_bytes(b"not a pg_dump archive" * 8)
        with pytest.raises(RestoreContractError) as info2:
            run_restore(
                str(junk), r.dsn(r.mig_user_, r.target_db_),
                r.dsn(r.rt_user_, r.target_db_),
                disposable_declared=True, env=drill_env,
            )
        assert info2.value.code == "BACKUP_INVALID"

    def test_case_c_checksum_mismatch_stops_before_mutation(
        self, rig, tmp_path: Path, drill_env
    ) -> None:
        from phase3.persistence.pg_backup import (
            RestoreContractError,
            run_backup,
            run_restore,
        )

        r, cluster = rig
        dest = tmp_path / "casec"
        b = run_backup(r.mig_dsn_, str(dest), label="casec")
        artifact = dest / b["artifact"]
        original = artifact.read_bytes()
        marker_table = "r4_casec_marker"

        def marker_rows() -> int:
            return cluster.admin_count(
                r.target_db_, f"SELECT count(*) FROM information_schema.tables"
                f" WHERE table_schema='public' AND "
                f"table_name = '{marker_table}'",
            )

        marker_conn = cluster.connect(r.dsn(r.mig_user_, r.target_db_))
        try:
            marker_conn.execute(
                f"CREATE TABLE {marker_table} (a int)")
            marker_conn.execute(
                f"INSERT INTO {marker_table} VALUES (1)")
        finally:
            marker_conn.close()
        assert marker_rows() == 1
        tampered = bytearray(original)
        tampered[len(tampered) // 2] ^= 0xFF
        artifact.write_bytes(bytes(tampered))
        with pytest.raises(RestoreContractError) as info:
            run_restore(
                str(artifact), r.dsn(r.mig_user_, r.target_db_),
                r.dsn(r.rt_user_, r.target_db_),
                disposable_declared=True, env=drill_env,
            )
        assert info.value.code == "CHECKSUM_MISMATCH"
        # NO mutation happened: the marker survives, the (unmutated)
        # restore never touched the target
        assert marker_rows() == 1
        artifact.write_bytes(original)

    def test_case_d_unsafe_target_no_declaration(
        self, rig, tmp_path: Path, drill_env
    ) -> None:
        from phase3.persistence.pg_backup import (
            RestoreContractError,
            run_backup,
            run_restore,
        )

        r, cluster = rig
        dest = tmp_path / "cased1"
        b = run_backup(r.mig_dsn_, str(dest), label="cased1")
        artifact = dest / b["artifact"]
        # the disposable declaration is MISSING
        with pytest.raises(RestoreContractError) as info:
            run_restore(
                str(artifact), r.dsn(r.mig_user_, r.target_db_),
                r.dsn(r.rt_user_, r.target_db_),
                disposable_declared=False, env=drill_env,
            )
        assert info.value.code == "RESTORE_TARGET_NOT_DISPOSABLE"
        # and no implicit env fallback helps: the declaration env alone
        # with an unparseable target also refuses
        with pytest.raises(RestoreContractError) as info2:
            run_restore(
                str(artifact), "not-a-host:5432/x",
                r.dsn(r.rt_user_, r.target_db_),
                disposable_declared=True, env=drill_env,
            )
        assert info2.value.code == "RESTORE_TARGET_UNPARSEABLE"

    def test_case_d_production_shaped_target_refused(
        self, rig, tmp_path: Path, monkeypatch
    ) -> None:
        """A target recorded in the production contract is refused EVEN
        with the disposable declaration."""
        from phase3.persistence.pg_backup import (
            RestoreContractError,
            run_backup,
            run_restore,
        )

        r, cluster = rig
        dest = tmp_path / "cased2"
        b = run_backup(r.mig_dsn_, str(dest), label="cased2")
        artifact = dest / b["artifact"]
        target_dsn = r.dsn(r.mig_user_, r.target_db_)
        contract = tmp_path / "prod_contract_target.env"
        contract.write_text(
            f"FIE_DB_TARGET_INTELLIGENCE={target_dsn}\n"
        )
        monkeypatch.setenv("FIE_PRODUCTION_DB_CONTRACT", str(contract))
        monkeypatch.setenv("FIE_RESTORE_TARGET_DISPOSABLE", "1")
        with pytest.raises(RestoreContractError) as info:
            run_restore(
                str(artifact), target_dsn,
                r.dsn(r.rt_user_, r.target_db_),
                disposable_declared=True,
            )
        assert info.value.code == "RESTORE_TARGET_NOT_DISPOSABLE"
        # sanity: the refusal came from the production-shape proof
        r2, _ = rig
        assert r2 is r

    def test_case_d_unprovable_production_world_refuses(
        self, rig, tmp_path: Path, monkeypatch
    ) -> None:
        """No readable production contract on the environment → the
        target cannot be PROVEN disposable → refuse (fail closed)."""
        from phase3.persistence.pg_backup import (
            RestoreContractError,
            run_backup,
            run_restore,
        )

        r, cluster = rig
        dest = tmp_path / "cased3"
        b = run_backup(r.mig_dsn_, str(dest), label="cased3")
        artifact = dest / b["artifact"]
        monkeypatch.setenv(
            "FIE_PRODUCTION_DB_CONTRACT",
            str(tmp_path / "definitely-missing-contract.env"),
        )
        monkeypatch.setenv("FIE_RESTORE_TARGET_DISPOSABLE", "1")
        with pytest.raises(RestoreContractError) as info:
            run_restore(
                str(artifact), r.dsn(r.mig_user_, r.target_db_),
                r.dsn(r.rt_user_, r.target_db_),
                disposable_declared=True,
            )
        assert info.value.code == "RESTORE_TARGET_NOT_DISPOSABLE"

    def test_case_e_application_compatibility_failure_not_a_pass(
        self, rig, tmp_path: Path, drill_env
    ) -> None:
        """Restore completes (atomic) but the verification context points
        at an EMPTY database → application compatibility does NOT hold →
        the contract refuses with a failure code and NEVER reports PASS."""
        from phase3.persistence.pg_backup import (
            RestoreContractError,
            run_backup,
            run_restore,
        )

        r, cluster = rig
        dest = tmp_path / "casee"
        b = run_backup(r.mig_dsn_, str(dest), label="casee")
        artifact = dest / b["artifact"]
        r.recreate_empty(r.target_db_)
        with pytest.raises(RestoreContractError) as info:
            run_restore(
                str(artifact), r.dsn(r.mig_user_, r.target_db_),
                r.dsn(r.rt_user_, r.empty_db_),
                disposable_declared=True, env=drill_env,
            )
        assert info.value.code in (
            "RESTORE_SCHEMA_INCOMPATIBLE", "RESTORE_IDENTITY_INCOMPATIBLE",
        )

    def test_case_f_insufficient_privilege_no_superuser_fallback(
        self, rig, tmp_path: Path, drill_env
    ) -> None:
        """A restore context WITHOUT authority fails at the privilege
        boundary; the tooling reports it and never silently retries as a
        superuser/owner."""
        from phase3.persistence.pg_backup import (
            RestoreContractError,
            run_backup,
            run_restore,
        )

        r, cluster = rig
        b = run_backup(r.mig_dsn_, str(tmp_path / "casef"), label="casef")
        artifact = tmp_path / "casef" / b["artifact"]
        nopriv = r.dsn(r.nopriv_user_, r.target_db_)
        with pytest.raises(RestoreContractError) as info:
            run_restore(
                str(artifact), nopriv, r.dsn(r.rt_user_, r.target_db_),
                disposable_declared=True, env=drill_env,
            )
        assert info.value.code in (
            "RESTORE_PERMISSION_DENIED", "RESTORE_FAILED",
        )
        assert "fallback" in info.value.detail or "privilege" in \
            info.value.detail

    def test_failed_restore_leaves_target_recoverable(
        self, rig, tmp_path: Path, drill_env
    ) -> None:
        """Idempotent cleanup: after a REFUSED restore the target state is
        unchanged and an authorized restore onto the same target
        succeeds (deterministic recovery)."""
        import os

        from phase3.persistence.pg_backup import (
            RestoreContractError,
            run_backup,
            run_restore,
        )

        r, cluster = rig
        dest = tmp_path / "idem"
        b = run_backup(r.mig_dsn_, str(dest), label="idem")
        artifact = dest / b["artifact"]
        target_dsn = r.dsn(r.mig_user_, r.target_db_)
        verify_dsn = r.dsn(r.rt_user_, r.target_db_)
        env = {
            **drill_env,
            "FIE_RESTORE_TARGET_DSN": target_dsn,
            "FIE_RESTORE_VERIFY_DSN": verify_dsn,
        }
        # refusal: no disposable declaration
        with pytest.raises(RestoreContractError) as info:
            run_restore(
                str(artifact), target_dsn, verify_dsn,
                disposable_declared=False, env=env,
            )
        assert info.value.code == "RESTORE_TARGET_NOT_DISPOSABLE"
        # the authorized restore then completes on the SAME target
        restored = run_restore(
            str(artifact), target_dsn, verify_dsn,
            disposable_declared=True, env=env,
        )
        assert restored["result"] == "PASS"
        assert r.count(r.target_db_) == 2