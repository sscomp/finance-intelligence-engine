"""Repository-owned PostgreSQL logical backup/restore contract (6.9B-R4).

The Phase 3B :mod:`phase3.persistence.backup` helpers handle the SQLite
side. This module adds the PostgreSQL side as deterministic operator
tooling driven from ``deploy/bin/fie-backup`` / ``deploy/bin/fie-restore``
(both wrap this module's CLI):

    python -m phase3.persistence.pg_backup backup  --dest DIR …
    python -m phase3.persistence.pg_backup restore --artifact PATH …

Contract highlights (WO §4–§7)
-----------------------------
* ``pg_dump --format=custom`` logical backup (schema + data + grants).
* Backup success is NEVER "exit 0" — the artifact must exist, be
  non-empty, carry a sha256 checksum sidecar and a metadata sheet, and
  record the secret-free source identity fingerprint, the applied
  schema version, the pg_dump tool version and per-public-table row
  counts (the data-verification pin).
* The restore DEFAULTS TO REJECTING any target that is not provably
  disposable: an explicit ``FIE_RESTORE_TARGET_DISPOSABLE=1``
  declaration is required, the target must parse as an explicit
  PostgreSQL target, and the target must not be production-shaped nor
  coincide with a production-shaped backup source. There is NO implicit
  production DSN fallback anywhere.
* Checksum / artifact validation happens BEFORE any target connection —
  mismatch, zero-byte, missing artifact and unconsumable dumps are
  fail-closed stops BEFORE mutation (Cases A/B/C).
* ``pg_restore`` runs with ``--single-transaction --clean --if-exists
  --exit-on-error`` under the migration/owner identity: the restore is
  atomic (any failure leaves NO partial target state — deterministic
  cleanup by construction), object ownership is PRESERVED (never
  silently reassigned to the runtime role) and the restore refuses
  rather than silently reassigning a missing owner role.
* Post-restore verification: schema gate + per-table row-count
  comparison + runtime identity gate on a SEPARATE verification context
  (``FIE_RESTORE_VERIFY_DSN`` / ``--verify-dsn`` — never the mutation
  context, which could otherwise self-certify).
* Insufficient privilege at the restore boundary is reported, never
  worked around with a superuser fallback (Case F).
* Machine-readable failures: one JSON line on stderr with a stable
  ``code``; exit 0 PASS / 2 classified failure / 78 configuration
  refusal (house exit discipline). Diagnostics never carry passwords,
  full DSNs, tokens or real credentials; RTO fields
  (``RESTORE_DB_SECONDS``/``RESTORE_VERIFY_SECONDS``/
  ``RESTORE_APPLICATION_READY_SECONDS``) are measured and reported.

Secret boundary: pg_dump/pg_restore receive connection material only
through the subprocess environment (``PGHOST/PGPORT/PGUSER/PGPASSWORD/
PGDATABASE``) — never as program arguments (no password leaks through
the process list). ``source_identity``/``source_fingerprint`` in the
metadata sheet are the R1 contract's credential-free identity forms.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from phase3.persistence.backend import DatabaseSpec, open_store, resolve_spec
from phase3.runtime_contract import (  # noqa: E402 - see below

    FailClosedTarget,
    TargetSpec,
    Unparseable,
    parse_target,
    target_identity,
)
from phase3.service import operational_events as operations_events  # noqa: E402

# Phase 6.9B-R4 (§9): BACKUP_*/RESTORE_* lifecycle events go through the
# shared operational-logging contract (one JSON line per event on the
# ``fie.operations`` logger, credential fields dropped/redacted).

__all__ = [
    "BACKUP_FORMAT",
    "RESTORE_SINGLE_TRANSACTION",
    "BackupContractError",
    "RestoreContractError",
    "run_backup",
    "run_restore",
    "main",
]

#: Phase 6.9B-R4 (§9): BACKUP_*/RESTORE_* lifecycle events go through the
#: shared operational-logging contract (one JSON line per event on the
#: ``fie.operations`` logger, credential fields dropped/redacted).
from phase3.service import operational_events as operations_events  # noqa: E402

BACKUP_FORMAT = "pg_dump --format=custom (default gzip compression)"
RESTORE_SINGLE_TRANSACTION = True

_EXIT_OK = 0
_EXIT_CLASSIFIED = 2
_EXIT_CONFIG = 78

#: The app-facing schema the data-verification pin counts tables in.
_VERIFIED_SCHEMA = "public"


class BackupContractError(RuntimeError):
    """Stable machine-testable backup contract failure."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class RestoreContractError(RuntimeError):
    """Stable machine-testable restore contract failure."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _emit_failure(exc: Exception) -> None:
    code = getattr(exc, "code", type(exc).__name__)
    event = operations_events.RESTORE_FAILED \
        if isinstance(exc, RestoreContractError) else operations_events.BACKUP_FAILED
    detail = getattr(exc, "detail", "")
    print(
        json.dumps(
            {
                "event": event,
                "result": "REJECTED",
                "code": code,
                "detail": detail,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    operations_events.emit(event, code=code, detail=detail)


def _redact(text: str) -> str:
    """Mask credential-bearing material in tool stderr before quoting."""
    import re

    text = re.sub(
        r"(postgres(?:ql)?://[^:/\s]+:)[^@\s/]+(@)", r"\1***\2", text
    )
    text = re.sub(r"(password\s*[=:]\s*)(\S+)", r"\1***", text, flags=re.I)
    return text


# ---------------------------------------------------------------------------
# DSN credential extraction (subprocess env ONLY — never argv, never logs)
# ---------------------------------------------------------------------------

def dsn_env_credential(dsn: str) -> dict[str, str]:
    """Extract libpq connection env vars from a DSN for pg_dump/pg_restore.

    The returned dict is merged into the subprocess environment ONLY —
    never rendered into logs, JSON outputs or argv. ``PGPASSWORD`` is
    present only when the DSN itself supplied a password.
    """
    parsed = parse_target(dsn)
    _require_pg(parsed, "BACKUP_SOURCE_NOT_POSTGRES")
    text = parsed.value.strip()
    lowered = text.lower()
    env: dict[str, str] = {}
    if lowered.startswith(("postgres://", "postgresql://")):
        from urllib.parse import unquote, urlsplit

        parts = urlsplit(text)
        userinfo, _, hostport = parts.netloc.rpartition("@")
        params: dict[str, list[str]] = {}
        for chunk in (parts.query or "").split("&"):
            if chunk:
                key, _, value = chunk.partition("=")
                params.setdefault(unquote(key).lower(), []).append(
                    unquote(value)
                )

        def _param(key: str) -> str:
            return params.get(key, [""])[0]

        host = _param("host")
        port = _param("port")
        dbname = _param("dbname") or _param("database")
        if not dbname:
            dbname = unquote(parts.path)[1:]
        account = unquote(userinfo)
        if ":" in account:
            user, _, password = account.partition(":")
            env["PGUSER"] = user
            env["PGPASSWORD"] = password
        elif account:
            env["PGUSER"] = account
        if not host:
            host = hostport
        if ":" in host:
            # bare host:port in the userinfo-less netloc form — libpq's
            # PGHOST never carries a port (PGPORT is the separate var)
            bare, _, portpart = host.partition(":")
            host = bare
            if portpart and not port:
                port = portpart
        if host.startswith("[") and "]" in host:
            host = host[1 : host.index("]")]
        if not host or not dbname:
            raise BackupContractError(
                "BACKUP_SOURCE_NOT_POSTGRES",
                "the URL-form DSN lacks host/dbname (value withheld)",
            )
        env["PGHOST"] = host
        env["PGDATABASE"] = dbname
        if port:
            env["PGPORT"] = port
        else:
            env.setdefault("PGPORT", "5432")
    else:
        import shlex

        mapping = {
            "host": "PGHOST",
            "port": "PGPORT",
            "dbname": "PGDATABASE",
            "database": "PGDATABASE",
            "user": "PGUSER",
            "password": "PGPASSWORD",
        }
        pairs = {
            k.lower(): v
            for k, _, v in (t.partition("=") for t in shlex.split(text))
        }
        for key, var in mapping.items():
            if pairs.get(key):
                env[var] = pairs[key]
        if "PGHOST" not in env or "PGDATABASE" not in env:
            raise BackupContractError(
                "BACKUP_SOURCE_NOT_POSTGRES",
                "the keyword-form DSN lacks host/dbname (value withheld)",
            )
        env.setdefault("PGPORT", "5432")
    return env


def _require_pg(spec: TargetSpec, code: str) -> str:
    if spec.backend != "postgres":
        raise BackupContractError(
            code,
            "the resolved target is not PostgreSQL (the SQLite side "
            "lives in the Phase 3B backup.py contract)",
        )
    return "postgres"


# ---------------------------------------------------------------------------
# catalog pins (metadata sheet / restore verification)
# ---------------------------------------------------------------------------

def _public_table_counts(store: Any) -> dict[str, int]:
    rows = store.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n "
        "ON n.oid = c.relnamespace WHERE n.nspname = %s "
        "AND c.relkind = 'r' ORDER BY 1",
        (_VERIFIED_SCHEMA,),
    ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        # rows are mapping-rows in both backends (row[0] is the first
        # COLUMN VALUE in both sqlite3.Row and the PG _Row adapter)
        table = str(row["relname"])
        got = store.execute(f'SELECT count(*) FROM "{table}"').fetchone()
        counts[table] = int(got[0])
    return counts


def _source_schema_version(store: Any) -> int:
    row = store.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations "
        "WHERE error IS NULL"
    ).fetchone()
    return int(row[0]) if row else 0


def _production_shaped(spec: TargetSpec) -> bool | None:
    """Three-state: True (production fingerprint match) / False / UNKNOWN.

    When the production contract set is unreadable on this environment,
    the source's production-shape is UNKNOWN (None) — never silently
    False ("unprovable" is not a non-production proof).
    """
    from phase3.runtime_contract import FailClosedTarget, load_production_identities

    try:
        prod = load_production_identities()
    except FailClosedTarget:
        return None
    try:
        return target_identity(spec).fingerprint in prod.fingerprints
    except Exception:  # noqa: BLE001 - unparseable identity is not a match
        return False


def _utc_stamp() -> str:
    """UTC timestamp for artifact names (module-level for testability)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pg_tool_version(tool: str) -> str:
    proc = subprocess.run([tool, "--version"], capture_output=True,
                          text=True, timeout=30)
    return (proc.stdout.strip() or proc.stderr.strip())


