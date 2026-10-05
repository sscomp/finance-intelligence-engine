#!/usr/bin/env python3
"""Bootstrap CLI contract tests (ABACUS_FIE_6_9A_R1).

Locks the argument-parsing contract of the canonical fresh-clone entrypoint
``scripts/bootstrap.sh`` (6.8C Task D):

    bash scripts/bootstrap.sh                  zero CLI options  → exit 0
    bash scripts/bootstrap.sh --with-test      supported option   → parse OK
    bash scripts/bootstrap.sh --all            supported option   → parse OK
    bash scripts/bootstrap.sh --definitely-…   unknown option     → fail closed
    bash scripts/bootstrap.sh ""               explicit empty arg → fail closed

The 6.9A Codex Cloud incident: the parser iterated ``"${@:-}"``, which under
a zero-argument invocation expanded to ONE empty word and was rejected as
``unknown option ''`` with exit 2 — blocking every fresh-clone gate. The
fix iterates the actual positional parameters (``"$@"``), and these tests
provoke that defect against the shipped source so it cannot regress.

Fail-closed behavior is part of the contract: unsupported options and an
explicit empty argument still exit non-zero, and zero arguments (argc=0)
is never conflated with one empty-string argument (argc=1, argv[0]="").

Pure parser-contract probing, no network, no venv creation, no DB: the
real ``bootstrap.sh`` text is executed up to the end of its argument loop
by injecting a probe that prints the parsed mode flags and argc, so any
future restructure of the script that moves or reshapes the parser breaks
these tests loudly instead of silently bypassing them.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO / "scripts" / "bootstrap.sh"

# A stable, contract-realistic environment: no operator env leaks into the
# probe (FIE_* unset), and the probe exits before any pip/venv side effect.
_PROBE_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}


def _parser_probe_stdout(args):
    """Run the shipped argument parser and return (returncode, stdout, stderr).

    Copies ``scripts/bootstrap.sh`` verbatim and appends, immediately after
    the ``for arg in ...`` loop's closing ``done``, a probe that prints the
    resolved mode flags and the original argc — then exits, so no venv
    creation, pip install, or import check runs. Any change to where or how
    the parser is structured makes the ``done`` search fail the test loudly.
    """
    assert BOOTSTRAP.is_file(), "scripts/bootstrap.sh missing from repository"
    lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines(keepends=True)
    out = []
    in_loop = False
    done_seen = False
    for line in lines:
        stripped = line.strip()
        if not done_seen and stripped.startswith("for arg in"):
            in_loop = True
        if in_loop and not done_seen and stripped == "done":
            out.append(line)
            out.append(
                'printf -- "PARSE_RESULT mode_test=%d mode_pg=%d argc=%d\\n"'
                ' "${MODE_TEST}" "${MODE_PG}" "$#"\n')
            out.append("exit 0\n")
            done_seen = True
            continue
        out.append(line)
    assert done_seen, (
        "bootstrap.sh argument-parsing loop not found in expected shape; "
        "tests/test_bootstrap_cli_contract.py must be updated with the script")
    probe_text = "".join(out)
    with tempfile.TemporaryDirectory(prefix="fie-bootstrap-contract-") as td:
        probe = Path(td) / "bootstrap.sh"
        probe.write_text(probe_text, encoding="utf-8")
        res = subprocess.run(
            ["bash", str(probe)] + list(args),
            env=_PROBE_ENV, capture_output=True, text=True, timeout=30)
    return res.returncode, res.stdout, res.stderr


class ZeroArgumentContractTests(unittest.TestCase):
    """C01 — zero arguments is the canonical successful bootstrap."""

    def test_zero_args_parses_to_default_modes(self):
        rc, out, err = _parser_probe_stdout([])
        self.assertEqual(rc, 0, msg=f"stderr={err!r}")
        self.assertIn("PARSE_RESULT mode_test=0 mode_pg=0 argc=0", out)

    def test_zero_args_never_yields_empty_option(self):
        # The 6.9A incident signature: 'unknown option '''. The parser must
        # not even reach the reject branch under a zero-argument invocation.
        rc, out, err = _parser_probe_stdout([])
        self.assertEqual(rc, 0)
        self.assertNotIn("unknown option ''", err)


class SupportedOptionContractTests(unittest.TestCase):
    """C02 — explicit supported options still parse to documented modes."""

    def test_with_test_option(self):
        rc, out, err = _parser_probe_stdout(["--with-test"])
        self.assertEqual(rc, 0, msg=f"stderr={err!r}")
        self.assertIn("PARSE_RESULT mode_test=1 mode_pg=0 argc=1", out)

    def test_with_postgres_option(self):
        rc, out, err = _parser_probe_stdout(["--with-postgres"])
        self.assertEqual(rc, 0, msg=f"stderr={err!r}")
        self.assertIn("PARSE_RESULT mode_test=0 mode_pg=1 argc=1", out)

    def test_all_option(self):
        rc, out, err = _parser_probe_stdout(["--all"])
        self.assertEqual(rc, 0, msg=f"stderr={err!r}")
        self.assertIn("PARSE_RESULT mode_test=1 mode_pg=1 argc=1", out)


class UnknownOptionFailClosedTests(unittest.TestCase):
    """C03 — unsupported options must fail closed (never silently ignored)."""

    def test_unknown_option_is_rejected(self):
        rc, out, err = _parser_probe_stdout(["--definitely-invalid-option"])
        self.assertNotEqual(rc, 0, msg="unknown option must not exit 0")
        self.assertIn("unknown option '--definitely-invalid-option'", err)

    def test_unknown_short_option_is_rejected(self):
        rc, out, err = _parser_probe_stdout(["-x"])
        self.assertNotEqual(rc, 0, msg="unknown option must not exit 0")
        self.assertIn("unknown option '-x'", err)


class ExplicitEmptyArgumentTests(unittest.TestCase):
    """C04 — argc=1 argv[0]='' is NOT zero arguments and must fail closed."""

    def test_explicit_empty_argument_is_rejected_not_conflated(self):
        rc, out, err = _parser_probe_stdout([""])
        self.assertNotEqual(
            rc, 0, msg="explicit empty argument must not exit 0")
        self.assertIn("unknown option ''", err)


if __name__ == "__main__":
    unittest.main()