"""Import tests: import "file.kbr", module cache, cycles, errors, REPL."""

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir.errors import KabirError
from kabir.interpreter import Interpreter

PYTHON = "python3"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def write(directory, name, content):
    """Write content (str, or bytes for a non-UTF-8 file) into directory."""
    path = os.path.join(directory, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(content, bytes):
        with open(path, "wb") as handle:
            handle.write(content)
    else:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
    return path


def run_main(main_path):
    """Run the .kbr file at main_path via Interpreter; return stdout."""
    with open(main_path, encoding="utf-8") as handle:
        source = handle.read()
    interp = Interpreter(source, filename=main_path)
    buf = io.StringIO()
    with redirect_stdout(buf):
        interp.run_source(source, filename=main_path)
    return buf.getvalue()


def run_main_error(main_path):
    """Run the file; return the KabirError raised (None if it runs)."""
    with open(main_path, encoding="utf-8") as handle:
        source = handle.read()
    interp = Interpreter(source, filename=main_path)
    try:
        interp.run_source(source, filename=main_path)
    except KabirError as err:
        return err
    return None


def run_cli(path, cwd=None):
    # PYTHONPATH keeps `python3 -m kabir` importable when cwd is a
    # temp dir (cwd itself still decides import path resolution).
    env = dict(os.environ, PYTHONPATH=ROOT)
    return subprocess.run([PYTHON, "-m", "kabir", "run", path],
                          capture_output=True, text=True,
                          cwd=cwd or ROOT, env=env)


class TestImportBasics(unittest.TestCase):
    def test_interpreter_starts_with_empty_module_cache(self):
        interp = Interpreter("")
        self.assertEqual(interp.modules, {})
        self.assertEqual(interp._importing, [])

    def test_basic_import_function(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "lib.kbr",
                  'func greet(name) { return "Hello, " + name }\n')
            main = write(tmp, "main.kbr",
                         'import "lib.kbr"\nprint(greet("Rafid"))\n')
            self.assertEqual(run_main(main), "Hello, Rafid\n")

    def test_flat_let_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "lib.kbr", "let answer = 42\n")
            main = write(tmp, "main.kbr",
                         'import "lib.kbr"\nprint(answer)\n')
            self.assertEqual(run_main(main), "42\n")

    def test_binding_undefined_before_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "lib.kbr", "let answer = 42\n")
            main = write(tmp, "main.kbr",
                         'print(answer)\nimport "lib.kbr"\n')
            err = run_main_error(main)
            self.assertIsNotNone(err)
            self.assertIn("Undefined variable", str(err))

    def test_isolation_module_function_closes_over_module_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "lib.kbr",
                  "let x = 1\nfunc get_x() { return x }\n")
            main = write(tmp, "main.kbr",
                         'import "lib.kbr"\nlet x = 99\n'
                         "print(get_x())\nprint(x)\n")
            self.assertEqual(run_main(main), "1\n99\n")

    def test_collision_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "lib.kbr", "let v = 2\n")
            main = write(tmp, "main.kbr",
                         'let v = 1\nimport "lib.kbr"\nprint(v)\n')
            self.assertEqual(run_main(main), "2\n")

    def test_nested_import_resolves_relative_to_importer(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "sub/c.kbr", "let deep = 7\n")
            write(tmp, "sub/b.kbr", 'import "c.kbr"\n')
            main = write(tmp, "a.kbr",
                         'import "sub/b.kbr"\nprint(deep)\n')
            self.assertEqual(run_main(main), "7\n")

    def test_absolute_path_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = write(tmp, "lib.kbr", "let answer = 42\n")
            main = write(tmp, "main.kbr",
                         'import "%s"\nprint(answer)\n' % lib)
            self.assertEqual(run_main(main), "42\n")

    def test_repl_filename_resolves_relative_to_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "lib.kbr", "let answer = 42\n")
            old = os.getcwd()
            os.chdir(tmp)
            try:
                interp = Interpreter("", filename="<repl>")
                buf = io.StringIO()
                with redirect_stdout(buf):
                    interp.run_source('import "lib.kbr"\nprint(answer)\n',
                                      filename="<repl>")
                self.assertEqual(buf.getvalue(), "42\n")
            finally:
                os.chdir(old)

    def test_diamond_import_executes_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "d.kbr",
                  'print("d loaded")\nfunc dfunc() { return 1 }\n')
            write(tmp, "b.kbr", 'import "d.kbr"\nlet b_fn = dfunc\n')
            write(tmp, "c.kbr", 'import "d.kbr"\nlet c_fn = dfunc\n')
            main = write(tmp, "main.kbr",
                         'import "b.kbr"\nimport "c.kbr"\n'
                         "print(b_fn == c_fn)\n")
            self.assertEqual(run_main(main), "d loaded\ntrue\n")

    def test_reimport_with_different_spellings_executes_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "sub"), exist_ok=True)
            write(tmp, "lib.kbr", 'print("lib loaded")\nlet answer = 42\n')
            main = write(tmp, "main.kbr",
                         'import "lib.kbr"\nimport "./lib.kbr"\n'
                         'import "sub/../lib.kbr"\nprint(answer)\n')
            self.assertEqual(run_main(main), "lib loaded\n42\n")

    def test_failed_import_is_not_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = write(tmp, "lib.kbr", "print(undefined_var)\n")
            main = write(tmp, "main.kbr",
                         'import "lib.kbr"\nprint(answer)\n')
            source = open(main, encoding="utf-8").read()
            interp = Interpreter(source, filename=main)
            with self.assertRaises(KabirError):
                interp.run_source(source, filename=main)
            self.assertEqual(interp._importing, [])
            canonical = os.path.realpath(os.path.abspath(lib))
            self.assertNotIn(canonical, interp.modules)
            write(tmp, "lib.kbr", "let answer = 42\n")
            buf = io.StringIO()
            with redirect_stdout(buf):
                interp.run_source(source, filename=main)
            self.assertEqual(buf.getvalue(), "42\n")


