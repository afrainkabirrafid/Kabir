"""Stdlib expansion tests (Kabir 3.0.0 Wave 4): file I/O, JSON, HTTP, time.

File I/O tests use temp dirs only. HTTP tests never touch the real
network -- they talk to a local http.server on 127.0.0.1. Each
feature is checked on the interpreter path (value()) and, for the
happy paths, on the VM path too (run_both() compares printed output).
"""

import io
import json
import os
import socket
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir.cli import main as cli_main
from kabir.errors import KabirError
from kabir.interpreter import Interpreter
from kabir.parser import parse
from kabir.vm import VM


def run(source, filename="<test>"):
    interp = Interpreter(source, filename=filename)
    buf = io.StringIO()
    with redirect_stdout(buf):
        interp.run_source(source, filename=filename)
    return buf.getvalue()


def value(source):
    interp = Interpreter(source, filename="<test>")
    return _value_on(interp, source)


def _value_on(interp, source):
    program = parse(source, "<test>")
    interp.source = source
    interp.lines = source.split("\n")
    last = None
    for stmt in program.statements:
        last = interp.run_stmt(stmt)
    return last


def run_vm(source, filename="<test>"):
    vm = VM(source, filename=filename)
    buf = io.StringIO()
    with redirect_stdout(buf):
        vm.run_source(source, filename=filename)
    return buf.getvalue()


def run_both(source):
    """Run source on both paths; return (vm_output, interp_output)."""
    return run_vm(source), run(source)


def _lit(text):
    """Embed a Python string as a Kabir double-quoted literal."""
    return json.dumps(text)


# --- local HTTP server (no real network) ------------------------------

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/hello":
            body = b"hello world"
            self.send_response(200)
        elif self.path == "/json":
            body = b'{"a": 1}'
            self.send_response(200)
        else:
            body = b"not here"
            self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        if self.path == "/echo":
            out = b"got: " + body
            self.send_response(200)
        else:
            out = b"not here"
            self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class TestFileIO(unittest.TestCase):
    def test_write_then_read_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "note.txt")
            src = ('write_file(%s, "hello\\nworld")\nread_file(%s)'
                   % (_lit(path), _lit(path)))
            self.assertEqual(value(src), "hello\nworld")

    def test_write_file_returns_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.txt")
            self.assertIsNone(value('write_file(%s, "x")' % _lit(path)))
            self.assertTrue(os.path.isfile(path))

    def test_read_file_unicode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "u.txt")
            self.assertIsNone(
                value('write_file(%s, "café বাংলা")' % _lit(path)))
            self.assertEqual(value('read_file(%s)' % _lit(path)),
                             "café বাংলা")

    def test_file_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "here.txt")
            self.assertFalse(value('file_exists(%s)' % _lit(path)))
            value('write_file(%s, "x")' % _lit(path))
            self.assertTrue(value('file_exists(%s)' % _lit(path)))
            # A directory is not a file.
            self.assertFalse(value('file_exists(%s)' % _lit(tmp)))
            # A missing parent is not an error, just absent.
            self.assertFalse(value('file_exists(%s)'
                                   % _lit(os.path.join(tmp, "no", "x"))))

    def test_read_file_missing_is_friendly_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gone.txt")
            with self.assertRaises(KabirError) as ctx:
                value('read_file(%s)' % _lit(path))
            self.assertIn("no such file", str(ctx.exception))
            self.assertNotIn("Traceback", str(ctx.exception))

    def test_read_file_directory_is_friendly_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(KabirError) as ctx:
                value('read_file(%s)' % _lit(tmp))
            self.assertIn("directory", str(ctx.exception))
            self.assertNotIn("Traceback", str(ctx.exception))

    def test_read_file_invalid_utf8_is_friendly_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bin.dat")
            Path(path).write_bytes(b"\xff\xfe\x00bad")
            with self.assertRaises(KabirError) as ctx:
                value('read_file(%s)' % _lit(path))
            self.assertIn("UTF-8", str(ctx.exception))
            self.assertNotIn("Traceback", str(ctx.exception))

    def test_write_file_missing_dir_is_friendly_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "no", "such", "a.txt")
            with self.assertRaises(KabirError) as ctx:
                value('write_file(%s, "x")' % _lit(path))
            self.assertNotIn("Traceback", str(ctx.exception))

    def test_write_file_to_directory_is_friendly_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(KabirError) as ctx:
                value('write_file(%s, "x")' % _lit(tmp))
            self.assertIn("directory", str(ctx.exception))

    def test_file_io_wrong_types_and_counts(self):
        with self.assertRaises(KabirError):
            value("read_file(42)")
        with self.assertRaises(KabirError):
            value('read_file("a", "b")')
        with self.assertRaises(KabirError):
            value("read_file()")
        with self.assertRaises(KabirError):
            value('write_file("a.txt", 42)')
        with self.assertRaises(KabirError):
            value("write_file(42, \"x\")")
        with self.assertRaises(KabirError):
            value('write_file("a.txt")')
        with self.assertRaises(KabirError):
            value("file_exists([1])")
        with self.assertRaises(KabirError):
            value('file_exists("a", "b")')


