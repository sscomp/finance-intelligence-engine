"""Phase 6.8A Task I1 — canonical runtime-contract resolver unit tests.

Covers the acceptance items the WO names for the resolver:

- explicit PostgreSQL target;
- explicit SQLite target;
- missing target;
- malformed target;
- unsupported scheme/backend;
- conflicting aliases;
- equivalent aliases;
- deprecated alias compatibility;
- normalized PG identity;
- normalized SQLite identity;
- credentials redaction.

All resolution tests pass an explicit ``env`` mapping — the suite never
depends on (and never mutates) the host environment, and never dials a
real database (identity-level only, no connections).
"""
import os
import tempfile
import unittest

from phase3.runtime_contract import (
    BACKEND_POSTGRES,
    BACKEND_SQLITE,
    ROLE_INTELLIGENCE,
    ROLE_RAW,
    FailClosedTarget,
    Unparseable,
    parse_postgres_dsn,
    parse_target,
    resolve_intelligence_target,
    resolve_raw_target,
    resolve_role_target,
    sanitize_target,
    target_identity,
    targets_equivalent,
)

# Fixture DSNs: identity-bearing but credential-free placeholders
# (single-line literals; no real host/db).
DSN_A = "postgresql://scn_a:demo@localhost:5433/scn_a_db?sslmode=disable"
DSN_A_ALIAS = "postgres://other_user:x@127.0.0.1:5433/scn_a_db"
DSN_A_SOCKET = "postgres://u:x/var/run/pg?host=/tmp/fie-pg&port=5433"
DSN_B = "postgresql://scn_b:demo@localhost:5434/scn_b_db"


class ExplicitTargetTests(unittest.TestCase):
    def test_explicit_postgresql_target(self) -> None:
        spec = parse_target(DSN_A)
        self.assertEqual(spec.backend, BACKEND_POSTGRES)
        self.assertEqual(spec.value, DSN_A)
        self.assertEqual(spec.source, "explicit")

    def test_explicit_sqlite_target_absolute(self) -> None:
        spec = parse_target("/tmp/fie_i1_fixture.db")
        self.assertEqual(spec.backend, BACKEND_SQLITE)
        self.assertEqual(spec.value, "/tmp/fie_i1_fixture.db")

    def test_explicit_sqlite_target_scheme(self) -> None:
        for value in ("sqlite:///tmp/fie_i1_fixture.db",
                      "sqlite:/tmp/fie_i1_fixture.db"):
            spec = parse_target(value)
            self.assertEqual(spec.backend, BACKEND_SQLITE)
            self.assertEqual(spec.value, "/tmp/fie_i1_fixture.db")

    def test_memory_target_is_explicit_sqlite(self) -> None:
        spec = parse_target(":memory:")
        self.assertEqual(spec.backend, BACKEND_SQLITE)
        self.assertEqual(spec.value, ":memory:")


class MalformedAndUnsupportedTests(unittest.TestCase):
    def test_missing_target_fail_closed(self) -> None:
        for role in (ROLE_RAW, ROLE_INTELLIGENCE):
            with self.assertRaises(FailClosedTarget) as ctx:
                resolve_role_target(role, None, {})
            self.assertEqual(ctx.exception.reason,
                             "FAIL_CLOSED_DB_TARGET_REQUIRED")

    def test_relative_path_malformed(self) -> None:
        with self.assertRaises(FailClosedTarget) as ctx:
            parse_target("relative/path.db")
        self.assertEqual(ctx.exception.reason,
                         "FAIL_CLOSED_MALFORMED_DB_TARGET")

    def test_empty_target_required_class(self) -> None:
        with self.assertRaises(FailClosedTarget) as ctx:
            parse_target("   ")
        self.assertEqual(ctx.exception.reason,
                         "FAIL_CLOSED_DB_TARGET_REQUIRED")

    def test_unsupported_named_scheme(self) -> None:
        for scheme in ("mysql", "oracle", "duckdb", "http"):
            with self.assertRaises(FailClosedTarget) as ctx:
                parse_target(f"{scheme}://host/value")
            self.assertEqual(ctx.exception.reason,
                             "FAIL_CLOSED_MALFORMED_DB_TARGET")

    def test_unsupported_generic_scheme(self) -> None:
        with self.assertRaises(FailClosedTarget) as ctx:
            parse_target("foo://host/value")
        self.assertEqual(ctx.exception.reason,
                         "FAIL_CLOSED_MALFORMED_DB_TARGET")

    def test_unparseable_pg_dsn(self) -> None:
        with self.assertRaises(Unparseable):
            parse_postgres_dsn("postgres://")
        with self.assertRaises(FailClosedTarget):
            parse_target("postgres://")


