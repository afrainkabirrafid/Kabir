"""CLI integration tests for Kabir 3.0.0.

Covers the wiring the coordinator owns: `kabir run --native`,
`kabir build --native`, `kabir add`, `kabir publish`. The registry
tests use a local temporary registry directory via KABIR_REGISTRY;
no real registry or network is ever contacted.
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir.cli import main  # noqa: E402

CC = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
NEEDS_CC = unittest.skipIf(CC is None, "no C compiler (cc/gcc/clang)")

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

GREET_KBR = (
    'func greet(who) {\n'
    '    return "Hello, " + who + "!"\n'
    '}\n'
)


def _run_cli(argv, env=None):
    """Run main(argv), capturing stdout/stderr. Returns (code, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    old = dict(os.environ)
    if env:
        os.environ.update(env)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
    finally:
        os.environ.clear()
        os.environ.update(old)
    return code, out.getvalue(), err.getvalue()


def _make_greet_registry(tmp):
    """A local registry dir with one package: greet 1.0.0."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = GREET_KBR.encode("utf-8")
        info = tarfile.TarInfo("greet.kbr")
        info.size = len(data)
        info.mtime = 0
        tar.addfile(info, io.BytesIO(data))
    blob = buf.getvalue()
    registry = Path(tmp) / "registry"
    (registry / "packages").mkdir(parents=True)
    (registry / "packages" / "greet-1.0.0.tar.gz").write_bytes(blob)
    index = {"packages": {"greet": {"1.0.0": {
        "url": "packages/greet-1.0.0.tar.gz",
        "sha256": hashlib.sha256(blob).hexdigest()}}}}
    (registry / "index.json").write_text(json.dumps(index, indent=2))
    return registry


class TestRunNative(unittest.TestCase):
    @NEEDS_CC
    def test_run_native_hello_matches_vm(self):
        # The native binary inherits stdio, so its output bypasses
        # redirect_stdout -- run the real CLI as a subprocess instead.
        root = Path(__file__).resolve().parent.parent
        env = dict(os.environ, PYTHONPATH=str(root))
        native = subprocess.run(
            [sys.executable, "-m", "kabir", "run", "--native",
             str(EXAMPLES / "hello.kbr")],
            cwd=root, env=env, capture_output=True, text=True)
        self.assertEqual(native.returncode, 0, native.stderr)
        vm = subprocess.run(
            [sys.executable, "-m", "kabir", "run",
             str(EXAMPLES / "hello.kbr")],
            cwd=root, env=env, capture_output=True, text=True)
        self.assertEqual(vm.returncode, 0, vm.stderr)
        self.assertEqual(native.stdout, vm.stdout)

    @NEEDS_CC
    def test_run_native_type_error_fails_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.kbr"
            bad.write_text('let x: number = "hello"\nprint(x)\n',
                           encoding="utf-8")
            code, out, err = _run_cli(["run", "--native", str(bad)])
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("number", err)

    @NEEDS_CC
    def test_run_native_closure_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            prog = Path(tmp) / "clos.kbr"
            prog.write_text(
                "func outer() {\n"
                "    let x = 5\n"
                "    func inner() {\n"
                "        return x\n"
                "    }\n"
                "    return inner()\n"
                "}\n"
                "print(outer())\n", encoding="utf-8")
            code, out, err = _run_cli(["run", "--native", str(prog)])
            self.assertEqual(code, 1)
            self.assertIn("closures not supported", err)

    def test_run_interp_and_native_together_is_usage_error(self):
        code, out, err = _run_cli(
            ["run", "--interp", "--native", str(EXAMPLES / "hello.kbr")])
        self.assertEqual(code, 2)
        self.assertIn("--interp and --native", err)

    def test_run_native_on_kbc_is_usage_error(self):
        code, out, err = _run_cli(["run", "--native", "prog.kbc"])
        self.assertEqual(code, 2)
        self.assertIn("--native needs a .kbr file", err)


class TestBuildNative(unittest.TestCase):
    @NEEDS_CC
    def test_build_native_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                code, out, err = _run_cli(["new", "demo"])
                self.assertEqual(code, 0, err)
                code, out, err = _run_cli(["build", "--native", "demo"])
                self.assertEqual(code, 0, err)
                self.assertIn("(native, arena)", out)
                binary = Path(tmp) / "demo" / "target" / "demo"
                self.assertTrue(binary.is_file())
                proc = subprocess.run([str(binary)], capture_output=True,
                                      text=True)
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, "Hello from demo!\n")
            finally:
                os.chdir(cwd)


class TestAddPublish(unittest.TestCase):
    def test_add_without_version_is_usage_error(self):
        code, out, err = _run_cli(["add", "greet"])
        self.assertEqual(code, 2)
        self.assertIn("exact version", err)

    def test_add_without_registry_is_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            (proj / "Kabir.toml").write_text(
                '[package]\nname = "proj"\nversion = "0.1.0"\n',
                encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                code, out, err = _run_cli(["add", "greet@1.0.0", str(proj)])
            self.assertEqual(code, 1)
            self.assertIn("registry", err.lower())

    def test_add_latest_via_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = _make_greet_registry(tmp)
            # Add a newer version 1.1.0 to the same registry.
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                data = ('func greet(who) {\n'
                        '    return "Hi, " + who + "!"\n'
                        '}\n').encode("utf-8")
                info = tarfile.TarInfo("greet.kbr")
                info.size = len(data)
                info.mtime = 0
                tar.addfile(info, io.BytesIO(data))
            blob = buf.getvalue()
            (registry / "packages" / "greet-1.1.0.tar.gz").write_bytes(blob)
            index = json.loads((registry / "index.json").read_text())
            index["packages"]["greet"]["1.1.0"] = {
                "url": "packages/greet-1.1.0.tar.gz",
                "sha256": hashlib.sha256(blob).hexdigest()}
            (registry / "index.json").write_text(
                json.dumps(index, indent=2))
            proj = Path(tmp) / "proj"
            proj.mkdir()
            (proj / "Kabir.toml").write_text(
                '[package]\nname = "proj"\nversion = "0.1.0"\n')
            code, out, err = _run_cli(
                ["add", "greet@latest", str(proj)],
                env={"KABIR_REGISTRY": str(registry)})
            self.assertEqual(code, 0, err)
            self.assertIn("Added greet 1.1.0", out)
            self.assertTrue((proj / "vendor" / "greet" / "1.1.0"
                             / "greet.kbr").is_file())
            self.assertIn('greet = "1.1.0"',
                          (proj / "Kabir.toml").read_text())

    def test_publish_stages_registry_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            (proj / "Kabir.toml").write_text(
                '[package]\nname = "proj"\nversion = "0.1.0"\n')
            (proj / "src").mkdir()
            (proj / "src" / "main.kbr").write_text('print("hi")\n')
            with mock.patch.dict(os.environ, {}, clear=True):
                code, out, err = _run_cli(["publish", str(proj)])
            self.assertEqual(code, 0, err)
            self.assertIn("Published proj 0.1.0", out)
            self.assertIn("Staged registry update", out)
            self.assertIn("git clone", out)
            self.assertIn("kabir-registry", out)
            stage = proj / "target" / "registry-stage"
            self.assertTrue((stage / "index.json").is_file())
            self.assertTrue(
                (stage / "packages" / "proj-0.1.0.tar.gz").is_file())
            staged = json.loads((stage / "index.json").read_text())
            self.assertIn("proj", staged["packages"])
            self.assertIn("0.1.0", staged["packages"]["proj"])

    def test_publish_stage_flag_and_bad_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            (proj / "Kabir.toml").write_text(
                '[package]\nname = "proj"\nversion = "0.1.0"\n')
            (proj / "src").mkdir()
            (proj / "src" / "main.kbr").write_text('print("hi")\n')
            stage = Path(tmp) / "my-stage"
            with mock.patch.dict(os.environ, {}, clear=True):
                code, out, err = _run_cli(
                    ["publish", str(proj), "--stage", str(stage)])
            self.assertEqual(code, 0, err)
            self.assertIn(str(stage), out)
            self.assertTrue((stage / "index.json").is_file())
            with mock.patch.dict(os.environ, {}, clear=True):
                code, out, err = _run_cli(
                    ["publish", str(proj), "--bogus"])
            self.assertEqual(code, 2)
            self.assertIn("unknown option", err)

    def test_add_and_publish_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = _make_greet_registry(tmp)
            proj = Path(tmp) / "proj"
            proj.mkdir()
            (proj / "Kabir.toml").write_text(
                '[package]\nname = "proj"\nversion = "0.1.0"\n',
                encoding="utf-8")
            (proj / "src").mkdir()
            (proj / "src" / "main.kbr").write_text(
                'print("hi")\n', encoding="utf-8")
            code, out, err = _run_cli(
                ["add", "greet@1.0.0", str(proj)],
                env={"KABIR_REGISTRY": str(registry)})
            self.assertEqual(code, 0, err)
            self.assertIn("Added greet 1.0.0", out)
            self.assertTrue((proj / "vendor" / "greet" / "1.0.0"
                             / "greet.kbr").is_file())
            self.assertTrue((proj / "Kabir.lock").is_file())
            self.assertIn("greet", (proj / "Kabir.toml").read_text())

            code, out, err = _run_cli(["publish", str(proj)])
            self.assertEqual(code, 0, err)
            self.assertIn("Published proj 0.1.0", out)
            self.assertIn("sha256:", out)
            self.assertIn('"packages"', out)
            self.assertTrue((proj / "target" / "proj-0.1.0.tar.gz").is_file())


if __name__ == "__main__":
    unittest.main()
