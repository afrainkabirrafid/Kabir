"""Parser tests: precedence, statements, and syntax errors."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir import ast_nodes as ast
from kabir.errors import KabirError
from kabir.parser import parse


def expr(source):
    prog = parse(source)
    return prog.statements[0].expr


class TestParser(unittest.TestCase):
    def test_multiplication_binds_tighter_than_addition(self):
        node = expr("1 + 2 * 3")
        self.assertEqual(node.op, "+")
        self.assertEqual(node.right.op, "*")

    def test_power_binds_tighter_than_multiplication(self):
        node = expr("2 * 3 ** 2")
        self.assertEqual(node.op, "*")
        self.assertEqual(node.right.op, "**")

    def test_power_is_right_associative(self):
        node = expr("2 ** 3 ** 2")
        self.assertEqual(node.op, "**")
        self.assertEqual(node.right.op, "**")

    def test_parentheses_override_precedence(self):
        node = expr("(1 + 2) * 3")
        self.assertEqual(node.op, "*")
        self.assertEqual(node.left.op, "+")

    def test_comparison_and_logic_precedence(self):
        node = expr("1 < 2 and 3 < 4 or false")
        self.assertEqual(node.op, "or")
        self.assertEqual(node.left.op, "and")

    def test_unary_minus(self):
        node = expr("-2 ** 2")
        self.assertEqual(node.op, "-")
        self.assertEqual(node.operand.op, "**")

    def test_let_statement(self):
        stmt = parse("let x = 5").statements[0]
        self.assertIsInstance(stmt, ast.LetStmt)
        self.assertEqual(stmt.name, "x")

    def test_if_elif_else(self):
        stmt = parse("if a { print(1) } elif b { print(2) } "
                     "else { print(3) }").statements[0]
        self.assertIsInstance(stmt, ast.IfStmt)
        self.assertEqual(len(stmt.branches), 2)
        self.assertIsNotNone(stmt.else_body)

    def test_while_and_for(self):
        self.assertIsInstance(parse("while true { break }")
                              .statements[0], ast.WhileStmt)
        stmt = parse("for x in [1, 2] { print(x) }").statements[0]
        self.assertIsInstance(stmt, ast.ForStmt)
        self.assertEqual(stmt.var, "x")

    def test_func_declaration(self):
        stmt = parse("func add(a, b) { return a + b }").statements[0]
        self.assertIsInstance(stmt, ast.FuncDecl)
        self.assertEqual(stmt.name, "add")
        self.assertEqual(stmt.params, ["a", "b"])

    def test_index_assignment_parses(self):
        stmt = parse("arr[0] = 5").statements[0]
        self.assertIsInstance(stmt, ast.AssignStmt)
        self.assertIsInstance(stmt.target, ast.Index)

    def test_array_and_dict_literals(self):
        self.assertIsInstance(expr("[1, 2, 3]"), ast.ArrayLiteral)
        node = expr('{"a": 1, "b": 2}')
        self.assertIsInstance(node, ast.DictLiteral)
        self.assertEqual([k for k, _ in node.pairs], ["a", "b"])

    def test_missing_expression_is_syntax_error(self):
        with self.assertRaises(KabirError) as ctx:
            parse("let x = ")
        self.assertIn("expression was expected", str(ctx.exception))

    def test_unclosed_block_is_syntax_error(self):
        with self.assertRaises(KabirError) as ctx:
            parse("if true { print(1)")
        self.assertIn("'}'", str(ctx.exception))

    def test_missing_close_paren_is_syntax_error(self):
        with self.assertRaises(KabirError):
            parse("print(1")

    def test_syntax_error_carries_line_number(self):
        try:
            parse("let a = 1\nlet b = ")
        except KabirError as err:
            self.assertEqual(err.line, 2)
        else:
            self.fail("expected KabirError")


if __name__ == "__main__":
    unittest.main()
