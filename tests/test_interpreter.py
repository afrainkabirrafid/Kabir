"""Interpreter semantics tests: scoping, closures, loops, values."""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir.errors import KabirError
from kabir.interpreter import Interpreter


def run(source, inputs=None):
    """Run Kabir source, return (printed output, interpreter)."""
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
    return buf.getvalue(), interp


def value(source):
    """Compute a single expression statement and return its value."""
    interp = Interpreter(source, filename="<test>")
    from kabir.parser import parse
    program = parse(source, "<test>")
    interp.source = source
    interp.lines = source.split("\n")
    last = None
    for stmt in program.statements:
        last = interp.run_stmt(stmt)
    return last


class TestArithmetic(unittest.TestCase):
    def test_basic_arithmetic(self):
        self.assertEqual(value("2 + 3 * 4"), 14)
        self.assertEqual(value("(2 + 3) * 4"), 20)
        self.assertEqual(value("10 - 4"), 6)
        self.assertEqual(value("2 ** 10"), 1024)
        self.assertEqual(value("10 % 3"), 1)

    def test_true_division(self):
        self.assertEqual(value("5 / 2"), 2.5)
        self.assertEqual(value("6 / 3"), 2)
        self.assertIsInstance(value("6 / 3"), int)
        self.assertIsInstance(value("5 / 2"), float)

    def test_integral_float_prints_without_trailing_zeros(self):
        out, _ = run("print(5.0)")
        self.assertEqual(out, "5\n")
        out, _ = run("print(2.5)")
        self.assertEqual(out, "2.5\n")

    def test_comparisons(self):
        self.assertTrue(value("1 < 2"))
        self.assertTrue(value("2 <= 2"))
        self.assertFalse(value("3 > 4"))
        self.assertTrue(value('"abc" < "abd"'))

    def test_deep_equality(self):
        self.assertTrue(value("[1, 2] == [1, 2]"))
        self.assertFalse(value("[1, 2] == [1, 3]"))
        self.assertTrue(value('{"a": 1} == {"a": 1}'))
        self.assertTrue(value("1 == 1.0"))
        self.assertFalse(value("1 == true"))


class TestStringsArraysDicts(unittest.TestCase):
    def test_string_concat_and_interpolation(self):
        self.assertEqual(value('"foo" + "bar"'), "foobar")
        out, _ = run('let name = "Kat"\nprint("Hello, {name}!")')
        self.assertEqual(out, "Hello, Kat!\n")

    def test_interpolation_with_expression(self):
        out, _ = run('let x = 3\nprint("x + 1 = {x + 1}")')
        self.assertEqual(out, "x + 1 = 4\n")

    def test_array_concat_and_indexing(self):
        self.assertEqual(value("[1, 2] + [3]"), [1, 2, 3])
        self.assertEqual(value("[10, 20, 30][1]"), 20)
        self.assertEqual(value('"hello"[1]'), "e")

    def test_index_assignment(self):
        out, _ = run("let a = [1, 2, 3]\na[0] = 9\nprint(a)")
        self.assertEqual(out, "[9, 2, 3]\n")

    def test_dict_access_and_assignment(self):
        out, _ = run('let d = {"a": 1}\nd["b"] = 2\nprint(d["b"])')
        self.assertEqual(out, "2\n")

    def test_compound_assignment(self):
        out, _ = run("let x = 5\nx += 3\nx -= 1\nprint(x)")
        self.assertEqual(out, "7\n")


class TestTruthinessAndControlFlow(unittest.TestCase):
    def test_falsy_values(self):
        for src in ('""', "0", "[]", "{}", "false", "null"):
            self.assertFalse(Interpreter.is_truthy(value(src)), src)

    def test_truthy_values(self):
        for src in ('"x"', "1", "[0]", '{"a": 1}', "true"):
            self.assertTrue(Interpreter.is_truthy(value(src)), src)

    def test_if_elif_else(self):
        out, _ = run("let x = 2\nif x == 1 { print(1) } "
                     "elif x == 2 { print(2) } else { print(3) }")
        self.assertEqual(out, "2\n")

    def test_while_loop(self):
        out, _ = run("let i = 0\nlet s = 0\nwhile i < 5 { s += i\n i += 1 }\n"
                     "print(s)")
        self.assertEqual(out, "10\n")

    def test_for_over_range_and_string(self):
        out, _ = run("for i in range(3) { print(i) }")
        self.assertEqual(out, "0\n1\n2\n")
        out, _ = run('for ch in "ab" { print(ch) }')
        self.assertEqual(out, "a\nb\n")

    def test_break_and_continue(self):
        out, _ = run("for i in range(10) { if i == 3 { break } print(i) }")
        self.assertEqual(out, "0\n1\n2\n")
        out, _ = run("for i in range(5) { if i == 2 { continue } "
                     "print(i) }")
        self.assertEqual(out, "0\n1\n3\n4\n")


class TestFunctionsAndScoping(unittest.TestCase):
    def test_recursion(self):
        out, _ = run("func fib(n) { if n < 2 { return n } "
                     "return fib(n - 1) + fib(n - 2) }\nprint(fib(10))")
        self.assertEqual(out, "55\n")

    def test_closure_captures_defining_scope(self):
        out, _ = run("func make_adder(n) { return func(x) { return x + n } }"
                     "\nlet add5 = make_adder(5)\nprint(add5(10))")
        self.assertEqual(out, "15\n")

    def test_functions_as_values(self):
        out, _ = run("func apply(f, x) { return f(x) }\n"
                     "let double = func(n) { return n * 2 }\n"
                     "print(apply(double, 21))")
        self.assertEqual(out, "42\n")

    def test_block_scoping(self):
        out, _ = run("let x = 1\nif true { let x = 2\n print(x) }\nprint(x)")
        self.assertEqual(out, "2\n1\n")

    def test_let_inside_block_not_visible_outside(self):
        with self.assertRaises(KabirError):
            run("if true { let hidden = 1 }\nprint(hidden)")

    def test_arity_mismatch_is_friendly_error(self):
        with self.assertRaises(KabirError) as ctx:
            run("func add(a, b) { return a + b }\nadd(1)")
        self.assertIn("takes 2 arguments", str(ctx.exception))

    def test_return_value_defaults_to_null(self):
        out, _ = run("func noop() { }\nprint(noop())")
        self.assertEqual(out, "null\n")


if __name__ == "__main__":
    unittest.main()