class AliasResolutionTests(unittest.TestCase):
    def test_deprecated_alias_compatibility_raw(self) -> None:
        spec = resolve_raw_target(None, {"FIE_DB_PATH": DSN_A})
        self.assertEqual(spec.backend, BACKEND_POSTGRES)
        self.assertEqual(spec.source, "legacy_alias")
        self.assertEqual(spec.env_var, "FIE_DB_PATH")

    def test_deprecated_alias_compatibility_intelligence(self) -> None:
        for alias in ("FIE_DATABASE_URL", "FIE_INTELLIGENCE_DB"):
            spec = resolve_intelligence_target(None, {alias: DSN_A})
            self.assertEqual(spec.source, "legacy_alias")
            self.assertEqual(spec.env_var, alias)

    def test_conflicting_aliases_fail_closed(self) -> None:
        env = {"FIE_DATABASE_URL": DSN_A, "FIE_INTELLIGENCE_DB": DSN_B}
        with self.assertRaises(FailClosedTarget) as ctx:
            resolve_intelligence_target(None, env)
        self.assertEqual(ctx.exception.reason,
                         "FAIL_CLOSED_CONTRADICTORY_DB_TARGETS")

    def test_conflicting_alias_backends_fail_closed(self) -> None:
        env = {"FIE_DB_PATH": "/tmp/one.db", "FIE_DB_TARGET_RAW": DSN_A}
        with self.assertRaises(FailClosedTarget) as ctx:
            resolve_raw_target(None, env)
        self.assertEqual(ctx.exception.reason,
                         "FAIL_CLOSED_CONTRADICTORY_DB_TARGETS")

    def test_equivalent_aliases_accepted(self) -> None:
        env = {"FIE_DATABASE_URL": DSN_A, "FIE_INTELLIGENCE_DB": DSN_A_ALIAS}
        spec = resolve_intelligence_target(None, env)
        self.assertEqual(spec.backend, BACKEND_POSTGRES)

    def test_canonical_with_equivalent_alias_accepted(self) -> None:
        env = {"FIE_DB_TARGET_RAW": DSN_A, "FIE_DB_PATH": DSN_A_ALIAS}
        spec = resolve_raw_target(None, env)
        self.assertEqual(spec.value, DSN_A)
        self.assertEqual(spec.source, "canonical_env")

    def test_canonical_contradicting_alias_fail_closed(self) -> None:
        env = {"FIE_DB_TARGET_RAW": DSN_B, "FIE_DB_PATH": DSN_A}
        with self.assertRaises(FailClosedTarget) as ctx:
            resolve_raw_target(None, env)
        self.assertEqual(ctx.exception.reason,
                         "FAIL_CLOSED_CONTRADICTORY_DB_TARGETS")

    def test_explicit_beats_env(self) -> None:
        env = {"FIE_DB_TARGET_RAW": DSN_B}
        spec = resolve_raw_target(DSN_A_ALIAS, env)
        self.assertEqual(spec.value, DSN_A_ALIAS)
        self.assertEqual(spec.source, "explicit")


class NormalizedIdentityTests(unittest.TestCase):
    def test_pg_hosts_and_default_port_coalesce(self) -> None:
        first = target_identity(parse_target(DSN_A))
        # a portless DSN applies the default port 5432
        no_port = target_identity(parse_target("postgresql://localhost/scn_a_db"))
        self.assertIn("@5432@", no_port.text)
        loopback = target_identity(
            parse_target("postgres://u:x@127.0.0.1:5433/scn_a_db"))
        self.assertEqual(first.text, loopback.text)

    def test_alias_dsn_forms_are_equivalent(self) -> None:
        a = target_identity(parse_target(DSN_A)).text
        b = target_identity(parse_target(DSN_A_ALIAS)).text
        self.assertEqual(a, b)

    def test_normalized_sqlite_identity(self) -> None:
        plain = target_identity(parse_target("/tmp/fie_i1_fixture.db"))
        scheme = target_identity(parse_target("sqlite:///tmp/fie_i1_fixture.db"))
        self.assertEqual(plain.text, scheme.text)

    def test_cross_backend_never_equivalent(self) -> None:
        pg = parse_target(DSN_A)
        sl = parse_target("/tmp/scn_a_db")
        self.assertFalse(targets_equivalent(pg, sl))

    def test_fingerprint_stable_and_hex(self) -> None:
        fp = target_identity(parse_target(DSN_A)).fingerprint
        self.assertEqual(fp,
                         target_identity(parse_target(DSN_A_ALIAS)).fingerprint)
        self.assertTrue(all(c in "0123456789abcdef" for c in fp))


