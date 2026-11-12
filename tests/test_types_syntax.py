"""Type annotation syntax tests (Kabir 2.0.0).

Annotations are parsed into the AST and ignored at runtime; the
checker (not this module) validates them.
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir import ast_nodes as ast
from kabir.formatter import format_source
from kabir.interpreter import Interpreter
from kabir.parser import parse


def run(source):
    """Run Kabir source, return the printed output."""
    interp = Interpreter(source, filename="<test>")
    buf = io.StringIO()
    with redirect_stdout(buf):
        interp.run_source(source, filename="<test>")
    return buf.getvalue()


class TestLetAnnotations(unittest.TestCase):
    def test_let_annotation_parses(self):
        stmt = parse("let x: number = 5").statements[0]
        self.assertIsInstance(stmt, ast.LetStmt)
        self.assertEqual(stmt.name, "x")
        self.assertIsInstance(stmt.type_ann, ast.TypeAnn)
        self.assertEqual(stmt.type_ann.name, "number")

    def test_let_without_annotation_has_none(self):
        stmt = parse("let x = 5").statements[0]
        self.assertIsNone(stmt.type_ann)

    def test_any_ident_is_accepted_as_type_name(self):
        # The checker validates names; the parser stores them verbatim.
        stmt = parse("let x: whatever = 5").statements[0]
        self.assertEqual(stmt.type_ann.name, "whatever")

    def test_annotation_ignored_at_runtime(self):
        # A "wrong" annotation changes nothing at runtime.
        out = run('let x: string = 5\nprint(x)')
        self.assertEqual(out, "5\n")


class TestFuncAnnotations(unittest.TestCase):
    def test_param_and_return_annotations_parse(self):
        stmt = parse("func add(a: number, b: number) -> number "
                     "{ return a + b }").statements[0]
        self.assertIsInstance(stmt, ast.FuncDecl)
        self.assertEqual(stmt.params, ["a", "b"])
        self.assertEqual([t.name for t in stmt.param_types],
                         ["number", "number"])
        self.assertEqual(stmt.return_type.name, "number")

    def test_partial_param_annotations(self):
        stmt = parse("func f(a: number, b) { return a }").statements[0]
        self.assertEqual(stmt.param_types[0].name, "number")
        self.assertIsNone(stmt.param_types[1])
        self.assertIsNone(stmt.return_type)

    def test_return_only_annotation(self):
        stmt = parse("func f() -> string { return \"hi\" }").statements[0]
        self.assertEqual(stmt.param_types, [])
        self.assertEqual(stmt.return_type.name, "string")

    def test_unannotated_func_has_none_types(self):
        stmt = parse("func f(a, b) { return a }").statements[0]
        self.assertEqual(stmt.param_types, [None, None])
        self.assertIsNone(stmt.return_type)

    def test_annotated_func_runs(self):
        out = run("func add(a: number, b: number) -> number "
                  "{ return a + b }\nprint(add(2, 3))")
        self.assertEqual(out, "5\n")

    def test_annotated_func_ignores_wrong_arg_types_at_runtime(self):
        out = run('func greet(name: string) -> string '
                  '{ return "hi " + str(name) }\nprint(greet(42))')
        self.assertEqual(out, "hi 42\n")


class TestAnonymousFuncAnnotations(unittest.TestCase):
    def test_anonymous_func_annotations_parse(self):
        prog = parse("let f = func (n: number) -> string { return str(n) }")
        func = prog.statements[0].value
        self.assertIsInstance(func, ast.FuncDecl)
        self.assertIsNone(func.name)
        self.assertEqual(func.params, ["n"])
        self.assertEqual(func.param_types[0].name, "number")
        self.assertEqual(func.return_type.name, "string")

    def test_anonymous_func_runs(self):
        out = run("let double = func (n: number) -> number "
                  "{ return n * 2 }\nprint(double(21))")
        self.assertEqual(out, "42\n")


class TestFormatterRoundTrip(unittest.TestCase):
    def test_annotated_let_round_trip(self):
        self.assertEqual(format_source("let x:number=5\n"),
                         "let x: number = 5\n")

    def test_annotated_func_round_trip(self):
        source = ("func add(a:number,b:number)->number { return a + b }\n")
        self.assertEqual(format_source(source),
                         "func add(a: number, b: number) -> number {\n"
                         "    return a + b\n"
                         "}\n")

    def test_annotated_anonymous_func_round_trip(self):
        source = ("let f = func(n:number)->string { return str(n) }\n")
        self.assertEqual(format_source(source),
                         "let f = func(n: number) -> string {\n"
                         "    return str(n)\n"
                         "}\n")

    def test_match_round_trip_one_line(self):
        source = ('let l=match x{0=>"zero",n if n>1=>"big",_=>"?"}\n')
        self.assertEqual(
            format_source(source),
            'let l = match x { 0 => "zero", n if n > 1 => "big", _ => "?" }\n')

    def test_match_round_trip_multi_line(self):
        source = (
            'let s = match some_long_variable_name {\n'
            '    0 => "a fairly long string here",\n'
            '    1 => "another long string value",\n'
            '    _ => "fallback value here"\n'
            '}\n')
        formatted = format_source(source)
        self.assertEqual(formatted, source)
        # And a long one-line match breaks the same way.
        one_line = ('let s = match some_long_variable_name { 0 => '
                    '"a fairly long string here", 1 => '
                    '"another long string value", _ => '
                    '"fallback value here" }\n')
        self.assertEqual(format_source(one_line), source)

    def test_match_formatting_is_idempotent(self):
        source = ('let s = match some_long_variable_name { 0 => '
                  '"a fairly long string here", 1 => '
                  '"another long string value", _ => '
                  '"fallback value here" }\n')
        once = format_source(source)
        self.assertEqual(format_source(once), once)

    def test_annotated_code_formatting_is_idempotent(self):
        source = ("let x: number = 5\n\n"
                  "func add(a: number, b: number) -> number {\n"
                  "    return a + b\n"
                  "}\n")
        self.assertEqual(format_source(source), source)


if __name__ == "__main__":
    unittest.main()