def _resolve_source(explicit_target: str | None) -> TargetSpec:
    """R1 resolution: explicit argument > canonical env (no soft default)."""
    if explicit_target and explicit_target.strip():
        try:
            return parse_target(explicit_target)
        except (FailClosedTarget, Unparseable) as exc:
            raise BackupContractError(
                "BACKUP_SOURCE_NOT_POSTGRES",
                "the supplied source does not parse as an explicit "
                "PostgreSQL target (fail-closed; value withheld)",
            ) from exc
    resolved = resolve_spec(None)
    try:
        return parse_target(resolved.dsn)
    except (FailClosedTarget, Unparseable) as exc:
        raise BackupContractError(
            "BACKUP_SOURCE_NOT_POSTGRES",
            "the resolved source is not an explicit PostgreSQL target "
            "(fail-closed; value withheld)",
        ) from exc


# ---------------------------------------------------------------------------
# backup contract
# ---------------------------------------------------------------------------

def run_backup(
    explicit_target: str | None = None,
    dest_dir: str | None = None,
    *,
    label: str | None = None,
    keep_partial: bool = False,
) -> dict[str, Any]:
    """Execute the full backup contract and return the summary dict.

    Every negative path raises :class:`BackupContractError` — the CLI
    renders the machine-readable line and exits 2. A successful return
    implies all WO §4 success properties: the artifact exists and is
    non-empty, the checksum was generated (sidecar + metadata), the
    metadata sheet exists, the secret-free source identity + schema
    version are recorded, and the artifact is pg_restore-consumable
    (validated by ``pg_restore --list`` BEFORE reporting PASS).
    Partial artifacts are deterministically cleaned at every failure.
    """
    t0 = time.monotonic()
    if not (dest_dir or "").strip():
        raise BackupContractError(
            "BACKUP_DESTINATION_INVALID",
            "a --dest directory is required (the backup destination is "
            "an explicit contract, never a default)",
        )
    spec = _resolve_source(explicit_target)
    _require_pg(spec, "BACKUP_SOURCE_NOT_POSTGRES")
    env_credential = dsn_env_credential(spec.value)
    identity = target_identity(spec)

    dest = Path(dest_dir).expanduser()
    if dest.exists() and not dest.is_dir():
        raise BackupContractError(
            "BACKUP_DESTINATION_INVALID",
            f"destination names a non-directory: {dest}",
        )
    dest.mkdir(parents=True, exist_ok=True)

    # --- live catalog pin (schema version + row counts) --------------------
    try:
        store = open_store(
            DatabaseSpec("postgres", spec.value, "explicit", spec.env_var)
        )
    except Exception as exc:  # noqa: BLE001 - classified refusal below
        raise BackupContractError(
            "BACKUP_SOURCE_UNREACHABLE",
            "the source database is not openable (detail withheld)",
        ) from exc
    try:
        try:
            schema_version = _source_schema_version(store)
            table_counts = _public_table_counts(store)
        except Exception as exc:  # noqa: BLE001
            raise BackupContractError(
                "BACKUP_SOURCE_UNREACHABLE",
                "the source catalog pin is not readable (detail withheld)",
            ) from exc
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass

    # --- pg_dump under env credentials (never argv) ------------------------
    stamp = _utc_stamp()
    operations_events.emit(operations_events.BACKUP_STARTED,
                           backend="postgres")
    safe_label = (label or "fie").replace("/", "_").replace(" ", "_")
    artifact_name = f"{safe_label}-{stamp}.backup"
    final_path = dest / artifact_name
    if final_path.exists():
        raise BackupContractError(
            "BACKUP_DESTINATION_INVALID",
            "destination already holds a same-named artifact "
            "(backup artifacts are never silently overwritten)",
        )
    partial_path = dest / f".{artifact_name}.partial"
    partial_path.unlink(missing_ok=True)

    try:
        proc = subprocess.run(
            ["pg_dump", "--format=custom", "--file", str(partial_path)],
            env={**os.environ, **env_credential},
            capture_output=True,
            text=True,
            timeout=600,
        )
        if proc.returncode != 0:
            raise BackupContractError(
                "BACKUP_DUMP_FAILED",
                "pg_dump exited rc=%d (stderr redacted, tail withheld)"
                % proc.returncode,
            )
        if not partial_path.is_file() or partial_path.stat().st_size == 0:
            raise BackupContractError(
                "BACKUP_DUMP_FAILED",
                "pg_dump exited rc=0 but produced no artifact",
            )
        # consumability gate: pg_restore --list must accept the artifact
        if not _pg_restore_list_validate(partial_path):
            raise BackupContractError(
                "BACKUP_DUMP_FAILED",
                "pg_restore --list does not accept the fresh artifact "
                "(unconsumable dump — partial artifact cleaned)",
            )
        checksum = _sha256(partial_path)
        source_prod_shape = _production_shaped(spec)
        meta = {
            "artifact": artifact_name,
            "artifact_bytes": partial_path.stat().st_size,
            "checksum_sha256": checksum,
            "compression": BACKUP_FORMAT,
            "created_utc": stamp,
            "label": safe_label,
            "pg_dump_version": _pg_tool_version("pg_dump"),
            "schema_version_applied": schema_version,
            "source_backend": "postgres",
            "source_fingerprint": identity.fingerprint,
            "source_identity": identity.text,
            "source_production_shaped": source_prod_shape,
            "table_row_counts": table_counts,
        }
        meta_path = dest / f"{artifact_name}.meta.json"
        sha_path = dest / f"{artifact_name}.sha256"
        meta_path.write_text(
            json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        sha_path.write_text(
            f"{checksum}  {artifact_name}\n", encoding="utf-8"
        )
        # the artifact is last: consumers that see the final name see a
        # complete (artifact, meta, checksum) triple
        os.rename(partial_path, final_path)
        operations_events.emit(
            operations_events.BACKUP_COMPLETED, artifact=artifact_name,
            bytes=meta["artifact_bytes"],
        )
    except BackupContractError:
        if not keep_partial:
            partial_path.unlink(missing_ok=True)
        _cleanup_orphan_siblings(dest, artifact_name)
        raise
    except Exception:  # noqa: BLE001 - deterministic cleanup on any failure
        if not keep_partial:
            partial_path.unlink(missing_ok=True)
        _cleanup_orphan_siblings(dest, artifact_name)
        raise
    return {
        "result": "PASS",
        "artifact": artifact_name,
        "dest": str(dest),
        "bytes": meta["artifact_bytes"],
        "checksum_sha256": checksum,
        "created_utc": stamp,
        "label": safe_label,
        "schema_version_applied": schema_version,
        "source_fingerprint": identity.fingerprint,
        "source_identity": identity.text,
        "source_production_shaped": source_prod_shape,
        "table_row_counts": table_counts,
        "pg_dump_version": meta["pg_dump_version"],
        "elapsed_seconds": round(time.monotonic() - t0, 3),
        "events": [
            {"event": "BACKUP_STARTED"},
            {"event": "BACKUP_COMPLETED", "artifact": artifact_name},
        ],
    }


def _cleanup_orphan_siblings(dest: Path, artifact_name: str) -> None:
    """Deterministic partial-failure cleanup: no orphan sidecars."""
    for suffix in (".meta.json", ".sha256"):
        (dest / f"{artifact_name}{suffix}").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# restore contract
# ---------------------------------------------------------------------------

#: Gate order for WO §6 Cases A–F: A/B/C BEFORE any target contact,
#: D/G/E BEFORE mutation, then the atomic restore, then I/J/K verification.
def run_restore(
    artifact_path: str,
    explicit_target: str | None = None,
    explicit_verify_dsn: str | None = None,
    *,
    disposable_declared: bool = False,
    expected_source_fingerprint: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Execute the full restore contract; machine-readable on every path.

    Gates, in fail-closed order:

    A  artifact missing                  →  BACKUP_NOT_FOUND (no contact)
    B  artifact zero-byte / unreadable / no metadata / unconsumable
                                         →  BACKUP_INVALID   (no contact)
    C  checksum mismatch                 →  CHECKSUM_MISMATCH (no contact)
    D  unprovable/undeclared/production target
                                         →  RESTORE_TARGET_NOT_DISPOSABLE
    G  target coincides with a production-shaped source
                                         →  RESTORE_TARGET_COINCIDES_WITH_SOURCE
    E  target unreachable                →  RESTORE_TARGET_UNREACHABLE
    H  pg_restore (single transaction)   →  atomic; failure leaves no state
    F  privilege boundary                →  RESTORE_PERMISSION_DENIED
       (never superuser fallback)
    I/J/K schema / data / identity verify →  no PASS unless all hold
    """
    t0 = time.monotonic()
    e = dict(os.environ if env is None else env)
    operations_events.emit(operations_events.RESTORE_STARTED)

    # --- Cases A/B/C: artifact intake, NO target contact yet --------------
    artifact = Path(artifact_path)
    if not artifact.is_file():
        raise RestoreContractError(
            "BACKUP_NOT_FOUND", f"no artifact at the supplied path ({artifact_path})",
        )
    if artifact.stat().st_size == 0:
        raise RestoreContractError(
            "BACKUP_INVALID",
            "the artifact is zero-byte (truncated/incomplete backup)",
        )
    checksum = _sha256(artifact)
    meta = _read_meta(artifact)
    if not meta:
        raise RestoreContractError(
            "BACKUP_INVALID",
            "the artifact has no readable metadata sheet "
            "(``<artifact>.meta.json``) — unverifiable backups are not "
            "consumable",
        )
    sidecar = artifact.with_name(f"{artifact.name}.sha256")
    if sidecar.is_file():
        recorded = sidecar.read_text(encoding="utf-8").strip().split()[0]
        if recorded != checksum:
            raise RestoreContractError(
                "CHECKSUM_MISMATCH",
                "the sidecar sha256 disagrees with the artifact bytes — "
                "restore STOPS BEFORE any target mutation",
            )
    if not _pg_restore_list_validate(artifact):
        raise RestoreContractError(
            "BACKUP_INVALID",
            "pg_restore --list does not accept the artifact (corrupt/"
            "truncated custom-format dump)",
        )
    if (
        expected_source_fingerprint
        and meta.get("source_fingerprint") != expected_source_fingerprint
    ):
        raise RestoreContractError(
            "RESTORE_SOURCE_META_MISMATCH",
            "the artifact's recorded source fingerprint does not match "
            "the operator-pinned expectation",
        )

    # --- Case D/G: target parsing + disposable proof (before contact) -----
    if not (explicit_target or "").strip():
        raise RestoreContractError(
            "RESTORE_TARGET_UNPARSEABLE",
            "no restore target was supplied (--db-target / "
            "FIE_RESTORE_TARGET_DSN; an implicit environment fallback "
            "is forbidden)",
        )
    try:
        tspec = parse_target(explicit_target)
    except (FailClosedTarget, Unparseable) as exc:
        raise RestoreContractError(
            "RESTORE_TARGET_UNPARSEABLE",
            "the restore target does not parse as an explicit "
            "PostgreSQL target (fail-closed; no implicit fallback)",
        ) from exc
    if tspec.backend != "postgres":
        raise RestoreContractError(
            "RESTORE_TARGET_NOT_DISPOSABLE",
            "the restore target is not a PostgreSQL target (the SQLite "
            "side lives in the Phase 3B contract)",
        )
    if not disposable_declared:
        raise RestoreContractError(
            "RESTORE_TARGET_NOT_DISPOSABLE",
            "FIE_RESTORE_TARGET_DISPOSABLE=1 (or --disposable) is "
            "required: restore refuses any target that is not "
            "explicitly declared disposable (fail-closed default)",
        )
    if not _target_provably_disposable(tspec, env=e):
        raise RestoreContractError(
            "RESTORE_TARGET_NOT_DISPOSABLE",
            "the target cannot be PROVEN disposable: its identity "
            "fingerprint matches the production contract, or the "
            "production contract set is unreadable on this environment "
            "(unprovable is not safe) — restore refuses",
        )
    source_shape = meta.get("source_production_shaped")
    if source_shape is not False and target_identity(
        tspec
    ).fingerprint == meta.get("source_fingerprint"):
        raise RestoreContractError(
            "RESTORE_TARGET_COINCIDES_WITH_SOURCE",
            "the artifact's source production-shape is not provably "
            "non-production (true or unknown) and the target coincides "
            "with its fingerprint — an in-place rewrite of an unknown/"
            "production source is never silent restore behavior",
        )

    # --- verification context (never self-certifying) ---------------------
    verify_dsn = (explicit_verify_dsn or "").strip() or e.get(
        "FIE_RESTORE_VERIFY_DSN", ""
    ).strip()
    if not verify_dsn:
        raise RestoreContractError(
            "RESTORE_VERIFY_CONTEXT_REQUIRED",
            "FIE_RESTORE_VERIFY_DSN / --verify-dsn is required: the "
            "restore mutation context can never certify its own result",
        )
    try:
        vspec = parse_target(verify_dsn)
    except (FailClosedTarget, Unparseable) as exc:
        raise RestoreContractError(
            "RESTORE_TARGET_UNPARSEABLE",
            "the verification DSN does not parse as an explicit "
            "PostgreSQL target (fail-closed)",
        ) from exc
    if vspec.backend != "postgres":
        raise RestoreContractError(
            "RESTORE_TARGET_NOT_DISPOSABLE",
            "the verification context is not a PostgreSQL target",
        )

    # --- Case E: target preflight (reachable + readable) -------------------
    try:
        tstore = open_store(
            DatabaseSpec("postgres", tspec.value, "explicit", tspec.env_var)
        )
    except Exception as exc:  # noqa: BLE001
        raise RestoreContractError(
            "RESTORE_TARGET_UNREACHABLE",
            "the restore target is not openable (detail withheld)",
        ) from exc
    try:
        # liveness-only probe: a low-privilege (or deliberately starved)
        # target context must still reach the pg_restore gate so real
        # privilege failures classify as RESTORE_PERMISSION_DENIED,
        # not as "unreachable"
        tstore.execute("SELECT 1").fetchone()
    except Exception as exc:  # noqa: BLE001
        raise RestoreContractError(
            "RESTORE_TARGET_UNREACHABLE",
            "the restore target is not openable (detail withheld)",
        ) from exc
    finally:
        try:
            tstore.close()
        except Exception:  # noqa: BLE001
            pass

    # --- Case H (+F): pg_restore — atomic, owner-preserving ---------------
    env_credential = dsn_env_credential(tspec.value)
    env_credential["PGHOST"] = env_credential.get("PGHOST", "")
    restored_proc = subprocess.run(
        [
            "pg_restore",
            "--single-transaction",
            "--clean",
            "--if-exists",
            "--exit-on-error",
            # pg_restore requires an explicit -d even under PG* env vars
            # (without it no database connection is attempted at all)
            "--dbname", env_credential["PGDATABASE"],
            str(artifact),
        ],
        env={**os.environ, **env_credential},
        capture_output=True,
        text=True,
        timeout=900,
    )
    t1 = time.monotonic()
    restore_seconds = round(t1 - t0, 3)
    if restored_proc.returncode != 0:
        code, detail = _classify_pg_restore_failure(restored_proc)
        raise RestoreContractError(code, detail)

    # --- I/J/K: schema / data / runtime-identity verification -------------
    try:
        vstore = open_store(
            DatabaseSpec("postgres", vspec.value, "explicit", vspec.env_var)
        )
    except Exception as exc:  # noqa: BLE001
        raise RestoreContractError(
            "RESTORE_SCHEMA_INCOMPATIBLE",
            "the verification context cannot open the restored database "
            "(detail withheld)",
        ) from exc
    try:
        verify_report = _post_restore_verify(vstore, meta)
    finally:
        try:
            vstore.close()
        except Exception:  # noqa: BLE001
            pass
    t2 = time.monotonic()
    verify_seconds = round(t2 - t1, 3)
    operations_events.emit(
        operations_events.RESTORE_COMPLETED, artifact=artifact.name,
        restore_db_seconds=restore_seconds,
        restore_verify_seconds=verify_seconds,
    )
    # T3: application readiness — schema gate + identity gate + data pin
    # are the persistence-plane readiness contract (the same probes the
    # service boundary's health op consults); the application-plane
    # /readyz verdict over the identical verify context is exercised in
    # the acceptance suite (R4 tests) and recorded in evidence.
    ready_seconds = round(t2 - t0, 3)
    return {
        "result": "PASS",
        "artifact": str(artifact),
        "verify": verify_report,
        "single_transaction": RESTORE_SINGLE_TRANSACTION,
        "RESTORE_DB_SECONDS": restore_seconds,
        "RESTORE_VERIFY_SECONDS": verify_seconds,
        "RESTORE_APPLICATION_READY_SECONDS": ready_seconds,
        "verify_context_backend": "postgres",
    }


def _post_restore_verify(vstore: Any, meta: dict[str, Any]) -> dict[str, Any]:
    """schema + data + runtime-identity gates over one verification store."""
    from phase3.persistence.identity_gate import (
        IdentityGateUnavailable,
        IdentityIncompatible,
        check_runtime_identity,
    )
    from phase3.persistence.schema_gate import (
        SchemaGateUnavailable,
        SchemaIncompatible,
        check_schema_compatibility,
    )

    verified: list[str] = []
    try:
        check_schema_compatibility(vstore)
    except SchemaIncompatible as exc:
        raise RestoreContractError(
            "RESTORE_SCHEMA_INCOMPATIBLE",
            "the restored schema is not application-compatible "
            f"({exc.code}) — restore is NOT a PASS",
        ) from exc
    except SchemaGateUnavailable as exc:
        raise RestoreContractError(
            "RESTORE_SCHEMA_INCOMPATIBLE",
            f"the restored registry is unreadable ({exc}) — restore is "
            "NOT a PASS",
        ) from exc
    verified.append("schema_compatible")

    want_counts = meta.get("table_row_counts") or {}
    try:
        got_counts = _public_table_counts(vstore)
    except Exception as exc:  # noqa: BLE001
        raise RestoreContractError(
            "RESTORE_DATA_MISMATCH",
            "the restored tables are not countable (catalog unreadable)",
        ) from exc
    mismatches = {
        table: {"backup": want_count, "restored": got_counts.get(table)}
        for table, want_count in want_counts.items()
        if got_counts.get(table) != want_count
    }
    if mismatches:
        raise RestoreContractError(
            "RESTORE_DATA_MISMATCH",
            "restored row counts disagree with the backup metadata sheet: "
            + json.dumps(mismatches, sort_keys=True),
        )
    verified.append("data_row_counts_match")

    try:
        identity_report = check_runtime_identity(vstore)
    except IdentityIncompatible as exc:
        raise RestoreContractError(
            "RESTORE_IDENTITY_INCOMPATIBLE",
            f"{exc.code}: {exc.detail}",
        ) from exc
    except IdentityGateUnavailable as exc:
        raise RestoreContractError(
            "RESTORE_IDENTITY_INCOMPATIBLE",
            f"the restored runtime identity is not verifiable: {exc}",
        ) from exc
    verified.append("runtime_identity_pass")
    return {"verified": verified, "runtime_identity": identity_report}


def _target_provably_disposable(tspec: TargetSpec, env: dict[str, str]) -> bool:
    """Disposable target proof: declared AND parsed AND provably
    non-production (a readable production contract set that does NOT
    match). An unreadable production-contract world proves nothing →
    False (fail closed)."""
    from phase3.runtime_contract import FailClosedTarget, load_production_identities

    try:
        prod = load_production_identities(env)
    except FailClosedTarget:
        return False
    try:
        return target_identity(tspec).fingerprint not in prod.fingerprints
    except Exception:  # noqa: BLE001 - unparseable identity is a refusal
        return False


def _read_meta(artifact: Path) -> dict[str, Any] | None:
    meta_path = artifact.with_name(f"{artifact.name}.meta.json")
    if not meta_path.is_file():
        return None
    try:
        parsed = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _pg_restore_list_validate(artifact: Path) -> bool:
    """``pg_restore --list`` must accept the artifact (no target contact)."""
    proc = subprocess.run(
        ["pg_restore", "--list", str(artifact)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        return False
    return bool(proc.stdout.strip())


def _classify_pg_restore_failure(
    proc: subprocess.CompletedProcess[str],
) -> tuple[str, str]:
    stderr = _redact(proc.stderr or "").strip()
    lines = [ln for ln in stderr.splitlines() if ln.strip()]
    tail = lines[-1] if lines else ""
    lowered = stderr.lower()
    if (
        "permission denied" in lowered
        or "is not owner" in lowered
        or "authorization" in lowered
    ):
        return (
            "RESTORE_PERMISSION_DENIED",
            "pg_restore failed at the privilege boundary (tail withheld) "
            "— no superuser fallback exists in this contract",
        )
    return (
        "RESTORE_FAILED",
        "pg_restore exited rc=%d inside the single transaction — the "
        "target is left unchanged (atomic rollback); stderr redacted"
        % proc.returncode,
    )


# ---------------------------------------------------------------------------
# CLI (wrapped by deploy/bin/fie-backup / deploy/bin/fie-restore)
# ---------------------------------------------------------------------------

def _parse_args(args: list[str], known: set[str]) -> dict[str, str]:
    opts: dict[str, str] = {}
    index = 0
    while index < len(args):
        token = args[index]
        if token in known:
            if "=" in token:
                key, _, value = token.partition("=")
                opts[key] = value
                index += 1
                continue
            if index + 1 < len(args) and not args[index + 1].startswith("--"):
                opts[token] = args[index + 1]
                index += 2
                continue
            opts[token] = ""
            index += 1
            continue
        index += 1
    return opts


def _flag(args: list[str], name: str) -> bool:
    return name in args or any(a.startswith(name + "=") for a in args)


def cli_backup(args: list[str]) -> int:
    """``backup --dest DIR [--label L] [--db-target DSN] [--keep-partial]``."""
    opts = _parse_args(args, {"--dest", "--label", "--db-target", "--keep-partial"})
    try:
        report = run_backup(
            opts.get("--db-target"),
            opts.get("--dest"),
            label=opts.get("--label") or None,
            keep_partial=_flag(args, "--keep-partial"),
        )
    except BackupContractError as exc:
        _emit_failure(exc)
        return _EXIT_CLASSIFIED
    report.pop("events", None)
    print(json.dumps(report, sort_keys=True))
    return _EXIT_OK


def cli_restore(args: list[str]) -> int:
    """``restore --artifact PATH --db-target DSN [--verify-dsn DSN] …``."""
    opts = _parse_args(
        args,
        {"--artifact", "--db-target", "--verify-dsn", "--expected-source", "--disposable"},
    )
    artifact = opts.get("--artifact")
    if not artifact:
        _emit_failure(
            RestoreContractError(
                "BACKUP_NOT_FOUND", "--artifact PATH is required",
            )
        )
        return _EXIT_CLASSIFIED
    try:
        report = run_restore(
            artifact,
            opts.get("--db-target"),
            opts.get("--verify-dsn"),
            disposable_declared=_flag(args, "--disposable")
            or _env_flag("FIE_RESTORE_TARGET_DISPOSABLE"),
            expected_source_fingerprint=opts.get("--expected-source") or None,
        )
    except RestoreContractError as exc:
        _emit_failure(exc)
        return _EXIT_CLASSIFIED
    report.pop("events", None)
    print(json.dumps(report, sort_keys=True))
    return _EXIT_OK


def _env_flag(name: str) -> bool:
    return (os.environ.get(name, "") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "backup":
        return cli_backup(args[1:])
    if args and args[0] == "restore":
        return cli_restore(args[1:])
    print(
        json.dumps(
            {
                "result": "REFUSED",
                "code": "CONFIGURATION_INVALID",
                "detail": "usage: python -m phase3.persistence.pg_backup "
                "backup|restore … (repository entry points: "
                "deploy/bin/fie-backup, deploy/bin/fie-restore)",
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return _EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())