class CredentialRedactionTests(unittest.TestCase):
    def test_url_password_masked_only(self) -> None:
        rendered = sanitize_target(parse_target(DSN_A))
        self.assertNotIn("demo", rendered)
        self.assertNotIn("scn_a:demo", rendered)
        self.assertIn("***", rendered)
        self.assertIn("scn_a_db", rendered)  # dbname is not a credential

    def test_keyword_password_masked(self) -> None:
        rendered = sanitize_target(
            parse_target("postgresql://u@localhost/db password=pw123 host=localhost"))
        self.assertNotIn("pw123", rendered)
        self.assertIn("password=***", rendered)

    def test_identity_never_carries_credentials(self) -> None:
        identity = target_identity(parse_target(DSN_A))
        self.assertNotIn("demo", identity.text)
        self.assertNotIn("scn_a:", identity.text)


class ServiceEnvClassificationTests(unittest.TestCase):
    def test_rehearsal_modes(self) -> None:
        from phase3.runtime_contract import classify_service_env
        self.assertEqual(classify_service_env("test"), "rehearsal")
        self.assertEqual(classify_service_env("STAGING"), "rehearsal")
        self.assertEqual(classify_service_env(" staging "), "rehearsal")

    def test_production_modes(self) -> None:
        from phase3.runtime_contract import classify_service_env
        self.assertEqual(classify_service_env(""), "production")
        self.assertEqual(classify_service_env(None), "production")
        self.assertEqual(classify_service_env("local"), "production")
        self.assertEqual(classify_service_env("Production"), "production")

    def test_invalid_mode_fail_closed(self) -> None:
        from phase3.runtime_contract import classify_service_env
        for junk in ("prod", "develop", "yes", "1"):
            with self.assertRaises(FailClosedTarget) as ctx:
                classify_service_env(junk)
            self.assertEqual(ctx.exception.reason,
                             "FAIL_CLOSED_PRODUCTION_MODE_DECLARATION_REQUIRED")


class ProductionIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fie_i1_prod_")
        self.dir = self._tmp.__enter__()
        self.addCleanup(self._tmp.__exit__, None, None, None)
        self.contract = os.path.join(self.dir, "contract.env")
        with open(self.contract, "w", encoding="utf-8") as fh:
            fh.write("export FIE_DB_PATH=postgresql://prod_raw@localhost:5433/prod_raw\n")
            fh.write("export FIE_INTELLIGENCE_DB=postgresql://prod_int@localhost:5433/prod_int\n")
            fh.write("export PGPASSWORD=never_in_identity\n")

    def test_identities_loaded_from_contract_files(self) -> None:
        from phase3.runtime_contract import load_production_identities
        prod = load_production_identities({}, [self.contract])
        self.assertIn(target_identity(parse_target(
            "postgresql://x@localhost:5433/prod_raw")).fingerprint,
            prod.fingerprints)
        self.assertIn(target_identity(parse_target(
            "postgresql://x@localhost:5433/prod_int")).fingerprint,
            prod.fingerprints)
        self.assertEqual(prod.sources, (self.contract,))

    def test_secret_keys_never_contribute(self) -> None:
        from phase3.runtime_contract import load_production_identities
        prod = load_production_identities({}, [self.contract])
        # the password value contributed no identity of its own and never
        # appears in any identity text (fingerprints are hex digests)
        self.assertEqual(len(prod.fingerprints), 2)
        joined = ",".join(prod.postgres_identity_texts)
        self.assertNotIn("never_in_identity", joined)
        self.assertNotIn("never_in_identity", ",".join(prod.sqlite_paths))

    def test_missing_contract_fail_closed(self) -> None:
        from phase3.runtime_contract import load_production_identities
        with self.assertRaises(FailClosedTarget) as ctx:
            load_production_identities({}, [])
        self.assertEqual(ctx.exception.reason,
                         "FAIL_CLOSED_PRODUCTION_IDENTITY_UNAVAILABLE")

    def test_dsnfree_contract_fail_closed(self) -> None:
        # 6.8C Task G: a readable contract file with no DB contract values
        # (DSN-free) must also fail closed — it proves nothing about the
        # Production identity (unknown != safe; docstring guarantee).
        from phase3.runtime_contract import load_production_identities
        empty = os.path.join(self.dir, "dsn_free_contract.env")
        with open(empty, "w", encoding="utf-8") as fh:
            fh.write("export FIE_TELEGRAM_CHAT_ID='0'\n")
            fh.write("# comments and non-DB keys only\n")
        with self.assertRaises(FailClosedTarget) as ctx:
            load_production_identities({}, [empty])
        self.assertEqual(ctx.exception.reason,
                         "FAIL_CLOSED_PRODUCTION_IDENTITY_UNAVAILABLE")


class RehearsalSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fie_i1_rehearse_")
        self.dir = self._tmp.__enter__()
        self.addCleanup(self._tmp.__exit__, None, None, None)
        self.contract = os.path.join(self.dir, "contract.env")
        self.pg_id = "postgresql://prod_int@localhost:5433/prod_int"
        self.pg_id_alias = "postgres://anybody:z@127.0.0.1:5433/prod_int"
        self.prod_sqlite = os.path.join(self.dir, "prod_store.db")
        with open(self.contract, "w", encoding="utf-8") as fh:
            fh.write(f"export FIE_INTELLIGENCE_DB={self.pg_id}\n")
            fh.write(f"export FIE_DB_PATH={self.prod_sqlite}\n")
        # materialize the production sqlite file so inode checks can arm
        with open(self.prod_sqlite, "wb") as fh:
            fh.write(b"fie_i1 production fixture bytes")

    def test_production_equivalent_pg_rejected(self) -> None:
        from phase3.runtime_contract import (assert_rehearsal_target_safe,
                                             load_production_identities)
        prod = load_production_identities({}, [self.contract])
        for dsn in (self.pg_id, self.pg_id_alias):
            with self.assertRaises(FailClosedTarget) as ctx:
                assert_rehearsal_target_safe(parse_target(dsn), prod)
            self.assertEqual(ctx.exception.reason,
                             "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED")

    def test_disposable_pg_accepted(self) -> None:
        from phase3.runtime_contract import (assert_rehearsal_target_safe,
                                             load_production_identities)
        prod = load_production_identities({}, [self.contract])
        # same host/port CLASS but a different database must be safe
        assert_rehearsal_target_safe(
            parse_target("postgres://u:x@localhost:5433/rehearsal_int"), prod)

    def test_same_host_different_db_is_safe(self) -> None:
        from phase3.runtime_contract import (assert_rehearsal_target_safe,
                                             load_production_identities)
        prod = load_production_identities({}, [self.contract])
        assert_rehearsal_target_safe(
            parse_target("postgresql://prod_int@localhost:5433/other_db"), prod)

    def test_production_sqlite_path_rejected(self) -> None:
        from phase3.runtime_contract import (assert_rehearsal_target_safe,
                                             load_production_identities)
        prod = load_production_identities({}, [self.contract])
        with self.assertRaises(FailClosedTarget):
            assert_rehearsal_target_safe(parse_target(self.prod_sqlite), prod)

    def test_hardlink_to_prod_sqlite_rejected(self) -> None:
        from phase3.runtime_contract import (assert_rehearsal_target_safe,
                                             load_production_identities)
        import sqlite3  # noqa: F401  (kept for symmetry; link is enough)
        prod = load_production_identities({}, [self.contract])
        link = os.path.join(self.dir, "link.db")
        if hasattr(os, "link"):
            os.link(self.prod_sqlite, link)
            with self.assertRaises(FailClosedTarget):
                assert_rehearsal_target_safe(parse_target(link), prod)

    def test_unresolved_identity_is_unsafe(self) -> None:
        from phase3.runtime_contract import (assert_rehearsal_target_safe,
                                             load_production_identities)
        with self.assertRaises(FailClosedTarget):
            assert_rehearsal_target_safe(parse_target("/tmp/any.db"),
                                         load_production_identities({}, []))

    def test_disposable_sqlite_accepted(self) -> None:
        from phase3.runtime_contract import (assert_rehearsal_target_safe,
                                             load_production_identities)
        prod = load_production_identities({}, [self.contract])
        assert_rehearsal_target_safe(
            parse_target(os.path.join(self.dir, "rehearsal_store.db")), prod)
        assert_rehearsal_target_safe(parse_target(":memory:"), prod)


if __name__ == "__main__":
    unittest.main(verbosity=2)