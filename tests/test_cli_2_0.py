"""CLI integration tests for Kabir 2.0.0.

Covers the wiring the coordinator owns: the type checker inside
`kabir run` (VM path), the `--interp` legacy path, `kabir new` /
`kabir build` / `kabir run <dir>` / `kabir run <.kbc>`.
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


class TestRunTypeChecking(unittest.TestCase):
    def test_mistyped_program_fails_at_compile_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.kbr"
            bad.write_text('let x: number = "hello"\nprint(x)\n',
                           encoding="utf-8")
            code, out, err = _run_cli(["run", str(bad)])
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("number", err)
            self.assertIn("string", err)

    def test_mistyped_return_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.kbr"
            bad.write_text(
                "func f(a: number) -> string { return a + 1 }\n"
                "print(f(1))\n", encoding="utf-8")
            code, out, err = _run_cli(["run", str(bad)])
            self.assertEqual(code, 1)
            self.assertEqual(out, "")

    def test_interp_path_ignores_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.kbr"
            bad.write_text('let x: number = "hello"\nprint(x)\n',
                           encoding="utf-8")
            code, out, err = _run_cli(["run", "--interp", str(bad)])
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "hello")

    def test_typed_match_program_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            prog = Path(tmp) / "m.kbr"
            prog.write_text(
                "func label(n: number) -> string {\n"
                '    return match n { 0 => "zero", 1 => "one", _ => "other" }\n'
                "}\n"
                "print(label(0))\n"
                "print(label(9))\n", encoding="utf-8")
            code, out, err = _run_cli(["run", str(prog)])
            self.assertEqual(code, 0, err)
            self.assertEqual(out, "zero\nother\n")


class TestProjectCommands(unittest.TestCase):
    def test_new_build_run_kbc_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                code, out, err = _run_cli(["new", "demo"])
                self.assertEqual(code, 0, err)
                self.assertTrue((Path(tmp) / "demo" / "Kabir.toml").is_file())
                code, out, err = _run_cli(["build", "demo"])
                self.assertEqual(code, 0, err)
                self.assertIn("Built demo v0.1.0", out)
                kbc = Path(tmp) / "demo" / "target" / "demo.kbc"
                self.assertTrue(kbc.is_file())
                code, out, err = _run_cli(["run", str(kbc)])
                self.assertEqual(code, 0, err)
                self.assertEqual(out.strip(), "Hello from demo!")
                code, out, err = _run_cli(["run", "demo"])
                self.assertEqual(code, 0, err)
                self.assertEqual(out.strip(), "Hello from demo!")
            finally:
                os.chdir(cwd)

    def test_new_duplicate_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                self.assertEqual(_run_cli(["new", "demo"])[0], 0)
                code, out, err = _run_cli(["new", "demo"])
                self.assertEqual(code, 1)
                self.assertIn("already exists", err)
            finally:
                os.chdir(cwd)

    def test_build_missing_project_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = _run_cli(["build", tmp])
            self.assertEqual(code, 1)
            self.assertIn("Kabir.toml", err)

    def test_run_plain_directory_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = _run_cli(["run", tmp])
            self.assertEqual(code, 2)
            self.assertIn("directory", err)


if __name__ == "__main__":
    unittest.main()
