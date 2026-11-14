"""Tests for native compilation (3.0.0 Wave 1): Kabir AST -> C99 -> binary.

Transpile tests run everywhere. End-to-end tests (compile + run and
compare stdout with the VM run of the same program) are skipped when
no C compiler is available. The VM is run in-process via the same
parse -> check -> compile -> run path `kabir run` uses.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from kabir import codegen
from kabir.checker import check_program
from kabir.compile import compile_program
from kabir.errors import KabirError
from kabir.parser import parse as parse_source
from kabir.vm import VM

CC = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
NEEDS_CC = unittest.skipIf(CC is None, "no C compiler (cc/gcc/clang)")


def run_vm(source, input_text=""):
    """Run *source* on the VM; return (exit_code, stdout, stderr)."""
    lines = source.split("\n")
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(input_text)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            program = parse_source(source, "<test>")
            check_program(program, filename="<test>", lines=lines)
            code = compile_program(program, "<test>", lines)
            VM(source, filename="<test>").run_code(
                code, filename="<test>", lines=lines)
    except KabirError as err:
        return 1, buf.getvalue(), err.render()
    except RecursionError:
        return 1, buf.getvalue(), "too many nested function calls"
    finally:
        sys.stdin = old_stdin
    return 0, buf.getvalue(), ""


class NativeTestCase(unittest.TestCase):
    """assert_same_output runs a program on the VM and on native."""

    def assert_same_output(self, source, input_text=""):
        vm_code, vm_out, vm_err = run_vm(source, input_text)
        self.assertEqual(vm_code, 0, "VM failed: %s" % vm_err)
        nat_code, nat_out, nat_err = codegen.run_native(
            source, input_text=input_text)
        self.assertEqual(nat_code, 0, "native failed: %s" % nat_err)
        self.assertEqual(nat_out, vm_out,
                         "native stdout differs from VM stdout")

    def assert_native_error(self, source, fragment):
        code, _out, err = codegen.run_native(source)
        self.assertNotEqual(code, 0, "native should have failed")
        self.assertIn(fragment, err)


# ----------------------------------------------------------------------
# Transpile-only tests (no compiler needed)
# ----------------------------------------------------------------------

class TestTranspile(unittest.TestCase):
    def test_hello_transpiles(self):
        c = codegen.transpile('print("hi")\n')
        self.assertIn('#include "rt.h"', c)
        self.assertIn("k_rt_init();", c)
        self.assertIn("k_rt_cleanup();", c)
        self.assertIn('k_builtin("print")', c)
        self.assertIn("k_call(", c)

    def test_function_hoisted_with_forward_decl(self):
        c = codegen.transpile(
            "func fib(n) {\n    if n < 2 {\n        return n\n    }\n"
            "    return fib(n - 1) + fib(n - 2)\n}\nprint(fib(10))\n")
        self.assertIn("static KVal kabir_fn_0(KVal *args, int nargs);", c)
        self.assertIn("static KVal kabir_fn_0(KVal *args, int nargs) {", c)
        self.assertIn("k_make_func(kabir_fn_0,", c)

    def test_each_construct_appears(self):
        c = codegen.transpile(
            "let a = [1, 2]\nlet d = {\"k\": 1}\n"
            "if a[0] == 1 {\n    print(\"one\")\n} elif a[0] == 2 {\n"
            "    print(\"two\")\n} else {\n    print(\"other\")\n}\n"
            "while len(a) > 0 {\n    pop(a)\n}\n"
            "for x in range(3) {\n    print(x)\n}\n"
            "let m = match 2 {\n    1 => \"one\",\n    n if n > 1 => \"big\",\n    _ => \"other\",\n}\n"
            "print(m)\nprint(\"m is {m}\")\nprint(\"a\".upper())\n")
        for fragment in ("k_array_of", "k_new_dict", "k_is_truthy",
                         "k_iter_items", "k_eq", "k_method",
                         "k_build_string"):
            self.assertIn(fragment, c, fragment)

    def test_let_mut_transpiles_like_let(self):
        c = codegen.transpile("let mut xs = [1]\npush(xs, 2)\nprint(xs)\n")
        self.assertIn("k_array_of", c)

    def test_closure_transpiles_to_make_closure(self):
        # 7.0.0: closures work in native mode -- a function that
        # reads an enclosing variable becomes k_make_closure with
        # the captured slot, and its body reads k_env_get.
        c = codegen.transpile(
            "func outer() {\n    let x = 5\n"
            "    func inner() {\n        return x\n    }\n"
            "    return inner()\n}\nprint(outer())\n")
        self.assertIn("k_make_closure(", c)
        self.assertIn("k_env_get(", c)

    def test_plain_function_still_uses_make_func(self):
        # A function with no captures is unchanged: k_make_func,
        # no closure machinery.
        c = codegen.transpile(
            "func fib(n) {\n    if n < 2 {\n        return n\n    }\n"
            "    return fib(n - 1) + fib(n - 2)\n}\nprint(fib(10))\n")
        self.assertIn("k_make_func(kabir_fn_0,", c)
        self.assertNotIn("k_make_closure(", c)

    def test_missing_import_is_a_clear_error(self):
        with self.assertRaises(KabirError) as ctx:
            codegen.transpile('import "helpers.kbr"\nprint(1)\n')
        self.assertIn('Cannot import "helpers.kbr" -- no such file',
                      str(ctx.exception))

    def test_undefined_variable_is_a_clear_error(self):
        with self.assertRaises(KabirError) as ctx:
            codegen.transpile("print(nope)\n")
        self.assertIn("Undefined variable 'nope'", str(ctx.exception))

    def test_return_outside_function_is_a_clear_error(self):
        with self.assertRaises(KabirError) as ctx:
            codegen.transpile("return 1\n")
        self.assertIn("'return' can only be used inside a function",
                      str(ctx.exception))

    def test_break_outside_loop_is_a_clear_error(self):
        with self.assertRaises(KabirError) as ctx:
            codegen.transpile("break\n")
        self.assertIn("'break' can only be used inside a loop",
                      str(ctx.exception))

    def test_string_escaping(self):
        c = codegen.transpile('print("a\\"b\\\\c\\nd")\n')
        self.assertIn('k_str("a\\"b\\\\c\\nd")', c)


# ----------------------------------------------------------------------
# End-to-end tests (need cc)
# ----------------------------------------------------------------------

@NEEDS_CC
class TestNativeEndToEnd(NativeTestCase):
    def test_compile_native_returns_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "prog")
            path = codegen.compile_native('print("ok")\n', out_path=out)
            self.assertEqual(path, out)
            self.assertTrue(os.path.isfile(out))
            self.assertTrue(os.access(out, os.X_OK))
            proc = subprocess.run([out], capture_output=True, text=True)
            self.assertEqual(proc.stdout, "ok\n")

    def test_arithmetic_and_number_formatting(self):
        self.assert_same_output(
            "print(2 + 3)\nprint(7 - 10)\nprint(3 * 4)\n"
            "print(10 / 4)\nprint(4 / 2)\nprint(7 % 3)\n"
            "print(-7 % 3)\nprint(2 ** 10)\nprint(0.1 + 0.2)\n"
            "print(2.5)\nprint(-5)\nprint(1000000)\n")

    def test_comparisons_and_logic(self):
        self.assert_same_output(
            "print(1 < 2)\nprint(2 <= 2)\nprint(3 > 4)\n"
            "print(\"a\" < \"b\")\nprint(1 == 1.0)\nprint(1 != 2)\n"
            "print(true and false)\nprint(true or false)\n"
            "print(not false)\nprint(1 and 2)\nprint(0 or \"x\")\n"
            "print(0 and 5)\nprint(\"\" or \"fallback\")\n")

    def test_short_circuit_skips_side_effects(self):
        self.assert_same_output(
            "func side() {\n    print(\"called\")\n    return true\n}\n"
            "print(false and side())\nprint(true or side())\n"
            "print(true and side())\n")

    def test_strings_and_interpolation(self):
        self.assert_same_output(
            "let name = \"Rafid\"\n"
            "print(\"Hello, {name}!\")\n"
            "print(\"2 + 3 = {2 + 3}\")\n"
            "print(\"a\" + \"b\" + \"c\")\n"
            "print(\"abc\"[1])\nprint(\"abc\"[-1])\n"
            "print(len(\"hello\"))\nprint(str(42))\n"
            "print(str([1, \"x\"]))\nprint(str({\"a\": 1}))\n")

    def test_string_methods(self):
        self.assert_same_output(
            "print(\"Hello\".upper())\nprint(\"Hello\".lower())\n"
            "print(\"  pad  \".strip())\n"
            "print(\"a,b,c\".split(\",\"))\n"
            "print(\"a b  c\".split())\n"
            "print(\"-\".join([\"a\", \"b\"]))\n"
            "print(\"hello\".contains(\"ell\"))\n"
            "print(\"hello\".starts_with(\"he\"))\n"
            "print(\"hello\".ends_with(\"lo\"))\n"
            "print(\"aaa\".replace(\"a\", \"b\"))\n")

    def test_arrays_and_dicts(self):
        self.assert_same_output(
            "let a = [3, 1, 2]\nprint(a)\nprint(a[0])\nprint(a[-1])\n"
            "a[1] = 9\nprint(a)\npush(a, 4)\nprint(a)\n"
            "print(pop(a))\nprint(a)\nprint(len(a))\n"
            "let d = {\"x\": 1, \"y\": 2}\nprint(d)\nprint(d[\"x\"])\n"
            "d[\"z\"] = 3\nprint(d)\nprint(keys(d))\n"
            "print([1, 2] + [3])\nprint(d == {\"x\": 1, \"y\": 2, \"z\": 3})\n")

    def test_nested_index_assignment(self):
        self.assert_same_output(
            "let todos = [{\"title\": \"a\", \"done\": false}]\n"
            "todos[0][\"done\"] = true\nprint(todos)\n"
            "print(todos[0][\"title\"])\n")

    def test_if_elif_else(self):
        self.assert_same_output(
            "func grade(n) {\n"
            "    if n >= 90 {\n        return \"A\"\n"
            "    } elif n >= 70 {\n        return \"B\"\n"
            "    } elif n >= 50 {\n        return \"C\"\n"
            "    } else {\n        return \"F\"\n    }\n}\n"
            "print(grade(95))\nprint(grade(75))\nprint(grade(55))\n"
            "print(grade(10))\n")

    def test_while_break_continue(self):
        self.assert_same_output(
            "let mut i = 0\nlet out = []\n"
            "while true {\n    i += 1\n"
            "    if i == 3 {\n        continue\n    }\n"
            "    if i > 5 {\n        break\n    }\n"
            "    push(out, i)\n}\nprint(out)\nprint(i)\n")

    def test_for_loops(self):
        self.assert_same_output(
            "for n in range(2, 5) {\n    print(n)\n}\n"
            "for ch in \"ab\" {\n    print(ch)\n}\n"
            "let d = {\"a\": 1, \"b\": 2}\n"
            "for k in d {\n    print(k)\n}\n"
            "let mut total = 0\n"
            "for x in [1, 2, 3] {\n    total += x\n}\nprint(total)\n")

    def test_recursion_and_mutual_recursion(self):
        self.assert_same_output(
            "func fib(n) {\n    if n < 2 {\n        return n\n    }\n"
            "    return fib(n - 1) + fib(n - 2)\n}\n"
            "print(fib(15))\n"
            "func is_even(n) {\n    if n == 0 {\n        return true\n    }\n"
            "    return is_odd(n - 1)\n}\n"
            "func is_odd(n) {\n    if n == 0 {\n        return false\n    }\n"
            "    return is_even(n - 1)\n}\n"
            "print(is_even(10))\nprint(is_odd(7))\n")

    def test_first_class_and_anonymous_functions(self):
        self.assert_same_output(
            "let add = func(a, b) {\n    return a + b\n}\n"
            "print(add(2, 3))\nprint(add)\n"
            "func apply(f, x) {\n    return f(x)\n}\n"
            "print(apply(func(n) { return n * 2 }, 21))\n")

    def test_nested_function_without_capture(self):
        self.assert_same_output(
            "func outer() {\n"
            "    func inner(x) {\n        return x * 2\n    }\n"
            "    return inner(21)\n}\nprint(outer())\n")

    def test_match(self):
        self.assert_same_output(
            "func describe(n) {\n    return match n {\n"
            "        0 => \"zero\",\n"
            "        1 => \"one\",\n"
            "        x if x > 10 => \"big {x}\",\n"
            "        _ => \"other\",\n    }\n}\n"
            "print(describe(0))\nprint(describe(1))\n"
            "print(describe(42))\nprint(describe(5))\n"
            "let s = match \"hi\" {\n    \"hi\" => \"hello\",\n"
            "    _ => \"bye\",\n}\nprint(s)\n"
            "print(match true { true => \"yes\", false => \"no\" })\n"
            "print(match null { null => \"nil\", _ => \"not nil\" })\n")

    def test_builtins(self):
        self.assert_same_output(
            "print(type(1))\nprint(type(1.5))\nprint(type(\"s\"))\n"
            "print(type(true))\nprint(type(null))\nprint(type([]))\n"
            "print(type({}))\nprint(num(\"42\"))\nprint(num(\"3.5\"))\n"
            "print(int(3.9))\nprint(int(\"7\"))\nprint(float(2))\n"
            "print(float(\"1.5\"))\nprint(abs(-3))\nprint(sqrt(16))\n"
            "print(pow(2, 8))\nprint(range(3))\nprint(range(2, 5))\n"
            "print(len(\"abc\"))\nprint(len([1, 2]))\n"
            "print(len({\"a\": 1}))\n")

    def test_stdlib_math(self):
        self.assert_same_output(
            "print(floor(3.7))\nprint(ceil(3.2))\nprint(sin(0))\n"
            "print(cos(0))\nprint(log(1))\nprint(exp(0))\n"
            "print(now() > 0)\nprint(sleep(0))\n")

    def test_random_properties(self):
        # Sequences differ from Python's Mersenne Twister by design;
        # assert ranges / membership / preservation instead.
        code, out, err = codegen.run_native(
            "print(randint(1, 6) >= 1 and randint(1, 6) <= 6)\n"
            "print(randrange(10) >= 0 and randrange(10) < 10)\n"
            "print(choice([7]) == 7)\n"
            "let a = [1, 2, 3]\nshuffle(a)\nprint(len(a) == 3)\n"
            "seed(42)\nprint(randint(0, 1000000))\n"
            "seed(42)\nprint(randint(0, 1000000))\n")
        self.assertEqual(code, 0, err)
        lines = out.strip().split("\n")
        self.assertEqual(lines[0], "true")
        self.assertEqual(lines[1], "true")
        self.assertEqual(lines[2], "true")
        self.assertEqual(lines[3], "true")
        self.assertEqual(lines[4], lines[5],
                         "seed() must reproduce the sequence")

    def test_file_io(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "note.txt").replace("\\", "\\\\")
            self.assert_same_output(
                "write_file(\"%s\", \"hello native\")\n" % path +
                "print(file_exists(\"%s\"))\n" % path +
                "print(read_file(\"%s\"))\n" % path +
                "print(file_exists(\"%s\"))\n"
                % os.path.join(tmp, "missing.txt"))
            with open(os.path.join(tmp, "note.txt"),
                      encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "hello native")

    def test_json(self):
        # A literal "{" cannot appear in a Kabir string (it starts an
        # interpolation), so the JSON text is built via str(dict),
        # exactly like tests/test_stdlib3.py does.
        self.assert_same_output(
            "let v = json_parse(str({\"a\": [1, 2.5, true, null]}))\n"
            "print(v)\nprint(v[\"a\"][1])\n"
            "print(json_stringify({\"x\": 1, \"y\": [true, null]}))\n"
            "print(json_stringify(\"hi\"))\n"
            "print(json_parse(\"[1, 2, 3]\"))\n")

    def test_http_get_and_post_local_server(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"hello-http"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                resp = b"echo:" + body
                self.send_response(200)
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:%d" % server.server_address[1]
            self.assert_same_output(
                "print(http_get(\"%s/\"))\n" % base +
                "print(http_post(\"%s/\", \"ping\"))\n" % base)
        finally:
            server.shutdown()

    def test_input(self):
        self.assert_same_output(
            "let name = input(\"name? \")\nprint(\"hi {name}\")\n",
            input_text="Rafid\n")

    def test_let_mut_is_runtime_noop(self):
        # NB: `let mut` is a runtime no-op (the checker owns
        # mutability); passing the array to `print` borrows it
        # since 6.0.0, so the last use stays last.
        self.assert_same_output(
            "let mut xs = [1, 2]\nxs[0] = 9\nprint(xs)\n"
            "let mut n = 5\nn = 6\nprint(n)\n")

    def test_division_by_zero_errors(self):
        self.assert_native_error("print(1 / 0)\n", "Division by zero")

    def test_index_out_of_range_errors(self):
        self.assert_native_error("let a = [1]\nprint(a[5])\n",
                                 "out of range")

    def test_no_match_arm_errors(self):
        self.assert_native_error(
            "print(match 5 { 1 => \"one\", 2 => \"two\" })\n",
            "No pattern matched")

    def test_shadowing_and_scopes(self):
        self.assert_same_output(
            "let x = 1\n"
            "if true {\n    let x = 2\n    print(x)\n}\nprint(x)\n"
            "func f() {\n    let x = 3\n    return x\n}\nprint(f())\n"
            "print(x)\n")

    def test_examples_match_vm(self):
        # Literal comparison against `kabir run` (the VM path).
        for name in ("hello.kbr", "fibonacci.kbr", "primes.kbr",
                     "fizzbuzz.kbr", "sorting.kbr"):
            with self.subTest(example=name):
                path = os.path.join(ROOT, "examples", name)
                with open(path, encoding="utf-8") as handle:
                    source = handle.read()
                vm = subprocess.run(
                    [sys.executable, "-m", "kabir", "run", path],
                    capture_output=True, text=True, cwd=ROOT)
                self.assertEqual(vm.returncode, 0, vm.stderr)
                code, out, err = codegen.run_native(source,
                                                    filename=name)
                self.assertEqual(code, 0, err)
                self.assertEqual(out, vm.stdout, name)


if __name__ == "__main__":
    unittest.main()
