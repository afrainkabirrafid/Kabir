"""Built-in function tests: every built-in, exercised directly."""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir.errors import KabirError
from kabir.interpreter import Interpreter


def run(source, inputs=None):
    interp = Interpreter(source, filename="<test>")
    buf = io.StringIO()
    if inputs is not None:
        import builtins as _b
        it = iter(inputs)
        orig = _b.input
        _b.input = lambda prompt="": next(it)
        try:
            with redirect_stdout(buf):
                interp.run_source(source, filename="<test>")
        finally:
            _b.input = orig
    else:
        with redirect_stdout(buf):
            interp.run_source(source, filename="<test>")
    return buf.getvalue()


def value(source):
    from kabir.parser import parse
    interp = Interpreter(source, filename="<test>")
    program = parse(source, "<test>")
    interp.source = source
    interp.lines = source.split("\n")
    last = None
    for stmt in program.statements:
        last = interp.run_stmt(stmt)
    return last


class TestBuiltins(unittest.TestCase):
    def test_print_multiple_args(self):
        self.assertEqual(run('print(1, "two", true, null)'),
                         "1 two true null\n")

    def test_print_no_args(self):
        self.assertEqual(run("print()"), "\n")

    def test_input_returns_typed_line(self):
        self.assertEqual(run('let name = input("Name: ")\nprint(name)',
                             inputs=["Kat"]), "Kat\n")

    def test_len(self):
        self.assertEqual(value('len("hello")'), 5)
        self.assertEqual(value("len([1, 2, 3])"), 3)
        self.assertEqual(value('len({"a": 1, "b": 2})'), 2)
        with self.assertRaises(KabirError):
            value("len(5)")

    def test_str(self):
        self.assertEqual(value("str(42)"), "42")
        self.assertEqual(value("str(2.5)"), "2.5")
        self.assertEqual(value("str([1, 2])"), "[1, 2]")
        self.assertEqual(value("str(null)"), "null")

    def test_num(self):
        self.assertEqual(value('num("42")'), 42)
        self.assertEqual(value('num("3.5")'), 3.5)
        self.assertEqual(value("num(7)"), 7)
        with self.assertRaises(KabirError) as ctx:
            value('num("abc")')
        self.assertIn("Cannot turn", str(ctx.exception))

    def test_type(self):
        self.assertEqual(value("type(1)"), "number")
        self.assertEqual(value('type("x")'), "string")
        self.assertEqual(value("type(true)"), "boolean")
        self.assertEqual(value("type(null)"), "null")
        self.assertEqual(value("type([])"), "array")
        self.assertEqual(value("type({})"), "dict")
        self.assertEqual(value("type(print)"), "function")

    def test_range(self):
        self.assertEqual(value("range(3)"), [0, 1, 2])
        self.assertEqual(value("range(2, 5)"), [2, 3, 4])
        self.assertEqual(value("range(0)"), [])

    def test_push_pop(self):
        out, = [run("let a = [1]\npush(a, 2)\nprint(a)\nprint(pop(a))\n"
                    "print(a)")]
        self.assertEqual(out, "[1, 2]\n2\n[1]\n")
        with self.assertRaises(KabirError):
            value("pop([])")

    def test_sqrt_pow_abs(self):
        self.assertEqual(value("sqrt(16)"), 4.0)
        self.assertEqual(value("pow(2, 10)"), 1024)
        self.assertEqual(value("pow(2, -1)"), 0.5)
        self.assertEqual(value("abs(-7)"), 7)
        self.assertEqual(value("abs(2.5)"), 2.5)
        with self.assertRaises(KabirError):
            value("sqrt(-1)")

    def test_keys(self):
        self.assertEqual(value('keys({"b": 1, "a": 2})'), ["b", "a"])
        with self.assertRaises(KabirError):
            value("keys([1])")

    def test_int_float(self):
        self.assertEqual(value("int(3.9)"), 3)
        self.assertEqual(value("int(-3.9)"), -3)
        self.assertEqual(value('int("42")'), 42)
        self.assertEqual(value("float(3)"), 3.0)
        self.assertIsInstance(value("float(3)"), float)
        self.assertEqual(value('float("2.5")'), 2.5)

    def test_wrong_arg_count_is_friendly(self):
        with self.assertRaises(KabirError) as ctx:
            value("len(1, 2)")
        self.assertIn("takes exactly 1 argument", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
