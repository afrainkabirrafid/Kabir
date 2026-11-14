"""Tests for the WASM target (kabir/wasm.py).

Every parity test actually executes the emitted .wasm under Node
(v24+) and compares its stdout against the bytecode VM's output for
the same source. The whole module is skipped when `node` is not on
PATH. Error tests check that unsupported programs are rejected with
a KabirError at build time, and that runtime errors trap the module
(the WASM target has no runtime error messages by design -- see
kabir/wasm.py).
"""

import io
import os
import shutil
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir.errors import KabirError
from kabir.vm import VM
from kabir.wasm import compile_wasm, html_harness, run_wasm

NODE = shutil.which("node")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "examples")


def run_vm(source):
    """Run source in the bytecode VM; return printed output."""
    vm = VM(source, filename="<test>")
    buf = io.StringIO()
    with redirect_stdout(buf):
        vm.run_source(source, filename="<test>")
    return buf.getvalue()


@unittest.skipUnless(NODE, "node is required to execute .wasm")
class TestWasmParity(unittest.TestCase):
    """VM-vs-WASM output parity for the required subset and more."""

    def assert_parity(self, source):
        self.assertEqual(run_wasm(compile_wasm(source)), run_vm(source))

    def test_magic_bytes(self):
        data = compile_wasm("print(1)\n")
        self.assertIsInstance(data, bytes)
        self.assertTrue(data.startswith(b"\x00asm"))

    def test_arith(self):
        self.assert_parity(
            "print(1 + 2 * 3)\n"
            "print((10 - 4) / 3)\n"
            "print(2 ** 10)\n"
            "print(7 % 3)\n"
            "print(-5 + 3)\n"
            "print(1.5 + 2.25)\n"
        )

    def test_bools(self):
        self.assert_parity(
            "print(true)\n"
            "print(false)\n"
            "print(null)\n"
            "print(1 < 2)\n"
            "print(2 <= 2)\n"
            "print(3 > 4)\n"
            "print(1 == 1)\n"
            "print(1 != 1)\n"
            "print(true and false)\n"
            "print(true or false)\n"
            "print(not true)\n"
        )

    def test_funcs(self):
        self.assert_parity(
            "func add(a, b) { return a + b }\n"
            "print(add(2, 3))\n"
            "func fib(n) { if n < 2 { return n } "
            "return fib(n - 1) + fib(n - 2) }\n"
            "print(fib(10))\n"
            "let f = fib\n"
            "print(f(6))\n"
        )

    def test_arrays(self):
        self.assert_parity(
            "let a = [1, 2, 3]\n"
            "print(a)\n"
            "print(len(a))\n"
            "print(a[0])\n"
            "print(a[-1])\n"
            "push(a, 4)\n"
            "print(a)\n"
            "print(pop(a))\n"
            "print(a)\n"
        )

    def test_strs(self):
        self.assert_parity(
            "let s = \"hello\"\n"
            "print(s)\n"
            "print(len(s))\n"
            "print(s[0])\n"
            "print(s[1])\n"
            "print(s + \" world\")\n"
        )

    def test_interp(self):
        self.assert_parity(
            "let name = \"Kabir\"\n"
            "let n = 42\n"
            "print(\"hello ${name}, ${n}\")\n"
            "print(\"sum: ${1 + 2}\")\n"
            "print(\"arr: ${[1, 2]}\")\n"
        )

    def test_shadow(self):
        self.assert_parity(
            "let x = 1\n"
            "if true { let x = 2\n print(x) }\n"
            "print(x)\n"
        )

    def test_match(self):
        self.assert_parity(
            "print(match 2 { 1 => \"one\", 2 => \"two\", _ => \"other\" })\n"
            "print(match \"a\" { \"a\" => \"A\", _ => \"?\" })\n"
            "print(match 9 { n if n > 5 => \"big\", _ => \"small\" })\n"
        )

    def test_builtins(self):
        self.assert_parity(
            "print(abs(-3))\n"
            "print(sqrt(16))\n"
            "print(pow(2, 8))\n"
            "print(int(3.7))\n"
            "print(float(3))\n"
            "print(num(\"42\"))\n"
            "print(type(1))\n"
            "print(type(\"s\"))\n"
            "print(type(true))\n"
            "print(type(null))\n"
            "print(type([1]))\n"
            "print(str(42))\n"
            "print(pi)\n"
            "print(e)\n"
        )

    def test_nested_loops(self):
        self.assert_parity(
            "for i in range(3) { for j in range(3) { print(i * 10 + j) } }\n"
        )

    def test_mod_neg(self):
        self.assert_parity(
            "print(-7 % 3)\n"
            "print(7 % -3)\n"
            "print(-7 % -3)\n"
        )

    def test_div(self):
        self.assert_parity(
            "print(7 / 2)\n"
            "print(1 / 3)\n"
            "print(10 / 4)\n"
        )

    def test_str_cmp(self):
        self.assert_parity(
            "print(\"a\" < \"b\")\n"
            "print(\"abc\" == \"abc\")\n"
            "print(\"b\" > \"a\")\n"
        )

    def test_arr_eq(self):
        self.assert_parity(
            "print([1, 2] == [1, 2])\n"
            "print([1] == [2])\n"
            "print([] == [])\n"
        )

    def test_concat(self):
        self.assert_parity(
            "print(\"foo\" + \"bar\")\n"
            "print([1] + [2])\n"
        )

    def test_range2(self):
        # Kabir range() takes 1 or 2 arguments only (no step).
        self.assert_parity(
            "for i in range(3) { print(i) }\n"
            "for i in range(1, 4) { print(i) }\n"
        )

    def test_str_fn(self):
        self.assert_parity(
            "print(str(42))\n"
            "print(str(3.5))\n"
            "print(str(true))\n"
            "print(str(null))\n"
            "print(str([1, 2]))\n"
        )

    def test_empty(self):
        self.assert_parity(
            "print(\"\")\n"
            "print([])\n"
            "print(len(\"\"))\n"
            "print(len([]))\n"
        )

    def test_while(self):
        self.assert_parity(
            "let mut i = 0\n"
            "while i < 5 { print(i)\n i = i + 1 }\n"
        )

    def test_for_over_string_and_array(self):
        self.assert_parity(
            "for c in \"ab\" { print(c) }\n"
            "for x in [10, 20] { print(x) }\n"
        )

    def test_loops_combined(self):
        self.assert_parity(
            "let mut total = 0\n"
            "for i in range(10) { "
            "if i == 3 { continue } "
            "if i == 7 { break } "
            "total = total + i }\n"
            "print(total)\n"
        )

    def test_break_only(self):
        self.assert_parity(
            "for i in range(10) { if i == 3 { break } print(i) }\n"
        )

    def test_continue_only(self):
        self.assert_parity(
            "for i in range(5) { if i == 2 { continue } print(i) }\n"
        )

    def test_break_nested(self):
        self.assert_parity(
            "for i in range(3) { "
            "for j in range(3) { if j == 1 { break } print(i * 10 + j) } }\n"
        )

    def test_continue_first_and_twice(self):
        self.assert_parity(
            "for i in range(4) { if i == 0 { continue } print(i) }\n"
            "for i in range(6) { "
            "if i == 1 { continue } "
            "if i == 4 { continue } print(i) }\n"
        )

    def test_break_over_array(self):
        self.assert_parity(
            "let a = [10, 20, 30]\n"
            "for x in a { if x == 20 { break } print(x) }\n"
        )

    def test_break_continue_in_function(self):
        self.assert_parity(
            "func f() { "
            "for i in range(10) { if i == 3 { break } print(i) } }\n"
            "f()\n"
            "func g() { "
            "for i in range(5) { if i == 2 { continue } print(i) } }\n"
            "g()\n"
        )

    def test_break_continue_while(self):
        self.assert_parity(
            "let mut j = 0\n"
            "while j < 10 { if j == 3 { break } print(j)\n j = j + 1 }\n"
            "let mut k = 0\n"
            "while k < 5 { k = k + 1\n"
            " if k == 2 { continue } print(k) }\n"
        )


