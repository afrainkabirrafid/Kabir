"""Project tooling tests: kabir new / build / run_project (Kabir 2.0.0)."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir.errors import KabirError
from kabir.interpreter import Interpreter
from kabir.parser import parse
from kabir.project import (build_project, new_project, parse_manifest,
                           read_manifest, run_project)

try:
    from kabir.checker import check_program  # noqa: F401
    from kabir.compile import compile_program, load_bytecode, save_bytecode  # noqa: F401
    from kabir.vm import VM  # noqa: F401
    HAS_COMPILER = True
except ImportError:
    HAS_COMPILER = False

EXPECTED_MANIFEST = (
    '[package]\n'
    'name = "hello_app"\n'
    'version = "0.1.0"\n'
    'authors = ["Afrain Kabir Rafid"]\n'
)

EXPECTED_MAIN = (
    'func main() {\n'
    '    print("Hello from hello_app!")\n'
    '}\n'
    '\n'
    'main()\n'
)


def run_with_interpreter(source):
    """Run Kabir source through the tree-walking interpreter."""
    interp = Interpreter(source, filename="<test>")
    buf = io.StringIO()
    with redirect_stdout(buf):
        interp.run_source(source, filename="<test>")
    return buf.getvalue()


class TestNewProject(unittest.TestCase):
    def test_scaffold_contents_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = new_project("hello_app", cwd=tmp)
            proj = Path(tmp) / "hello_app"
            self.assertEqual(result, proj)
            self.assertTrue(proj.is_dir())
            self.assertTrue((proj / "src").is_dir())
            self.assertEqual((proj / "Kabir.toml").read_text(),
                             EXPECTED_MANIFEST)
            self.assertEqual((proj / "src" / "main.kbr").read_text(),
                             EXPECTED_MAIN)
            self.assertEqual((proj / ".gitignore").read_text(),
                             "target/\nvendor/\n")

    def test_scaffold_program_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = new_project("hello_app", cwd=tmp)
            source = (proj / "src" / "main.kbr").read_text()
            parse(source, "<test>")  # must be valid Kabir
            self.assertEqual(run_with_interpreter(source),
                             "Hello from hello_app!\n")

    def test_name_with_hyphen_and_underscore(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = new_project("my-app_v2", cwd=tmp)
            self.assertTrue((proj / "Kabir.toml").exists())
            self.assertIn('name = "my-app_v2"',
                          (proj / "Kabir.toml").read_text())

    def test_invalid_names(self):
        bad = ["", "1abc", "-abc", "_abc", "my app", "a.b", "a/b",
               "a:b", "a b", "éclair", "a+b", None, 42]
        with tempfile.TemporaryDirectory() as tmp:
            for name in bad:
                with self.subTest(name=name):
                    with self.assertRaises(KabirError):
                        new_project(name, cwd=tmp)
                    # Nothing was created for the bad name.
                    if isinstance(name, str) and name and "/" not in name:
                        self.assertFalse((Path(tmp) / name).exists())

    def test_existing_directory_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            new_project("hello_app", cwd=tmp)
            with self.assertRaises(KabirError) as ctx:
                new_project("hello_app", cwd=tmp)
            self.assertIn('Project "hello_app" already exists',
                          str(ctx.exception))

    def test_preexisting_empty_dir_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "taken").mkdir()
            with self.assertRaises(KabirError):
                new_project("taken", cwd=tmp)

    def test_existing_file_with_same_name_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "taken").write_text("not a dir")
            with self.assertRaises(KabirError):
                new_project("taken", cwd=tmp)

    def test_default_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            try:
                os.chdir(tmp)
                proj = new_project("from_cwd")
                self.assertEqual(proj, Path("from_cwd"))
                self.assertTrue((Path(tmp) / "from_cwd" / "Kabir.toml")
                                .exists())
            finally:
                os.chdir(old)


class TestManifestParsing(unittest.TestCase):
    def test_exact_format_new_project_writes(self):
        manifest = parse_manifest(EXPECTED_MANIFEST)
        self.assertEqual(manifest["name"], "hello_app")
        self.assertEqual(manifest["version"], "0.1.0")
        self.assertEqual(manifest["authors"], ["Afrain Kabir Rafid"])

    def test_comments_blank_lines_and_spacing(self):
        text = ('# a comment\n'
                '\n'
                '[package]  # section comment\n'
                'name="spaced"\n'
                'version  =  "2.0.0"\n'
                'authors = [ "A One" , "B Two" ]\n')
        manifest = parse_manifest(text)
        self.assertEqual(manifest["name"], "spaced")
        self.assertEqual(manifest["version"], "2.0.0")
        self.assertEqual(manifest["authors"], ["A One", "B Two"])

    def test_missing_version_and_authors_default(self):
        manifest = parse_manifest('[package]\nname = "bare"\n')
        self.assertEqual(manifest["version"], "0.1.0")
        self.assertEqual(manifest["authors"], [])

    def test_malformed_manifests(self):
        bad = [
            "",  # empty: no [package]
            'name = "no_section"\n',  # key before any section
            '[package]\nversion = "0.1.0"\n',  # missing name
            '[package]\nname = unquoted\n',  # unquoted value
            '[package]\nname = 42\n',  # non-string value
            '[package]\nname = "x"\nname = "y"\n',  # duplicate key
            '[package]\nname = "x"\n[package]\n',  # duplicate section
            '[other]\nname = "x"\n',  # unknown section
            '[package]\nname = "x"\nlicense = "MIT"\n',  # unknown key
            '[package]\nname = "x"\nthis is not toml\n',  # garbage line
            '[package]\nname = "1bad"\n',  # invalid project name
            '[package]\nname = "x"\nauthors = ["a", 1]\n',  # bad list
            '[package]\nname = "x"\nauthors = "solo"\n',  # authors not list
            '[package\nname = "x"\n',  # unclosed section header
        ]
        for text in bad:
            with self.subTest(text=text):
                with self.assertRaises(KabirError):
                    parse_manifest(text)

    def test_read_manifest_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(KabirError):
                read_manifest(Path(tmp) / "Kabir.toml")

    def test_read_manifest_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = new_project("hello_app", cwd=tmp)
            manifest = read_manifest(proj / "Kabir.toml")
            self.assertEqual(manifest["name"], "hello_app")


class TestRunProject(unittest.TestCase):
    def test_resolves_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = new_project("hello_app", cwd=tmp)
            self.assertEqual(run_project(proj), proj / "src" / "main.kbr")
            self.assertEqual(run_project(str(proj)),
                             proj / "src" / "main.kbr")

    def test_missing_dir_and_missing_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(KabirError):
                run_project(Path(tmp) / "nope")
            empty = Path(tmp) / "empty"
            empty.mkdir()
            with self.assertRaises(KabirError):
                run_project(empty)


class TestBuildProject(unittest.TestCase):
    def test_missing_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(KabirError):
                build_project(tmp)

    def test_missing_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            (proj / "Kabir.toml").write_text(
                '[package]\nname = "proj"\n')
            with self.assertRaises(KabirError):
                build_project(proj)

    def test_malformed_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = new_project("hello_app", cwd=tmp)
            (proj / "Kabir.toml").write_text('[package]\nname = 42\n')
            with self.assertRaises(KabirError):
                build_project(proj)

    def test_missing_project_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(KabirError):
                build_project(Path(tmp) / "nope")

    def test_build_prints_nothing(self):
        # Even the failure path must not print (CLI owns output).
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with redirect_stdout(buf):
                with self.assertRaises(KabirError):
                    build_project(tmp)
            self.assertEqual(buf.getvalue(), "")

    @unittest.skipUnless(HAS_COMPILER,
                         "kabir.compile / kabir.checker / kabir.vm "
                         "not built yet (parallel workers)")
    def test_build_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = new_project("hello_app", cwd=tmp)
            out = build_project(proj)
            self.assertEqual(out, proj / "target" / "hello_app.kbc")
            self.assertTrue(out.is_file())
            with open(out, encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertEqual(data["magic"], "KBC1")
            self.assertEqual(data["version"], 2)
            code = load_bytecode(str(out))
            vm = VM()
            buf = io.StringIO()
            with redirect_stdout(buf):
                vm.run_code(code)
            source = (proj / "src" / "main.kbr").read_text()
            self.assertEqual(buf.getvalue(), run_with_interpreter(source))
            self.assertEqual(buf.getvalue(), "Hello from hello_app!\n")

    @unittest.skipUnless(HAS_COMPILER,
                         "kabir.compile / kabir.checker / kabir.vm "
                         "not built yet (parallel workers)")
    def test_build_from_different_cwd_and_nested_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "a" / "b"
            proj = new_project("nested_app", cwd=nested)
            old = os.getcwd()
            try:
                os.chdir(tmp)  # build from elsewhere, absolute path
                out = build_project(proj)
                self.assertEqual(out, proj / "target" / "nested_app.kbc")
                # And with a relative path from another cwd.
                os.chdir(Path(tmp) / "a")
                out2 = build_project(Path("b") / "nested_app")
                self.assertEqual(out2, proj / "target" / "nested_app.kbc")
            finally:
                os.chdir(old)

    @unittest.skipIf(HAS_COMPILER,
                     "compiler exists: this error path does not apply")
    def test_build_without_compiler_reports_clearly(self):
        # kabir/compile.py and kabir/checker.py are built by parallel
        # workers; until they land, build must fail with a clear
        # KabirError (not an ImportError traceback).
        with tempfile.TemporaryDirectory() as tmp:
            proj = new_project("hello_app", cwd=tmp)
            with self.assertRaises(KabirError) as ctx:
                build_project(proj)
            self.assertIn("not available yet", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
