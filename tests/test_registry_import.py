"""Registry import wiring tests (Kabir 7.0.0 Leap 1).

Bare-name ``import <package>`` resolves via the project's Kabir.toml
[dependencies] + Kabir.lock to vendor/<name>/<version>/src/main.kbr,
on both the bytecode VM and the legacy interpreter, including
transitive dependencies (a vendored package importing its own
dependency). End-to-end against local test registries only -- the
live registry repo does not exist and is never contacted.

The flow under test is the v5.0.0 pattern: publish a tiny package
with ``stage_publish`` against a local index directory, copy the
staged files into the registry, then ``kabir add`` from it.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir.errors import KabirError
from kabir.formatter import format_source
from kabir.imports import (find_project_root, is_package_spec,
                           resolve_file_import, resolve_import_spec)
from kabir.interpreter import Interpreter
from kabir.parser import parse
from kabir.project import add_package, new_project, stage_publish

PYTHON = "python3"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GREET_KBR = (
    'func greet(who) {\n'
    '    return "Hello, " + who + "!"\n'
    '}\n'
)

SHOUT_KBR = (
    'func shout(who) {\n'
    '    return "HEY, " + who + "!"\n'
    '}\n'
)

GREET_ON_SHOUT_KBR = (
    'import shout\n'
    'func greet(who) {\n'
    '    return shout(who)\n'
    '}\n'
)


def publish_to_registry(tmp, registry, name, main_src, deps=None):
    """Publish *name* 0.1.0 into the local registry dir *registry*.

    Builds the package with ``stage_publish`` against the registry's
    current index, then copies the staged index.json + tarball into
    the registry (the manual step the owner does with git push).
    Successive calls accumulate in the same index.
    """
    lib = new_project(name, cwd=tmp)
    (lib / "src" / "main.kbr").write_text(main_src)
    if deps:
        text = (lib / "Kabir.toml").read_text()
        text += "\n[dependencies]\n"
        text += "".join('%s = "%s"\n' % item for item in sorted(deps.items()))
        (lib / "Kabir.toml").write_text(text)
    staged = stage_publish(lib, registry=registry)
    shutil.copy(staged["staged_index"], registry / "index.json")
    shutil.copy(staged["staged_tarball"],
                registry / "packages" / ("%s-0.1.0.tar.gz" % name))
    return staged


def run_interp(main_path):
    """Run the .kbr file at main_path via Interpreter; return stdout."""
    source = Path(main_path).read_text(encoding="utf-8")
    interp = Interpreter(source, filename=str(main_path))
    buf = io.StringIO()
    with redirect_stdout(buf):
        interp.run_source(source, filename=str(main_path))
    return buf.getvalue()


def run_interp_error(main_path):
    """Run the file via Interpreter; return the KabirError raised."""
    source = Path(main_path).read_text(encoding="utf-8")
    interp = Interpreter(source, filename=str(main_path))
    try:
        interp.run_source(source, filename=str(main_path))
    except KabirError as err:
        return err
    return None


def run_cli(path, cwd=None):
    """Run the .kbr file via `kabir run` (the VM path); return the proc."""
    env = dict(os.environ, PYTHONPATH=ROOT)
    return subprocess.run([PYTHON, "-m", "kabir", "run", path],
                          capture_output=True, text=True,
                          cwd=cwd or ROOT, env=env)


class RegistryImportBase(unittest.TestCase):
    """A temp registry + a consumer project that added greet 0.1.0."""

    def setUp(self):
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

    def write_main(self, source):
        self.main.write_text(source, encoding="utf-8")
        return self.main


class TestBareImportEndToEnd(RegistryImportBase):
    def test_interpreter(self):
        self.write_main('import greet\nprint(greet("Rafid"))\n')
        self.assertEqual(run_interp(self.main), "Hello, Rafid!\n")

    def test_vm_via_cli(self):
        self.write_main('import greet\nprint(greet("Rafid"))\n')
        proc = run_cli(str(self.main))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "Hello, Rafid!\n")

    def test_quoted_import_still_works(self):
        # A quoted file import next to the bare one, same program.
        (self.app / "src" / "lib.kbr").write_text(
            'let note = "from lib.kbr"\n', encoding="utf-8")
        self.write_main('import greet\nimport "lib.kbr"\n'
                        'print(greet("Rafid"))\nprint(note)\n')
        self.assertEqual(run_interp(self.main),
                         "Hello, Rafid!\nfrom lib.kbr\n")
        proc = run_cli(str(self.main))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "Hello, Rafid!\nfrom lib.kbr\n")

    def test_import_twice_uses_module_cache(self):
        self.write_main('import greet\nimport greet\n'
                        'print(greet("Rafid"))\n')
        self.assertEqual(run_interp(self.main), "Hello, Rafid!\n")
        proc = run_cli(str(self.main))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_publish_stage_still_works(self):
        # stage_publish keeps working: staged index validates and the
        # tarball lands in the stage dir (covered end-to-end above,
        # asserted here directly for the publish half).
        staged = publish_to_registry(self.tmp, self.registry,
                                     "other", 'func other() {\n'
                                              '    return 42\n}\n')
        self.assertEqual(staged["name"], "other")
        self.assertEqual(staged["version"], "0.1.0")
        self.assertTrue(Path(staged["staged_tarball"]).is_file())
        index = json.loads(
            Path(staged["staged_index"]).read_text(encoding="utf-8"))
        self.assertIn("other", index["packages"])
        self.assertIn("0.1.0", index["packages"]["other"])
        self.assertIn("greet", index["packages"])


class TestTransitiveImport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.registry = self.tmp / "registry"
        (self.registry / "packages").mkdir(parents=True)
        publish_to_registry(self.tmp, self.registry, "shout", SHOUT_KBR)
        publish_to_registry(self.tmp, self.registry, "greet",
                            GREET_ON_SHOUT_KBR, deps={"shout": "0.1.0"})
        self.app = new_project("my_app", cwd=self.tmp)
        add_package("greet", "0.1.0", self.app, self.registry)
        self.main = self.app / "src" / "main.kbr"

    def tearDown(self):
        self._tmp.cleanup()

    def test_transitive_dep_resolves_from_consumer_vendor_tree(self):
        # greet's own `import shout` finds shout in the *consumer*
        # project's vendor/ tree, on both backends.
        self.main.write_text('import greet\nprint(greet("Rafid"))\n',
                             encoding="utf-8")
        self.assertEqual(run_interp(self.main), "HEY, Rafid!\n")
        proc = run_cli(str(self.main))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "HEY, Rafid!\n")

    def test_transitive_dep_importable_directly(self):
        # shout is vendored + locked, so the consumer can import it
        # directly too -- the lockfile is the installed set.
        self.main.write_text('import shout\nprint(shout("Rafid"))\n',
                             encoding="utf-8")
        self.assertEqual(run_interp(self.main), "HEY, Rafid!\n")
        proc = run_cli(str(self.main))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "HEY, Rafid!\n")


class TestHyphenatedPackageName(unittest.TestCase):
    def test_hyphenated_name_parses_and_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            registry = tmp / "registry"
            (registry / "packages").mkdir(parents=True)
            publish_to_registry(tmp, registry, "my-pkg", GREET_KBR)
            app = new_project("my_app", cwd=tmp)
            add_package("my-pkg", "0.1.0", app, registry)
            main = app / "src" / "main.kbr"
            main.write_text('import my-pkg\nprint(greet("Rafid"))\n',
                            encoding="utf-8")
            self.assertEqual(run_interp(main), "Hello, Rafid!\n")
            proc = run_cli(str(main))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, "Hello, Rafid!\n")


class TestBareImportErrors(RegistryImportBase):
    def test_missing_package_names_the_package(self):
        self.write_main('import nope\n')
        err = run_interp_error(self.main)
        self.assertIsNotNone(err)
        self.assertIn('"nope"', err.message)
        self.assertIn("kabir add nope@", err.message)
        proc = run_cli(str(self.main))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('"nope"', proc.stderr)
        self.assertIn("kabir add nope@", proc.stderr)

    def test_package_not_in_manifest_but_lockfile_only(self):
        # A name in neither [dependencies] nor the lockfile is a plain
        # "not a dependency" error, not a crash.
        self.write_main('import other\n')
        err = run_interp_error(self.main)
        self.assertIsNotNone(err)
        self.assertIn('"other"', err.message)
        self.assertIn("not a dependency", err.message)

    def test_lockfile_entry_missing(self):
        # Listed in Kabir.toml but no Kabir.lock entry: reinstall.
        lock_path = self.app / "Kabir.lock"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        del lock["packages"]["greet"]
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
        self.write_main('import greet\n')
        err = run_interp_error(self.main)
        self.assertIsNotNone(err)
        self.assertIn('"greet"', err.message)
        self.assertIn("Kabir.lock", err.message)
        self.assertIn("kabir add greet@0.1.0", err.message)

    def test_version_mismatch_manifest_vs_lock(self):
        # Kabir.toml bumped without reinstalling: exact versions only,
        # so this is an error naming both versions.
        text = (self.app / "Kabir.toml").read_text(encoding="utf-8")
        (self.app / "Kabir.toml").write_text(
            text.replace('greet = "0.1.0"', 'greet = "0.2.0"'),
            encoding="utf-8")
        self.write_main('import greet\n')
        err = run_interp_error(self.main)
        self.assertIsNotNone(err)
        self.assertIn('"greet"', err.message)
        self.assertIn("Version mismatch", err.message)
        self.assertIn("0.2.0", err.message)
        self.assertIn("0.1.0", err.message)

    def test_vendored_entry_point_missing(self):
        vendored = self.app / "vendor" / "greet" / "0.1.0" / "src" / "main.kbr"
        vendored.unlink()
        self.write_main('import greet\n')
        err = run_interp_error(self.main)
        self.assertIsNotNone(err)
        self.assertIn('"greet"', err.message)
        self.assertIn("missing", err.message)
        self.assertIn("kabir add greet@0.1.0", err.message)

    def test_no_project_above(self):
        # A bare import outside any project: no Kabir.toml to read.
        with tempfile.TemporaryDirectory() as tmp:
            stray = Path(tmp) / "stray.kbr"
            stray.write_text('import greet\n', encoding="utf-8")
            err = run_interp_error(stray)
            self.assertIsNotNone(err)
            self.assertIn('"greet"', err.message)
            self.assertIn("no Kabir.toml", err.message)


class TestBareImportParser(unittest.TestCase):
    def test_bare_name_parses(self):
        program = parse('import greet\n')
        self.assertEqual(len(program.statements), 1)
        self.assertEqual(program.statements[0].path, "greet")

    def test_hyphenated_name_parses(self):
        program = parse('import my-pkg\n')
        self.assertEqual(program.statements[0].path, "my-pkg")

    def test_quoted_path_still_parses(self):
        program = parse('import "lib.kbr"\n')
        self.assertEqual(program.statements[0].path, "lib.kbr")

    def test_number_is_a_parse_error(self):
        with self.assertRaises(KabirError) as ctx:
            parse('import 5\n')
        self.assertIn("file path", ctx.exception.message)

    def test_underscore_name_is_a_parse_error(self):
        # Package names must start with a letter, like project names.
        with self.assertRaises(KabirError) as ctx:
            parse('import _foo\n')
        self.assertIn("Invalid package name", ctx.exception.message)

    def test_bare_name_with_dot_suffix_is_a_parse_error(self):
        with self.assertRaises(KabirError):
            parse('import greet.kbr\n')

    def test_formatter_keeps_bare_import_bare(self):
        self.assertEqual(format_source('import greet\n'), 'import greet\n')
        self.assertEqual(format_source('import my-pkg\n'),
                         'import my-pkg\n')
        self.assertEqual(format_source('import "lib.kbr"\n'),
                         'import "lib.kbr"\n')


class TestResolverUnit(RegistryImportBase):
    def test_is_package_spec(self):
        self.assertTrue(is_package_spec("greet"))
        self.assertTrue(is_package_spec("my-pkg"))
        self.assertFalse(is_package_spec("lib.kbr"))
        self.assertFalse(is_package_spec("sub/lib.kbr"))
        self.assertFalse(is_package_spec(""))

    def test_resolve_bare_name_to_vendor_entry(self):
        expected = os.path.realpath(str(
            self.app / "vendor" / "greet" / "0.1.0" / "src" / "main.kbr"))
        self.assertEqual(
            resolve_import_spec("greet", str(self.main)), expected)

    def test_resolve_file_import_relative_to_importer(self):
        expected = os.path.realpath(str(self.app / "src" / "lib.kbr"))
        self.assertEqual(
            resolve_import_spec("lib.kbr", str(self.main)), expected)
        self.assertEqual(
            resolve_file_import("lib.kbr", str(self.main)), expected)

    def test_find_project_root_skips_vendored_manifest(self):
        # From inside vendor/greet/0.1.0/src the walk must skip the
        # package's own Kabir.toml and land on the consumer project.
        inside = self.app / "vendor" / "greet" / "0.1.0" / "src"
        self.assertEqual(find_project_root(str(inside)),
                         self.app.resolve())
        self.assertEqual(find_project_root(str(self.app / "src")),
                         self.app.resolve())

    def test_find_project_root_none_without_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(find_project_root(tmp))


if __name__ == "__main__":
    unittest.main()