class TestImportCycles(unittest.TestCase):
    def test_two_file_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "a.kbr", 'import "b.kbr"\n')
            write(tmp, "b.kbr", 'import "a.kbr"\n')
            main = os.path.join(tmp, "a.kbr")
            err = run_main_error(main)
            self.assertIsNotNone(err)
            self.assertIn("Import cycle detected", str(err))
            self.assertIn("a.kbr", str(err))
            self.assertIn("b.kbr", str(err))

    def test_self_import_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = write(tmp, "a.kbr", 'import "a.kbr"\n')
            err = run_main_error(main)
            self.assertIsNotNone(err)
            self.assertIn("Import cycle detected", str(err))
            self.assertIn("a.kbr", str(err))

    def test_three_file_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "a.kbr", 'import "b.kbr"\n')
            write(tmp, "b.kbr", 'import "c.kbr"\n')
            write(tmp, "c.kbr", 'import "a.kbr"\n')
            main = os.path.join(tmp, "a.kbr")
            err = run_main_error(main)
            self.assertIsNotNone(err)
            self.assertIn("Import cycle detected", str(err))
            self.assertIn("a.kbr", str(err))
            self.assertIn("b.kbr", str(err))
            self.assertIn("c.kbr", str(err))

    def test_cycle_cli_no_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "a.kbr", 'import "b.kbr"\n')
            write(tmp, "b.kbr", 'import "a.kbr"\n')
            proc = run_cli(os.path.join(tmp, "a.kbr"), cwd=tmp)
            self.assertEqual(proc.returncode, 1)
            self.assertNotIn("Traceback", proc.stderr)
            self.assertIn("Import cycle detected", proc.stderr)


