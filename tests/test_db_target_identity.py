#!/usr/bin/env python3
"""Unit tests for scripts/db_target_identity.py (guard identity helper).

Pure functions, no network, no DB — every "production" identity below is
a fixture on an unbound loopback port. Locks the semantic equivalences the
rehearsal guard depends on (WO Task B/C): alias DSN spellings resolve to
the same production identity; password/user never participate; loopback
aliases coalesce; malformed input fails with Unparseable.
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.db_target_identity import (  # noqa: E402
    Unparseable,
    equivalent,
    identity_fingerprint,
    parse_postgres_dsn,
)


class ParseTests(unittest.TestCase):
    def test_url_form_full(self):
        ident = parse_postgres_dsn(
            "postgresql://user:pw@db.example:5433/appdb?sslmode=require")
        self.assertEqual(
            ident, {"kind": "postgres", "host": "db.example",
                    "port": "5433", "dbname": "appdb"})

    def test_url_default_port(self):
        ident = parse_postgres_dsn("postgres://user@127.0.0.1/fie_prod")
        self.assertEqual(ident["port"], "5432")

    def test_socket_dir_host(self):
        ident = parse_postgres_dsn(
            "postgresql://fie@/fie_parity?host=/tmp/fie-pg&port=54329")
        self.assertEqual(
            ident, {"kind": "postgres", "host": "socket:/tmp/fie-pg",
                    "port": "54329", "dbname": "fie_parity"})

    def test_params_override_url(self):
        ident = parse_postgres_dsn(
            "postgresql://a@1.2.3.4:1/db?host=9.9.9.9&port=9999&dbname=zz")
        self.assertEqual(
            ident, {"kind": "postgres", "host": "9.9.9.9",
                    "port": "9999", "dbname": "zz"})

    def test_keyword_form(self):
        ident = parse_postgres_dsn(
            "host=127.0.0.1 port=5432 dbname=fie_prod user=foo")
        self.assertEqual(
            ident, {"kind": "postgres", "host": "loopback",
                    "port": "5432", "dbname": "fie_prod"})

    def test_loopback_alias_coalesce(self):
        for host in ("localhost", "127.0.0.1", "127.0.0.9", "0.0.0.0",
                     "[::1]"):
            ident = parse_postgres_dsn(f"postgres://u@{host}:5432/db")
            self.assertEqual(ident["host"], "loopback", host)

    def test_bare_ipv6_is_unparseable(self):
        # An unbracketed bare IPv6 literal does not fit the URL grammar
        # libpq requires; it must fail closed, not silently mis-host it.
        with self.assertRaises(Unparseable):
            parse_postgres_dsn("postgres://u@::1:5432/db")

    def test_encodings_decoded(self):
        ident = parse_postgres_dsn(
            "postgres://u%40x@H%2FY%20case:5432/db%2Fname")
        self.assertEqual(ident["host"], "h/y case")
        self.assertEqual(ident["dbname"], "db/name")

    def test_rejects_missing_or_bad(self):
        for bad in ("", "host=1.2.3.4", "postgres://u@1.2.3.4/",
                    "postgres://u@1.2.3.4", "mysql://u@1.2.3.4/d",
                    "not a dsn at all", "postgres://u@1.2.3.4/d?port=abc"):
            with self.assertRaises(Unparseable, msg=repr(bad)):
                parse_postgres_dsn(bad)


class EquivalenceTests(unittest.TestCase):
    PROD = "postgresql://fixture_prod@127.0.0.1:59999/fie_prod_fixture"

    def alias(self, text):
        return equivalent(parse_postgres_dsn(self.PROD),
                          parse_postgres_dsn(text))

    def test_same(self):
        self.assertTrue(self.alias(self.PROD))
        self.assertTrue(self.alias(
            "postgres://fixture_prod@localhost:59999/fie_prod_fixture"))

    def test_user_and_password_are_not_identity(self):
        # user/role and password deliberately excluded (S3).
        self.assertTrue(self.alias(
            "postgres://otheruser:otherpw@127.0.0.1:59999/fie_prod_fixture"))

    def test_not_equivalent_variants(self):
        for other in (
            "postgres://u@127.0.0.1:59998/fie_prod_fixture",   # port
            "postgres://u@127.0.0.1:59999/other_db",           # database
            "postgres://u@h.example:59999/fie_prod_fixture",   # non-local host
            "postgres://u@127.0.0.1:59999/fie_prod_fixture_raw",
        ):
            self.assertFalse(self.alias(other), other)


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_stable_and_short(self):
        prod = parse_postgres_dsn(
            "postgresql://fixture_prod@127.0.0.1:59999/fie_prod_fixture")
        self.assertEqual(identity_fingerprint(prod), "d4aa6bec366c68ea")


if __name__ == "__main__":
    unittest.main(verbosity=2)