"""Tests for native imports (7.0.0 Leap 2): Kabir AST -> C99 -> binary.

`import "path.kbr"` transpiles the imported module too: its
top-level code runs once (guarded init function, in execution
order), its bindings are copied into the importer, and re-imports
only copy. Each case runs the same files on the VM and on native
and compares stdout; error cases compare the exit code and the
clear compile-time message.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from kabir import codegen
from kabir.errors import KabirError

CC = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
NEEDS_CC = unittest.skipIf(CC is None, "no C compiler (cc/gcc/clang)")


def run_cli(args, cwd=None):
    """Run `python -m kabir <args>`; return (code, stdout, stderr)."""
    env = dict(os.environ, PYTHONPATH=ROOT)
    proc = subprocess.run([sys.executable, "-m", "kabir"] + args,
                          capture_output=True, text=True,
                          cwd=cwd or ROOT, env=env)
    return proc.returncode, proc.stdout, proc.stderr


LIB_A = (
    "print(\"lib_a top\")\n"
    "func greet(name) { return \"Hello, \" + name + \"!\" }\n"
    "let note = \"from lib_a\"\n")

LIB_B = (
    "import \"lib_a.kbr\"\n"
    "print(\"lib_b top\")\n"
    "func shout(name) { return greet(name).upper() }\n"
    "let note_b = note + \" + lib_b\"\n")


@NEEDS_CC
class TestNativeImport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kabir-native-import-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, source):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source)
        return path

    def assert_same_output(self, main_path):
        vm_code, vm_out, vm_err = run_cli(["run", main_path])
        self.assertEqual(vm_code, 0, "VM failed: %s" % vm_err)
        nat_code, nat_out, nat_err = run_cli(
            ["run", "--native", "--no-cache", main_path])
        self.assertEqual(nat_code, 0, "native failed: %s" % nat_err)
        self.assertEqual(nat_out, vm_out,
                         "native stdout differs from VM stdout")
        return nat_out

    def test_simple_import(self):
        self.write("lib_a.kbr", LIB_A)
        main = self.write("main.kbr",
                          "import \"lib_a.kbr\"\n"
                          "print(greet(\"Rafid\"))\n"
                          "print(note)\n")
        out = self.assert_same_output(main)
        self.assertEqual(out, "lib_a top\nHello, Rafid!\nfrom lib_a\n")

    def test_nested_import(self):
        self.write("lib_a.kbr", LIB_A)
        self.write("lib_b.kbr", LIB_B)
        main = self.write("main.kbr",
                          "import \"lib_b.kbr\"\n"
                          "print(shout(\"rafid\"))\n"
                          "print(note_b)\n")
        out = self.assert_same_output(main)
        self.assertEqual(out, "lib_a top\nlib_b top\nHELLO, RAFID!\n"
                              "from lib_a + lib_b\n")

    def test_reimport_runs_top_level_once(self):
        self.write("lib_a.kbr", LIB_A)
        main = self.write("main.kbr",
                          "import \"lib_a.kbr\"\n"
                          "print(greet(\"one\"))\n"
                          "import \"lib_a.kbr\"\n"
                          "print(greet(\"two\"))\n")
        out = self.assert_same_output(main)
        self.assertEqual(out, "lib_a top\nHello, one!\nHello, two!\n")

    def test_module_mutation_shared(self):
        self.write("lib_counter.kbr",
                   "print(\"counter lib top\")\n"
                   "let mut count = 0\n"
                   "func bump() { count += 1 return count }\n")
        main = self.write("main.kbr",
                          "import \"lib_counter.kbr\"\n"
                          "print(bump())\n"
                          "print(bump())\n")
        out = self.assert_same_output(main)
        self.assertEqual(out, "counter lib top\n1\n2\n")

    def test_module_closure(self):
        self.write("lib_closure.kbr",
                   "func make_adder(x) { "
                   "return func(y) { return x + y } }\n"
                   "let add10 = make_adder(10)\n")
        main = self.write("main.kbr",
                          "import \"lib_closure.kbr\"\n"
                          "print(add10(5))\n"
                          "let add100 = make_adder(100)\n"
                          "print(add100(5))\n")
        out = self.assert_same_output(main)
        self.assertEqual(out, "15\n105\n")

    def test_import_inside_function(self):
        self.write("lib_a.kbr", LIB_A)
        main = self.write("main.kbr",
                          "func load() {\n"
                          "    import \"lib_a.kbr\"\n"
                          "    return greet(\"infunc\")\n"
                          "}\n"
                          "print(load())\n")
        out = self.assert_same_output(main)
        self.assertEqual(out, "lib_a top\nHello, infunc!\n")

    def test_import_top_and_inside_function_runs_once(self):
        self.write("lib_a.kbr", LIB_A)
        main = self.write("main.kbr",
                          "func early() {\n"
                          "    import \"lib_a.kbr\"\n"
                          "    return greet(\"early\")\n"
                          "}\n"
                          "print(early())\n"
                          "import \"lib_a.kbr\"\n"
                          "print(greet(\"late\"))\n")
        out = self.assert_same_output(main)
        self.assertEqual(out, "lib_a top\nHello, early!\nHello, late!\n")

    def test_diamond_import_runs_shared_module_once(self):
        self.write("lib_d.kbr",
                   "print(\"lib_d top\")\n"
                   "let dval = 40\n")
        self.write("lib_b2.kbr",
                   "import \"lib_d.kbr\"\n"
                   "let bval = dval + 1\n")
        self.write("lib_c2.kbr",
                   "import \"lib_d.kbr\"\n"
                   "let cval = dval + 2\n")
        main = self.write("main.kbr",
                          "import \"lib_b2.kbr\"\n"
                          "import \"lib_c2.kbr\"\n"
                          "print(bval + cval)\n")
        out = self.assert_same_output(main)
        self.assertEqual(out, "lib_d top\n83\n")

    def test_subdirectory_import(self):
        self.write("sub/deep.kbr",
                   "func deep() { return \"deep\" }\n")
        main = self.write("main.kbr",
                          "import \"sub/deep.kbr\"\n"
                          "print(deep())\n")
        out = self.assert_same_output(main)
        self.assertEqual(out, "deep\n")

    def test_closure_over_imported_function(self):
        self.write("lib_a.kbr", LIB_A)
        main = self.write("main.kbr",
                          "import \"lib_a.kbr\"\n"
                          "let f = func() { return greet(\"cap\") }\n"
                          "print(f())\n")
        out = self.assert_same_output(main)
        self.assertEqual(out, "lib_a top\nHello, cap!\n")

    def test_importer_redefinition_shadows_import(self):
        self.write("lib_a.kbr", LIB_A)
        main = self.write("main.kbr",
                          "import \"lib_a.kbr\"\n"
                          "print(greet(\"x\"))\n"
                          "let note = \"overwritten by main\"\n"
                          "print(note)\n")
        out = self.assert_same_output(main)
        self.assertEqual(out, "lib_a top\nHello, x!\n"
                              "overwritten by main\n")

    def test_import_cycle_is_a_clear_error(self):
        self.write("cyc_a.kbr",
                   "import \"cyc_b.kbr\"\nlet a = 1\n")
        self.write("cyc_b.kbr",
                   "import \"cyc_a.kbr\"\nlet b = 2\n")
        main = self.write("main.kbr",
                          "import \"cyc_a.kbr\"\nprint(\"nope\")\n")
        vm_code, _vm_out, _vm_err = run_cli(["run", main])
        self.assertEqual(vm_code, 1)
        nat_code, _nat_out, nat_err = run_cli(
            ["run", "--native", "--no-cache", main])
        self.assertEqual(nat_code, 1)
        self.assertIn("Import cycle detected", nat_err)

    def test_missing_import_is_a_clear_error(self):
        main = self.write("main.kbr",
                          "import \"nope.kbr\"\nprint(\"nope\")\n")
        nat_code, _nat_out, nat_err = run_cli(
            ["run", "--native", "--no-cache", main])
        self.assertEqual(nat_code, 1)
        self.assertIn('Cannot import "nope.kbr" -- no such file', nat_err)

    def test_example_import_demo(self):
        # The repo's own import example, end to end.
        main = os.path.join(ROOT, "examples", "import-demo.kbr")
        vm_code, vm_out, vm_err = run_cli(["run", main])
        self.assertEqual(vm_code, 0, vm_err)
        nat_code, nat_out, nat_err = run_cli(
            ["run", "--native", "--no-cache", main])
        self.assertEqual(nat_code, 0, nat_err)
        self.assertEqual(nat_out, vm_out)
        self.assertEqual(nat_out, "Hello, Rafid!\n"
                                  "imported from greet-lib.kbr\n")

    def test_build_native_with_import(self):
        proj = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(proj, "src"))
        with open(os.path.join(proj, "Kabir.toml"), "w",
                  encoding="utf-8") as handle:
            handle.write("[package]\nname = \"demo\"\n"
                         "version = \"0.1.0\"\n")
        with open(os.path.join(proj, "src", "lib_a.kbr"), "w",
                  encoding="utf-8") as handle:
            handle.write(LIB_A)
        with open(os.path.join(proj, "src", "main.kbr"), "w",
                  encoding="utf-8") as handle:
            handle.write("import \"lib_a.kbr\"\n"
                         "print(greet(\"built\"))\n")
        code, out, err = run_cli(["build", "--native", proj])
        self.assertEqual(code, 0, err)
        self.assertIn("(native, arena)", out)
        binary = os.path.join(proj, "target", "demo")
        proc = subprocess.run([binary], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "lib_a top\nHello, built!\n")


@NEEDS_CC
class TestNativeBareImport(unittest.TestCase):
    """Bare-name package imports in native mode (7.0.0 wiring).

    ``import greet`` resolves through the ONE shared resolver
    (kabir/imports.py) to vendor/<name>/<version>/src/main.kbr,
    exactly like the VM path -- native codegen must not grow a
    second resolver. Fixture: a temp local registry + a consumer
    project that added greet 0.1.0 (Leap 1's helpers).
    """

    def setUp(self):
        from pathlib import Path

        from kabir.project import add_package, new_project
        from tests.test_registry_import import (GREET_KBR,
                                                publish_to_registry)

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.registry = self.tmp / "registry"
        (self.registry / "packages").mkdir(parents=True)
        publish_to_registry(self.tmp, self.registry, "greet", GREET_KBR)
        self.app = new_project("my_app", cwd=self.tmp)
        add_package("greet", "0.1.0", self.app, self.registry)
        self.main = self.app / "src" / "main.kbr"

    def tearDown(self):
        self._tmp.cleanup()

    def test_bare_import_runs_same_on_vm_and_native(self):
        self.main.write_text('import greet\nprint(greet("World"))\n',
                             encoding="utf-8")
        vm_code, vm_out, vm_err = run_cli(["run", str(self.main)])
        self.assertEqual(vm_code, 0, "VM failed: %s" % vm_err)
        nat_code, nat_out, nat_err = run_cli(
            ["run", "--native", "--no-cache", str(self.main)])
        self.assertEqual(nat_code, 0, "native failed: %s" % nat_err)
        self.assertEqual(nat_out, vm_out)
        self.assertEqual(nat_out, "Hello, World!\n")

    def test_missing_package_is_a_clear_native_error(self):
        self.main.write_text('import nope\nprint(1)\n', encoding="utf-8")
        nat_code, _nat_out, nat_err = run_cli(
            ["run", "--native", "--no-cache", str(self.main)])
        self.assertNotEqual(nat_code, 0)
        self.assertIn('Package "nope" is not a dependency', nat_err)
        self.assertIn("kabir add nope@<version>", nat_err)
        # Reported at the import statement, like the VM path.
        self.assertIn("main.kbr:1:1", nat_err)


if __name__ == "__main__":
    unittest.main()
