"""Error tests: every error is friendly -- file, line, caret, no traceback."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir.errors import KabirError
from kabir.interpreter import Interpreter


def run(source, filename="test.kbr"):
    interp = Interpreter(source, filename=filename)
    interp.run_source(source, filename=filename)


def expect_error(testcase, source, fragment, filename="test.kbr"):
    with testcase.assertRaises(KabirError) as ctx:
        run(source, filename)
    rendered = str(ctx.exception)
    testcase.assertIn(fragment, rendered)
    testcase.assertIn(filename, rendered, rendered)
    testcase.assertIn("^", rendered, rendered)
    testcase.assertNotIn("Traceback", rendered)
    return rendered


class TestErrors(unittest.TestCase):
    def test_undefined_variable(self):
        rendered = expect_error(self, "print(nope)", "Undefined variable")
        self.assertIn("test.kbr:1:", rendered)

    def test_division_by_zero(self):
        expect_error(self, "print(1 / 0)", "Division by zero")

    def test_modulo_by_zero(self):
        expect_error(self, "print(1 % 0)", "Division by zero")

    def test_index_out_of_range(self):
        expect_error(self, "let a = [1]\nprint(a[5])", "out of range")

    def test_calling_non_function(self):
        expect_error(self, 'let x = 5\nx()', "not a function")

    def test_return_outside_function(self):
        expect_error(self, "return 5", "'return' can only be used")

    def test_break_outside_loop(self):
        expect_error(self, "break", "'break' can only be used")

    def test_continue_outside_loop(self):
        expect_error(self, "continue", "'continue' can only be used")

    def test_type_error_on_add(self):
        expect_error(self, 'print(1 + "a")', "Cannot add")

    def test_syntax_error_format(self):
        rendered = expect_error(self, "let x = \n", "expression was expected")
        self.assertIn("test.kbr:1:", rendered)

    def test_error_line_number_is_correct(self):
        try:
            run("let a = 1\nlet b = 2\nprint(missing)", "prog.kbr")
        except KabirError as err:
            self.assertEqual(err.line, 3)
            self.assertIn("print(missing)", err.render())
        else:
            self.fail("expected KabirError")

    def test_assign_to_undefined_variable(self):
        expect_error(self, "x = 5", "never defined")


if __name__ == "__main__":
    unittest.main()
