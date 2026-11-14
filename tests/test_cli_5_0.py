"""CLI integration tests for Kabir 5.0.0.

Covers the wiring the coordinator owns: `kabir run --selfhost`
(compile with the Kabir compiler, run on the VM) and its flag
conflicts. The selfhost runs execute the real Kabir compiler, so
they take a few seconds each.
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

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _run_cli(argv):
    """Run main(argv), capturing stdout/stderr. Returns (code, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class TestRunSelfhost(unittest.TestCase):
    def test_run_selfhost_hello(self):
        code, out, err = _run_cli(
            ["run", "--selfhost", str(EXAMPLES / "hello.kbr")])
        self.assertEqual(code, 0, err)
        self.assertIn("Hello, Rafid!", out)
        self.assertIn("Welcome to Kabir version 6.0.0.", out)
        self.assertIn("2 + 3 = 5", out)

    def test_run_selfhost_matches_plain_run(self):
        _, plain_out, _ = _run_cli(
            ["run", str(EXAMPLES / "fibonacci.kbr")])
        code, out, err = _run_cli(
            ["run", "--selfhost", str(EXAMPLES / "fibonacci.kbr")])
        self.assertEqual(code, 0, err)
        self.assertEqual(out, plain_out)

    def test_selfhost_type_error_still_reported(self):
        with tempfile.NamedTemporaryFile(
                "w", suffix=".kbr", delete=False,
                encoding="utf-8") as handle:
            handle.write('print(nope)\n')
            path = handle.name
        try:
            code, out, err = _run_cli(["run", "--selfhost", path])
        finally:
            os.unlink(path)
        self.assertEqual(code, 1)
        self.assertIn("nope", err)

    def test_selfhost_conflicts_with_native(self):
        code, out, err = _run_cli(
            ["run", "--selfhost", "--native",
             str(EXAMPLES / "hello.kbr")])
        self.assertEqual(code, 2)
        self.assertIn("--selfhost", err)

    def test_selfhost_conflicts_with_interp(self):
        code, out, err = _run_cli(
            ["run", "--selfhost", "--interp",
             str(EXAMPLES / "hello.kbr")])
        self.assertEqual(code, 2)
        self.assertIn("--selfhost", err)


if __name__ == "__main__":
    unittest.main()