@unittest.skipUnless(NODE, "node is required to execute .wasm")
class TestWasmExamples(unittest.TestCase):
    """The shipped examples must produce identical output in the VM
    and in WASM."""

    def test_examples(self):
        for name in ("fibonacci", "fizzbuzz", "primes", "sorting", "hello"):
            path = os.path.join(EXAMPLES, name + ".kbr")
            with open(path, "r", encoding="utf-8") as handle:
                source = handle.read()
            with self.subTest(example=name):
                self.assertEqual(
                    run_wasm(compile_wasm(source, filename=path)),
                    run_vm(source),
                )


@unittest.skipUnless(NODE, "node is required to execute .wasm")
class TestWasmRejections(unittest.TestCase):
    """Unsupported programs are rejected with a KabirError at build
    time (never a silent stub)."""

    def test_rejects_import(self):
        with self.assertRaises(KabirError):
            compile_wasm("import foo\n")

    def test_rejects_dict(self):
        with self.assertRaises(KabirError):
            compile_wasm("let d = {\"a\": 1}\nprint(d)\n")

    def test_rejects_string_method(self):
        with self.assertRaises(KabirError):
            compile_wasm("print(\"abc\".upper())\n")

    def test_rejects_closure(self):
        with self.assertRaises(KabirError):
            compile_wasm(
                "func outer() {\n"
                "  let x = 1\n"
                "  func inner() { return x }\n"
                "  return inner()\n"
                "}\n"
                "print(outer())\n"
            )

    def test_rejects_too_many_args(self):
        args = ", ".join(["1"] * 33)
        with self.assertRaises(KabirError):
            compile_wasm("print(%s)\n" % args)

    def test_runtime_error_traps(self):
        # Division by zero is a KabirError in the VM; in WASM the
        # module traps (unreachable) instead -- still an error, just
        # without a message. run_wasm surfaces it as KabirError.
        with self.assertRaises(KabirError):
            run_wasm(compile_wasm("print(1 / 0)\n"))

    def test_out_of_scope_traps(self):
        # The VM raises "Undefined variable 'x'"; WASM traps. Both
        # are errors -- the WASM target must not read a stale value.
        with self.assertRaises(KabirError):
            run_wasm(compile_wasm(
                "if true { let x = 1 }\n"
                "print(x)\n"
            ))


class TestWasmHarness(unittest.TestCase):
    """The browser harness helper needs no Node."""

    def test_html_harness(self):
        page = html_harness("out.wasm", title="My program")
        self.assertIn("out.wasm", page)
        self.assertIn("WebAssembly", page)
        self.assertIn("My program", page)


if __name__ == "__main__":
    unittest.main()
