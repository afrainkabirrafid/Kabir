"""Tests for the native binary cache (5.0.0 Leap 3).

The cache lives in ~/.kabir/cache/native (or $KABIR_CACHE_DIR/native)
and is keyed by a fingerprint of the generated C source + rt.c + rt.h
+ Kabir version + C compiler identity, so a cached binary is reused
only when every input that could change it is identical. These tests
point KABIR_CACHE_DIR at a temp dir, so they never touch the user's
real cache. End-to-end tests are skipped when no C compiler is
available.
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


class CacheTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kabir-cache-test-")
        self.cache_dir = os.path.join(self.tmp, "cache")
        self._old_env = os.environ.get("KABIR_CACHE_DIR")
        os.environ["KABIR_CACHE_DIR"] = self.cache_dir

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("KABIR_CACHE_DIR", None)
        else:
            os.environ["KABIR_CACHE_DIR"] = self._old_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def native_dir(self):
        return os.path.join(self.cache_dir, "native")


class TestNativeCache(CacheTestCase):
    @NEEDS_CC
    def test_second_compile_is_a_cache_hit(self):
        """Same source twice: same binary path, binary still runs."""
        src = 'print("cached")\n'
        first = codegen.cached_compile_native(src, filename="<test>")
        self.assertTrue(os.path.isfile(first))
        self.assertTrue(first.startswith(self.native_dir()))
        mtime = os.path.getmtime(first)
        second = codegen.cached_compile_native(src, filename="<test>")
        self.assertEqual(first, second)
        self.assertEqual(os.path.getmtime(first), mtime,
                         "cache hit must not rewrite the binary")
        proc = subprocess.run([second], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "cached\n")

    @NEEDS_CC
    def test_changed_source_misses_the_cache(self):
        """A one-character source change compiles a different binary."""
        first = codegen.cached_compile_native('print("one")\n',
                                              filename="<test>")
        second = codegen.cached_compile_native('print("two")\n',
                                               filename="<test>")
        self.assertNotEqual(first, second)
        for path, expected in ((first, "one\n"), (second, "two\n")):
            proc = subprocess.run([path], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, expected)

    @NEEDS_CC
    def test_no_cache_forces_a_fresh_compile(self):
        """use_cache=False never returns the cached binary path."""
        src = 'print("fresh")\n'
        cached = codegen.cached_compile_native(src, filename="<test>")
        fresh = codegen.cached_compile_native(src, filename="<test>",
                                              use_cache=False)
        self.assertNotEqual(cached, fresh)
        self.assertFalse(fresh.startswith(self.native_dir()))
        proc = subprocess.run([fresh], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "fresh\n")
        # The cached binary is still there and still correct.
        proc = subprocess.run([cached], capture_output=True, text=True)
        self.assertEqual(proc.stdout, "fresh\n")

    @NEEDS_CC
    def test_cache_key_changes_with_generated_c(self):
        """The key covers the generated C, not just the Kabir source."""
        cc = codegen._find_cc()
        key1 = codegen._native_cache_key('print("a");\n', cc)
        key2 = codegen._native_cache_key('print("b");\n', cc)
        self.assertNotEqual(key1, key2)
        self.assertEqual(key1, codegen._native_cache_key(
            'print("a");\n', cc))

    @NEEDS_CC
    def test_rt_object_is_cached_and_reused(self):
        """rt.c is compiled once; a second program reuses the .o."""
        codegen.cached_compile_native('print(1)\n', filename="<test>")
        objects = [name for name in os.listdir(self.native_dir())
                   if name.startswith("rt-") and name.endswith(".o")]
        self.assertEqual(len(objects), 1,
                         "expected exactly one cached rt.o, got %r"
                         % objects)
        mtime = os.path.getmtime(os.path.join(self.native_dir(),
                                              objects[0]))
        codegen.cached_compile_native('print(2)\n', filename="<test>")
        self.assertEqual(os.path.getmtime(os.path.join(
            self.native_dir(), objects[0])), mtime,
            "second program must reuse the cached rt.o, not rebuild it")

    @NEEDS_CC
    def test_build_out_path_gets_a_copy_of_the_cached_binary(self):
        """cached_compile_native with out_path copies, not moves."""
        src = 'print("built")\n'
        target = os.path.join(self.tmp, "target", "my_app")
        out = codegen.cached_compile_native(src, filename="<test>",
                                            out_path=target)
        self.assertEqual(out, os.path.abspath(target))
        self.assertTrue(os.path.isfile(target))
        self.assertTrue(os.access(target, os.X_OK))
        proc = subprocess.run([target], capture_output=True, text=True)
        self.assertEqual(proc.stdout, "built\n")
        # The cache entry still exists: a repeat build is a hit.
        cached = codegen.cached_compile_native(src, filename="<test>")
        self.assertTrue(os.path.isfile(cached))
        out2 = codegen.cached_compile_native(src, filename="<test>",
                                             out_path=target)
        self.assertEqual(out2, os.path.abspath(target))

    @NEEDS_CC
    def test_run_native_uses_the_cache(self):
        """run_native output is identical on a cache hit."""
        src = 'print(40 + 2)\n'
        code1, out1, _err1 = codegen.run_native(src)
        self.assertEqual(code1, 0)
        self.assertEqual(out1, "42\n")
        code2, out2, _err2 = codegen.run_native(src)
        self.assertEqual(code2, 0)
        self.assertEqual(out2, "42\n")
        self.assertEqual(len([n for n in os.listdir(self.native_dir())
                              if n.startswith("prog-")]), 2,
                         "one cached binary + its sha256 sidecar, got %r"
                         % os.listdir(self.native_dir()))
        self.assertEqual(len([n for n in os.listdir(self.native_dir())
                              if n.startswith("rt-")]), 1,
                         "one cached rt.o, got %r"
                         % os.listdir(self.native_dir()))

    @NEEDS_CC
    def test_cache_survives_a_stale_binary(self):
        """A damaged cache entry is recompiled, never executed.

        The key is a hash of the inputs, so there is no separate
        manifest to go stale -- and each cached binary carries a
        sha256 sidecar, so corrupting the binary (truncated here)
        makes the next compile treat it as a miss and rebuild a
        correct binary.
        """
        src = 'print("good")\n'
        cached = codegen.cached_compile_native(src, filename="<test>")
        with open(cached, "wb") as handle:
            handle.write(b"not a binary")
        rebuilt = codegen.cached_compile_native(src, filename="<test>")
        self.assertEqual(rebuilt, cached)
        proc = subprocess.run([rebuilt], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "good\n")


class TestNativeCacheCli(CacheTestCase):
    @NEEDS_CC
    def test_run_native_twice_via_cli(self):
        """`kabir run --native` twice: same output, second is a hit."""
        prog = os.path.join(self.tmp, "hello.kbr")
        with open(prog, "w", encoding="utf-8") as handle:
            handle.write('print("cli cached")\n')
        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT
        for extra in ([], ["--no-cache"]):
            cmd = [sys.executable, "-m", "kabir", "run", "--native"] \
                + extra + [prog]
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, "cli cached\n")
        self.assertTrue(os.path.isdir(self.native_dir()))
        self.assertTrue(any(name.startswith("prog-")
                            for name in os.listdir(self.native_dir())))

    def test_no_cache_without_native_is_a_usage_error(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT
        prog = os.path.join(self.tmp, "hello.kbr")
        with open(prog, "w", encoding="utf-8") as handle:
            handle.write('print("x")\n')
        proc = subprocess.run(
            [sys.executable, "-m", "kabir", "run", "--no-cache", prog],
            capture_output=True, text=True, env=env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--no-cache needs --native", proc.stderr)


if __name__ == "__main__":
    unittest.main()


class TestNativeRuntimeMissing(unittest.TestCase):
    def test_missing_runtime_files_raise_kabir_error(self):
        """A broken install (rt.c/rt.h absent) must fail with a
        friendly KabirError, never a raw FileNotFoundError traceback.
        Regression: a fresh pip install before 5.0.0 shipped no
        rt.c/rt.h at all (package-data was missing), and
        `kabir run --native` crashed with a traceback."""
        missing = os.path.join(tempfile.gettempdir(), "kabir-no-rt-here")
        os.makedirs(missing, exist_ok=True)
        old_rt_c, old_rt_h = codegen._RT_C, codegen._RT_H
        codegen._RT_C = os.path.join(missing, "rt.c")
        codegen._RT_H = os.path.join(missing, "rt.h")
        try:
            with self.assertRaises(KabirError) as ctx:
                codegen.cached_compile_native(
                    'print("hi")\n', filename="<test>")
            self.assertIn("cannot read", str(ctx.exception))
        finally:
            codegen._RT_C, codegen._RT_H = old_rt_c, old_rt_h