class TestJSON(unittest.TestCase):
    def test_json_parse_scalars(self):
        self.assertEqual(value('json_parse("42")'), 42)
        self.assertEqual(value('json_parse("3.5")'), 3.5)
        self.assertEqual(value('json_parse("\\"hi\\"")'), "hi")
        self.assertIs(value('json_parse("true")'), True)
        self.assertIsNone(value('json_parse("null")'))

    def test_json_parse_object_and_array(self):
        # NOTE: a "{" cannot appear in a Kabir string literal (it
        # starts interpolation, and there is no \{ escape), so the
        # JSON text here comes from str() of a dict literal, whose
        # printed form is valid JSON for non-string values.
        parsed = value('json_parse(str({"a": [1, 2.5, true, null]}))')
        self.assertEqual(parsed, {"a": [1, 2.5, True, None]})
        # A string-valued object round-trips through json_stringify.
        self.assertEqual(
            value('json_parse(json_stringify({"s": "hi"}))'),
            {"s": "hi"})

    def test_json_stringify(self):
        self.assertEqual(value('json_stringify(42)'), "42")
        self.assertEqual(value('json_stringify("hi")'), '"hi"')
        self.assertEqual(value('json_stringify(true)'), "true")
        self.assertEqual(value('json_stringify(null)'), "null")
        self.assertEqual(value('json_stringify([1, "a", true])'),
                         '[1, "a", true]')
        self.assertEqual(value('json_stringify({"a": 1})'), '{"a": 1}')

    def test_json_round_trip(self):
        src = ('let v = {"name": "kat", "n": [1, 2, 3], "ok": true}\n'
               'json_parse(json_stringify(v)) == v')
        self.assertTrue(value(src))

    def test_json_parse_invalid_is_friendly_error(self):
        with self.assertRaises(KabirError) as ctx:
            value('json_parse("[1, 2")')
        self.assertIn("JSON", str(ctx.exception))
        self.assertNotIn("Traceback", str(ctx.exception))
        with self.assertRaises(KabirError):
            value('json_parse("hello")')
        with self.assertRaises(KabirError):
            value('json_parse("")')
        # A "{" can only reach json_parse from a file (no \{ escape
        # in string literals), so the brace case is covered that way.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.json")
            Path(path).write_text("{not json", encoding="utf-8")
            with self.assertRaises(KabirError) as ctx:
                value('json_parse(read_file(%s))' % _lit(path))
            self.assertIn("JSON", str(ctx.exception))
            self.assertNotIn("Traceback", str(ctx.exception))

    def test_json_wrong_types(self):
        with self.assertRaises(KabirError):
            value("json_parse(42)")
        with self.assertRaises(KabirError):
            value('json_parse("1", "2")')
        with self.assertRaises(KabirError):
            value("json_stringify()")
        with self.assertRaises(KabirError):
            value('json_stringify(1, 2)')

    def test_json_stringify_function_is_friendly_error(self):
        with self.assertRaises(KabirError) as ctx:
            value("func f() { return 1 }\njson_stringify(f)")
        self.assertIn("JSON", str(ctx.exception))
        self.assertNotIn("Traceback", str(ctx.exception))


class TestHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        import threading
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_http_get(self):
        self.assertEqual(value('http_get(%s)' % _lit(self.base + "/hello")),
                         "hello world")

    def test_http_get_json_body_is_plain_string(self):
        body = value('http_get(%s)' % _lit(self.base + "/json"))
        self.assertEqual(json.loads(body), {"a": 1})
        # ... and json_parse can consume it directly.
        self.assertEqual(
            value('json_parse(http_get(%s))' % _lit(self.base + "/json")),
            {"a": 1})

    def test_http_post_echo(self):
        self.assertEqual(
            value('http_post(%s, "ping")'
                  % _lit(self.base + "/echo")),
            "got: ping")

    def test_http_get_404_is_friendly_error(self):
        with self.assertRaises(KabirError) as ctx:
            value('http_get(%s)' % _lit(self.base + "/missing"))
        self.assertIn("404", str(ctx.exception))
        self.assertNotIn("Traceback", str(ctx.exception))

    def test_http_post_404_is_friendly_error(self):
        with self.assertRaises(KabirError) as ctx:
            value('http_post(%s, "x")' % _lit(self.base + "/missing"))
        self.assertIn("404", str(ctx.exception))

    def test_http_get_unreachable_is_friendly_error(self):
        dead = "http://127.0.0.1:%d/nope" % _free_port()
        with self.assertRaises(KabirError) as ctx:
            value('http_get(%s)' % _lit(dead))
        self.assertNotIn("Traceback", str(ctx.exception))

    def test_http_bad_url_is_friendly_error(self):
        with self.assertRaises(KabirError) as ctx:
            value('http_get("not a url")')
        self.assertNotIn("Traceback", str(ctx.exception))

    def test_http_wrong_types_and_counts(self):
        with self.assertRaises(KabirError):
            value("http_get(42)")
        with self.assertRaises(KabirError):
            value('http_get("http://x", "y")')
        with self.assertRaises(KabirError):
            value('http_post(%s, 42)' % _lit(self.base + "/echo"))
        with self.assertRaises(KabirError):
            value('http_post(%s)' % _lit(self.base + "/echo"))