class TestImportErrors(unittest.TestCase):
    def test_missing_file_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = write(tmp, "main.kbr", 'import "missing.kbr"\n')
            err = run_main_error(main)
            self.assertIsNotNone(err)
            rendered = str(err)
            self.assertIn("main.kbr:1:1:", rendered)
            self.assertIn("no such file", rendered)
            self.assertIn("missing.kbr", rendered)
            proc = run_cli(main, cwd=tmp)
            self.assertEqual(proc.returncode, 1)
            self.assertNotIn("Traceback", proc.stderr)
            self.assertIn("no such file", proc.stderr)

    def test_syntax_error_in_imported_file_points_at_lib(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "lib.kbr", "let x =\n")
            main = write(tmp, "main.kbr", 'import "lib.kbr"\n')
            err = run_main_error(main)
            self.assertIsNotNone(err)
            first_line = str(err).split("\n")[0]
            self.assertIn("lib.kbr", first_line)
            self.assertNotIn("main.kbr", first_line)

    def test_runtime_error_in_imported_file_points_at_lib(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "lib.kbr", "print(undefined_var)\n")
            main = write(tmp, "main.kbr", 'import "lib.kbr"\n')
            err = run_main_error(main)
            self.assertIsNotNone(err)
            first_line = str(err).split("\n")[0]
            self.assertIn("lib.kbr", first_line)
            self.assertIn("Undefined variable", str(err))

    def test_error_after_import_points_at_outer_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "lib.kbr", "let answer = 42\n")
            main = write(tmp, "main.kbr",
                         'import "lib.kbr"\nlet x = 1\n'
                         "print(undefined_var)\n")
            err = run_main_error(main)
            self.assertIsNotNone(err)
            first_line = str(err).split("\n")[0]
            self.assertIn("main.kbr:3:", first_line)

    def test_top_level_return_in_imported_file_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "lib.kbr", "return 1\n")
            main = write(tmp, "main.kbr", 'import "lib.kbr"\n')
            err = run_main_error(main)
            self.assertIsNotNone(err)
            self.assertIn("'return' can only be used inside a function",
                          str(err))

    def test_top_level_break_in_imported_file_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "lib.kbr", "break\n")
            main = write(tmp, "main.kbr", 'import "lib.kbr"\n')
            err = run_main_error(main)
            self.assertIsNotNone(err)
            self.assertIn("'break' can only be used inside a loop",
                          str(err))

    def test_import_inside_function_with_returning_module_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "lib.kbr", "return 1\n")
            main = write(tmp, "main.kbr",
                         'func load_it() {\n    import "lib.kbr"\n}\n'
                         "load_it()\n")
            err = run_main_error(main)
            self.assertIsNotNone(err)
            self.assertIn("'return' can only be used inside a function",
                          str(err))

    def test_import_inside_loop_with_breaking_module_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "bad.kbr", "break\n")
            main = write(tmp, "main.kbr",
                         'let i = 0\nwhile i < 1 {\n'
                         '    import "bad.kbr"\n    i = i + 1\n}\n')
            err = run_main_error(main)
            self.assertIsNotNone(err)
            self.assertIn("'break' can only be used inside a loop",
                          str(err))

    def test_non_utf8_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "lib.kbr", b"\xff\xfe")
            main = write(tmp, "main.kbr", 'import "lib.kbr"\n')
            err = run_main_error(main)
            self.assertIsNotNone(err)
            self.assertIn("not valid UTF-8", str(err))
            proc = run_cli(main, cwd=tmp)
            self.assertEqual(proc.returncode, 1)
            self.assertNotIn("Traceback", proc.stderr)

    def test_repl_import_and_error_points_at_repl(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "lib.kbr",
                  'func greet(name) { return "Hello, " + name }\n')
            proc = subprocess.run(
                [PYTHON, "-m", "kabir", "repl"],
                input='import "lib.kbr"\ngreet("World")\n'
                      "print(undefined_var)\n",
                capture_output=True, text=True, cwd=tmp,
                env=dict(os.environ, PYTHONPATH=ROOT))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Hello, World", proc.stdout)
            self.assertIn("<repl>", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)


class TestImportExample(unittest.TestCase):
    def test_import_demo_cli(self):
        proc = subprocess.run(
            [PYTHON, "-m", "kabir", "run",
             os.path.join(ROOT, "examples", "import-demo.kbr")],
            capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout,
                         "Hello, Rafid!\nimported from greet-lib.kbr\n")


if __name__ == "__main__":
    unittest.main()
