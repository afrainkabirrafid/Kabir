"""CLI integration tests for Kabir 6.0.0 (the breaking clean-up).

End-to-end proof, through the real `kabir run` command, of the two
breaking changes: read-only builtins borrow instead of moving, and
plain `let` is immutable. The checker runs on the default `kabir
run` path, so a plain-`let` reassignment fails before any code runs.
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir.cli import main  # noqa: E402


def _run_cli(argv):
    """Run main(argv), capturing stdout/stderr. Returns (code, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def _run_source(source):
    """Run *source* through `kabir run` on a temp file."""
    with tempfile.NamedTemporaryFile("w", suffix=".kbr",
                                     delete=False) as handle:
        handle.write(source)
        path = handle.name
    try:
        return _run_cli(["run", path])
    finally:
        os.unlink(path)


class TestVersion(unittest.TestCase):
    def test_version_is_6_0_0(self):
        code, out, err = _run_cli(["--version"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "kabir 7.0.0")


class TestReadOnlyBuiltinsBorrowCli(unittest.TestCase):
    def test_print_then_reuse_runs(self):
        code, out, err = _run_source(
            "let mut x = [1, 2]\nprint(x)\nprint(x)\nprint(len(x))\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "[1, 2]\n[1, 2]\n2\n")

    def test_mutating_builtin_still_moves(self):
        code, out, err = _run_source(
            "let mut x = [1]\npush(x, 2)\nprint(x)\n")
        self.assertEqual(code, 1)
        self.assertIn("used after move", err)


class TestImmutableByDefaultCli(unittest.TestCase):
    def test_plain_let_reassign_is_compile_error(self):
        code, out, err = _run_source("let x = 5\nx = 6\nprint(x)\n")
        self.assertEqual(code, 1)
        self.assertIn("declared with plain 'let', which is immutable", err)
        self.assertIn("let mut x = ...", err)
        self.assertEqual(out, "")  # nothing runs: the error is compile-time

    def test_plain_let_compound_assign_is_compile_error(self):
        code, out, err = _run_source("let x = 5\nx += 1\nprint(x)\n")
        self.assertEqual(code, 1)
        self.assertIn("declared with plain 'let', which is immutable", err)

    def test_let_mut_reassign_runs(self):
        code, out, err = _run_source(
            "let mut x = 5\nx = 6\nx += 1\nprint(x)\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "7\n")


if __name__ == "__main__":
    unittest.main()
