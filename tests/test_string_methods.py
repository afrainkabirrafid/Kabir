"""String method tests: the Member infrastructure + string methods."""

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


class TestStringMethods(unittest.TestCase):
    def test_upper(self):
        self.assertEqual(value('"abc".upper()'), "ABC")

    def test_bound_method_stored_then_called(self):
        self.assertEqual(value('let u = "abc".upper\n u()'), "ABC")

    def test_unknown_method_errors(self):
        with self.assertRaises(KabirError) as ctx:
            value('"abc".frobnicate()')
        self.assertIn("frobnicate", str(ctx.exception))

    def test_dict_dot_access_errors(self):
        with self.assertRaises(KabirError) as ctx:
            value('{"a": 1}.keys()')
        self.assertIn('["name"]', str(ctx.exception))

    def test_number_receiver_errors(self):
        with self.assertRaises(KabirError) as ctx:
            value('42 .upper()')
        self.assertIn("number", str(ctx.exception))

    # --- M-2: remaining methods (happy path + errors) ---

    def test_lower(self):
        self.assertEqual(value('"ABC".lower()'), "abc")

    def test_upper_mixed_case(self):
        self.assertEqual(value('"aBc".upper()'), "ABC")

    def test_strip(self):
        self.assertEqual(value('"  hi  ".strip()'), "hi")

    def test_strip_with_arg_errors(self):
        with self.assertRaises(KabirError):
            value('"  hi  ".strip(",")')

    def test_split_with_separator(self):
        self.assertEqual(value('"a,b".split(",")'), ["a", "b"])

    def test_split_no_args_splits_on_whitespace(self):
        self.assertEqual(value('"a  b".split()'), ["a", "b"])

    def test_split_non_string_separator_errors(self):
        with self.assertRaises(KabirError):
            value('"a,b".split(1)')

    def test_split_empty_separator_errors(self):
        with self.assertRaises(KabirError):
            value('"a,b".split("")')

    def test_join(self):
        self.assertEqual(value('", ".join(["a", "b"])'), "a, b")

    def test_join_empty_array(self):
        self.assertEqual(value('"".join([])'), "")

    def test_join_non_array_errors(self):
        with self.assertRaises(KabirError):
            value('"-".join("ab")')

    def test_join_non_string_element_errors(self):
        with self.assertRaises(KabirError):
            value('"-".join([1, 2])')

    def test_contains(self):
        self.assertTrue(value('"hello".contains("ell")'))
        self.assertFalse(value('"hello".contains("z")'))

    def test_contains_non_string_errors(self):
        with self.assertRaises(KabirError):
            value('"hello".contains(1)')

    def test_starts_with(self):
        self.assertTrue(value('"hello".starts_with("he")'))
        self.assertFalse(value('"hello".starts_with("lo")'))

    def test_ends_with(self):
        self.assertTrue(value('"hello".ends_with("lo")'))
        self.assertFalse(value('"hello".ends_with("he")'))

    def test_replace(self):
        self.assertEqual(value('"aaa".replace("a", "b")'), "bbb")
        self.assertEqual(value('"hello".replace("l", "L")'), "heLLo")

    def test_replace_wrong_arg_count_errors(self):
        with self.assertRaises(KabirError):
            value('"a".replace("a")')

    def test_replace_non_string_arg_errors(self):
        with self.assertRaises(KabirError):
            value('"a".replace(1, "b")')

    def test_array_receiver_errors(self):
        with self.assertRaises(KabirError) as ctx:
            value('[1, 2].upper()')
        self.assertIn("array", str(ctx.exception))

    def test_boolean_receiver_errors(self):
        with self.assertRaises(KabirError) as ctx:
            value('true.upper()')
        self.assertIn("boolean", str(ctx.exception))

    # --- M-3: chaining, interpolation, assignment through method results ---

    def test_chain_strip_then_upper(self):
        self.assertEqual(value('" a ".strip().upper()'), "A")

    def test_split_then_index(self):
        self.assertEqual(value('"a b".split(" ")[1]'), "b")

    def test_chain_upper_then_lower(self):
        self.assertEqual(value('"a".upper().lower()'), "a")

    def test_method_call_inside_interpolation(self):
        self.assertEqual(run('print("{"abc".upper()}")'), "ABC\n")

    def test_assign_through_method_result_index(self):
        self.assertEqual(
            value('let parts = "a b".split(" ")\nparts[0] = "x"\nparts[0]'),
            "x")

    def test_method_call_is_not_an_assignment_target(self):
        with self.assertRaises(KabirError) as ctx:
            value('"a".upper() = "b"')
        self.assertIn("Only a variable or an index can be assigned to",
                      str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
