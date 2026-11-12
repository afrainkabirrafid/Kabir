"""Match expression tests: parser + interpreter behaviour (Kabir 2.0.0)."""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir import ast_nodes as ast
from kabir.errors import KabirError
from kabir.interpreter import Interpreter
from kabir.parser import parse


def run(source):
    """Run Kabir source, return the printed output."""
    interp = Interpreter(source, filename="<test>")
    buf = io.StringIO()
    with redirect_stdout(buf):
        interp.run_source(source, filename="<test>")
    return buf.getvalue()


def value(source):
    """Compute a single expression statement and return its value."""
    interp = Interpreter(source, filename="<test>")
    program = parse(source, "<test>")
    interp.source = source
    interp.lines = source.split("\n")
    last = None
    for stmt in program.statements:
        last = interp.run_stmt(stmt)
    return last


def match_expr(source):
    """Parse one match expression statement and return the MatchExpr."""
    prog = parse(source)
    return prog.statements[0].expr


class TestMatchParsing(unittest.TestCase):
    def test_literal_binding_and_wildcard_arms(self):
        node = match_expr('match x { 0 => "zero", n => "other", _ => "?" }')
        self.assertIsInstance(node, ast.MatchExpr)
        self.assertIsInstance(node.scrutinee, ast.Var)
        self.assertEqual(node.scrutinee.name, "x")
        self.assertEqual(len(node.arms), 3)
        self.assertIsInstance(node.arms[0].pattern, ast.Literal)
        self.assertEqual(node.arms[0].pattern.value, 0)
        self.assertIsInstance(node.arms[1].pattern, ast.Var)
        self.assertEqual(node.arms[1].pattern.name, "n")
        self.assertIsInstance(node.arms[2].pattern, ast.Wildcard)
        self.assertIsNone(node.arms[0].guard)
        self.assertIsInstance(node.arms[0].body, ast.StringLiteral)

    def test_string_bool_and_null_literal_patterns(self):
        node = match_expr('match s { "a" => 1, true => 2, null => 3 }')
        self.assertEqual(node.arms[0].pattern.value, "a")
        self.assertIsInstance(node.arms[0].pattern, ast.Literal)
        self.assertEqual(node.arms[1].pattern.value, True)
        self.assertEqual(node.arms[2].pattern.value, None)

    def test_guard_parses(self):
        node = match_expr('match x { n if n > 1 => "big", _ => "small" }')
        self.assertIsInstance(node.arms[0].guard, ast.BinaryOp)
        self.assertEqual(node.arms[0].guard.op, ">")
        self.assertIsNone(node.arms[1].guard)

    def test_trailing_comma_allowed(self):
        node = match_expr('match x { 0 => "zero", _ => "?", }')
        self.assertEqual(len(node.arms), 2)

    def test_arms_may_span_lines(self):
        node = match_expr('match x {\n 0 => "zero",\n _ => "?",\n}')
        self.assertEqual(len(node.arms), 2)

    def test_empty_match_parses(self):
        node = match_expr("match x { }")
        self.assertEqual(node.arms, [])

    def test_missing_arrow_is_syntax_error(self):
        with self.assertRaises(KabirError):
            parse('match x { 0 "zero" }')

    def test_interpolated_string_pattern_errors(self):
        with self.assertRaises(KabirError):
            parse('match x { "{y}" => 1 }')


class TestMatchRuntime(unittest.TestCase):
    def test_literal_arm_matches(self):
        self.assertEqual(value('match 0 { 0 => "zero", _ => "?" }'), "zero")
        self.assertEqual(value('match 1 { 0 => "zero", 1 => "one" }'), "one")

    def test_binding_arm_binds_the_value(self):
        self.assertEqual(value('match 5 { n => n + 1 }'), 6)
        self.assertEqual(
            value('match 5 { 0 => "zero", n => "other: " + str(n) }'),
            "other: 5")

    def test_wildcard_matches_anything(self):
        self.assertEqual(value('match "x" { 0 => "zero", _ => "?" }'), "?")

    def test_first_matching_arm_wins(self):
        self.assertEqual(
            value('match 1 { 1 => "first", 1 => "second", _ => "?" }'),
            "first")

    def test_binding_arm_shadows_later_arms(self):
        # A binding always matches, so arms after it never run.
        self.assertEqual(value('match 9 { n => "bound", _ => "?" }'), "bound")

    def test_guard_must_be_truthy_to_match(self):
        self.assertEqual(
            value('match 3 { n if n > 10 => "big", n if n > 1 => "mid", '
                  '_ => "small" }'),
            "mid")
        self.assertEqual(
            value('match 0 { n if n > 1 => "big", _ => "small" }'),
            "small")

    def test_no_match_raises(self):
        with self.assertRaises(KabirError) as ctx:
            value('match 5 { 0 => "zero", 1 => "one" }')
        self.assertIn("No pattern matched the value 5", str(ctx.exception))

    def test_no_match_error_names_the_value(self):
        with self.assertRaises(KabirError) as ctx:
            value('match "q" { "a" => 1 }')
        self.assertIn("No pattern matched the value q", str(ctx.exception))

    def test_string_bool_null_patterns(self):
        self.assertEqual(value('match "a" { "a" => 1, _ => 2 }'), 1)
        self.assertEqual(value('match true { false => 1, true => 2 }'), 2)
        self.assertEqual(value('match null { null => "nil", _ => "?" }'),
                         "nil")

    def test_match_as_expression_in_let(self):
        out = run('let x = 2\n'
                  'let label = match x { 0 => "zero", 2 => "two", _ => "?" }\n'
                  'print(label)')
        self.assertEqual(out, "two\n")

    def test_match_as_statement(self):
        out = run('match 0 { 0 => print("zero"), _ => print("other") }')
        self.assertEqual(out, "zero\n")

    def test_match_in_call_argument(self):
        out = run('print(match 1 { 1 => "one", _ => "?" })')
        self.assertEqual(out, "one\n")

    def test_match_inside_function(self):
        out = run('func describe(n) { return match n '
                  '{ 0 => "zero", n if n < 0 => "negative", _ => "positive" } }\n'
                  'print(describe(0))\n'
                  'print(describe(-4))\n'
                  'print(describe(7))')
        self.assertEqual(out, "zero\nnegative\npositive\n")

    def test_nested_match(self):
        self.assertEqual(
            value('match match 3 { 3 => 10, _ => 20 } '
                  '{ 10 => "ten", _ => "other" }'),
            "ten")

    def test_binding_does_not_leak(self):
        with self.assertRaises(KabirError) as ctx:
            run('let r = match 7 { n => n + 1 }\nprint(n)')
        self.assertIn("Undefined variable 'n'", str(ctx.exception))

    def test_match_in_return(self):
        out = run('func f(x) { return match x { 0 => "z", _ => "o" } }\n'
                  'print(f(0))')
        self.assertEqual(out, "z\n")


if __name__ == "__main__":
    unittest.main()