class TestTime(unittest.TestCase):
    def test_now_is_epoch_seconds(self):
        before = time.time()
        got = value("now()")
        after = time.time()
        self.assertIsInstance(got, float)
        self.assertGreaterEqual(got, before)
        self.assertLessEqual(got, after)
        self.assertGreater(got, 1700000000)  # after Nov 2023

    def test_now_is_non_decreasing(self):
        self.assertLessEqual(value("now()"), value("now()"))

    def test_now_takes_no_arguments(self):
        with self.assertRaises(KabirError):
            value("now(1)")

    def test_sleep_zero_returns_null(self):
        self.assertIsNone(value("sleep(0)"))

    def test_sleep_waits(self):
        start = time.time()
        self.assertIsNone(value("sleep(0.05)"))
        self.assertGreaterEqual(time.time() - start, 0.04)

    def test_sleep_rejects_negative_and_wrong_types(self):
        with self.assertRaises(KabirError):
            value("sleep(-1)")
        with self.assertRaises(KabirError):
            value('sleep("1")')
        with self.assertRaises(KabirError):
            value("sleep(true)")
        with self.assertRaises(KabirError):
            value("sleep()")
        with self.assertRaises(KabirError):
            value("sleep(1, 2)")


class TestVMPath(unittest.TestCase):
    """Happy paths must print identically on the VM and interpreter."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        import threading
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_vm_file_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "vm.txt")
            src = ('write_file(%s, "vm says hi")\n'
                   'print(read_file(%s))\nprint(file_exists(%s))'
                   % (_lit(path), _lit(path), _lit(path)))
            vm_out, interp_out = run_both(src)
            self.assertEqual(vm_out, "vm says hi\ntrue\n")
            self.assertEqual(vm_out, interp_out)

    def test_vm_json_round_trip(self):
        src = ('let v = json_parse(str({"a": [1, 2]}))\n'
               'print(json_stringify(v))\nprint(v["a"][1])')
        vm_out, interp_out = run_both(src)
        self.assertEqual(vm_out, '{"a": [1, 2]}\n2\n')
        self.assertEqual(vm_out, interp_out)

    def test_vm_http_get(self):
        src = 'print(http_get(%s))' % _lit(self.base + "/hello")
        vm_out, interp_out = run_both(src)
        self.assertEqual(vm_out, "hello world\n")
        self.assertEqual(vm_out, interp_out)

    def test_vm_http_post(self):
        src = 'print(http_post(%s, "vm"))' % _lit(self.base + "/echo")
        vm_out, interp_out = run_both(src)
        self.assertEqual(vm_out, "got: vm\n")
        self.assertEqual(vm_out, interp_out)

    def test_vm_time(self):
        src = 'print(type(now()))\nprint(sleep(0) == null)'
        vm_out, interp_out = run_both(src)
        self.assertEqual(vm_out, "number\ntrue\n")
        self.assertEqual(vm_out, interp_out)

    def test_vm_missing_file_error_is_friendly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gone.txt")
            vm = VM('read_file(%s)' % _lit(path), filename="<test>")
            with self.assertRaises(KabirError) as ctx:
                vm.run_source('read_file(%s)' % _lit(path),
                              filename="<test>")
            self.assertIn("no such file", str(ctx.exception))
            self.assertNotIn("Traceback", str(ctx.exception))


class TestCLIEndToEnd(unittest.TestCase):
    """`kabir run` (parse -> check -> compile -> VM) with the new names.

    The checker has no entries for these names yet (its
    _check_stdlib_call else-branch would wrongly demand a number
    argument), so they must type as "any" and pass through to the VM.
    """

    def _run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli_main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_cli_file_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = os.path.join(tmp, "data.txt")
            prog = os.path.join(tmp, "prog.kbr")
            Path(prog).write_text(
                'write_file(%s, str({"n": 7}))\n'
                'let v = json_parse(read_file(%s))\n'
                'print(v["n"])\nprint(file_exists(%s))\n'
                % (_lit(data), _lit(data), _lit(data)),
                encoding="utf-8")
            code, out, err = self._run_cli(["run", prog])
            self.assertEqual(code, 0, err)
            self.assertEqual(out, "7\ntrue\n")

    def test_cli_missing_file_friendly_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            prog = os.path.join(tmp, "prog.kbr")
            Path(prog).write_text(
                'print(read_file(%s))\n'
                % _lit(os.path.join(tmp, "gone.txt")),
                encoding="utf-8")
            code, out, err = self._run_cli(["run", prog])
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("no such file", err)
            self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
